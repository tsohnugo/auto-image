"""ClaudeSDKClient 生产实现：包装成与会话抽象同形的工厂。

真 SDK 把 CLI JSON 行解析成 dataclass 消息（AssistantMessage 等），
服务端的映射层消费的是 CLI JSON 形状的 dict——to_dict 在此适配，
使假剧本（fake.py 的 dict）与真会话走同一条 normalize 路径。

transcript 时刻读取器（transcript_times / _session_transcript_path）也
在此：SDK 的 SessionMessage 形状不带 timestamp，事件时刻透传（重启重放/
克隆转录找源时刻）只能自读 transcript JSONL——不 import SDK 私有模块
（_internal 随版本漂移），文件定位与目录名派生在 web 层薄薄复刻。
"""
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_messages,
    list_sessions,
    rename_session,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 方案第 9 节「固定上限」的取值：一个完整部署回合的 turns 预算；
# 不设 wall-clock 超时（理由与实测记录见 session.py 模块注释）
MAX_TURNS = 200
THINKING_BUDGET_TOKENS = 10000

# 方案第 9 节「系统提示词至少要求」的六要素原文基线
SYSTEM_PROMPT = """你是 auto-image 部署流水线的 Web 会话执行者，与部署使用者在浏览器会话里交互。

- 只处理本项目的部署任务，不执行与部署无关的命令。
- 部署一律按当前 deploy skill（.claude/skills/deploy/SKILL.md）编排执行，四个阶段依次推进、不得合并或内联替做。
- 子 agent 同一时刻至多一个在跑；派发后必须阻塞等待其完成（TaskOutput 等待）并校验产物落盘，不得派发后结束回合等通知；四阶段全部完成、汇总呈现后才收尾回合。
- 不向输出暴露凭据：API Key、密码、SSH 私钥等不在消息、思维链与工具摘要中出现。
- 未经用户明确确认，验证未通过不得归档；用户显式要求跳过门禁时，先复述风险、取得用户确认后再执行。
- 已提交的云操作（创建 ECS、制镜像等）不可撤销；用户要求停止或调整时，如实告知这一边界。"""

# guide 阶段联网链路：SDK 会话内内置 WebFetch 被域名安全校验拦截、
# WebSearch 被权限层拒（实测记录见 web/README.md），显式接入既有
# exa MCP（与本机 ~/.claude.json 全局配置同源），不依赖运行者个人配置
def _exa_env():
    """exa 的 API key：进程环境优先（Claude Code settings 注入），回退读
    ~/.claude/settings.json 的 env——用户手动 shell 起服务时无此变量，
    无 key 则工具注册成功但调用 401。key 只进 SDK options，不经事件流。"""
    key = os.environ.get("EXA_API_KEY")
    if not key:
        try:
            cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
            key = cfg.get("env", {}).get("EXA_API_KEY")
        except (OSError, ValueError):
            key = None
    return {"EXA_API_KEY": key} if key else {}


EXA_MCP_SERVER = {"type": "stdio", "command": "npx", "args": ["-y", "exa-mcp-server"], "env": _exa_env()}


@dataclass(frozen=True)
class SessionStart:
    """一次部署 SDK 连接的启动意图。

    target_session_id 始终是当前 Web 会话拥有的身份；context_session_id 只说明
    启动时从哪条 transcript 取上下文。两者相同是普通续接，不同且
    fork_session=True 是分叉，新建则没有上下文来源。
    """

    target_session_id: str
    context_session_id: str | None
    fork_session: bool

    @classmethod
    def fresh(cls, target_session_id):
        return cls(target_session_id, None, False)

    @classmethod
    def resume(cls, session_id):
        return cls(session_id, session_id, False)

    @classmethod
    def fork(cls, target_session_id, source_session_id):
        return cls(target_session_id, source_session_id, True)


