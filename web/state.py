"""落盘的 run 簿记（薄）：墓碑 + 身份映射 + 克隆链镜像，仅此三样。

对话内容的单一事实源是 CLI 侧 transcript（~/.claude/projects）——
stage/title/first_prompt/created_at 全部可从 transcript 重放推导（title 本就
写回 transcript 的 custom-title 行），不入册。簿记只存 transcript 里没有的：

- 墓碑（ended_sessions）：用户显式结束（ENDED）的 session_id 集合——
  transcript 是 CLI 的地盘写不进去，不落册重启后会话就复活成可续聊；
- 身份映射（sessions）：run_id ↔ session_id——重启后 run_id 稳定，开着的
  标签页不死。映射只登记有自身 session_id 的会话（首回合被接受后）；克隆
  未发首条指令的空会话身份天然丢失，按接受处理（无内容可恢复）；
- 克隆链镜像（clone_sources）：session_id → 来源 run_id——transcript 里
  没有克隆血缘，重放会话凭镜像找回克隆链父指针 resumed_from（前端
  「⑂ 克隆自」标记）。

写入为全量原子替换（tmp + rename），每次状态变更即写。文件缺失/损坏/
形状不对一律返回空册，服务照常启动（降级为无墓碑无映射的重放，不阻断）。
旧格式（runs 记录数组，含 stage/title 等字段）弃用不读——旧字段全部可从
transcript 推导，旧记录的 run_id 让位给 run_hist_ 派生规则重新分配。
"""
import json
import logging
import os
import tempfile
from pathlib import Path

from .runs import ENDED

logger = logging.getLogger("web")


def save_state(runs, path, clone_sources=None):
    """全部 run 的墓碑、身份映射与克隆链镜像全量落盘（原子替换）。失败只
    告警不抛——落盘是恢复增强，不能反过来打断会话执行。"""
    ended = sorted(r.session_id for r in runs if r.status == ENDED and r.session_id)
    sessions = {r.run_id: r.session_id for r in runs if r.session_id}
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({
                    "ended_sessions": ended,
                    "sessions": sessions,
                    "clone_sources": clone_sources or {},
                }, f, ensure_ascii=False)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        logger.warning("状态落盘失败（重启恢复能力降级，不影响会话执行）", exc_info=True)


def load_state(path):
    """读回 {ended_sessions: set, sessions: {run_id: session_id}, clone_sources:
    {session_id: 来源 run_id}}；文件缺失/损坏/形状不对一律整体空册（降级，
    不阻断启动；部分损坏不挑拣——簿记是一份一体的小文件，半份无从判真）。"""
    empty = {"ended_sessions": set(), "sessions": {}, "clone_sources": {}}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    ended = data.get("ended_sessions") if isinstance(data, dict) else None
    sessions = data.get("sessions") if isinstance(data, dict) else None
    clones = data.get("clone_sources") if isinstance(data, dict) else None
    well_formed = (
        isinstance(ended, list) and all(isinstance(s, str) and s for s in ended)
        and isinstance(sessions, dict) and all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in sessions.items())
        and isinstance(clones, dict) and all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in clones.items())
    )
    if not well_formed:
        return empty
    return {"ended_sessions": set(ended), "sessions": sessions, "clone_sources": clones}
