"""进程内事件存储：seq 递增、Last-Event-ID 断点重放、全局订阅者唤醒。

单进程单 worker 前提下的最简实现（dict + asyncio.Event 广播）。
每条事件带 ts（事件时刻，秒——实时事件即落库当下，重放/转录透传源
时刻）——前端时长的冻结点（最后活动时刻）与累计执行时长的回合分段
都以它为准，不受页面刷新/SSE 全量重放影响。
全局广播日志（引用同批事件）供全局流增量拉取：连接起点即日志末尾，
连接前的事件不入流，历史由快照端点补。
"""
import asyncio
import contextlib
import time
from collections import defaultdict


class EventStore:
    def __init__(self):
        self._events = defaultdict(list)
        self._broadcast = []
        self._global_subscribers = set()
        self._runs = None

    def bind_runs(self, runs):
        """挂接 run 表（app 装配时调用）：append 即同步 run.last_event_at，
        时长冻结点只此一处更新，摘要与簿记落盘都读它。"""
        self._runs = runs

    def create(self, run_id):
        self._events[run_id] = []

    def append(self, run_id, etype, payload, ts=None):
        """追加一条内部事件，返回带递增 seq 与 ts 的完整事件。

        ts 缺省是 append 当下（实时路径）；重放/转录路径传入源时刻
        （transcript 行的 timestamp）——ts 语义是「事件时刻」而非
        「落流时刻」，同一条消息派生的多条事件共享同一值。
        """
        event = {
            "seq": len(self._events[run_id]) + 1,
            "ts": ts if ts is not None else time.time(),
            "run_id": run_id,
            "type": etype,
            "payload": payload,
        }
        self._events[run_id].append(event)
        self._broadcast.append(event)
        if self._runs is not None:
            run = self._runs.get(run_id)
            if run is not None:
                run.last_event_at = event["ts"]
        for flag in self._global_subscribers:
            flag.set()
        return event

    def replay_from(self, run_id, after_seq):
        """返回 seq 严格大于 after_seq 的全部事件（快照重放用）。"""
        return [ev for ev in self._events[run_id] if ev["seq"] > after_seq]

    def adopt_history(self, dst_run_id, src_run_id, skip_types=()):
        """把源 run 的全部事件转录进目标 run（seq 重新递增、唤醒订阅者）——
        克隆创建的新会话由此自带源会话历史（CLI resume 的浏览体验）。
        skip_types 排除源流的生命周期事件（session.started / session.ended：
        起点会与新流重复，终态收尾会被前端当成本流终态关流判死）。"""
        for ev in self._events[src_run_id]:
            if ev["type"] not in skip_types:
                self.append(dst_run_id, ev["type"], ev["payload"], ts=ev["ts"])

    def broadcast_from(self, after):
        """返回广播日志中位置严格大于 after 的全部事件（全局流增量拉取）。"""
        return self._broadcast[after:]

    def broadcast_len(self):
        """广播日志末尾位置（全局流的连接起点，之前的事件不重放）。"""
        return len(self._broadcast)

    @contextlib.contextmanager
    def subscribe_global(self):
        """全局唤醒信号：任何 run 的事件 append 都 set。游标归订阅方自持
        （局部变量），服务端无连接簿记。"""
        flag = asyncio.Event()
        self._global_subscribers.add(flag)
        try:
            yield flag
        finally:
            self._global_subscribers.discard(flag)
