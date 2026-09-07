#!/usr/bin/env python
"""web 服务主缝测试 —— HTTP 进、SSE 出。

缝：FastAPI ASGI 测试客户端驱动全部外部行为；会话经工厂注入
脚本化假实现，不触网、不启动真 SDK。纯 assert，无 pytest。

运行：python web/tests/test_api.py
"""
import asyncio
import json
import os
import sys
import time
from uuid import UUID

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.state import load_state  # noqa: E402
from web.tests.support import StreamingASGITransport, make_test_app  # noqa: E402

DELAY = 0.02


def make_app(script=None, delay=DELAY, max_parallel_runs=None):
    return make_test_app(
        session_factory=FakeSessionFactory(script=script if script is not None else DEFAULT_SCRIPT, delay=delay),
        max_parallel_runs=max_parallel_runs,
    )


class ProbeSession:
    """可控的 SDK 边界探针：只记录公开生命周期与调用，不窥探回合内部。"""

    def __init__(self, start, behavior):
        self.start = start
        self.behavior = behavior
        self.enter_started = asyncio.Event()
        self.release_enter = asyncio.Event()
        self.query_started = asyncio.Event()
        self.release_response = asyncio.Event()
        self.query_text = None
        self.query_calls = 0
        self.cloud_actions = 0
        self.interrupt_calls = 0
        self.stale_interrupt_calls = 0
        self.active = False
        self.exited = False
        self.interrupted = False

    async def __aenter__(self):
        self.enter_started.set()
        if self.behavior == "slow_enter":
            await self.release_enter.wait()
        self.active = True
        return self

    async def __aexit__(self, *exc_info):
        self.active = False
        self.exited = True
        return False

    async def query(self, text):
        self.query_calls += 1
        self.cloud_actions += 1
        self.query_text = text
        self.query_started.set()

    async def interrupt(self):
        self.interrupt_calls += 1
        if not self.active:
            self.stale_interrupt_calls += 1
            raise RuntimeError("旧 adapter 已退出")
        self.interrupted = True
        self.release_response.set()

    async def receive_response(self):
        if self.behavior == "block":
            await self.release_response.wait()
        if self.behavior == "exception":
            raise RuntimeError("SDK 流异常")
        subtype = (
            "error_during_execution"
            if self.behavior == "failure" or self.interrupted
            else "success"
        )
        yield {
            "type": "result",
            "subtype": subtype,
            "is_error": subtype != "success",
            "result": "" if subtype != "success" else "完成",
            "session_id": self.start.target_session_id,
        }


class ProbeSessionFactory:
    def __init__(self, behaviors):
        self.behaviors = iter(behaviors)
        self.sessions = []

    def __call__(self, start):
        session = ProbeSession(start, next(self.behaviors))
        self.sessions.append(session)
        return session


def parse_sse_block(block_lines):
    """一个 SSE 事件块（若干属性行）→ {id, event, data}。"""
    ev = {}
    for line in block_lines:
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "data":
            ev["data"] = json.loads(value)
        elif key in ("id", "event"):
            ev[key] = value
    return ev


def drop_title_events(events):
    """剔除 session.title_changed（标题生成异步落流、时序自由，主缝断言不关心）。"""
    return [e for e in events if e["event"] != "session.title_changed"]


def transcript_prompts(factory, session_id):
    """测试 transcript adapter 中真实用户指令的有序文本。"""
    return [
        message.message["content"]
        for message in factory.get_session_messages(session_id)
        if message.type == "user" and isinstance(message.message.get("content"), str)
    ]


async def collect_sse(resp, stop=None, deadline_s=5.0, max_pings=None):
    """聚合 SSE 流为 (事件列表, 心跳行数)；stop(ev) 为 True 时停止读取，
    max_pings 收满即停（常驻的流不会自己结束）。"""
    pings = 0
    events = []
    block = []
    deadline = time.monotonic() + deadline_s
    async for line in resp.aiter_lines():
        if time.monotonic() > deadline:
            break
        if line == "":
            if block:
                ev = parse_sse_block(block)
                events.append(ev)
                block = []
                if stop and stop(ev):
                    break
            continue
        if line.startswith(":"):
            pings += 1
            if max_pings is not None and pings >= max_pings:
                break
            continue
        block.append(line)
    return events, pings


async def wait_status(client, run_id, want, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = await client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] == want:
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} 未进入 {want}，最后状态 {last}")