def default_options(start=None):
    """SDK options 全配：cwd=项目根，setting_sources 不设（SDK 默认
    user/project/local，project source 从 cwd 发现 .claude/ 与 CLAUDE.md）。

    tools 必须显式给 claude_code 预设（--tools default）：SDK 不传 --tools
    时 CLI 的基础工具集不含子 agent 工具，四阶段流水线无从推进。
    permission_mode 必须给 bypassPermissions：无值守会话无人批准，SDK 默认
    权限下 Write 与 Bash 写路径一律被拒（真部署实测），产物无法落盘；信任
    边界由运行形态承担（只监听 127.0.0.1 + 系统提示词任务边界）。
    start 明确给出本会话的目标身份、上下文来源与是否 Fork。普通续接不能
    同时传 session_id，因此只设置 resume；Fork 同时传目标 session_id、
    源 resume 与 fork_session，让 SDK 建立独立 transcript。"""
    target_session_id = None
    context_session_id = None
    fork_session = False
    if start is not None:
        target_session_id = start.target_session_id
        context_session_id = start.context_session_id
        fork_session = start.fork_session
    return ClaudeAgentOptions(
        cwd=str(PROJECT_ROOT),
        resume=context_session_id,
        session_id=target_session_id if context_session_id is None or fork_session else None,
        fork_session=fork_session,
        system_prompt=SYSTEM_PROMPT,
        permission_mode="bypassPermissions",
        tools={"type": "preset", "preset": "claude_code"},
        mcp_servers={"exa-search": EXA_MCP_SERVER},
        # 非交互会话无人批准：Bash 等内置工具随 claude_code 预设放行，
        # MCP 工具默认要审批，exa 通配单独放行（guide 联网唯一路径）
        allowed_tools=["mcp__exa-search__*"],
        forward_subagent_text=True,
        include_partial_messages=True,
        thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS},
        max_turns=MAX_TURNS,
    )


# 标题生成的一次性会话 cwd：服务私有目录，transcript 落它名下的项目目录
# （~/.claude/projects/-tmp-auto-image-titles），不进项目根的发现层——
# 重启 rebuild 的 list_sessions(directory=项目根) 不会把标题会话当历史任务
# 捡进列表（Codex 的 ephemeral 线程同款隔离语义，SDK 无 ephemeral 开关，
# 以 cwd 分流实现）。CLI 要求 cwd 存在，导入时创建（幂等）。
TITLE_SESSION_CWD = "/tmp/auto-image-titles"
Path(TITLE_SESSION_CWD).mkdir(parents=True, exist_ok=True)


def title_options():
    """标题会话 options：与部署会话无关的极简配置——默认模型、无系统提示词
    覆盖、无工具、上限收紧。setting_sources 保持默认（user 在场）：本环境
    认证（ANTHROPIC_AUTH_TOKEN / BASE_URL）经 ~/.claude/settings.json 的
    env 注入，清空即 not logged in；项目级（.claude/、CLAUDE.md）随独立
    cwd 天然不载入，无需在此排除。"""
    return ClaudeAgentOptions(
        cwd=TITLE_SESSION_CWD,
        system_prompt="You generate concise session titles. Output only the title text.",
        tools=[],
        max_turns=1,
    )


def to_dict(message):
    """SDK dataclass 消息 → CLI JSON 形状 dict；不认识的消息至多保留
    session_id（无 type 字段，映射层仍自然忽略 partial/system/限流等）。"""
    if isinstance(message, AssistantMessage):
        return _with_session_id(message, {
            "type": "assistant",
            "message": {"content": _blocks(message.content)},
            "parent_tool_use_id": message.parent_tool_use_id,
        })
    if isinstance(message, UserMessage):
        content = message.content if isinstance(message.content, list) else []
        return _with_session_id(message, {
            "type": "user",
            "message": {"content": _blocks(content)},
            "parent_tool_use_id": message.parent_tool_use_id,
        })
    if isinstance(message, ResultMessage):
        return _with_session_id(message, {
            "type": "result",
            "subtype": message.subtype,
            "result": message.result,
        })
    return _with_session_id(message, {})


def _with_session_id(message, payload):
    """公开 SDK 消息携带的身份统一提升到适配结果顶层。

    Python SDK 的 SystemMessage 把初始化身份放在 data 内，其余已知消息若有
    session_id 则是直接属性。未知/增量消息仍不产生事件，但身份不会被丢弃。
    """
    session_id = getattr(message, "session_id", None)
    if not session_id:
        data = getattr(message, "data", None)
        session_id = data.get("session_id") if isinstance(data, dict) else None
    if session_id:
        return {**payload, "session_id": session_id}
    return payload


