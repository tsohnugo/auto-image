#!/usr/bin/env python3
"""历史列表与重启重放主缝测试 —— 列表摘要、可续聊约束、假 transcript 驱动的重放。

缝：同 test_api 的 ASGI 测试客户端；list_sessions / get_session_messages
注入假实现（SDKSessionInfo / SessionMessage 同形的 SimpleNamespace），
不读本机 ~/.claude 的真实 transcript。纯 assert，无 pytest。

运行：python web/tests/test_history.py
"""
import asyncio
import logging
import os
import sys
import time
from types import SimpleNamespace

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import StreamingASGITransport, async_client, make_test_app  # noqa: E402
from web.tests.test_api import collect_sse, open_stream, wait_status  # noqa: E402


def drop_title_events(events):
    """剔除 session.title_changed（标题生成异步落流、时序自由，断言不关心）。"""
    return [e for e in events if e["event"] != "session.title_changed"]

# 与 SDKSessionInfo 同形：重启重建只读这些字段（first_prompt / custom_title /
# created_at / last_modified / session_id）
def session_info(session_id, first_prompt, created_ms, last_ms=None, custom_title=None):
    return SimpleNamespace(
        session_id=session_id,
        summary=custom_title or first_prompt,
        last_modified=last_ms if last_ms is not None else created_ms + 5_000,
        file_size=1024,
        custom_title=custom_title,
        first_prompt=first_prompt,
        git_branch="feat/web-mvp",
        cwd="/proj",
        tag=None,
        created_at=created_ms,
    )


# 与 SessionMessage 同形：type + 原始 API message dict（role/content）
def msg(mtype, content):
    return SimpleNamespace(
        type=mtype,
        uuid=f"u_{mtype}_{abs(hash(str(content))) % 10**8}",
        session_id="sess",
        message={"role": "user" if mtype == "user" else "assistant", "content": content},
        parent_tool_use_id=None,
        parent_agent_id=None,
    )


# 消息链的稳定 uuid 版本：同 content 不同回合的消息也各得其所（时刻表即
# {消息 uuid: epoch 秒}，按 uuid 精确对拍）
def uuided(messages):
    for i, m in enumerate(messages):
        m.uuid = f"u{i:03d}"
    return messages


# 一段两回合的部署 transcript：首回合走 GUIDE 子 agent 工具调用，
# 第二回合是纯文本追问——覆盖回合边界推导与阶段推导两条映射路径
def deploy_transcript():
    return [
        msg("user", "部署 nginx 1.25 到 server-a"),
        msg("assistant", [{"type": "thinking", "thinking": "先查文档。scope AK HWPFEJ9AB3CDEFGHIJKL 不外泄。"}]),
        msg("assistant", [{"type": "text", "text": "开始生成部署指南。"}]),
        msg("assistant", [{"type": "tool_use", "id": "toolu_01", "name": "Task",
                           "input": {"subagent_type": "deploy-guide", "prompt": "生成指南"}}]),
        msg("user", [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "指南已生成"}]),
        msg("assistant", [{"type": "text", "text": "指南阶段完成，等待安装指令。"}]),
        msg("user", "跳过验证直接打包"),
        msg("assistant", [{"type": "text", "text": "按要求复述风险并继续。"}]),
    ]


def history_app(infos, messages_fn, times_fn=None, **kwargs):
    """以假 list_sessions / get_session_messages 装配的应用（重启后形态）；
    times_fn 为假时刻表读取器（时刻透传对拍缝），缺省不透传。"""
    return make_test_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT),
        list_sessions_fn=lambda: list(infos),
        get_session_messages_fn=messages_fn,
        transcript_times_fn=times_fn or (lambda sid: {}),
        **kwargs,
    )


def plain_app(**kwargs):
    """无历史的常规应用（列表摘要等行为测试用）。"""
    return make_test_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT),
        **kwargs,
    )


async def test_list_endpoint_returns_summaries_without_clearing_old_runs():
    app = plain_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        empty = (await client.post("/api/runs", json={})).json()["run_id"]
        busy = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{busy}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
        await wait_status(client, busy, "READY")

        r = await client.get("/api/runs")
        assert r.status_code == 200, r.text
        runs = r.json()["runs"]
        assert [x["run_id"] for x in runs] == [busy, empty], runs  # 新启动的在前
        for key in ("run_id", "status", "stage", "first_prompt", "started_at"):
            assert key in runs[0], key
        assert runs[0]["first_prompt"] == "部署 nginx 1.25 到 server-a"
        assert runs[0]["stage"] == "GUIDE"
        assert runs[1]["first_prompt"] is None  # 空会话尚无任务名

        # 提交新任务不清理旧 run 记录
        third = (await client.post("/api/runs", json={})).json()["run_id"]
        runs = (await client.get("/api/runs")).json()["runs"]
        assert {x["run_id"] for x in runs} == {empty, busy, third}


