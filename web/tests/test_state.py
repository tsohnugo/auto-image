"""落盘簿记与重启恢复（合流单路径）：墓碑不复活、身份映射 run_id 稳定、
未收尾回合 turn.interrupted、克隆链 resumed_from 保留、簿记损坏降级、
旧格式弃用、端到端重启（真簿记 + 假 transcript）。"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.runs import READY, RunManager  # noqa: E402
from web.state import load_state, save_state  # noqa: E402
from web.tests.support import StreamingASGITransport, make_test_app  # noqa: E402
from web.tests.test_api import (  # noqa: E402
    collect_sse,
    open_stream,
    transcript_prompts,
    wait_status,
)
from web.tests.test_history import session_info  # noqa: E402

def tmsg(mtype, content):
    """与 SessionMessage 同形的 transcript 桩条目。"""
    return SimpleNamespace(
        type=mtype,
        message={"role": "user" if mtype == "user" else "assistant", "content": content},
    )


def two_turn_transcript():
    return [
        tmsg("user", "部署 nginx"),
        tmsg("assistant", [{"type": "text", "text": "完成。"}]),
        tmsg("user", "为什么成功"),
        tmsg("assistant", [{"type": "text", "text": "因为流程正确。"}]),
    ]


def open_turn_transcript():
    """未收尾回合：末条用户输入后只有部分 agent 输出、无下一条输入收口。"""
    return [
        tmsg("user", "部署 nginx"),
        tmsg("assistant", [{"type": "text", "text": "开始执行，进行到一半。"}]),
    ]


def write_state(path, ended_sessions=(), sessions=None, clone_sources=None):
    path.write_text(json.dumps({
        "ended_sessions": list(ended_sessions),
        "sessions": sessions or {},
        "clone_sources": clone_sources or {},
    }, ensure_ascii=False), encoding="utf-8")


def restore_app(state_path, infos, transcripts, factory=None):
    """簿记 + 假 transcript 装配的重启后应用。"""
    def get_messages(sid):
        if sid not in transcripts:
            raise FileNotFoundError(sid)
        return transcripts[sid]

    return make_test_app(
        session_factory=factory or FakeSessionFactory(script=DEFAULT_SCRIPT),
        list_sessions_fn=lambda: list(infos),
        get_session_messages_fn=get_messages,
        state_path=state_path,
    )


async def test_save_load_roundtrip_tombstones_and_id_map():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        manager = RunManager()
        live = manager.create()           # 挂起、有 session：入身份映射
        live.session_id = "sess_live"
        ended = manager.create()          # 显式结束：入墓碑 + 映射
        ended.session_id = "sess_ended"
        ended.status = "ENDED"
        empty = manager.create()          # 从未接受回合：无 session_id，不入册
        save_state(manager.runs.values(), path)
        state = load_state(path)
        assert state["ended_sessions"] == {"sess_ended"}, state
        assert state["sessions"] == {
            live.run_id: "sess_live", ended.run_id: "sess_ended",
        }, state
        assert empty.run_id not in state["sessions"], state

        # 损坏/形状不对：空册降级，不阻断
        (Path(d) / "corrupt.json").write_text("not json{", encoding="utf-8")
        assert load_state(Path(d) / "corrupt.json") == {
            "ended_sessions": set(), "sessions": {}, "clone_sources": {}}
        (Path(d) / "badshape.json").write_text(
            json.dumps({"ended_sessions": "x", "sessions": [1]}), encoding="utf-8")
        assert load_state(Path(d) / "badshape.json") == {
            "ended_sessions": set(), "sessions": {}, "clone_sources": {}}
        # 部分损坏整体弃册（半份无从判真）：墓碑损坏时不挑拣保留映射
        (Path(d) / "partial.json").write_text(json.dumps(
            {"ended_sessions": "x", "sessions": {"run_1": "sess_live"},
             "clone_sources": {}}), encoding="utf-8")
        assert load_state(Path(d) / "partial.json") == {
            "ended_sessions": set(), "sessions": {}, "clone_sources": {}}


async def test_old_state_format_discarded_not_parsed():
    """旧簿记（runs 记录数组）弃用重建：不读旧字段、不沿用旧 run_id——
    重放按 transcript 会话粒度走 run_hist_ 派生。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        state_path.write_text(json.dumps({"runs": [{
            "run_id": "run_1", "status": "READY", "stage": "INSTALL",
            "first_prompt": "部署 nginx", "created_at": 1000.0,
            "session_id": "sess_z", "resumed_from": None,
        }]}, ensure_ascii=False), encoding="utf-8")
        app = restore_app(str(state_path), [session_info("sess_z", "部署 nginx", 500)], {"sess_z": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            # 旧 run_id 不沿用：派生 id 重建，且依旧可续聊（单路径语义）
            assert [r["run_id"] for r in runs] == ["run_hist_sess_z"], runs
            assert runs[0]["status"] == "READY", runs


async def test_restart_all_sessions_chattable():
    """合流核心：所有 transcript 会话（含原只读历史）重启后直接可续聊。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path)
        infos = [session_info("sess_z", "老任务", 500), session_info("sess_y", "更老任务", 300)]
        app = restore_app(str(state_path), infos, {"sess_z": two_turn_transcript(), "sess_y": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            for run in (await client.get("/api/runs")).json()["runs"]:
                assert run["status"] == "READY", run
                r = await client.post(f"/api/runs/{run['run_id']}/messages", json={"text": "继续"})
                assert r.status_code == 200, r.text
                await wait_status(client, run["run_id"], "READY")
            # 重放流无终态收尾事件：快照重放完即结束（无心跳常驻）
            resp = await open_stream(client, "run_hist_sess_z")
            events, pings = await collect_sse(resp)
            types = [e["event"] for e in events]
            assert "session.ended" not in types, types
            assert pings == 0, pings


async def test_tombstone_sessions_stay_ended_after_restart():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path, ended_sessions=["sess_dead"],
                    sessions={"run_7": "sess_dead"})
        app = restore_app(str(state_path), [session_info("sess_dead", "已结束任务", 900)], {"sess_dead": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            assert runs[0]["run_id"] == "run_7"  # 身份映射命中沿用原 id
            assert runs[0]["status"] == "ENDED", runs
            # 可回看（快照重放完即结束）、发送 409 session_not_active、可克隆
            resp = await open_stream(client, "run_7")
            events, pings = await collect_sse(resp)
            types = [e["event"] for e in events]
            assert "user.message" in types, types
            assert pings == 0, pings
            r = await client.post("/api/runs/run_7/messages", json={"text": "继续"})
            assert r.status_code == 409 and r.json() == {"detail": "session_not_active"}, r.text
            r = await client.post("/api/runs/run_7/clone")
            assert r.status_code == 200, r.text


async def test_first_turn_ended_before_result_keeps_identity_after_restart():
    """transcript 已形成、Result 未返回时结束，重启仍是原 run 的 ENDED。"""
    class PendingResultFactory:
        def __init__(self):
            self.transcripts = {}
            self.transcript_ready = asyncio.Event()
            self.release_result = asyncio.Event()
            self.starts = []

        def __call__(self, start):
            self.starts.append(start)
            factory = self

            class PendingResultSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_exc_info):
                    return False

                async def query(self, text):
                    factory.transcripts[start.target_session_id] = [tmsg("user", text)]

                async def receive_response(self):
                    factory.transcripts[start.target_session_id].append(
                        tmsg("assistant", [{"type": "text", "text": "已开始部署。"}])
                    )
                    yield {
                        "type": "assistant",
                        "session_id": start.target_session_id,
                        "message": {"content": [{"type": "text", "text": "已开始部署。"}]},
                    }
                    # yield 返回后回合执行已确认身份；停在 Result 之前。
                    factory.transcript_ready.set()
                    await factory.release_result.wait()
                    yield {
                        "type": "result",
                        "subtype": "success",
                        "result": "完成",
                        "session_id": start.target_session_id,
                    }

                async def interrupt(self):
                    pass

            return PendingResultSession()

    with tempfile.TemporaryDirectory() as d:
        state_path = str(Path(d) / "state.json")
        factory = PendingResultFactory()
        app_a = make_test_app(session_factory=factory, state_path=state_path)
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app=app_a), base_url="http://testserver"
        ) as client:
            run_id = (await client.post("/api/runs", json={})).json()["run_id"]
            response = await client.post(
                f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"}
            )
            assert response.status_code == 200, response.text
            await asyncio.wait_for(factory.transcript_ready.wait(), timeout=1)
            target = factory.starts[0].target_session_id

            before_end = load_state(state_path)
            assert before_end["sessions"] == {run_id: target}, before_end
            response = await client.post(f"/api/runs/{run_id}/end")
            assert response.status_code == 200, response.text

        ended_state = load_state(state_path)
        assert ended_state["sessions"] == {run_id: target}, ended_state
        assert ended_state["ended_sessions"] == {target}, ended_state

        app_b = restore_app(
            state_path,
            [session_info(target, "部署 nginx", 1_700_000_000_000)],
            factory.transcripts,
        )
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app=app_b), base_url="http://testserver"
        ) as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            assert [(run["run_id"], run["status"]) for run in runs] == [(run_id, "ENDED")], runs
            assert all(not run["run_id"].startswith("run_hist_") for run in runs), runs

            events, _ = await collect_sse(await open_stream(client, run_id))
            assert "user.message" in [event["event"] for event in events], events
            assert "agent.message" in [event["event"] for event in events], events
            response = await client.post(
                f"/api/runs/{run_id}/messages", json={"text": "继续"}
            )
            assert response.status_code == 409
            assert response.json() == {"detail": "session_not_active"}
            response = await client.post(f"/api/runs/{run_id}/clone")
            assert response.status_code == 200, response.text


async def test_open_turn_interrupted_and_back_to_ready():
    """重启前未收尾的回合：transcript 推导 turn_open 补 turn.interrupted，
    不伪造 turn.completed，会话回 READY。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path)
        app = restore_app(str(state_path), [session_info("sess_x", "部署 nginx", 1000)], {"sess_x": open_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            assert runs[0]["status"] == "READY", runs  # 不自动重跑
            events, _ = await collect_sse(await open_stream(client, "run_hist_sess_x"))
            types = [e["event"] for e in events]
            assert types[-1] == "turn.interrupted", types
            assert "turn.completed" not in types, types


async def test_run_id_stable_across_restart():
    """两次重启同一 transcript：身份映射命中的沿用原 run_id、克隆链
    resumed_from 保留；簿记损坏后映射丢失但 run_id 派生稳定。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        # 会话曾被克隆：run_1 为源、run_2 为克隆，映射与血缘镜像各两条
        write_state(state_path, sessions={"run_1": "sess_x", "run_2": "sess_c"},
                    clone_sources={"sess_c": "run_1"})
        infos = [session_info("sess_x", "部署 nginx", 1000), session_info("sess_c", "部署变体", 800)]
        app = restore_app(str(state_path), infos, {"sess_x": two_turn_transcript(), "sess_c": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = {r["run_id"]: r for r in (await client.get("/api/runs")).json()["runs"]}
            assert set(runs) == {"run_1", "run_2"}, runs
            assert runs["run_2"]["resumed_from"] == "run_1", runs  # 克隆链保留
        # 两次重启：簿记继续写（恢复本身不触发 persist，显式 save 一次模拟）
        # ——直接再起一个实例读同一簿记，run_id 依旧
        app2 = restore_app(str(state_path), infos, {"sess_x": two_turn_transcript(), "sess_c": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app2), base_url="http://testserver") as client:
            runs = {r["run_id"] for r in (await client.get("/api/runs")).json()["runs"]}
            assert runs == {"run_1", "run_2"}, runs

        # 簿记损坏（映射丢失）：run_id 换派生规则但两次派生稳定
        state_path.write_text("garbage{", encoding="utf-8")
        app3 = restore_app(str(state_path), infos, {"sess_x": two_turn_transcript(), "sess_c": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app3), base_url="http://testserver") as client:
            runs = {r["run_id"] for r in (await client.get("/api/runs")).json()["runs"]}
            assert runs == {"run_hist_sess_x", "run_hist_sess_c"}, runs  # 启动不炸


async def test_single_failure_skipped_and_service_starts():
    """单条会话恢复失败（读不到/损坏/空）只跳过该条并告警，服务照常启动。"""
    import logging
    logs = []

    class Capture(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    logger = logging.getLogger("web")
    handler = Capture()
    logger.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "state.json"
            write_state(state_path)
            ok_sid, broken_sid, empty_sid = "sess_ok", "sess_broken", "sess_empty"
            infos = [
                session_info(ok_sid, "部署 nginx", 1_700_000_000_000),
                session_info(empty_sid, "空会话", 1_700_000_100_000),
                session_info(broken_sid, "损坏会话", 1_700_000_200_000),
            ]
            # broken 不入 transcripts（get_messages 抛 FileNotFoundError）、
            # empty 给空链——三条都只跳过该条并告警
            app = restore_app(str(state_path), infos, {
                ok_sid: two_turn_transcript(),
                empty_sid: [],
            })
            async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
                runs = (await client.get("/api/runs")).json()["runs"]
                assert [r["first_prompt"] for r in runs] == ["部署 nginx"], runs
            assert any(broken_sid in m for m in logs), logs
            assert any(empty_sid in m for m in logs), logs
    finally:
        logger.removeHandler(handler)


async def test_id_counter_advances_past_restored_ids():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path, sessions={"run_2": "sess_x"})
        app = restore_app(str(state_path), [session_info("sess_x", "部署 nginx", 1000)], {"sess_x": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            new_id = (await client.post("/api/runs", json={})).json()["run_id"]
            assert new_id == "run_3", new_id  # 计数器前拨过已恢复的 run_2


async def test_end_to_end_restart_with_real_state_file():
    """端到端：第一个进程跑会话、克隆、结束其一 → 中止（模拟重启）→
    第二个进程以真实簿记 + transcript 恢复：身份/墓碑/克隆链全保留。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = str(Path(d) / "state.json")
        # 第一个进程：源会话跑完一回合、克隆再跑一回合、结束源会话
        app_a = make_test_app(
            session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
            state_path=state_path,
        )
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_a), base_url="http://testserver") as client:
            src = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{src}/messages", json={"text": "部署 nginx"})
            await wait_status(client, src, "READY")
            state = load_state(state_path)
            src_sid = state["sessions"][src]
            assert src_sid
            clone = (await client.post(f"/api/runs/{src}/clone")).json()["run_id"]
            await client.post(f"/api/runs/{clone}/messages", json={"text": "换个变体"})
            await wait_status(client, clone, "READY")
            clone_sid = load_state(state_path)["sessions"][clone]
            assert clone_sid and clone_sid != src_sid
            await client.post(f"/api/runs/{src}/end")

        # 第二个进程（重启后形态）：身份映射 + 墓碑叠加，run_id 与克隆链保留
        factory_b = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        infos = [session_info(src_sid, "部署 nginx", 1000), session_info(clone_sid, "部署 nginx", 900)]
        app_b = restore_app(state_path, infos, {
            src_sid: two_turn_transcript(), clone_sid: two_turn_transcript(),
        }, factory=factory_b)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_b), base_url="http://testserver") as client:
            runs = {r["run_id"]: r for r in (await client.get("/api/runs")).json()["runs"]}
            assert set(runs) == {src, clone}, runs
            assert runs[src]["status"] == "ENDED", runs  # 墓碑不复活
            assert runs[clone]["status"] == "READY", runs
            assert runs[clone]["resumed_from"] == src, runs  # 克隆链保留
            # 克隆会话续聊：回合以自身 session resume
            r = await client.post(f"/api/runs/{clone}/messages", json={"text": "继续"})
            assert r.status_code == 200, r.text
            await wait_status(client, clone, "READY")
        resumed = factory_b.starts[-1]
        assert resumed.target_session_id == clone_sid
        assert resumed.context_session_id == clone_sid
        assert resumed.fork_session is False