def _block(block):
    """内容块 → CLI JSON 形状；四类已知块之外返回 None（调用处丢弃）。"""
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    return None


def _blocks(content):
    return [b for b in (_block(x) for x in content or []) if b]


class SDKSession:
    """与会话抽象同形：async with 连接/断开，query/interrupt 透传，
    receive_response 把每回合消息适配成 CLI JSON 形状再产出。"""

    def __init__(self, start=None, options=None):
        self._client = ClaudeSDKClient(options=options or default_options(start))

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._client.__aexit__(*exc_info)

    async def query(self, text):
        await self._client.query(text)

    async def interrupt(self):
        await self._client.interrupt()

    async def receive_response(self):
        async for message in self._client.receive_response():
            yield to_dict(message)


class SDKSessionFactory:
    def __call__(self, start=None):
        return SDKSession(start)


class TitleSessionFactory:
    """标题生成会话工厂：独立 options（title_options），不经部署会话配置。"""

    def __call__(self, _start=None):
        return SDKSession(options=title_options())


def list_project_sessions(project_root=None):
    """项目根目录下的 SDK 会话清单（重启重建的发现源；cwd 过滤由 SDK 完成）。"""
    return list_sessions(directory=str(project_root or PROJECT_ROOT))


def project_session_messages(session_id, project_root=None):
    """单条 SDK 会话的可见消息链（重启重建的事件映射源，只读 transcript）。"""
    return get_session_messages(session_id, directory=str(project_root or PROJECT_ROOT))


# ---------------------------------------------------------------------------
# transcript 时刻读取器 —— 事件时刻透传的对齐表源
# ---------------------------------------------------------------------------

logger = logging.getLogger("web")

# 目录名派生与 SDK 同规则：cwd 非字母数字全替换为 '-'（CLI 目录命名约定）
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


def _sanitize_project_dir_name(path):
    return _SANITIZE_RE.sub("-", path)


def _claude_config_home():
    """Claude 配置根（CLI 同约定：CLAUDE_CONFIG_DIR 优先）。"""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def _project_transcript_dirs(project_root):
    """部署会话 transcript 的候选目录集：项目根自身的派生目录 + git
    worktree 兄弟目录（发现范围与 SDK list_sessions 的重启重放一致）。"""
    root = str(Path(project_root).resolve())
    dirs = [_claude_config_home() / "projects" / _sanitize_project_dir_name(root)]
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return dirs  # git 缺失/慢：只查自身目录，找得到照常用
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            if path and path != root:
                dirs.append(_claude_config_home() / "projects" / _sanitize_project_dir_name(path))
    return dirs


def _session_transcript_path(session_id, project_root):
    """session_id → transcript JSONL 路径；找不到返回 None。"""
    for d in _project_transcript_dirs(project_root):
        candidate = d / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def transcript_times(session_id, project_root=None):
    """会话 transcript 的 uuid → 时刻对齐表（epoch 秒）。

    重放/转录路径的事件时刻以此对齐源 transcript 行；SDK 的
    SessionMessage 不带 timestamp，uuid 是两边共有的关联键。每行只抽
    uuid 与 timestamp 两个字段，不建消息链、不 import SDK 私有模块。
    文件缺失/坏行/缺字段降级——整表读不出返回空 map 不抛（该会话
    时刻回退当下，恢复不阻断，与 recover_sessions 逐会话容错一致）。
    """
    path = _session_transcript_path(session_id, str(project_root or PROJECT_ROOT))
    if path is None:
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning("transcript 时刻读取失败（ts 回退当下）：%s", path, exc_info=True)
        return {}
    times = {}
    for line in content.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        uuid = entry.get("uuid")
        raw_ts = entry.get("timestamp")
        if not isinstance(uuid, str) or not isinstance(raw_ts, str):
            continue
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        times[uuid] = ts.timestamp()
    return times