async def test_list_endpoint_sorts_by_latest_event_not_creation_time():
    """较早会话产生新事件后升到较新但不活跃的会话前。"""
    app = plain_app()
    async with async_client(app) as client:
        older = (await client.post("/api/runs", json={})).json()["run_id"]
        newer = (await client.post("/api/runs", json={})).json()["run_id"]

        await client.post(
            f"/api/runs/{older}/messages",
            json={"text": "继续较早会话的部署"},
        )
        await wait_status(client, older, "READY")

        runs = (await client.get("/api/runs")).json()["runs"]
        assert [run["run_id"] for run in runs] == [older, newer], runs


async def test_list_endpoint_falls_back_to_end_then_creation_time():
    """旧摘要缺活动时刻时，ENDED 取结束时刻，空会话取创建时刻。"""
    app = plain_app()
    async with async_client(app) as client:
        ended = (await client.post("/api/runs", json={})).json()["run_id"]
        empty = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{ended}/end")

        # 模拟 last_event_at 尚未进入摘要的旧进程内数据；观测仍只走 HTTP。
        manager = app.state.run_manager
        for run_id in (ended, empty):
            manager.get(run_id).last_event_at = None

        runs = (await client.get("/api/runs")).json()["runs"]
        assert [run["run_id"] for run in runs] == [ended, empty], runs
        by_id = {run["run_id"]: run for run in runs}
        assert by_id[ended]["ended_at"] is not None
        assert by_id[empty]["ended_at"] is None
        assert all(run["last_event_at"] is None for run in runs)
        assert set(runs[0]) == {
            "run_id", "status", "stage", "first_prompt", "title",
            "started_at", "ended_at", "last_event_at", "resumed_from",
        }


async def test_list_endpoint_breaks_activity_ties_by_creation_time():
    """活动时刻相同时，较晚创建的会话稳定在前。"""
    infos = [
        session_info(
            "aaaaaaaa-0000-0000-0000-000000000000", "较早会话", 1_000, last_ms=5_000,
        ),
        session_info(
            "bbbbbbbb-0000-0000-0000-000000000000", "较晚会话", 2_000, last_ms=5_000,
        ),
    ]
    app = history_app(infos, lambda _sid: [msg("user", "部署 nginx")])
    async with async_client(app) as client:
        runs = (await client.get("/api/runs")).json()["runs"]
        assert [run["run_id"] for run in runs] == [
            "run_hist_bbbbbbbb",
            "run_hist_aaaaaaaa",
        ], runs


async def test_list_endpoint_breaks_exact_ties_by_run_id():
    """活动与创建时刻全相同时，发现顺序和重复轮询都不改变列表。"""
    info_a = session_info(
        "aaaaaaaa-0000-0000-0000-000000000000", "相同会话", 1_000, last_ms=5_000,
    )
    info_b = session_info(
        "bbbbbbbb-0000-0000-0000-000000000000", "相同会话", 1_000, last_ms=5_000,
    )
    expected = ["run_hist_bbbbbbbb", "run_hist_aaaaaaaa"]

    async def poll_orders(infos):
        app = history_app(infos, lambda _sid: [msg("user", "部署 nginx")])
        async with async_client(app) as client:
            return [
                [run["run_id"] for run in (await client.get("/api/runs")).json()["runs"]]
                for _ in range(3)
            ]

    assert await poll_orders([info_a, info_b]) == [expected] * 3
    assert await poll_orders([info_b, info_a]) == [expected] * 3