async def test_fork_transcripts_restore_stable_runs_and_scoped_tombstone():
    """真实簿记叠加两份独立 transcript：重启保身份、可续聊、墓碑不串线。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = str(Path(d) / "state.json")
        factory = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        app_a = make_test_app(session_factory=factory, state_path=state_path)
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app=app_a), base_url="http://testserver"
        ) as client:
            source = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(
                f"/api/runs/{source}/messages", json={"text": "共享的部署起点"}
            )
            await wait_status(client, source, "READY")
            fork = (await client.post(f"/api/runs/{source}/clone")).json()["run_id"]
            await client.post(
                f"/api/runs/{fork}/messages", json={"text": "建立 Fork transcript"}
            )
            await wait_status(client, fork, "READY")

        saved = load_state(state_path)
        source_sid = saved["sessions"][source]
        fork_sid = saved["sessions"][fork]
        assert source_sid != fork_sid
        assert transcript_prompts(factory, source_sid) == ["共享的部署起点"]
        assert transcript_prompts(factory, fork_sid) == [
            "共享的部署起点",
            "建立 Fork transcript",
        ]

        app_b = make_test_app(
            session_factory=factory,
            state_path=state_path,
            list_sessions_fn=factory.list_sessions,
            get_session_messages_fn=factory.get_session_messages,
        )
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app=app_b), base_url="http://testserver"
        ) as client:
            runs = {
                run["run_id"]: run
                for run in (await client.get("/api/runs")).json()["runs"]
            }
            assert set(runs) == {source, fork}, runs
            assert runs[source]["status"] == runs[fork]["status"] == "READY"
            assert runs[fork]["resumed_from"] == source

            source_response, fork_response = await asyncio.gather(
                client.post(
                    f"/api/runs/{source}/messages", json={"text": "重启后续写源"}
                ),
                client.post(
                    f"/api/runs/{fork}/messages", json={"text": "重启后续写 Fork"}
                ),
            )
            assert source_response.status_code == fork_response.status_code == 200
            await asyncio.gather(
                wait_status(client, source, "READY"),
                wait_status(client, fork, "READY"),
            )

            response = await client.post(f"/api/runs/{source}/end")
            assert response.status_code == 200, response.text
            assert (await client.get(f"/api/runs/{fork}")).json()["status"] == "READY"

        assert transcript_prompts(factory, source_sid) == [
            "共享的部署起点",
            "重启后续写源",
        ]
        assert transcript_prompts(factory, fork_sid) == [
            "共享的部署起点",
            "建立 Fork transcript",
            "重启后续写 Fork",
        ]
        saved = load_state(state_path)
        assert saved["sessions"] == {source: source_sid, fork: fork_sid}
        assert saved["ended_sessions"] == {source_sid}

        app_c = make_test_app(
            session_factory=factory,
            state_path=state_path,
            list_sessions_fn=factory.list_sessions,
            get_session_messages_fn=factory.get_session_messages,
        )
        async with httpx.AsyncClient(
            transport=StreamingASGITransport(app=app_c), base_url="http://testserver"
        ) as client:
            runs = {
                run["run_id"]: run
                for run in (await client.get("/api/runs")).json()["runs"]
            }
            assert set(runs) == {source, fork}, runs
            assert runs[source]["status"] == "ENDED"
            assert runs[fork]["status"] == "READY"


async def test_empty_clone_identity_lost_accepted():
    """克隆未发首条指令的空会话：无自身 session_id、transcript 无记录——
    重启后身份丢失（接受并锁定）。"""
    with tempfile.TemporaryDirectory() as d:
        state_path = str(Path(d) / "state.json")
        app_a = make_test_app(
            session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
            state_path=state_path,
        )
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_a), base_url="http://testserver") as client:
            src = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{src}/messages", json={"text": "部署 nginx"})
            await wait_status(client, src, "READY")
            empty_clone = (await client.post(f"/api/runs/{src}/clone")).json()["run_id"]
        # 空克隆不入身份映射
        state = load_state(state_path)
        assert empty_clone not in state["sessions"], state

        # 重启后：transcript 里只有源会话，空克隆不可找回
        src_sid = state["sessions"][src]
        app_b = restore_app(state_path, [session_info(src_sid, "部署 nginx", 1000)], {src_sid: two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_b), base_url="http://testserver") as client:
            runs = {r["run_id"] for r in (await client.get("/api/runs")).json()["runs"]}
            assert runs == {"run_1"}, runs  # run_2（空克隆）不在


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
