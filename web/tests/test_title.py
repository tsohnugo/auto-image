#!/usr/bin/env python3
"""标题生成纯函数与主缝断言 —— prompt 形状、输出清洗、事件流接线。

LLM 会话经 FakeSessionFactory 注入（工厂对标题生成与部署会话同形）；
transcript 写回在生产为 sdk.rename_session，测试以 monkeypatch 造桩。
纯 assert，无 pytest。

运行：python web/tests/test_title.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web import sdk as sdk_mod  # noqa: E402
from web import title as title_mod  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSession, FakeSessionFactory  # noqa: E402
from web.runs import RunManager  # noqa: E402
from web.tests.support import StreamingASGITransport, make_test_app  # noqa: E402
from web.tests.test_api import collect_sse, open_stream, wait_status  # noqa: E402


def test_prompt_shape():
    """指令拼装：指令 + 截断的用户消息，超预算不切 UTF-8 之外的部分。"""
    prompt = title_mod.title_prompt("部署 nginx 1.25")
    assert title_mod.TITLE_INSTRUCTIONS in prompt
    assert prompt.endswith("User prompt:\n部署 nginx 1.25")
    long = "x" * 2000
    assert len(title_mod.title_prompt(long)) <= len(title_mod.TITLE_INSTRUCTIONS) + 20 + 960


def test_clean_title_variants():
    """清洗：引号/空白/尾标点剥除、超长截断、空与非字符串为 None。"""
    assert title_mod.clean_title("  部署 nginx。  ") == "部署 nginx"
    assert title_mod.clean_title('"Deploy nginx"') == "Deploy nginx"
    assert title_mod.clean_title("“制作 RPM 包”") == "制作 RPM 包"
    assert title_mod.clean_title("a  b\tc?") == "a b c"
    assert title_mod.clean_title("x" * 100) == "x" * title_mod.TITLE_MAX_CHARS
    assert title_mod.clean_title(None) is None
    assert title_mod.clean_title("") is None
    assert title_mod.clean_title("   ") is None


def test_clean_title_redacts_credentials():
    """生成结果同样过脱敏层：内联凭据不进标题。"""
    cleaned = title_mod.clean_title("部署 nginx，password: Xk9$mPq2LwzR")
    assert "Xk9$mPq2LwzR" not in cleaned
    assert "***" in cleaned


async def test_generate_title_uses_one_shot_session():
    """标题会话独立于部署会话：一次性 query、取 Result 文本、无续接参数。"""
    script = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "部署 nginx"}]}},
        {"type": "result", "subtype": "success", "result": "部署 nginx"},
    ]

    class OneShot(FakeSession):
        def __init__(self):
            super().__init__(script=script)

    class Factory:
        def __init__(self):
            self.calls = []

        def __call__(self, session_id=None):
            self.calls.append(session_id)
            return OneShot()

    factory = Factory()
    out = await title_mod.generate_title("部署 nginx 到 server-a", factory)
    assert out == "部署 nginx"
    assert factory.calls == [None]  # 无续接、全新会话


async def test_generate_title_failure_returns_none():
    """会话异常与无 Result 均静默返回 None（维持临时标题）。"""

    class Boom(FakeSession):
        async def receive_response(self):
            raise RuntimeError("sdk crashed")
            yield  # pragma: no cover

    assert await title_mod.generate_title("x", lambda sid=None: Boom()) is None
    empty = FakeSession(script=[])
    assert await title_mod.generate_title("x", lambda sid=None: empty) is None


async def test_first_message_assigns_title_and_emits_event():
    """首条消息 → 事件流出现 session.title_changed，摘要与 transcript 写回到位。
    标题会话经独立 title_factory（生产为隔离 cwd 配置，不落项目根 transcript）。"""
    title_script = [{"type": "result", "subtype": "success", "result": "「部署 nginx」"}]
    title_calls = []

    class TitleFactory:
        def __call__(self, session_id=None):
            title_calls.append(session_id)
            return FakeSession(script=title_script, session_id="sess_title")

    app = make_test_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
        title_factory=TitleFactory(),
    )
    renames = []
    rename_confirmations = []
    orig_rename = sdk_mod.rename_session

    def fake_rename(session_id, t, directory=None):
        renames.append((session_id, t))
        run = next(iter(app.state.run_manager.runs.values()))
        rename_confirmations.append(run.session_confirmed)

    sdk_mod.rename_session = fake_rename
    try:
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            run_id = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
            await wait_status(client, run_id, "READY")
            # 快照即完（不再有整秒读窗）：标题事件通常回合中已落流，晚到小等一轮
            events, _ = await collect_sse(await open_stream(client, run_id))
            if not [e for e in events if e["event"] == "session.title_changed"]:
                await asyncio.sleep(0.2)
                events, _ = await collect_sse(await open_stream(client, run_id))
            types = [e["event"] for e in events]
            # 标题事件在场（清洗剥掉了引号）
            title_events = [e for e in events if e["event"] == "session.title_changed"]
            assert len(title_events) == 1, types
            assert title_events[0]["data"]["title"] == "部署 nginx"
            # 摘要带 title 字段
            summary = (await client.get(f"/api/runs/{run_id}")).json()
            assert summary["title"] == "部署 nginx"
        # transcript 写回：写回等 SDK 确认预分配身份（50ms 轮询）后才发生，
        # 与快照读取是两条独立异步路径——小等收尾
        for _ in range(50):
            if renames:
                break
            await asyncio.sleep(0.02)
        target = app.state.session_factory.starts[0].target_session_id
        assert renames == [(target, "部署 nginx")], renames
        assert rename_confirmations == [True], rename_confirmations
        assert title_calls == [None]  # 标题会话全新起、无续接
    finally:
        sdk_mod.rename_session = orig_rename


async def test_title_session_isolated_from_discovery():
    """标题会话 options 与部署会话隔离：独立 cwd（transcript 落服务私有项目
    目录，不进项目根的 list_sessions 发现层）、无工具、上限收紧——
    重启后任务列表不会冒出标题会话条目。setting_sources 保持默认：认证经
    user settings env 注入，清空会导致 CLI not logged in（实测踩坑）。"""
    options = sdk_mod.title_options()
    assert options.cwd == sdk_mod.TITLE_SESSION_CWD != str(sdk_mod.PROJECT_ROOT)
    assert options.tools == []
    assert options.max_turns == 1
    assert options.setting_sources is None  # 认证依赖 user settings，不可清空
    # cwd 对应的项目目录与项目根不同名（transcript 分流验证）
    import re

    sanitize = lambda p: re.sub(r"[^A-Za-z0-9]", "-", p)  # noqa: E731
    assert sanitize(sdk_mod.TITLE_SESSION_CWD) != sanitize(str(sdk_mod.PROJECT_ROOT))


async def test_second_message_does_not_retitle():
    """标题只生成一次：第二回合不再触发（无第二个标题会话；回合连接按回合开合，
    部署工厂每条指令各调一次）。"""
    deploy_calls = []
    title_calls = []

    class CountingFactory(FakeSessionFactory):
        def __call__(self, session_id=None):
            deploy_calls.append(session_id)
            return super().__call__(session_id)

    class TitleFactory:
        def __call__(self, session_id=None):
            title_calls.append(session_id)
            return FakeSession(script=[{"type": "result", "subtype": "success", "result": "部署 nginx"}])

    app = make_test_app(
        session_factory=CountingFactory(script=DEFAULT_SCRIPT, delay=0.02),
        title_factory=TitleFactory(),
    )
    with patch.object(sdk_mod, "rename_session", lambda *_args, **_kwargs: None):
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            run_id = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
            await wait_status(client, run_id, "READY")
            await asyncio.sleep(0.2)  # 等标题会话（无 delay）跑完
            await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
            await wait_status(client, run_id, "READY")
            await asyncio.sleep(0.1)
    # 部署会话 2 次（按回合开合：每条指令各起新连接）+ 标题会话 1 次；
    # 第二回合不再生成
    assert len(deploy_calls) == 2 and len(title_calls) == 1, (deploy_calls, title_calls)


async def test_continued_session_does_not_retitle():
    """续聊不生成标题：重启重放恢复的老会话（first_prompt 非空、title 未生成过）
    继续对话，不得把续聊指令总结成标题（「继续执行任务」类）——只有新对话
    的首条指令触发生成。"""
    state_dir = tempfile.mkdtemp()
    state_path = Path(state_dir) / "state.json"
    state_path.write_text(json.dumps(
        {"ended_sessions": [], "sessions": {"run_1": "sess_x"}, "clone_sources": {}},
        ensure_ascii=False,
    ), encoding="utf-8")
    title_calls = []

    class TitleFactory:
        def __call__(self, session_id=None):
            title_calls.append(1)
            return FakeSession(script=[{"type": "result", "subtype": "success", "result": "继续执行任务"}])

    from types import SimpleNamespace

    from web.tests.test_state import two_turn_transcript

    infos = [SimpleNamespace(session_id="sess_x", summary="部署 nginx", last_modified=9_950_000,
                             file_size=1, custom_title=None, first_prompt="部署 nginx",
                             git_branch=None, cwd=None, tag=None, created_at=1_000_000)]
    app = make_test_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
        title_factory=TitleFactory(),
        list_sessions_fn=lambda: infos,
        get_session_messages_fn=lambda sid: two_turn_transcript(),
        state_path=str(state_path),
    )
    async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/api/runs")).json()["runs"][0]["title"] is None
        await client.post("/api/runs/run_1/messages", json={"text": "继续之前的部署"})
        await wait_status(client, "run_1", "READY")
        await asyncio.sleep(0.2)
        summary = (await client.get("/api/runs/run_1")).json()
    assert title_calls == [], title_calls  # 续聊指令不起标题会话
    assert summary["title"] is None, summary  # 维持无名（回退截断）


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        out = fn()
        if asyncio.iscoroutine(out):
            await out
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
