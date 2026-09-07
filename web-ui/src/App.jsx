// A 形态 —— Claude Code 会话的 Web 对话界面。
// header 全宽（run_id · 会话状态 · 当前阶段 · 时长 · 结束会话）+ 左侧可
// 收起侧栏（「会话 | 产物」两面板）+ 主区混合标签栏（会话与产物文件同栏
// 混排，只压主区）+ 编辑区（按激活标签页渲染消息流或文件内容）+ 底部
// 常驻对话输入条。header 与输入条构成控制面，绑定最后激活的会话标签页
// ——激活文件标签页不换对象。
import { useEffect, useRef, useState } from 'react'
import './App.css'
import * as store from './store.js'
import { RUN_STATUS_LABEL, STAGE_LABEL, fmtActive, fmtLastActivity } from './derive.js'
import { mdToHtml } from './markdown.js'
import { tabKey } from './tabState.js'
import ArtifactView from './components/ArtifactView.jsx'
import ChatBar from './components/ChatBar.jsx'
import Tabs from './components/Tabs.jsx'
import SidePanel from './components/SidePanel.jsx'

const STATUS_TONE = { RUNNING: 'running', ENDED: 'warn' }

// 回合汇总与最后一条 agent 消息同文时降级为轻量状态线：正常完成的回合
// result 就是最后一条 assistant 文本（CLI Result 语义），重复成框是噪音；
// 异常收尾（无最终文本 / 被停止 / 失败摘要）才保留汇总框。
// 线上不断言「可继续」——活会话输入条已表达，只读回放里则与语义相悖
function turnCompletedRow(ev, prev) {
  const result = String(ev.payload.result ?? '').trim()
  if (!result) return null // 空结果（重放的历史常见）不成空框
  const dup =
    prev?.type === 'agent.message' &&
    String(prev.payload.text ?? '').trim() === result
  if (dup) return <div className="va-stage-line">─ 回合完成 ─</div>
  return (
    <div className="va-result">
      <div className="va-result-title">回合汇总（turn.completed，会话可继续）</div>
      <div className="va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(ev.payload.result) }} />
    </div>
  )
}

// 工具事件索引：started/finished 按 id 关联（旧事件无 id 时不入索引，
// 各自独立成行兜底）
function toolIndex(events) {
  const startedById = new Map()
  const finishedIds = new Set()
  for (const ev of events) {
    if (!ev.payload?.id) continue
    if (ev.type === 'agent.tool_started') startedById.set(ev.payload.id, ev)
    if (ev.type === 'agent.tool_finished') finishedIds.add(ev.payload.id)
  }
  return { startedById, finishedIds }
}

// 工具展开体的一块：小标题 + 浅底圆角内容块，输入/输出各一块
function IoBlock({ title, text }) {
  return (
    <div className="va-tool-io">
      <div className="va-tool-io-title">{title}</div>
      <pre className="va-tool-io-text">{text}</pre>
    </div>
  )
}

// Edit/Write 的输入块：diff 文本按行首染色（+ 绿 / - 红 / 头部灰）
function IoDiff({ text }) {
  return (
    <div className="va-tool-io">
      <div className="va-tool-io-title">diff</div>
      <pre className="va-tool-io-text va-diff">
        {text.split('\n').map((line, i) => {
          const cls = line.startsWith('+++') || line.startsWith('---')
            ? 'meta'
            : line.startsWith('+')
              ? 'add'
              : line.startsWith('-')
                ? 'del'
                : 'ctx'
          return <span key={i} className={cls}>{line}{'\n'}</span>
        })}
      </pre>
    </div>
  )
}

// TodoWrite 的输入块：checkbox 列表（☑ 完成 / ◐ 进行中 / ☐ 待办）
function IoTodos({ todos }) {
  const MARK = { completed: '☑', in_progress: '◐', pending: '☐' }
  return (
    <div className="va-tool-io">
      <div className="va-tool-io-title">todos</div>
      <div className="va-todos">
        {todos.map((t, i) => (
          <div key={i} className={`va-todo ${t.status}`}>{MARK[t.status] ?? '☐'} {t.content}</div>
        ))}
      </div>
    </div>
  )
}

