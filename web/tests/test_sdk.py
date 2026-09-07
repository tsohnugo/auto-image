#!/usr/bin/env python
"""sdk options 契约 —— 服务端固定的控制边界（不触网、不启动真 SDK）。

方案第 9 节「Agent 控制边界」要求服务端固定：系统提示词六要素、
最大 turns、最大执行时间、工具与脱敏规则（脱敏在 redact 层，另有测试）。
纯 assert，无 pytest。

运行：python web/tests/test_sdk.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.sdk import EXA_MCP_SERVER, SYSTEM_PROMPT, SessionStart, default_options  # noqa: E402


def test_system_prompt_covers_control_boundary():
    """系统提示词覆盖方案第 9 节六要素：任务边界、执行方式、凭据、
    门禁确认路径、无关命令。云操作不可撤销边界一并声明。"""
    for phrase in (
        "只处理本项目的部署任务",
        "deploy skill",
        "凭据",
        "验证未通过不得归档",
        "复述风险",
        "取得用户确认",
        "与部署无关",
        "不可撤销",
    ):
        assert phrase in SYSTEM_PROMPT, f"系统提示词缺要素：{phrase}"


def test_options_pin_control_boundary():
    """options 装配：系统提示词注入、固定上限在场。"""
    options = default_options()
    assert options.system_prompt == SYSTEM_PROMPT
    assert options.max_turns == 200


def test_session_start_distinguishes_new_resume_and_fork():
    """部署会话工厂的公开启动意图同时表达目标身份、上下文来源与 Fork。"""
    target = "11111111-2222-4333-8444-555555555555"
    source = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    fresh = SessionStart.fresh(target)
    resumed = SessionStart.resume(target)
    forked = SessionStart.fork(target, source)

    assert (fresh.target_session_id, fresh.context_session_id, fresh.fork_session) == (
        target, None, False,
    )
    assert (resumed.target_session_id, resumed.context_session_id, resumed.fork_session) == (
        target, target, False,
    )
    assert (forked.target_session_id, forked.context_session_id, forked.fork_session) == (
        target, source, True,
    )

    fresh_options = default_options(fresh)
    resumed_options = default_options(resumed)
    assert (fresh_options.session_id, fresh_options.resume, fresh_options.fork_session) == (
        target, None, False,
    )
    assert (resumed_options.session_id, resumed_options.resume, resumed_options.fork_session) == (
        None, target, False,
    )
    forked_options = default_options(forked)
    assert (forked_options.session_id, forked_options.resume, forked_options.fork_session) == (
        target, source, True,
    )


def test_options_grant_unattended_write_permission():
    """无值守会话的写权限：流水线必须落盘产物（指南/meta/结果），SDK 默认
    权限下 Write 与 Bash 写路径一律被拒（真部署实测，指南只能以文本返回）。
    信任边界由运行形态承担：只监听 127.0.0.1 + 系统提示词任务边界。"""
    options = default_options()
    assert options.permission_mode == "bypassPermissions"


def test_exa_server_env_carries_only_api_key():
    """exa MCP 的 env 只有 API key 一项（有则带、无则空）——防止将来塞入
    其他变量；key 值本身不设断言（依赖运行环境，只约束形状）。"""
    env = EXA_MCP_SERVER["env"]
    assert set(env) in (set(), {"EXA_API_KEY"})


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
