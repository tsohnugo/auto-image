#!/usr/bin/env python3
"""通用 Web 应用 fixture 的生产依赖隔离回归。"""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web import app as app_mod  # noqa: E402
from web.tests.support import StreamingASGITransport  # noqa: E402
from web.tests.test_api import make_app as api_app, wait_status  # noqa: E402
from web.tests.test_artifacts import make_app as artifacts_app  # noqa: E402
from web.tests.test_history import history_app, plain_app  # noqa: E402
from web.tests.test_state import restore_app  # noqa: E402
from web.title import title_prompt  # noqa: E402


def forbidden(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"production dependency touched: {name}")

    return fail


async def exercise_first_turn(name, app, instruction):
    async with httpx.AsyncClient(
        transport=StreamingASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/runs")).json()["runs"] == [], name
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        response = await client.post(
            f"/api/runs/{run_id}/messages", json={"text": instruction}
        )
        assert response.status_code == 200, (name, response.text)
        await wait_status(client, run_id, "READY")
        await asyncio.sleep(0)

    deployment = app.state.session_factory
    titles = app.state.title_factory
    assert deployment is not titles, name
    assert deployment.queries == [instruction], name
    assert titles.queries == [title_prompt(instruction)], name


async def test_common_fixtures_keep_first_turn_off_production_dependencies():
    """每种通用装配的首回合都只触达本地假会话与临时文件。"""
    instruction = "部署 nginx 到 server-a"
    scope_paths = []
    real_load_state = app_mod.state_mod.load_state
    real_save_state = app_mod.state_mod.save_state

    def guarded_scope_load(path):
        assert Path(path) != app_mod.DEFAULT_SCOPE_CONFIG
        scope_paths.append(Path(path))

    def guarded_state_load(path):
        assert Path(path) != app_mod.DEFAULT_STATE_PATH
        return real_load_state(path)

    def guarded_state_save(runs, path, clone_sources=None):
        assert Path(path) != app_mod.DEFAULT_STATE_PATH
        return real_save_state(runs, path, clone_sources)

    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(app_mod, "SDKSessionFactory", forbidden("deployment factory")),
        patch.object(app_mod.sdk_mod, "TitleSessionFactory", forbidden("title factory")),
        patch.object(app_mod.sdk_mod, "ClaudeSDKClient", forbidden("Claude CLI")),
        patch.object(app_mod.sdk_mod, "list_project_sessions", forbidden("session discovery")),
        patch.object(app_mod.sdk_mod, "project_session_messages", forbidden("transcript read")),
        patch.object(app_mod.sdk_mod, "transcript_times", forbidden("transcript time scan")),
        patch.object(app_mod, "residual_cli_processes", forbidden("residual CLI scan")),
        patch.object(app_mod.redact_mod, "load_scope_secrets", guarded_scope_load),
        patch.object(app_mod.state_mod, "load_state", guarded_state_load),
        patch.object(app_mod.state_mod, "save_state", guarded_state_save),
    ):
        root = Path(tmp)
        artifact_root = root / "artifacts"
        artifact_root.mkdir()
        apps = [
            ("api", api_app(script=[])),
            ("artifacts", artifacts_app(artifact_root)),
            ("plain history", plain_app()),
            ("history replay", history_app([], lambda _session_id: [])),
            ("state restore", restore_app(root / "restored-state.json", [], {})),
        ]
        for name, app in apps:
            await exercise_first_turn(name, app, instruction)

    assert scope_paths == [app.state.test_root / "scope.yaml" for _name, app in apps]
    for _name, app in apps:
        assert (app.state.test_root / "scope.yaml").read_text(encoding="utf-8") == "{}\n"


async def main():
    await test_common_fixtures_keep_first_turn_off_production_dependencies()
    print("ok test_common_fixtures_keep_first_turn_off_production_dependencies")
    print("1 passed")


if __name__ == "__main__":
    asyncio.run(main())
