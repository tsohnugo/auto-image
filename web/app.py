"""FastAPI 应用：创建会话 / 发送·停止·克隆·结束 / SSE 事件流。

错误统一走 HTTPException 默认响应体。事件通道两条：per-run 端点是纯
快照——SSE 的 id 即内部事件 seq，按 Last-Event-ID 重放历史、重放完毕正常
结束响应；全局流常驻广播全部会话的实时事件，空闲按 heartbeat_interval
发 `: ping` 注释行保活。

停止的执行动作（session.interrupt）在 request_stop 置标记之后由 HTTP 层
调用；连接仍在建立时只保留停止意图，run_turn 会在 query 前消费。标记与
回合收尾在单线程事件循环上互斥，interrupt 晚于回合结束时停止目标已达成，
无需把失败放大成错误。

end 的收尾序列（RUNNING 中）：end 校验 → 取消在飞回合任务（回合不补
收尾事件）→ session.ended 作为流的最后一条事件 → 墓碑入册。快照端点
不替前端判终态：session.ended 本身在历史里，重放完毕自然断开。
"""
import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import artifacts as artifacts_mod
from . import rebuild as rebuild_mod
from . import redact as redact_mod
from . import runs as runs_mod
from . import sdk as sdk_mod
from . import state as state_mod
from . import title as title_mod
from .events import EventStore
from .runs import ENDED, RunManager
from .sdk import SDKSessionFactory
from .session import run_turn

# 前端构建产物（vite build 输出），存在才挂载；开发时走 vite dev proxy
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "web-ui" / "dist"
# 流水线产物根：deploy（deploy.config.yaml 的 output_dir 固定前缀）+ rpm
# （rpm-build/verify/archive 落盘处）。键即清单组与 URL 里的根前缀段，
# Agent cwd 即项目根
DEFAULT_ARTIFACT_ROOTS = {
    "deploy": Path(__file__).resolve().parent.parent / "deploy",
    "rpm": Path(__file__).resolve().parent.parent / "rpm",
}
# 产物文件名约定的权威源（见 artifacts.load_file_stages）
DEFAULT_DEPLOY_CONFIG = Path(__file__).resolve().parent.parent / "deploy.config.yaml"
# 运行时真实凭据源（ak/sk/ECS 密码值进脱敏已知清单，见 redact.load_scope_secrets）
DEFAULT_SCOPE_CONFIG = Path(__file__).resolve().parent.parent / "scope.yaml"
# 恢复簿记（墓碑 + 身份映射 + 克隆链镜像；同一 HOME 下多实例共用一份）
DEFAULT_STATE_PATH = Path.home() / ".auto-image-web" / "state.json"
# 并发上限（数执行中回合；新建、克隆、标题生成不占名额）
DEFAULT_MAX_PARALLEL_RUNS = 10


