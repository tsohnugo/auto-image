"""会话状态机与并发规则（三态：READY / RUNNING / ENDED）。

会话（Session）与回合（Turn）分离：回合是用户指令的一次执行，会话是对话
身份。回合完成（turn.completed）、被停止（turn.stopped）或失败
（turn.failed）都不结束会话——一律回 READY，下一条指令即续聊；会话终态
只有用户显式结束（end → ENDED，不可续聊只能克隆，墓碑入册防重启复活）。
重启恢复（rebuild.py）全量重放 transcript，墓碑会话重放后仍标 ENDED。

并发：无全局门禁，任意多会话可同时各跑一个回合；执行中回合数由
max_parallel 限制（WEB_MAX_PARALLEL_RUNS，send 时检查——新建、克隆不占
执行名额）。409 判定值为本模块顶部常量（调用方以 Conflict.detail 透传）。
"""
import asyncio
import itertools
import time
import uuid

READY = "READY"
RUNNING = "RUNNING"
ENDED = "ENDED"

TURN_IN_PROGRESS = "turn_in_progress"
SESSION_RUNNING = "session_running"
PARALLEL_LIMIT_REACHED = "parallel_limit_reached"
SESSION_NOT_ACTIVE = "session_not_active"


class Conflict(Exception):
    """并发规则拒绝（转成 HTTP 409，detail 即判定值）。"""

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class Run:
    def __init__(self, run_id):
        self.run_id = run_id
        self.status = READY
        self.stage = None
        self.created_at = time.time()
        self.first_prompt = None
        self.title = None            # LLM 生成标题（title.py），列表展示优先于截断
        self.turn_task = None        # 当前回合的 asyncio.Task（READY 时为 None）
        self.session = None          # 当前回合的 SDK 连接（回合内非空）
        self.session_id = None       # 本会话拥有的 SDK id（首回合接受时预分配）
        self.session_confirmed = False  # SDK 消息已回报并确认上述目标身份
        self.resume_session_id = None  # 回合起连接时的续接源（克隆/恢复带入）
        self.resumed_from = None     # 克隆来源 run_id（对外呈现）
        self.clone_source = None     # 克隆血缘（落克隆链镜像用，见 app.persist）
        self.stop_requested = False  # 停止请求标记：turn.stopped 的权威判定
        self.ended_at = None         # ENDED 时刻（时长定格；非终态为 None）
        self.last_event_at = None    # 最后活动时刻（时长冻结点；rebuild 兜底）

    def summary(self):
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "first_prompt": self.first_prompt,
            "title": self.title,
            "started_at": self.created_at,
            "ended_at": self.ended_at,
            "last_event_at": self.last_event_at,
            "resumed_from": self.resumed_from,
        }


class RunManager:
    def __init__(self, max_parallel=10):
        self.runs = {}
        self._ids = itertools.count(1)
        self.max_parallel = max_parallel

    def get(self, run_id):
        return self.runs.get(run_id)

    def register(self, run):
        """注册重启恢复的 run（不经 create 的并发校验：启动时无执行）。"""
        self.runs[run.run_id] = run

    def adopt_ids(self, run_ids):
        """恢复既有 run_id 后把计数器前拨过已用号，新建不撞号（run_N 解析
        数字取 max；解析不出的 id 与计数序列无关，跳过）。"""
        top = 0
        for run_id in run_ids:
            _, _, num = run_id.rpartition("run_")
            if num.isdigit():
                top = max(top, int(num))
        if top >= next(self._ids):
            self._ids = itertools.count(top + 1)

    def summaries(self):
        """全部 run 摘要，按最后活动及稳定次键降序排列。"""
        def sort_key(run):
            activity_at = run.last_event_at
            if activity_at is None:
                activity_at = run.ended_at
            if activity_at is None:
                activity_at = run.created_at
            return activity_at, run.created_at, run.run_id

        return [
            run.summary()
            for run in sorted(self.runs.values(), key=sort_key, reverse=True)
        ]

    def running_count(self):
        """执行中回合数（并发上限的计数口径）。"""
        return sum(1 for r in self.runs.values() if r.status == RUNNING)

    def create(self):
        """新建空会话（不占执行名额、不受并发上限约束）。"""
        run = Run(f"run_{next(self._ids)}")
        self.runs[run.run_id] = run
        return run

    def begin_turn(self, run, text):
        """回合开卷的前置校验与状态置位（同步块，与回合收尾互斥）：ENDED
        拒发、执行中拒发、并发上限拒发；通过则置 RUNNING、记首条指令。
        连接的建立由调用方随后起回合任务执行。"""
        if run.status == ENDED:
            raise Conflict(SESSION_NOT_ACTIVE)
        if run.status == RUNNING:
            raise Conflict(TURN_IN_PROGRESS)
        if self.running_count() >= self.max_parallel:
            raise Conflict(PARALLEL_LIMIT_REACHED)
        if run.session_id is None:
            # SDK 的 --session-id 只接受 UUID。必须在异步回合任务启动及本次
            # 状态落盘前分配，Result 尚未返回时结束也能留下身份映射与墓碑。
            run.session_id = str(uuid.uuid4())
        run.status = RUNNING
        # 上回合异常收尾未消费的停止标记作废：停止只作用于当时的回合
        run.stop_requested = False
        if run.first_prompt is None:
            run.first_prompt = text

    def request_stop(self, run):
        """停止目标会话的当前回合：置 stop_requested（回合收尾以此判定
        turn.stopped），打断动作由调用方随后执行。READY 会话无回合可停，
        幂等无操作；ENDED 不可干预。"""
        if run.status == ENDED:
            raise Conflict(SESSION_NOT_ACTIVE)
        if run.status == RUNNING:
            run.stop_requested = True

    def clone(self, run):
        """克隆校验：READY / ENDED 源可克隆，RUNNING 源拒（resume 一个正在
        被写入的 transcript，克隆回合会基于过时上下文执行云操作）。
        新会话的构造（事件转录、标题继承）由调用方执行。"""
        if run.status == RUNNING:
            raise Conflict(SESSION_RUNNING)
        new = self.create()
        new.resumed_from = run.run_id
        new.clone_source = run.run_id
        new.resume_session_id = run.session_id  # 回合以此续接源起新连接
        # 标题继承：克隆与源是同一任务的分叉，first_prompt 不再生成标题
        new.first_prompt = run.first_prompt
        new.title = run.title
        return new

    def end(self, run):
        """显式结束会话的前置校验；在飞回合的取消与收尾由调用方执行。
        已 ENDED 幂等拒绝（不可干预之外的一切动作）。"""
        if run.status == ENDED:
            raise Conflict(SESSION_NOT_ACTIVE)