async def wait_until(predicate, timeout_s=1.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


async def wait_replay(client, run_id, ok, timeout_s=8.0):
    """轮询快照直到 ok(events) 满足（回合收尾是异步的，状态转挂起与事件
    落库之间可能隔着第二回合的启动）。"""
    deadline = time.monotonic() + timeout_s
    events = []
    while time.monotonic() < deadline:
        events, _ = await collect_sse(await open_stream(client, run_id))
        if events and ok(events):
            return events
        await asyncio.sleep(0.02)
    raise AssertionError(f"快照未满足条件，当前 {[e['event'] for e in events]}")


async def open_stream(client, run_id, last_event_id=None):
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id else {}
    return await client.send(
        client.build_request("GET", f"/api/runs/{run_id}/events", headers=headers),
        stream=True,
    )


# 打开全局流：一条连接广播全部会话的实时事件，无 Last-Event-ID 断点语义
async def open_global_stream(client):
    return await client.send(
        client.build_request("GET", "/api/stream"),
        stream=True,
    )


async def test_create_run_returns_ready_immediately():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"].startswith("run_")
        assert body["status"] == "READY"
        assert body["resumed_from"] is None
        r = await client.get(f"/api/runs/{body['run_id']}")
        assert r.json()["status"] == "READY"
        assert app.state.session_factory.starts == []
        assert load_state(app.state.test_root / "state.json")["sessions"] == {}


async def test_first_message_drives_scripted_turn():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        assert [e["event"] for e in events][:2] == ["session.started"]

        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "RUNNING"

        await wait_status(client, run_id, "READY")
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        events = drop_title_events(events)
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
        ], types
        # seq 从 1 起严格递增，SSE 的 id 即 seq
        seqs = [int(e["id"]) for e in events]
        assert seqs == list(range(1, len(events) + 1)), seqs
        # 首条指令原样进入事件流
        assert events[2]["data"]["text"] == "部署 nginx 1.25 到 server-a"
        # 每条事件带服务端 ts（时长冻结点），单调不回退
        tss = [e["data"]["ts"] for e in events]
        assert all(isinstance(t, (int, float)) for t in tss), tss
        assert tss == sorted(tss), tss
        # 阶段由 Task + subagent_type 推导
        assert events[5]["data"] == {"stage": "GUIDE", "status": "running", "ts": events[5]["data"]["ts"]}
        # 工具事件带工具名 + 脱敏摘要（折叠行）+ 脱敏全文（展开查看）
        assert events[6]["data"]["tool"] == "Task"
        assert "detail" in events[6]["data"] and "summary" in events[6]["data"]
        assert events[7]["data"]["tool"] == "Task"
        assert "detail" in events[7]["data"] and "summary" in events[7]["data"]
        # 回合汇总携带 result 文本
        assert "result" in events[-1]["data"]


async def test_ready_run_snapshot_completes_without_heartbeat():
    """快照语义：挂起会话的历史一次给完即结束响应，无心跳（实时事件
    由全局流续接，per-run 通道不再常驻）。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")

        async with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            events, pings = await collect_sse(resp)
        assert [e["event"] for e in drop_title_events(events)][-1] == "turn.completed"
        assert pings == 0, pings


async def test_ended_run_snapshot_contains_session_ended():
    """ENDED 会话的快照含 session.ended 末条，重放完结束响应（服务端不替
    前端关流——终态语义由事件本身承载）。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        await client.post(f"/api/runs/{run_id}/end")

        events, pings = await collect_sse(await open_stream(client, run_id))
        types = [e["event"] for e in events]
        assert types[-1] == "session.ended", types
        # 重放完全部历史后响应正常结束：读循环自然结束，无心跳
        assert pings == 0, pings
        r = await client.get(f"/api/runs/{run_id}")
        assert r.json()["status"] == "ENDED"


async def test_last_event_id_replays_from_next_seq_without_duplicates():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")

        # 第一段连接只读到 seq=3 即断开
        resp = await open_stream(client, run_id)
        first, _ = await collect_sse(resp, stop=lambda e: int(e["id"]) == 3)
        assert int(first[-1]["id"]) == 3
        await resp.aclose()

        # 重连携带 Last-Event-ID=3：从 seq+1 重放，已收事件不重发
        resp = await open_stream(client, run_id, last_event_id=3)
        second, _ = await collect_sse(resp, deadline_s=1.0)
        ids = [int(e["id"]) for e in second]
        assert ids[0] == 4, ids
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        assert not [e for e in second if int(e["id"]) <= 3]