def create_app(session_factory=None, heartbeat_interval=15.0, static_dir=None,
               artifact_roots=None, deploy_config=None, scope_config=None,
               list_sessions_fn=None, get_session_messages_fn=None, transcript_times_fn=None,
               residual_cli_scan=None,
    state_path=None, title_factory=None, max_parallel_runs=None):
    """session_factory 可注入：生产为 ClaudeSDKClient 真实现（默认），
    测试注入按剧本推消息的假实现——注入边界即唯一测试缝。artifact_roots
    （根名 → 目录映射）与 deploy_config 同理注入（产物目录与文件名约定
    造桩用），默认项目根下。
    scope_config 为脱敏已知值清单的凭据源（测试传造桩，不载真实凭据）。
    list_sessions_fn / get_session_messages_fn 注入假历史（重启重建测试缝），
    transcript_times_fn 注入假时刻表（重放事件时刻透传的测试缝，形状
    session_id → {uuid: epoch 秒}），residual_cli_scan 注入残留 CLI 检测
    （pgrep 告警测试缝），默认生产实现。
    state_path 为簿记落盘路径（恢复测试缝），默认 HOME 下固定位置。
    title_factory 为标题生成会话工厂（测试缝；生产为独立 cwd 的隔离配置，
    transcript 不落项目根、不进重启恢复的发现层）。
    max_parallel_runs 为并发上限（默认 WEB_MAX_PARALLEL_RUNS 环境变量，
    缺省 10；测试注入收紧）。"""
    # 已知凭据值入脱敏清单（幂等；scope 缺失时只剩形状正则防线）
    redact_mod.load_scope_secrets(scope_config or DEFAULT_SCOPE_CONFIG)
    app = FastAPI(title="auto-image deploy web")
    limit = max_parallel_runs if max_parallel_runs is not None else int(
        os.environ.get("WEB_MAX_PARALLEL_RUNS", DEFAULT_MAX_PARALLEL_RUNS))
    manager = RunManager(max_parallel=limit)
    store = EventStore()
    store.bind_runs(manager.runs)
    factory = session_factory or SDKSessionFactory()
    titles = title_factory or sdk_mod.TitleSessionFactory()
    artifact_roots = {name: Path(p) for name, p in (artifact_roots or DEFAULT_ARTIFACT_ROOTS).items()}
    file_stages = artifacts_mod.load_file_stages(deploy_config or DEFAULT_DEPLOY_CONFIG)
    app.state.run_manager = manager
    app.state.event_store = store
    app.state.session_factory = factory
    app.state.heartbeat_interval = heartbeat_interval
    state_file = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH

    # 克隆链镜像（session_id → 来源 run_id）：transcript 里没有克隆血缘，
    # 簿记撤销后克隆链父指针无从找回——克隆挂 run.clone_source，persist 时
    # （首回合接受并预分配 session_id 后）随身份映射一并入册；启动时播种
    clone_sources = {}

    def persist():
        """状态变更点统一落盘（墓碑 + 身份映射 + 克隆链镜像，全量原子替换，
        见 state.save_state）。"""
        for r in manager.runs.values():
            if r.clone_source and r.session_id:
                clone_sources[r.session_id] = r.clone_source
        state_mod.save_state(manager.runs.values(), state_file, clone_sources)

    def maybe_assign_title(run, text, is_first):
        """新对话的首条指令到达即起标题生成（Codex 同构：不等回合完成）。
        is_first 由调用方在 begin_turn 前快照（begin_turn 首条指令写
        first_prompt，事后无法判定）——续聊/克隆/重启恢复的老会话一律不再
        生成（否则续聊指令被总结成「继续执行任务」类标题）。"""
        if is_first:
            return asyncio.create_task(
                title_mod.assign_title(run, text, titles, store, on_change=persist)
            )
        return None

    def start_turn(run, text):
        """起回合任务（begin_turn 校验通过后调用）：按回合开合连接，
        收尾即散；任务引用挂 run 供 stop / end 定向。"""
        run.turn_task = asyncio.create_task(run_turn(run, text, factory, store, on_change=persist))

    # 服务重启语义：全量 transcript 重放恢复所有会话（可续聊），state 簿记
    # （墓碑 + 身份映射 + 克隆链镜像）叠加；未收尾回合补 turn.interrupted
    # 提示，不自动重试；残留 CLI 子进程只告警不杀（可能处于云操作中间态）
    bookkeeping = state_mod.load_state(state_file)
    clone_sources.update(bookkeeping["clone_sources"])
    restored = rebuild_mod.recover_sessions(
        manager, store,
        list_sessions_fn or sdk_mod.list_project_sessions,
        get_session_messages_fn or sdk_mod.project_session_messages,
        bookkeeping,
        transcript_times=transcript_times_fn or sdk_mod.transcript_times,
    )
    if restored:
        logging.getLogger("web").info("服务重启后重放恢复 %d 条会话（可续聊）", len(restored))
    residual_pids = (residual_cli_scan or residual_cli_processes)()
    if residual_pids:
        logging.getLogger("web").warning(
            "检测到残留 CLI 子进程（不自动处理，可能处于云操作中间态，请人工处置）：%s",
            residual_pids,
        )

    @app.get("/api/runs")
    async def list_runs():
        return {"runs": manager.summaries()}

    @app.post("/api/runs")
    async def create_run(body: dict | None = None):
        run = manager.create()
        store.create(run.run_id)
        # 会话流同步开卷：session.started 先行（无历史转录——续接语义已由
        # clone 承担，新建即全新会话）
        store.append(run.run_id, "session.started", {})
        persist()
        return {"run_id": run.run_id, "status": run.status, "resumed_from": run.resumed_from}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return _get_run_or_404(manager, run_id).summary()

    @app.post("/api/runs/{run_id}/messages")
    async def send_message(run_id: str, body: dict):
        run = _get_run_or_404(manager, run_id)
        text = (body or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="text required")
        is_first = run.first_prompt is None  # 快照先于 begin_turn（它写 first_prompt）
        try:
            manager.begin_turn(run, text)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        # 接受指令与开卷事件是同一个同步段：成功响应一旦返回，随后到达的
        # stop 必然排在这两条事实之后，不依赖异步回合任务是否已获调度。
        store.append(run.run_id, "turn.started", {})
        store.append(run.run_id, "user.message", {"text": text})
        persist()
        maybe_assign_title(run, text, is_first)
        start_turn(run, text)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str, body: dict | None = None):
        run = _get_run_or_404(manager, run_id)
        try:
            manager.request_stop(run)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        persist()
        await _interrupt_if_requested(run)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/clone")
    async def clone_run(run_id: str):
        run = _get_run_or_404(manager, run_id)
        try:
            new = manager.clone(run)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        store.create(new.run_id)
        store.append(new.run_id, "session.started", {})
        # 源流转录进新会话（seq 重新编号、ts 原样透传——实时事件的 ts 本就是
        # 真实时刻）：生命周期事件不是新会话的状态——源的 session.ended 会被
        # 前端当成本流终态关流判死，克隆后无法续聊
        store.adopt_history(
            new.run_id, run.run_id,
            skip_types={"session.started", "session.ended"},
        )
        # 转录不是真实活动（与 rebuild「重放事件不是真实活动」同款手法）：
        # 末活动时刻覆写为克隆操作时刻——刚点的克隆在列表排最前，归档历史
        # 的「N 分钟前」归源会话自己显示
        new.last_event_at = time.time()
        persist()
        return {"run_id": new.run_id, "status": new.status, "resumed_from": run.run_id}

    @app.post("/api/runs/{run_id}/end")
    async def end_run(run_id: str):
        run = _get_run_or_404(manager, run_id)
        try:
            manager.end(run)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if run.turn_task is not None:
            run.turn_task.cancel()
            try:
                await run.turn_task  # 等取消收尾（回合不补事件）再写终态
            except asyncio.CancelledError:
                pass
            run.turn_task = None
        run.status = ENDED
        run.ended_at = time.time()
        store.append(run.run_id, "session.ended", {})
        persist()
        return {"run_id": run.run_id, "status": run.status}

    # 产物浏览不依赖会话存在（deploy/ + rpm/ 全量镜像，含历史轮次）
    @app.get("/api/artifacts")
    async def list_artifacts():
        return artifacts_mod.browse(artifact_roots, file_stages)

    # /file/ 前缀段：{rel_path:path} 可匹配空串，无前缀段会与清单端点路由歧义
    @app.get("/api/artifacts/file/{rel_path:path}")
    async def read_artifact(rel_path: str):
        found = artifacts_mod.read(artifact_roots, file_stages, rel_path)
        if found is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        entry, target = found
        if entry.get("binary"):  # 二进制产物无文本内容，指引到下载端点
            raise HTTPException(
                status_code=422,
                detail=f"binary artifact; use /api/artifacts/download/{rel_path}",
            )
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # 二进制产物按不可读处理，不 500
            raise HTTPException(status_code=404, detail="artifact not found") from None
        return {**entry, "content": content}

    # 单文件下载：原始字节走附件响应（浏览端点只读文本，.sh 等非文本与
    # 将来的二进制产物从这里拿全量原文件）
    @app.get("/api/artifacts/download/{rel_path:path}")
    async def download_artifact(rel_path: str):
        found = artifacts_mod.resolve(artifact_roots, file_stages, rel_path)
        if found is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        _entry, target = found
        return FileResponse(target, filename=target.name)

    # 批量打包下载：POST {"paths": [根前缀相对路径…]} → 一个 zip（保留
    # deploy/…、rpm/… 目录树；越界/缺失项如实跳过，一个都收不到 404）
    @app.post("/api/artifacts/zip")
    async def zip_artifacts(body: dict | None = None):
        paths = (body or {}).get("paths")
        if not isinstance(paths, list) or not paths or not all(
            isinstance(p, str) and p for p in paths
        ):
            raise HTTPException(status_code=422, detail="paths required")
        stream, count = artifacts_mod.zip_files(artifact_roots, file_stages, paths)
        if stream is None:
            raise HTTPException(status_code=404, detail="no artifacts to zip")
        name = f"auto-image-artifacts-{count}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return StreamingResponse(
            stream,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # per-run 事件端点收窄为纯快照：按 Last-Event-ID 重放历史（断点续传），
    # 重放完毕正常结束响应——不常驻、无心跳、无 ENDED 关流判定（终态事件
    # session.ended 本身在历史里，快照一次给完；实时事件由全局流续接）
    @app.get("/api/runs/{run_id}/events")
    async def event_stream(run_id: str, request: Request):
        _get_run_or_404(manager, run_id)  # 未知 run 404
        seen = _parse_last_event_id(request.headers.get("Last-Event-ID"))

        async def generate():
            for event in store.replay_from(run_id, seen):
                yield _sse_chunk(event)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 全局事件流：一条连接广播全部会话的实时事件（帧带 run_id），零连接
    # 状态——无 Last-Event-ID 断点、无连接簿记、无 TTL，增量游标是流自身
    # 的局部变量；连接前的事件不重放（历史由快照补），断线重连靠快照重拉
    # + per-run seq 去重吸收。永不因会话终态主动关闭：终态后不再产事件，
    # 天然静默，空闲按 heartbeat_interval 心跳保活
    @app.get("/api/stream")
    async def global_stream():
        async def generate():
            with store.subscribe_global() as flag:
                cursor = store.broadcast_len()
                while True:
                    flag.clear()
                    for event in store.broadcast_from(cursor):
                        cursor += 1
                        yield _broadcast_chunk(event)
                    try:
                        await asyncio.wait_for(flag.wait(), timeout=heartbeat_interval)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    directory = static_dir or DEFAULT_STATIC_DIR
    if Path(directory).is_dir():
        app.mount("/", StaticFiles(directory=str(directory), html=True), name="ui")

    return app


