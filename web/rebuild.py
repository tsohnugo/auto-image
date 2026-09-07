"""服务重启后的恢复：全量 transcript 重放 + 落盘簿记叠加（单一流程）。

内存 run 记录随重启丢失，恢复只有一个事实源——CLI 侧 transcript
（~/.claude/projects）按 session 粒度重放，state 簿记（state.py）的薄数据
在其上叠加：

- 身份映射（run_id ↔ session_id）命中的会话沿用原 run_id（开着的标签页
  不死、克隆链来源标记不丢）；未命中的按 run_hist_ 派生规则分配；
- 墓碑（用户 ENDED 的 session_id）会话标 ENDED：可回看、可克隆、不可续聊
  ——重启不产生也不洗掉显式结束。

重放会话一律恢复 READY（可续聊，回合连接按回合开合、由下一条指令起）；
重启前未收尾的回合以 turn.interrupted 如实呈现（transcript 推导 turn_open
即截断），不自动重跑——已提交的云操作不可重复执行，续聊由用户指令驱动。

transcript 里没有 Result 消息：回合边界由「下一条真实用户输入」推导，回合
汇总取该回合最后一条 agent 文本（CLI 的 result 同源于此）。重放流不补
终态收尾事件——session.ended 只在用户显式结束时发出，重放完毕快照
正常断开、续聊由用户指令驱动（终态语义只由事件与摘要承载）。
"""
import logging
import time

from .normalize import normalize_message
from .redact import redact_text
from .runs import ENDED, READY, Run

logger = logging.getLogger("web")


def recover_sessions(manager, store, list_sessions, get_session_messages, state,
                     transcript_times=None):
    """启动时重放全部可找回的 transcript 会话，返回恢复的 run 列表（最新
    修改的在前）。

    state 为落盘簿记整册（{ended_sessions, sessions, clone_sources}，见
    state.load_state）。transcript_times 为会话时刻对齐表读取器（uuid →
    epoch 秒；重放事件的时刻透传源，缺省不透传、ts 回退当下）。历史读取
    失败只跳过对应会话（空 transcript、损坏文件），不阻断服务启动——恢复
    是找回尽量多的历史，不是启动的前置条件。
    """
    try:
        infos = list_sessions()
    except Exception:  # noqa: BLE001 —— 发现层失败不牵连服务本身
        logger.warning("list_sessions 失败，本次启动无恢复", exc_info=True)
        return []
    id_map = state["sessions"]
    reversed_map = {sid: rid for rid, sid in id_map.items()}
    resumed_from = state["clone_sources"]
    ended_sessions = state["ended_sessions"]
    restored = []
    for info in infos:
        try:
            messages = get_session_messages(info.session_id)
        except Exception:  # noqa: BLE001 —— 单条会话损坏只跳过该条
            logger.warning("读取会话 %s 的 transcript 失败，跳过该会话", info.session_id, exc_info=True)
            continue
        if not messages:
            logger.warning("会话 %s 无可见消息，跳过该会话", info.session_id)
            continue
        try:
            times = transcript_times(info.session_id) if transcript_times else {}
        except Exception:  # noqa: BLE001 —— 时刻源坏只回退当下，不丢会话
            logger.warning("读取会话 %s 的时刻对齐表失败，ts 回退当下", info.session_id, exc_info=True)
            times = {}
        try:
            restored.append(_recover_run(manager, store, info, messages, reversed_map, ended_sessions, resumed_from, times))
        except Exception:  # noqa: BLE001 —— 重放中途的任何异常只丢该条
            logger.warning("重放会话 %s 失败，跳过该会话", info.session_id, exc_info=True)
            continue
    return restored


def user_prompt_text(message):
    """真实用户指令文本：content 为字符串或纯 text 块；tool_result 行返回 None。

    transcript 的 user 行两类混杂（指令原文与工具结果回填），只有前者构成
    回合边界；空文本视为无指令（与干预端点的非空校验一致）。
    """
    if message.get("type") != "user":
        return None
    content = message.get("message", {}).get("content")
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    blocks = [b for b in content if isinstance(b, dict)]
    if not blocks or any(b.get("type") != "text" for b in blocks):
        return None
    text = "\n".join(b.get("text", "") for b in blocks if isinstance(b.get("text"), str))
    return text or None