async def test_parallel_runs_execute_independently():
    """并行核心：A 执行中新建 B 成功、向 B 发送成功，两回合同时 RUNNING，
    事件不串线。"""
    app = make_app(delay=0.1)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        # A 执行中：新建不被拒（无全局门禁）
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text
        run_b = r.json()["run_id"]
        # A 执行中：向 B 发送成功
        r = await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        assert r.status_code == 200, r.text
        # 两会话同时 RUNNING
        assert (await client.get(f"/api/runs/{run_a}")).json()["status"] == "RUNNING"
        assert (await client.get(f"/api/runs/{run_b}")).json()["status"] == "RUNNING"

        await wait_status(client, run_a, "READY", timeout_s=8.0)
        await wait_status(client, run_b, "READY", timeout_s=8.0)
        # 事件各归各流：A 里只有 nginx 指令，B 里只有 redis 指令
        events_a, _ = await collect_sse(await open_stream(client, run_a))
        events_b, _ = await collect_sse(await open_stream(client, run_b))
        texts_a = [e["data"]["text"] for e in drop_title_events(events_a) if e["event"] == "user.message"]
        texts_b = [e["data"]["text"] for e in drop_title_events(events_b) if e["event"] == "user.message"]
        assert texts_a == ["部署 nginx"], texts_a
        assert texts_b == ["部署 redis"], texts_b
        assert [e["event"] for e in drop_title_events(events_a)][-1] == "turn.completed"
        assert [e["event"] for e in drop_title_events(events_b)][-1] == "turn.completed"


async def test_stop_a_does_not_affect_b():
    factory = ProbeSessionFactory(["block", "block"])
    app = make_test_app(session_factory=factory)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        await wait_until(
            lambda: len(factory.sessions) == 2
            and all(session.query_started.is_set() for session in factory.sessions)
        )
        by_query = {session.query_text: session for session in factory.sessions}

        r = await client.post(f"/api/runs/{run_a}/stop", json={})
        assert r.status_code == 200, r.text
        assert by_query["部署 nginx"].interrupt_calls == 1
        assert by_query["部署 redis"].interrupt_calls == 0
        by_query["部署 redis"].release_response.set()
        await wait_status(client, run_a, "READY", timeout_s=8.0)
        # B 不受 A 停止影响：照常执行到完成
        await wait_status(client, run_b, "READY", timeout_s=8.0)
        events_b, _ = await collect_sse(await open_stream(client, run_b))
        types_b = [e["event"] for e in drop_title_events(events_b)]
        assert types_b[-1] == "turn.completed", types_b
        assert "turn.stopped" not in types_b, types_b
        events_a, _ = await collect_sse(await open_stream(client, run_a))
        assert "turn.stopped" in [e["event"] for e in drop_title_events(events_a)]


async def test_failed_turn_returns_to_ready_and_continues():
    """回合失败（连接异常）是回合结果：会话回 READY，同会话可立即续发。"""
    fail_script = [RuntimeError("sdk crashed: password=leaked-secret")]
    app = make_app(script=fail_script, delay=0.1)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")

        events, _ = await collect_sse(await open_stream(client, run_id))
        types = [e["event"] for e in events]
        assert types[-1] == "turn.failed", types
        # 错误摘要同样过脱敏层
        assert "leaked-secret" not in json.dumps(events[-1], ensure_ascii=False)

        # 同一会话直接续发：新回合照常执行（重试 = 新连接）
        app.state.session_factory.script = list(DEFAULT_SCRIPT)
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "刚才哪里失败，继续"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_id, "READY")
        events, _ = await collect_sse(await open_stream(client, run_id))
        types = [e["event"] for e in drop_title_events(events)]
        assert types[-1] == "turn.completed", types
        assert types.count("turn.failed") == 1, types


async def test_max_turns_result_fails_turn_not_session():
    """Result 的错误 subtype（max_turns 触发）→ turn.failed 携带脱敏摘要，
    会话回 READY 可续聊。判定取兜底：非 success 即回合失败。"""
    script = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "开始部署。"}]}},
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "已达到回合上限，回合被截断 password: leak-me"},
    ]
    app = make_app(script)
    async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        events = await wait_replay(client, run_id, lambda evs: evs[-1]["event"] == "turn.failed")
        assert events[-1]["event"] == "turn.failed"
        message = events[-1]["data"]["message"]
        assert "error_max_turns" in message
        assert "回合上限" in message
        assert "leak-me" not in message  # 摘要脱敏：密码不外泄


async def test_send_while_running_409_turn_in_progress():
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        # 执行中发送：409 turn_in_progress，不打断在飞回合
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "删掉刚创建的 ECS"})
        assert r.status_code == 409
        assert r.json() == {"detail": "turn_in_progress"}
        await wait_status(client, run_id, "READY", timeout_s=8.0)
        # 回合没有被隐式打断：正常完成
        events, _ = await collect_sse(await open_stream(client, run_id))
        types = [e["event"] for e in drop_title_events(events)]
        assert types[-1] == "turn.completed", types
        assert types.count("user.message") == 1, types