function EventRow({ ev, prev, tools }) {
  if (ev.type === 'stage.changed') {
    return <div className="va-stage-line">─ 进入 {STAGE_LABEL[ev.payload.stage] ?? ev.payload.stage} ─</div>
  }
  if (ev.type === 'user.message') {
    return <div className="va-user">{ev.payload.text}</div>
  }
  if (ev.type === 'turn.started') {
    return null // 回合开卷标记（与 user.message 配对），消息行已表达
  }
  if (ev.type === 'turn.stopped') {
    return (
      <div className="va-paused">
        — 已停止，等待指令 · 已提交的云操作不受停止影响，无法撤销 —
      </div>
    )
  }
  if (ev.type === 'turn.failed') {
    return <div className="va-failed">回合失败（会话可继续）：{ev.payload.message}</div>
  }
  if (ev.type === 'turn.interrupted') {
    return (
      <div className="va-paused">
        — 服务重启，上一回合被中断 · 已提交的云操作不受影响，无法撤销 —
      </div>
    )
  }
  if (ev.type === 'agent.thinking') {
    return (
      <details className="va-thinking">
        <summary>Thinking &gt;</summary>
        <div className="va-thinking-body">{ev.payload.text}</div>
      </details>
    )
  }
  if (ev.type === 'agent.message') {
    return <div className="va-msg va-md" dangerouslySetInnerHTML={{ __html: mdToHtml(ev.payload.text) }} />
  }
  if (ev.type === 'agent.tool_started' || ev.type === 'agent.tool_finished') {
    // 同 id 已有 finished：行移到 finished 位置渲染成 ✓，此处跳过
    if (ev.type === 'agent.tool_started' && ev.payload.id && tools.finishedIds.has(ev.payload.id)) {
      return null
    }
    const started = ev.type === 'agent.tool_finished' && ev.payload.id
      ? tools.startedById.get(ev.payload.id)
      : null
    const head = started?.payload ?? ev.payload // 行显示入参主参数；旧事件兜底自身摘要
    const input = head.detail ?? head.summary // 输入全文（k: v 行），折叠行截断的完整版
    const output = ev.type === 'agent.tool_finished' // 完成后附结果全文
      ? ev.payload.detail ?? ev.payload.summary
      : null
    const mark = ev.type === 'agent.tool_started' ? '▶' : '✓'
    return (
      <details className="va-tool">
        <summary>{mark} <b>{head.tool}</b>({head.summary})</summary>
        <div className="va-tool-detail">
          {head.diff ? <IoDiff text={head.diff} /> : head.todos ? <IoTodos todos={head.todos} /> : <IoBlock title="输入" text={input} />}
          {output !== null && <IoBlock title="输出" text={output} />}
        </div>
      </details>
    )
  }
  if (ev.type === 'turn.completed') {
    return turnCompletedRow(ev, prev)
  }
  if (ev.type === 'session.started') {
    return null // 会话流开卷，header 已表达
  }
  if (ev.type === 'session.title_changed') {
    return null // 标题落 header / 下拉，不在消息流渲染
  }
  if (ev.type === 'session.ended') {
    return <div className="va-canceled">— 会话已结束（可回看，只能 Fork）—</div>
  }
  return null
}

// 结束会话：显式且不可逆，一律二次确认——READY 可能挂着一整天工作上下文，
// 结束后只能 Fork；执行中结束还会打断在飞回合
function onEndRun(run) {
  const msg =
    run.status === 'RUNNING'
      ? `会话 ${run.runId} 回合执行中，结束将打断在飞回合（已提交的云操作不受影响）。确定结束？`
      : `确定结束会话 ${run.runId}？结束后不可恢复（只读回看，只能 Fork 后继续）。`
  if (!window.confirm(msg)) return
  store.endRun()
}

// ---------- 侧栏宽度（拖拽调整，localStorage 持久化） ----------

const SIDE_W_KEY = 'va-side-w'
export const SIDE_W_DEFAULT = 280
const SIDE_W_MIN = 220
const sideWMax = () => Math.round(window.innerWidth * 0.4) // 主区事件流是心脏，侧栏至多吃四成
const clampSideW = (w) => Math.max(SIDE_W_MIN, Math.min(sideWMax(), w))

function readSideW() {
  try {
    const raw = localStorage.getItem(SIDE_W_KEY)
    if (raw != null) {
      const v = Number(raw)
      // Number(null) 是 0——先判 null 再转换，缺失时落默认宽而非最小宽
      if (Number.isFinite(v)) return clampSideW(v) // 窗口变小后恢复时按当前视口收敛
    }
  } catch {
    // 存储不可用：用默认宽，不影响功能
  }
  return SIDE_W_DEFAULT
}

function persistSideW(w) {
  try {
    localStorage.setItem(SIDE_W_KEY, String(w))
  } catch {
    // 存储不可用：只丢宽度存活，不影响使用
  }
}

// 消息流：激活的会话标签页的事件渲染。ref/scroll 逻辑属主在本层，
// 组件随标签页切换重挂（key=runId），follow 态自然复位
function Stream({ run }) {
  const scrollRef = useRef(null)
  const [follow, setFollow] = useState(true)
  const tools = toolIndex(run.events)

  const scrollToBottom = () => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  useEffect(() => {
    if (follow) scrollToBottom()
  }, [run.events.length, follow])

  const onScroll = () => {
    const el = scrollRef.current
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }

  return (
    <div className="va-stream" ref={scrollRef} onScroll={onScroll}>
      {run.events.length === 0 && (
        <div className="va-empty-hint">空会话——输入第一条部署指令（软件 + 文档链接 + 目标机器）。</div>
      )}
      {run.events.map((ev, i) => (
        <EventRow key={ev.seq} ev={ev} prev={run.events[i - 1]} tools={tools} />
      ))}
      {!follow && (
        <button
          className="va-jump"
          onClick={() => {
            setFollow(true)
            scrollToBottom()
          }}
        >
          ↓ 回到最新
        </button>
      )}
    </div>
  )
}