def _recover_run(manager, store, info, messages, reversed_map, ended_sessions, resumed_from, times):
    """单条 transcript 会话 → 内存 run + 事件流重放。

    身份映射命中的沿用原 run_id；墓碑命中标 ENDED；克隆链镜像命中找回
    resumed_from。turn_open 重放补 turn.interrupted（重启截断的未收尾回合，
    删除伪造 turn.completed 的行为）。事件时刻按 times（uuid 对齐表）
    透传源 transcript 行——消息 uuid 不在表内回退 append 当下；创建与末
    活动时刻以 transcript 元信息近似（重放事件不是真实活动，见收尾处）。
    """
    run_id = reversed_map.get(info.session_id)
    if run_id is None or run_id in manager.runs:
        run_id = _derived_run_id(manager, info.session_id)
    run = Run(run_id)
    run.status = ENDED if info.session_id in ended_sessions else READY
    run.session_id = info.session_id
    run.session_confirmed = True
    run.resume_session_id = info.session_id  # 回合以自身 session 续接
    run.resumed_from = resumed_from.get(info.session_id)
    run.first_prompt = getattr(info, "first_prompt", None)
    # 标题优先读 transcript 的 custom-title 行（title.py 生成后写回），
    # 没有则维持 first_prompt 截断
    run.title = getattr(info, "custom_title", None)
    started_ms = getattr(info, "created_at", None) or getattr(info, "last_modified", 0)
    run.created_at = started_ms / 1000 if started_ms else time.time()
    last_event_at = (getattr(info, "last_modified", 0) or 0) / 1000 or None
    run.last_event_at = last_event_at
    if run.status == ENDED:
        run.ended_at = last_event_at
    manager.register(run)
    manager.adopt_ids([run.run_id])
    store.create(run.run_id)
    store.append(run.run_id, "session.started", {}, ts=_first_time(messages, times))
    replay_messages(run, store, messages, times)
    run.last_event_at = last_event_at  # 重放事件不是真实活动，恢复簿记值
    return run


def _source_ts(message, times):
    """消息的源时刻：uuid 对齐表命中取表值，缺项（表空 / 行缺 timestamp）
    None → append 当下。"""
    return times.get(getattr(message, "uuid", None)) if times else None


def _first_time(messages, times):
    """会话首个时刻（session.started 的透传源）：首条消息的源时刻，全缺
    时 None → append 当下。"""
    for message in messages:
        ts = _source_ts(message, times)
        if ts is not None:
            return ts
    return None


def replay_messages(run, store, messages, times=None):
    """transcript 可见消息链 → 内部事件流（first_prompt / stage 随重放恢复），
    不含生命周期起止事件——新起点由调用方先补；收尾：完整回合补
    turn.completed，未收尾回合（transcript 推导 turn_open）补
    turn.interrupted（重启截断，不伪造完成）。

    事件时刻透传源消息行（times 按 uuid 对齐，缺项回退 append 当下）；
    回合收尾事件取边界消息的时刻——turn.completed 是下一条用户输入前的
    最后一条已落消息，turn.interrupted 是回合内最后一条已落消息（执行
    确认推进到的最后位置，之后的时间没在执行）。"""
    tool_names = {}
    last_text = ""     # 当前回合最后一条 agent 文本（回合汇总来源）
    last_ts = None     # 当前回合最后一条已落消息的源时刻（收尾事件透传源）
    turn_open = False  # 是否有未收尾的回合（首条用户输入之后、无下一条输入收口）
    for message in messages:
        raw = {"type": message.type, "message": message.message}
        ts = _source_ts(message, times)
        prompt = user_prompt_text(raw)
        if prompt is not None:
            if turn_open:
                store.append(run.run_id, "turn.completed", {"result": redact_text(last_text)}, ts=last_ts)
            # turn.started 与 user.message 配对（与实时回合一致），SSE 消费端
            # 不用区分实时流与重放流
            store.append(run.run_id, "turn.started", {}, ts=ts)
            store.append(run.run_id, "user.message", {"text": prompt}, ts=ts)
            if run.first_prompt is None:
                run.first_prompt = prompt
            turn_open, last_text, last_ts = True, "", ts
            continue
        for etype, payload in normalize_message(raw, tool_names):
            store.append(run.run_id, etype, payload, ts=ts)
            if etype == "stage.changed":
                run.stage = payload["stage"]
            elif etype == "agent.message":
                last_text = payload["text"]
        if ts is not None:
            last_ts = ts
    if turn_open:
        store.append(run.run_id, "turn.interrupted", {}, ts=last_ts)


def _derived_run_id(manager, session_id):
    """无身份映射会话的稳定标识：session 前缀派生，重启多次重放同一会话
    不改名。

    与 create 的计数 id（纯数字后缀）不冲突；前缀撞车时逐段加长。
    """
    for size in (8, 12, 16, len(session_id)):
        run_id = f"run_hist_{session_id[:size]}"
        if run_id not in manager.runs:
            return run_id
    raise ValueError(f"session {session_id} 无法分配唯一 run id")