async def test_parallel_limit_reached_409():
    """WEB_MAX_PARALLEL_RUNS 数执行中回合：超限的发送 409，新建与克隆
    不受限。"""
    app = make_app(delay=0.2, max_parallel_runs=2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        # 第三个回合超限
        run_c = (await client.post("/api/runs", json={})).json()["run_id"]
        r = await client.post(f"/api/runs/{run_c}/messages", json={"text": "部署 pi"})
        assert r.status_code == 409
        assert r.json() == {"detail": "parallel_limit_reached"}
        # 新建与克隆不受限（不占执行名额）
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text
        run_d = r.json()["run_id"]
        r = await client.post(f"/api/runs/{run_d}/clone")
        assert r.status_code == 200, r.text

        # 名额释放（A 完成）后重发成功
        await wait_status(client, run_a, "READY", timeout_s=8.0)
        r = await client.post(f"/api/runs/{run_c}/messages", json={"text": "部署 pi"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_c, "READY", timeout_s=8.0)


async def test_stop_returns_to_ready_and_session_continues():
    # 慢剧本保证 stop 必落在回合执行中（快剧本下时序不稳）
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        r = await client.post(f"/api/runs/{run_id}/stop", json={})
        assert r.status_code == 200, r.text
        await wait_status(client, run_id, "READY", timeout_s=8.0)

        # 会话保留：停止后可继续任意指令
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "删掉刚创建的 ECS"})
        assert r.status_code == 200, r.text
        events = await wait_replay(
            client, run_id,
            lambda evs: [e["event"] for e in evs].count("user.message") == 2
            and evs[-1]["event"] == "turn.completed",
        )
        types = [e["event"] for e in events]
        assert types.count("turn.stopped") == 1, types
        assert types.count("turn.completed") == 1, types  # 仅第二回合正常收尾
        ums = [i for i, t in enumerate(types) if t == "user.message"]
        assert ums[0] < types.index("turn.stopped") < ums[1], types


async def test_stop_during_sdk_startup_skips_query_and_cloud_actions():
    factory = ProbeSessionFactory(["slow_enter"])
    app = make_test_app(session_factory=factory)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        sent = await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署生产环境"})
        assert sent.status_code == 200, sent.text

        await wait_until(lambda: len(factory.sessions) == 1)
        session = factory.sessions[0]
        await asyncio.wait_for(session.enter_started.wait(), timeout=1.0)
        stopped = await client.post(f"/api/runs/{run_id}/stop", json={})
        assert stopped.status_code == 200, stopped.text
        assert session.interrupt_calls == 0

        session.release_enter.set()
        summary = await wait_status(client, run_id, "READY")
        assert summary["status"] == "READY"
        assert session.query_calls == 0
        assert session.cloud_actions == 0

        events, _ = await collect_sse(await open_stream(client, run_id))
        types = [event["event"] for event in drop_title_events(events)]
        assert types == [
            "session.started",
            "turn.started",
            "user.message",
            "turn.stopped",
        ], types


async def test_closed_adapters_are_not_interrupted_by_later_turns():
    """所有普通收尾都关闭旧 adapter；下一回合启动期 stop 不会打到旧实例。"""
    for behavior in ("success", "failure", "exception", "block"):
        factory = ProbeSessionFactory([behavior, "slow_enter"])
        app = make_test_app(session_factory=factory)
        transport = StreamingASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            run_id = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{run_id}/messages", json={"text": f"首回合 {behavior}"})
            await wait_until(lambda: len(factory.sessions) == 1)
            first = factory.sessions[0]
            if behavior == "block":
                await asyncio.wait_for(first.query_started.wait(), timeout=1.0)
                stopped = await client.post(f"/api/runs/{run_id}/stop", json={})
                assert stopped.status_code == 200, stopped.text

            await wait_status(client, run_id, "READY")
            assert first.exited and not first.active
            prior_interrupts = first.interrupt_calls

            # READY 上 stop 是无操作，即使旧 adapter 对陈旧调用会主动报错。
            noop = await client.post(f"/api/runs/{run_id}/stop", json={})
            assert noop.status_code == 200, noop.text
            assert first.interrupt_calls == prior_interrupts

            await client.post(f"/api/runs/{run_id}/messages", json={"text": "第二回合"})
            await wait_until(lambda: len(factory.sessions) == 2)
            second = factory.sessions[1]
            await asyncio.wait_for(second.enter_started.wait(), timeout=1.0)
            stopped = await client.post(f"/api/runs/{run_id}/stop", json={})
            assert stopped.status_code == 200, stopped.text
            assert first.interrupt_calls == prior_interrupts
            assert first.stale_interrupt_calls == 0
            assert second.interrupt_calls == 0

            second.release_enter.set()
            await wait_status(client, run_id, "READY")
            assert second.query_calls == 0
            assert second.exited and not second.active


async def test_end_cancellation_closes_live_adapter():
    factory = ProbeSessionFactory(["block"])
    app = make_test_app(session_factory=factory)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "长回合"})
        await wait_until(lambda: len(factory.sessions) == 1)
        session = factory.sessions[0]
        await asyncio.wait_for(session.query_started.wait(), timeout=1.0)

        ended = await client.post(f"/api/runs/{run_id}/end")
        assert ended.status_code == 200, ended.text
        assert ended.json()["status"] == "ENDED"
        assert session.exited and not session.active
        assert session.interrupt_calls == 0