export default function App() {
  const s = store.useRunState()
  const control = store.useControlRun()
  const [sideOpen, setSideOpen] = useState(true)
  const [sideW, setSideW] = useState(readSideW)
  const dragRef = useRef(false)
  const activeTab = s.tabs.find((t) => tabKey(t) === s.activeKey) ?? null
  const activeRun = activeTab?.kind === 'session' ? s.runs[activeTab.runId] : null
  const activeArtifact = activeTab?.kind === 'file' ? s.artifactCache[activeTab.relPath] : null

  // 拖把手：Pointer Events + setPointerCapture（触控板/触屏同路径）。拖拽中
  // 禁 pin 钮的 left transition（否则钮滞后光标拖影）；pointerup 落定并
  // 持久化。宽度写 --side-w 变量，.va-side 与 pin 钮定位同源引用。
  const onHandlePointerDown = (e) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = true
    document.body.classList.add('va-side-resizing') // 拖拽期全局光标 + 禁选中
  }
  const onHandlePointerMove = (e) => {
    if (!dragRef.current) return
    setSideW(clampSideW(e.clientX))
  }
  const onHandlePointerUp = (e) => {
    if (!dragRef.current) return
    dragRef.current = false
    e.currentTarget.releasePointerCapture(e.pointerId)
    document.body.classList.remove('va-side-resizing')
    persistSideW(sideW)
  }
  // 双击把手回默认宽
  const onHandleDoubleClick = () => {
    setSideW(SIDE_W_DEFAULT)
    persistSideW(SIDE_W_DEFAULT)
  }
  // 窗口缩放：超限宽度按当前视口收敛（拖拽上限 40vw 的动态半边）
  useEffect(() => {
    const onResize = () => setSideW((w) => clampSideW(w))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  return (
    <div className="va-root">
      <header className="va-head">
        {control ? (
          <>
            <span className="va-runid" title={control.title ?? undefined}>{control.runId}</span>
            <span className={`dot tone-${STATUS_TONE[control.status] ?? 'ok'}`} />
            <span>{RUN_STATUS_LABEL[control.status]}</span>
            <span className="va-spacer" />
            <span className="va-stage">{control.stage ? STAGE_LABEL[control.stage] ?? control.stage : null}</span>
            <span className="va-elapsed" title="累计执行：各回合之和，扣除等待输入">
              总计时间：{fmtActive(control, s.now)}
            </span>
            <span className="va-elapsed" title="最后一次用户发送指令的时刻">最近指令 {fmtLastActivity(control)}</span>
            <button className="va-end" onClick={() => onEndRun(control)} disabled={!store.isOperable(control.status)}>
              结束会话
            </button>
          </>
        ) : (
          <span className="va-runid">auto-image 部署会话</span>
        )}
        {s.connection === 'reconnecting' && (
          <span className="va-conn">事件流连接断开，重连中（恢复后自动追平）…</span>
        )}
      </header>

      <div className="va-body" style={{ '--side-w': `${sideW}px` }}>
        {sideOpen && <SidePanel />}
        {sideOpen && (
          <div
            className="va-side-resize"
            role="separator"
            aria-orientation="vertical"
            aria-label="拖拽调整侧栏宽度（双击恢复默认）"
            onPointerDown={onHandlePointerDown}
            onPointerMove={onHandlePointerMove}
            onPointerUp={onHandlePointerUp}
            onDoubleClick={onHandleDoubleClick}
          />
        )}
        <button
          className={`va-side-pin${sideOpen ? ' open' : ''}`}
          onClick={() => setSideOpen(!sideOpen)}
          title={sideOpen ? '收起侧栏' : '展开侧栏'}
          aria-label={sideOpen ? '收起侧栏' : '展开侧栏'}
          aria-expanded={sideOpen}
          aria-controls="task-side"
        >
          <span className="va-btn-sym" aria-hidden="true">{sideOpen ? '«' : '»'}</span>
        </button>
        <div className="va-main">
          <Tabs />
          <div className="va-tab-body" id="va-tab-body">
            {activeTab === null ? (
              <div className="empty-state">
                <div className="big">未开始</div>
                <div>点标签栏「+ 新建」创建会话，输入第一条部署指令</div>
              </div>
            ) : activeTab.kind === 'file' ? (
              <ArtifactView artifact={activeArtifact} onDownload={store.downloadArtifact} />
            ) : activeRun ? (
              <Stream key={activeRun.runId} run={activeRun} />
            ) : null}
          </div>
        </div>
      </div>

      {s.submitError && <div className="error-bar">{s.submitError}</div>}

      <ChatBar />
    </div>
  )
}