async def test_replay_restores_history_chattable():
    infos = [session_info("11111111-2222-3333-4444-555555555555", "部署 nginx 1.25 到 server-a",
                          created_ms=1_700_000_000_000, custom_title="部署 nginx")]
    app = history_app(infos, lambda sid: deploy_transcript())
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        runs = (await client.get("/api/runs")).json()["runs"]
        assert len(runs) == 1, runs
        run = runs[0]
        assert run["status"] == "READY"  # 重放历史：可续聊，非只读终态
        assert run["first_prompt"] == "部署 nginx 1.25 到 server-a"  # 名字来自 SDK first_prompt
        assert run["title"] == "部署 nginx"  # 标题来自 transcript 的 custom-title 行
        assert run["started_at"] == 1_700_000_000.0
        assert run["stage"] == "GUIDE"
        # 历史无逐事件时刻，最后活动以 transcript 落盘时刻近似
        assert run["last_event_at"] == 1_700_000_005.0, run
        run_id = run["run_id"]

        # 事件流从 transcript 消息重新映射：session.started 起步、无终态收尾
        resp = await open_stream(client, run_id)
        events, pings = await collect_sse(resp, deadline_s=0.5)
        types = [e["event"] for e in events]
        assert types == [
            "session.started",
            "turn.started",
            "user.message",
            "agent.thinking",
            "agent.message",
            "stage.changed",
            "agent.tool_started",
            "agent.tool_finished",
            "agent.message",
            "turn.completed",
            "turn.started",
            "user.message",
            "agent.message",
            "turn.interrupted",  # 末回合无下一条输入收口：如实呈现截断，不伪造完成
        ], types
        seqs = [int(e["id"]) for e in events]
        assert seqs == list(range(1, len(events) + 1)), seqs
        assert events[2]["data"]["text"] == "部署 nginx 1.25 到 server-a"
        # 回合边界由下一条用户输入推导；汇总取该回合最后一条 agent 文本
        assert "指南阶段完成" in events[8]["data"]["text"]
        assert events[9]["data"]["result"] == events[8]["data"]["text"]
        # transcript 重放同样过脱敏与映射路径
        assert "HWPFEJ9AB3CDEFGHIJKL" not in str(events)
        # 无终态收尾事件：重放会话可续聊，快照重放完即结束（无心跳常驻）
        assert "session.ended" not in types, types
        assert pings == 0, pings

        # 直接续聊：同会话发指令照常执行
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续之前的部署"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_id, "READY")


async def test_replayed_run_interventions_allowed():
    sid = "11111111-2222-3333-4444-555555555555"
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)], lambda s: deploy_transcript())
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.get("/api/runs")).json()["runs"][0]["run_id"]
        # 重放会话可干预（可续聊、可停止、可结束），不再是只读历史
        for path in ("messages", "stop", "end"):
            r = await client.post(f"/api/runs/{run_id}/{path}", json={"text": "继续"} if path == "messages" else {})
            assert r.status_code == 200, (path, r.text)
        assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "ENDED"
        # 结束后墓碑入册：重启不再复活（test_state 覆盖端到端）


async def test_replayed_run_serves_as_clone_source():
    sid = "11111111-2222-3333-4444-555555555555"
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)], lambda s: deploy_transcript())
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.get("/api/runs")).json()["runs"][0]["run_id"]
        r = await client.post(f"/api/runs/{run_id}/clone")
        assert r.status_code == 200, r.text
        assert r.json()["resumed_from"] == run_id

        # 克隆会话照常执行首条指令（回合连接以源 session resume）
        new_id = r.json()["run_id"]
        await client.post(f"/api/runs/{new_id}/messages", json={"text": "继续之前的部署"})
        await wait_status(client, new_id, "READY")
        resp = await open_stream(client, new_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        assert [e["event"] for e in drop_title_events(events)][-1] == "turn.completed"
        # Fork 目标使用新身份，并从重放找回的源身份取上下文。
        start = app.state.session_factory.starts[-1]
        assert start.target_session_id != sid
        assert start.context_session_id == sid
        assert start.fork_session is True


async def test_replay_passes_source_times_through():
    """重放事件 ts 透传源 transcript 行时刻（uuid 对齐）：session.started
    取会话首个时刻、一条消息派生的多条事件共享同一时刻、收尾事件取边界
    消息时刻——快照端点读回对拍 + 回合求和可得真实时长。"""
    sid = "11111111-2222-3333-4444-555555555555"
    T = 1_700_000_000.0
    messages = uuided(deploy_transcript())
    times = {m.uuid: T + 600 * i for i, m in enumerate(messages)}
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)],
                      lambda s: messages, times_fn=lambda s: times)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.get("/api/runs")).json()["runs"][0]["run_id"]
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=0.5)
        by_type = [(e["event"], e["data"]["ts"]) for e in events]
        # 首条消息（用户指令）派生的事件共享其时刻；session.started 取会话首时刻
        assert by_type[0] == ("session.started", T)
        assert by_type[1] == ("turn.started", T)
        assert by_type[2] == ("user.message", T)
        # 一条 assistant 消息派生的事件共享同一源时刻（turn.started/user.message
        # 与首消息同刻，第二消息的 thinking/text 各自取自己行的时刻）
        assert by_type[3] == ("agent.thinking", T + 600)
        assert by_type[4] == ("agent.message", T + 1200)
        # 完整回合收尾：下一条用户输入前的最后一条消息时刻（u005 = T + 3000）
        assert by_type[9] == ("turn.completed", T + 3000)
        # 未收尾回合：以回合内最后一条已落消息时刻收口（u007 = T + 4200）
        assert by_type[-1] == ("turn.interrupted", T + 4200)
        # 前端求和口径对拍：各回合时长之和（扣空档）
        assert _replayed_seconds(events) == (T + 3000 - T) + (T + 4200 - (T + 3600))