async def test_stop_on_ready_run_is_noop():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 挂起中无回合可停：幂等无操作
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        r = await client.post(f"/api/runs/{run_id}/stop", json={})
        assert r.status_code == 200, r.text
        assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "READY"
        events, _ = await collect_sse(await open_stream(client, run_id))
        assert [e["event"] for e in events].count("turn.stopped") == 0


async def test_end_semantics():
    """end：显式结束会话——RUNNING 中 end 取消在飞回合、session.ended 是
    流的最后一条事件、ENDED 后一切干预 409 session_not_active（克隆除外）。"""
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        r = await client.post(f"/api/runs/{run_id}/end")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ENDED"

        # 结束后的干预端点全部拒绝（发送 / 停止 / 再结束）
        for path in ("messages", "stop", "end"):
            r = await client.post(f"/api/runs/{run_id}/{path}", json={"text": "继续"} if path == "messages" else {})
            assert r.status_code == 409, (path, r.text)
            assert r.json() == {"detail": "session_not_active"}, (path, r.text)

        # 事件流收尾序列：回合不补收尾事件，session.ended 是最后一条；
        # 快照重放完即结束（无心跳）
        events, pings = await collect_sse(await open_stream(client, run_id))
        types = [e["event"] for e in events]
        assert types[-1] == "session.ended", types
        assert "turn.stopped" not in types and "turn.completed" not in types and "turn.failed" not in types, types
        assert pings == 0, pings

        # READY 会话同样可结束
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        await wait_status(client, run_b, "READY")
        r = await client.post(f"/api/runs/{run_b}/end")
        assert r.status_code == 200, r.text
        assert (await client.get(f"/api/runs/{run_b}")).json()["status"] == "ENDED"


async def test_clone_from_ready_source():
    """克隆 READY 源：新 run_id、事件流转录、标题/首条指令继承、
    首条指令不再生成标题、源会话不变、克隆回合以源 session_id resume。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_a, "READY")

        r = await client.post(f"/api/runs/{run_a}/clone")
        assert r.status_code == 200, r.text
        body = r.json()
        run_b = body["run_id"]
        assert run_b != run_a
        assert body["resumed_from"] == run_a
        assert body["status"] == "READY"

        # 源会话不变
        summary_a = (await client.get(f"/api/runs/{run_a}")).json()
        assert summary_a["status"] == "READY"
        assert summary_a["first_prompt"] == "部署 nginx"

        # 新会话带转录历史（跳过源的生命周期事件），seq 重新编号
        events_b, _ = await collect_sse(await open_stream(client, run_b))
        types_b = [e["event"] for e in drop_title_events(events_b)]
        assert types_b[0] == "session.started"
        assert "turn.completed" in types_b and "user.message" in types_b
        assert "session.ended" not in types_b, types_b  # 源收尾不带入
        seqs = [int(e["id"]) for e in events_b]
        assert seqs == list(range(1, len(events_b) + 1)), seqs

        # 克隆后首条指令：正常执行、不再生成标题（title_calls 见 test_title）
        r = await client.post(f"/api/runs/{run_b}/messages", json={"text": "换个变体试试"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_b, "READY")
        events_b2, _ = await collect_sse(await open_stream(client, run_b))
        assert [e["event"] for e in drop_title_events(events_b2)][-1] == "turn.completed"
        # 工厂收到 Fork 的目标身份、源上下文身份与显式 Fork 意图。
        starts = app.state.session_factory.starts
        source = starts[0].target_session_id
        forked = starts[-1]
        assert forked.context_session_id == source, starts
        assert forked.target_session_id != source, starts
        assert forked.fork_session is True, starts


async def test_clone_from_ended_source():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_a, "READY")
        await client.post(f"/api/runs/{run_a}/end")
        r = await client.post(f"/api/runs/{run_a}/clone")
        assert r.status_code == 200, r.text
        run_b = r.json()["run_id"]
        events_b, _ = await collect_sse(await open_stream(client, run_b))
        types_b = [e["event"] for e in drop_title_events(events_b)]
        assert "user.message" in types_b and "turn.completed" in types_b, types_b
        # 克隆出的新会话可正常续聊（源 ENDED 不传染）
        r = await client.post(f"/api/runs/{run_b}/messages", json={"text": "继续"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_b, "READY")


async def test_clone_running_source_409():
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        r = await client.post(f"/api/runs/{run_a}/clone")
        assert r.status_code == 409
        assert r.json() == {"detail": "session_running"}
        # 源会话状态不变、不受克隆尝试影响
        assert (await client.get(f"/api/runs/{run_a}")).json()["status"] == "RUNNING"
        await wait_status(client, run_a, "READY", timeout_s=8.0)


async def test_second_turn_resumes_own_session_id():
    """首回合接受时预分配合法身份并落盘；第二回合续接同一身份。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        starts = app.state.session_factory.starts
        assert len(starts) == 1, starts
        target = starts[0].target_session_id
        UUID(target)
        assert starts[0].context_session_id is None
        assert starts[0].fork_session is False
        state = load_state(app.state.test_root / "state.json")
        assert state["sessions"] == {run_id: target}, state

        await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
        await wait_status(client, run_id, "READY")
        resumed = app.state.session_factory.starts[-1]
        assert resumed.target_session_id == target
        assert resumed.context_session_id == target
        assert resumed.fork_session is False