async def _interrupt_if_requested(run):
    """执行 request_stop 排队的打断。回合可能刚好已自然结束（停止目标视为
    达成）、会话可能尚未建立（此时由回合在 query 前消费），两种情形均
    无需报错；打断失败不改变服务端权威状态（回合如何收尾以事件流为准）。"""
    if not run.stop_requested or run.session is None:
        return
    try:
        await run.session.interrupt()
    except Exception:  # noqa: BLE001 —— 外部打断失败不放大为 HTTP 错误
        pass


def _sse_chunk(event):
    # ts 随 payload 下发（前端时长的冻结点），seq 走 SSE id 维持断点续传
    data = json.dumps({**event["payload"], "ts": event["ts"]}, ensure_ascii=False)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


def _broadcast_chunk(event):
    # 全局帧：run_id/seq/ts/type/payload 全量下发（seq 仍是 per-run seq，
    # 客户端去重锚点）；无 id 行——断点语义不存在，重连靠快照重拉
    data = json.dumps({
        "run_id": event["run_id"],
        "seq": event["seq"],
        "ts": event["ts"],
        "type": event["type"],
        "payload": event["payload"],
    }, ensure_ascii=False)
    return f"event: {event['type']}\ndata: {data}\n\n"


def _parse_last_event_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _get_run_or_404(manager, run_id):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def residual_cli_processes():
    """pgrep -f claude 检测残留 CLI 子进程，返回 pid 列表；无匹配或 pgrep 缺失为空。

    只发现不处置：残留进程可能正处于云操作中间态，杀不杀由人工判断。
    """
    try:
        proc = subprocess.run(["pgrep", "-f", "claude"], capture_output=True, text=True)
    except OSError:
        return []
    return proc.stdout.split() if proc.returncode == 0 else []
