"""脚本化假会话：按剧本推 SDK 形状的消息，测试注入用，不触真 SDK。

剧本是消息列表（形状见 normalize_message）；条目为异常实例时在该点抛出，
用于演练会话异常终止。剧本含敏感样例，验证事件出口的脱敏层。

打断行为对齐真 SDK 实测形态（web/README.md 实测记录第 5 条）：回合执行中
interrupt 后流终止，尾随一条 subtype=error_during_execution、result 为空
的 Result；新回合 query 时打断状态清零。部署会话按工厂收到的目标身份回报
session_id；标题等无启动意图的假会话仍按创建次序分配本地身份。
"""
import asyncio
import itertools
from copy import deepcopy
from dataclasses import dataclass

# 华为云 AK/SK 与密码样例：任何一条漏遮都应被测试捕获
DEFAULT_SCRIPT = [
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "用户要部署 nginx。scope 里的 AK HWPFEJ9AB3CDEFGHIJKL 与 SK f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831 不能出现在输出。"},
        ]},
    },
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "收到，开始执行部署流水线。"},
        ]},
    },
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_01", "name": "Task", "input": {"subagent_type": "deploy-guide", "prompt": "生成 nginx 1.25 的部署与验证指南"}},
        ]},
    },
    {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_01", "content": "指南已生成：deploy/nginx/1.25/install.md，机器 password: Xk9$mPq2LwzR 已配置。"},
        ]},
    },
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "指南阶段完成，机器 root password: Xk9$mPq2LwzR 已就绪，等待安装指令。"},
        ]},
    },
    {
        "type": "result",
        "subtype": "success",
        "result": "回合完成：GUIDE 阶段产物已落盘。secret=topsecret-token 已由脱敏层遮蔽。",
    },
]


@dataclass
class FakeTranscriptMessage:
    """与 SDK SessionMessage 的重建读取字段同形。"""

    type: str
    message: dict
    uuid: str
    session_id: str


@dataclass
class FakeSessionInfo:
    """与 SDKSessionInfo 的重启发现字段同形。"""

    session_id: str
    summary: str | None
    last_modified: int
    file_size: int
    custom_title: str | None
    first_prompt: str | None
    git_branch: str | None
    cwd: str
    tag: str | None
    created_at: int


class FakeSession:
    """与会话抽象同形的假实现：query 记录文本，receive_response 逐条产出剧本。"""

    def __init__(self, script=None, delay=0.0, session_id="sess_fake",
                 start=None, transcript_adapter=None):
        self.script = list(script if script is not None else DEFAULT_SCRIPT)
        self.delay = delay
        self.session_id = session_id
        self.start = start
        self.transcript_adapter = transcript_adapter
        self.queries = []
        self.interrupted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def query(self, text):
        self.queries.append(text)
        # 打断状态随回合生效：新回合从清零开始
        self.interrupted = False
        if self.start is not None and self.transcript_adapter is not None:
            self.transcript_adapter.begin_query(self.start, text)

    async def interrupt(self):
        self.interrupted = True

    async def receive_response(self):
        for step in self.script:
            await asyncio.sleep(self.delay)
            if self.interrupted:
                yield {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "",
                    "session_id": self.session_id,
                }
                return
            if isinstance(step, BaseException):
                raise step
            if step.get("type") == "result":
                step = {**step, "session_id": self.session_id}
            elif self.start is not None and self.transcript_adapter is not None:
                self.transcript_adapter.record_response(self.session_id, step)
            yield step


class FakeSessionFactory:
    """部署会话遵循目标身份，并记录启动意图与 query；无意图会话自分配 id。"""

    def __init__(self, script=None, delay=0.0):
        self.script = script
        self.delay = delay
        self._ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._clock = itertools.count(1_700_000_000_000)
        self.starts = []
        self.sessions = []
        self.transcripts = {}
        self._created_at = {}
        self._last_modified = {}

    @property
    def queries(self):
        """全部已创建会话收到的 query，按会话创建与调用顺序展平。"""
        return [query for session in self.sessions for query in session.queries]

    def begin_query(self, start, text):
        """按启动意图建立目标 transcript，并写入本回合用户指令。"""
        target = start.target_session_id
        if start.fork_session:
            source = self.transcripts.get(start.context_session_id, [])
            self.transcripts[target] = [
                FakeTranscriptMessage(
                    type=message.type,
                    message=deepcopy(message.message),
                    uuid=message.uuid,
                    session_id=target,
                )
                for message in source
            ]
        else:
            self.transcripts.setdefault(target, [])
        self._append_transcript(target, "user", text)

    def record_response(self, session_id, step):
        """SDK 可见的 user/assistant 响应写入当前目标 transcript。"""
        if step.get("type") not in {"user", "assistant"}:
            return
        message = step.get("message")
        if isinstance(message, dict):
            self._append_transcript(session_id, step["type"], message.get("content"))

    def get_session_messages(self, session_id):
        """按 session_id 读取独立 transcript，返回副本避免测试侧改写。"""
        return deepcopy(self.transcripts.get(session_id, []))

    def list_sessions(self):
        """列出已有 transcript，形状与 SDK 的重启发现接口一致。"""
        infos = []
        for session_id, messages in self.transcripts.items():
            if not messages:
                continue
            first_prompt = next((
                message.message.get("content")
                for message in messages
                if message.type == "user"
                and isinstance(message.message.get("content"), str)
                and message.message.get("content")
            ), None)
            infos.append(FakeSessionInfo(
                session_id=session_id,
                summary=first_prompt,
                last_modified=self._last_modified[session_id],
                file_size=len(repr(messages).encode("utf-8")),
                custom_title=None,
                first_prompt=first_prompt,
                git_branch=None,
                cwd="/fake-auto-image",
                tag=None,
                created_at=self._created_at[session_id],
            ))
        return sorted(infos, key=lambda info: info.last_modified, reverse=True)

    def _append_transcript(self, session_id, message_type, content):
        self.transcripts.setdefault(session_id, []).append(FakeTranscriptMessage(
            type=message_type,
            message={"role": message_type, "content": deepcopy(content)},
            uuid=f"fake_{next(self._message_ids)}",
            session_id=session_id,
        ))
        timestamp = next(self._clock)
        self._created_at.setdefault(session_id, timestamp)
        self._last_modified[session_id] = timestamp

    def __call__(self, start=None):
        self.starts.append(start)
        session = FakeSession(
            script=self.script,
            delay=self.delay,
            session_id=(
                start.target_session_id if start is not None
                else f"sess_fake_{next(self._ids)}"
            ),
            start=start,
            transcript_adapter=self,
        )
        self.sessions.append(session)
        return session