async def test_sdk_cannot_replace_preallocated_session_identity():
    """SDK 任一公开消息回报其他身份时，回合失败且既有映射不被改写。"""
    wrong = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    script = [
        {
            "type": "assistant",
            "session_id": wrong,
            "message": {"content": [{"type": "text", "text": "不应进入事件流"}]},
        },
        {"type": "result", "subtype": "success", "result": "不应完成"},
    ]
    app = make_app(script=script)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")

        target = app.state.session_factory.starts[0].target_session_id
        assert target != wrong
        state = load_state(app.state.test_root / "state.json")
        assert state["sessions"] == {run_id: target}, state
        events, _ = await collect_sse(await open_stream(client, run_id))
        assert [e["event"] for e in events][-1] == "turn.failed", events
        assert "身份" in events[-1]["data"]["message"]
        assert all(e["event"] != "agent.message" for e in events), events


async def test_clone_second_turn_resumes_clone_own_session_id():
    """克隆会话分叉后：第二回合续自己的 session_id（克隆首回合建立），
    不再重复 resume 源会话（否则克隆首回合上下文丢失）。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_a, "READY")
        run_b = (await client.post(f"/api/runs/{run_a}/clone")).json()["run_id"]
        # 克隆首回合从源身份取上下文，同时使用自己的预分配目标身份。
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "换个变体"})
        await wait_status(client, run_b, "READY")
        clone_target = app.state.session_factory.starts[-1].target_session_id
        # 克隆第二回合只续自己的身份。
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "继续"})
        await wait_status(client, run_b, "READY")
        resumed = app.state.session_factory.starts[-1]
        assert resumed.target_session_id == clone_target
        assert resumed.context_session_id == clone_target
        assert resumed.fork_session is False


async def test_fork_transcripts_share_prefix_then_diverge_during_parallel_turns():
    """Fork 建立后源与分支可并行演进，SDK transcript 与 Web 事件均不串线。"""
    script = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "已处理当前指令"}]},
        },
        {"type": "result", "subtype": "success", "result": "已处理当前指令"},
    ]
    factory = FakeSessionFactory(script=script, delay=0.05)
    app = make_test_app(session_factory=factory)
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app=app), base_url="http://testserver"
    ) as client:
        source = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(
            f"/api/runs/{source}/messages", json={"text": "共享的分叉前指令"}
        )
        await wait_status(client, source, "READY")
        source_state_before = (await client.get(f"/api/runs/{source}")).json()
        source_events_before, _ = await collect_sse(await open_stream(client, source))
        source_sid = load_state(app.state.test_root / "state.json")["sessions"][source]

        fork = (await client.post(f"/api/runs/{source}/clone")).json()["run_id"]
        assert (await client.get(f"/api/runs/{source}")).json() == source_state_before
        source_events_after, _ = await collect_sse(await open_stream(client, source))
        assert source_events_after == source_events_before
        assert load_state(app.state.test_root / "state.json")["sessions"] == {
            source: source_sid,
        }

        # 首回合建立独立 Fork transcript；其后两侧同时运行不同回合。
        response = await client.post(
            f"/api/runs/{fork}/messages", json={"text": "建立 Fork 分支"}
        )
        assert response.status_code == 200, response.text
        await wait_status(client, fork, "READY")
        fork_sid = load_state(app.state.test_root / "state.json")["sessions"][fork]
        assert fork_sid != source_sid

        source_response, fork_response = await asyncio.gather(
            client.post(
                f"/api/runs/{source}/messages", json={"text": "只写入源会话"}
            ),
            client.post(
                f"/api/runs/{fork}/messages", json={"text": "只写入 Fork"}
            ),
        )
        assert source_response.status_code == fork_response.status_code == 200
        assert (await client.get(f"/api/runs/{source}")).json()["status"] == "RUNNING"
        assert (await client.get(f"/api/runs/{fork}")).json()["status"] == "RUNNING"
        await asyncio.gather(
            wait_status(client, source, "READY"),
            wait_status(client, fork, "READY"),
        )

        assert transcript_prompts(factory, source_sid) == [
            "共享的分叉前指令",
            "只写入源会话",
        ]
        assert transcript_prompts(factory, fork_sid) == [
            "共享的分叉前指令",
            "建立 Fork 分支",
            "只写入 Fork",
        ]

        source_events, _ = await collect_sse(await open_stream(client, source))
        fork_events, _ = await collect_sse(await open_stream(client, fork))
        assert [
            event["data"]["text"]
            for event in source_events
            if event["event"] == "user.message"
        ] == ["共享的分叉前指令", "只写入源会话"]
        assert [
            event["data"]["text"]
            for event in fork_events
            if event["event"] == "user.message"
        ] == ["共享的分叉前指令", "建立 Fork 分支", "只写入 Fork"]

        fork_starts = [
            start for start in factory.starts if start.target_session_id == fork_sid
        ]
        assert (
            fork_starts[0].context_session_id,
            fork_starts[0].fork_session,
        ) == (source_sid, True)
        assert (
            fork_starts[-1].context_session_id,
            fork_starts[-1].fork_session,
        ) == (fork_sid, False)


async def test_second_turn_after_completed_turn_replays_new_events_only():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        resp = await open_stream(client, run_id)
        first, _ = await collect_sse(resp, deadline_s=1.0)
        first = drop_title_events(first)
        assert first[-1]["event"] == "turn.completed"
        await resp.aclose()

        await client.post(f"/api/runs/{run_id}/messages", json={"text": "删除刚创建的 ECS"})
        await wait_status(client, run_id, "READY")
        # 以首回合末尾为断点重连：只补发第二回合
        resp = await open_stream(client, run_id, last_event_id=int(first[-1]["id"]))
        second, _ = await collect_sse(resp, deadline_s=1.0)
        types = [e["event"] for e in drop_title_events(second)]
        assert types[0] == "turn.started", types
        assert "session.started" not in types, types


async def test_messages_conflicts_and_unknown_404():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 不存在的 run
        r = await client.post("/api/runs/run_missing/messages", json={"text": "x"})
        assert r.status_code == 404
        r = await client.get("/api/runs/run_missing/events")
        assert r.status_code == 404
        # 空文本 422
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "  "})
        assert r.status_code == 422
        r = await client.post(f"/api/runs/{run_id}/messages", json={})
        assert r.status_code == 422


async def test_redaction_masks_credentials_everywhere():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        raw = json.dumps(events, ensure_ascii=False)
        # thinking（AK/SK）、message（password）、tool 的 summary/detail
        # （tool_result 内联 password）、turn.completed 的 result（secret）
        for secret in ("HWPFEJ9AB3CDEFGHIJKL", "f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831", "Xk9$mPq2LwzR", "topsecret-token"):
            assert secret not in raw, secret
        assert "***" in raw


async def test_multiple_subscribers_same_session():
    """多客户端拉同一会话的快照收到相同事件（全局流的多客户端等价另测）。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        events1, _ = await collect_sse(await open_stream(client, run_id))
        events2, _ = await collect_sse(await open_stream(client, run_id))
        types1 = [e["event"] for e in drop_title_events(events1)]
        types2 = [e["event"] for e in drop_title_events(events2)]
        assert types1 == types2, (types1, types2)