def _replayed_seconds(events):
    """前端 activeSeconds 同款求和：user.message 开段、回合收尾事件闭段。"""
    seconds = 0.0
    start = None
    for e in events:
        if e["event"] == "user.message":
            start = e["data"]["ts"]
        elif e["event"] in ("turn.completed", "turn.interrupted") and start is not None:
            seconds += e["data"]["ts"] - start
            start = None
    return seconds


async def test_replay_falls_back_to_now_without_source_times():
    """消息 uuid 不在时刻表（transcript 缺 timestamp / 文件损坏）时 ts 回退
    append 当下，恢复照常不阻断。"""
    sid = "11111111-2222-3333-4444-555555555555"
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)],
                      lambda s: deploy_transcript(),
                      times_fn=lambda s: {"u-does-not-exist": 123.0})
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        runs = (await client.get("/api/runs")).json()["runs"]
        assert len(runs) == 1, runs  # 缺时刻源只回退、不阻断恢复
        run_id = runs[0]["run_id"]
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=0.5)
        now = time.time()
        for e in events:  # 未知 uuid 全部回退当下（远晚于 transcript 元信息时刻）
            assert abs(e["data"]["ts"] - now) < 60, e


async def test_replayed_run_clones_with_overwritten_activity():
    """克隆会话历史事件 ts 与源一致（透传）；摘要 last_event_at 为克隆操作
    时刻（覆写，防刚克隆会话沉底、侧栏排最前）。"""
    sid = "11111111-2222-3333-4444-555555555555"
    T = 1_700_000_000.0
    messages = uuided(deploy_transcript())
    times = {m.uuid: T + 600 * i for i, m in enumerate(messages)}
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)],
                      lambda s: messages, times_fn=lambda s: times)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.get("/api/runs")).json()["runs"][0]["run_id"]
        src_events, _ = await collect_sse(await open_stream(client, run_id), deadline_s=0.5)
        # 源流历史（除 session.started：克隆新流有自己的起点事件）
        src_ts = [(e["event"], e["data"]["ts"]) for e in src_events[1:]]

        before = time.time()
        r = await client.post(f"/api/runs/{run_id}/clone")
        assert r.status_code == 200, r.text
        new_id = r.json()["run_id"]

        runs = {x["run_id"]: x for x in (await client.get("/api/runs")).json()["runs"]}
        assert runs[new_id]["last_event_at"] >= before  # 覆写为克隆操作时刻
        assert (await client.get("/api/runs")).json()["runs"][0]["run_id"] == new_id  # 排最前

        clone_events, _ = await collect_sse(await open_stream(client, new_id), deadline_s=0.5)
        # 历史事件时刻原样透传（对拍源流；克隆自己的 session.started 是新
        # 会话真实起点，取当下，不在对拍范围）
        assert [(e["event"], e["data"]["ts"]) for e in clone_events[1:]] == src_ts


async def test_replay_skips_messageless_and_broken_sessions():
    ok_sid = "11111111-2222-3333-4444-555555555555"
    empty_sid = "99999999-8888-7777-6666-555555555555"
    broken_sid = "77777777-6666-5555-4444-333333333333"
    infos = [
        session_info(ok_sid, "部署 nginx", 1_700_000_000_000),
        session_info(empty_sid, None, 1_700_000_100_000),
        session_info(broken_sid, "损坏会话", 1_700_000_200_000),
    ]

    def messages(sid):
        if sid == broken_sid:
            raise OSError("transcript unreadable")
        return deploy_transcript() if sid == ok_sid else []

    app = history_app(infos, messages)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        runs = (await client.get("/api/runs")).json()["runs"]
        assert [r["first_prompt"] for r in runs] == ["部署 nginx"], runs  # 空与损坏的都跳过


async def test_residual_cli_processes_warned_not_killed():
    logs = []

    class Capture(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    logger = logging.getLogger("web")
    handler = Capture()
    logger.addHandler(handler)
    try:
        # 发现残留：写日志告警（不自动杀，由人工处置）
        plain_app(residual_cli_scan=lambda: ["123", "456"])
        assert any("123" in m and "456" in m and "人工" in m for m in logs), logs
        # 无残留：安静启动
        logs.clear()
        plain_app(residual_cli_scan=lambda: [])
        assert logs == [], logs
    finally:
        logger.removeHandler(handler)


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