# ---------- 全局事件流 ----------


async def test_global_stream_frames_match_per_run_seq():
    """帧形状 {run_id, seq, ts, type, payload}：seq 即 per-run 流的 SSE id
    （客户端去重锚点，保持原值）；id 行不承载断点语义（无全局 seq）。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        resp = await open_global_stream(client)
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        events, _ = await collect_sse(resp, stop=lambda e: e["event"] == "turn.completed")
        await resp.aclose()
        await wait_status(client, run_id, "READY")

        events = drop_title_events(events)
        per_run = drop_title_events((await collect_sse(await open_stream(client, run_id)))[0])
        # 开流前的 session.started 不在全局流上：连接晚于事件发生拿不到该事件
        tail = [e for e in per_run if e["event"] != "session.started"]
        assert [e["event"] for e in events] == [e["event"] for e in tail]
        for g, p in zip(events, tail):
            assert "id" not in g, g
            d = g["data"]
            assert d["run_id"] == run_id
            assert d["seq"] == int(p["id"]), (d, p)
            assert d["ts"] == p["data"]["ts"]
            assert d["type"] == p["event"]
            assert d["payload"] == {k: v for k, v in p["data"].items() if k != "ts"}
        seqs = [e["data"]["seq"] for e in events]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), seqs


async def test_global_stream_mixes_multiple_runs_in_occurrence_order():
    """两个并行会话的事件混在同一条流里：按发生序广播、各自 per-run seq
    不重排。"""
    app = make_app(delay=0.05)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await open_global_stream(client)
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        done = set()

        def both_completed(e):
            if e["event"] == "turn.completed":
                done.add(e["data"]["run_id"])
            return done == {run_a, run_b}

        events, _ = await collect_sse(resp, stop=both_completed)
        await resp.aclose()
        await wait_status(client, run_a, "READY", timeout_s=8.0)
        await wait_status(client, run_b, "READY", timeout_s=8.0)
        events = drop_title_events(events)
        assert {e["data"]["run_id"] for e in events} == {run_a, run_b}
        for rid in (run_a, run_b):
            seqs = [e["data"]["seq"] for e in events if e["data"]["run_id"] == rid]
            assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), (rid, seqs)
        # 发生序：全流 ts 单调不减
        tss = [e["data"]["ts"] for e in events]
        assert tss == sorted(tss), tss
        texts = sorted(e["data"]["payload"]["text"] for e in events if e["event"] == "user.message")
        assert texts == ["部署 nginx", "部署 redis"]


async def test_global_stream_multiple_clients_receive_equivalent_events():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp1 = await open_global_stream(client)
        resp2 = await open_global_stream(client)
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        events1, _ = await collect_sse(resp1, stop=lambda e: e["event"] == "turn.completed")
        events2, _ = await collect_sse(resp2, stop=lambda e: e["event"] == "turn.completed")
        await resp1.aclose()
        await resp2.aclose()
        await wait_status(client, run_id, "READY")
        assert drop_title_events(events1) == drop_title_events(events2)
        assert [e["event"] for e in drop_title_events(events1)][-1] == "turn.completed"


async def test_global_stream_silent_on_ready_runs():
    """READY 会话不产事件：全局流上静默，只有心跳保活。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        async with client.stream("GET", "/api/stream") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            events, pings = await collect_sse(resp, deadline_s=2.0, max_pings=3)
        assert drop_title_events(events) == []
        assert pings >= 1, pings


async def test_global_stream_does_not_replay_prior_events():
    """连接前发生的事件不重放（历史靠快照补）；连接后的新回合实时到达。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")

        # 连接晚于事件发生：历史（session.started 与第一回合）不出现在流上
        resp = await open_global_stream(client)
        first, pings = await collect_sse(resp, deadline_s=2.0, max_pings=3)
        await resp.aclose()
        assert drop_title_events(first) == [], first
        assert pings >= 1, pings

        # 连接先于第二回合：实时收到，从 turn.started 续接
        resp = await open_global_stream(client)
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
        second, _ = await collect_sse(resp, stop=lambda e: e["event"] == "turn.completed")
        await resp.aclose()
        types = [e["event"] for e in drop_title_events(second)]
        assert types[0] == "turn.started", types
        assert "session.started" not in types, types
        assert types[-1] == "turn.completed", types


async def test_snapshot_and_global_stream_union_without_duplicates():
    """组合读法（打开标签页的次序）：先连全局流，再以已有最大 seq 为断点
    拉快照——并集恰好是全量事件，重叠由 per-run seq 去重吸收、无重复
    无空洞。"""
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        # 连流前先产生第一回合：快照负责补这段历史
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        first, _ = await collect_sse(await open_stream(client, run_id))
        max_seq = max(int(e["id"]) for e in first)

        resp = await open_global_stream(client)  # 先连流（缓存期间事件经流到达）
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
        live, _ = await collect_sse(resp, stop=lambda e: e["event"] == "turn.completed")
        await resp.aclose()
        await wait_status(client, run_id, "READY")
        # 客户端去重锚点随流推进：重拉快照的断点是已见最大 seq（快照与流并取）
        live_own = [e["data"] for e in drop_title_events(live) if e["data"]["run_id"] == run_id]
        max_seen = max([max_seq] + [d["seq"] for d in live_own])
        tail, _ = await collect_sse(await open_stream(client, run_id, last_event_id=max_seen))

        seqs = sorted([int(e["id"]) for e in first] + [d["seq"] for d in live_own] + [int(e["id"]) for e in tail])
        # 并集无重复、无空洞、恰好是全量（快照补历史、流续实时，重叠为零）
        assert seqs == list(range(1, len(seqs) + 1)), seqs
        assert len(seqs) == len(set(seqs))
        # 断点重拉只补后续事件：已见事件不重发
        assert all(s > max_seen for s in (int(e["id"]) for e in tail))


async def test_summary_tracks_last_event_at():
    """摘要的 last_event_at 随事件推进：创建即 session.started 的 ts，回合
    推进后等于最近一条事件 ts（前端时长的冻结点，页面刷新后从摘要恢复）。"""
    app = make_app()
    async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        events, _ = await collect_sse(await open_stream(client, run_id))
        summary = (await client.get(f"/api/runs/{run_id}")).json()
        assert summary["last_event_at"] == events[-1]["data"]["ts"], summary

        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "READY")
        events, _ = await collect_sse(await open_stream(client, run_id))
        summary = (await client.get(f"/api/runs/{run_id}")).json()
        assert summary["last_event_at"] == events[-1]["data"]["ts"], summary


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
