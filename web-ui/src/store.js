// 共享会话状态：多会话并行（并发上限内的执行中回合可多个），动作经
// HTTP/SSE 与服务端交互。事件通道是一条全局 SSE：启动即连 /api/stream、
// 永不主动关闭，广播帧按 runId 分发给打开的标签页（未打开的丢弃）；
// 打开会话标签页 = 拉一次快照补历史（按 Last-Event-ID 重放，与流重叠的
// 事件由 per-run seq 去重吸收）；断线由浏览器自动重连，恢复后对打开的
// 标签页逐个重拉快照追平。
//
// 标签页是纯客户端视图（会话与产物文件同栏混排，开-关-激活-控制面决策
// 全走 tabState 纯模块）：closeTab 只关视图，会话仍在列表里，重新打开
// （selectRun）重拉快照恢复历史；「结束会话」才是服务端动作。
// 非查看中的标签页状态点由 GET /api/runs 摘要轮询驱动。header 与输入条
// 构成控制面，绑定 controlRunId 解析出的会话——激活文件标签页不换对象。
import { useSyncExternalStore } from 'react'
import { mergeSessionEvents, mergeSessionSummary, SESSION_STATUS } from './eventMerge.js'
import * as tabState from './tabState.js'

// 与服务端内部事件协议一致的事件类型全集（四族：session.* / turn.* /
// user.message / agent.* / stage.*）
export const EVENT_TYPES = [
  'session.started',
  'session.title_changed',
  'session.ended',
  'user.message',
  'agent.thinking',
  'agent.message',
  'agent.tool_started',
  'agent.tool_finished',
  'stage.changed',
  'turn.started',
  'turn.stopped',
  'turn.completed',
  'turn.failed',
  'turn.interrupted',
]

// 会触发产物清单刷新的事件：阶段推进（新产物落盘）与回合/会话收尾。
// 清单是 deploy/ + rpm/ 全量镜像（与查看中的会话无关），任一 run 触发都全局刷新
const REFRESH_EVENT_TYPES = ['stage.changed', 'turn.completed', 'turn.stopped', 'turn.failed', 'session.ended']

const { RUNNING, READY, ENDED } = SESSION_STATUS
// 可继续操作的会话状态集合：判定值与服务端状态机一致，单处维护
const OPERABLE = [RUNNING, READY]
export const isOperable = (status) => OPERABLE.includes(status)

// 409 detail 判定值 → 人话提示（判定值与服务端 runs.Conflict.detail 一致，单处维护）
const CONFLICT_HINT = {
  turn_in_progress: '本会话回合执行中，想改方向先点「停止」',
  session_running: '源会话正在执行，回合结束后才能 Fork',
  parallel_limit_reached: '执行中回合已达并发上限，稍后再发',
  session_not_active: '会话已结束，不可再操作（可 Fork 后继续）',
}

const listeners = new Set()
// 标签页视图随浏览器刷新与重开存活（刷新/误关/关窗重开后标签页与会话
// 一一对应还在），服务端已不存在的会话（重启丢了空会话等）在 loadRuns
// 合并列表时自然剪掉。localStorage（跨窗口、跨浏览器会话）而非
// sessionStorage：用户故事要求「关闭浏览器后重新打开」标签也还在；共享
// 服务时各客户端各存各的（存储按本机源隔离），互不沾染。只存会话标签页
// （含次序与激活态），文件标签页刷新后消失、内容缓存随之丢弃。
const TABS_KEY = 'va-open-tabs'
function restoreTabs() {
  try {
    const saved = JSON.parse(localStorage.getItem(TABS_KEY) || 'null')
    if (saved && Array.isArray(saved.openTabs)) {
      const openTabs = saved.openTabs.filter((id) => typeof id === 'string')
      const viewRunId = openTabs.includes(saved.viewRunId) ? saved.viewRunId : openTabs[0] ?? null
      return { openTabs, viewRunId }
    }
  } catch {
    // 存储不可用/损坏：从空标签集开始，功能照常
  }
  return { openTabs: [], viewRunId: null }
}

// 落盘走 tabState.persistableTabs（文件标签页滤掉、viewRun 落到记住的
// 最后激活会话标签页）；形状与旧版本兼容（openTabs 纯 runId 数组 + viewRunId）
function persistTabs() {
  try {
    const { openTabs, viewRunId } = tabState.persistableTabs(state.tabs, state.activeKey, state.lastSessionKey)
    localStorage.setItem(TABS_KEY, JSON.stringify({ openTabs, viewRunId }))
  } catch {
    // 存储不可用（隐私模式等）：只丢刷新存活，不影响使用
  }
}

// order：全部会话的列表序（含未打开的，服务端列表同源）；tabs：混合标签
// 栏的标签页数组（{kind:'session',runId} | {kind:'file',relPath,name}），
// activeKey 复合 key 寻址（session:<runId> / file:<relPath>），决策全走
// tabState 纯模块，这里只当状态容器。lastSessionKey 记住最后激活的会话
// 标签页——激活文件标签页时控制面（header/输入条）仍绑定它。
const restored = restoreTabs()
let state = {
  runs: {},
  order: [],
  tabs: restored.openTabs.map((runId) => ({ kind: 'session', runId })),
  activeKey: restored.viewRunId ? `session:${restored.viewRunId}` : null,
  lastSessionKey: restored.viewRunId ? `session:${restored.viewRunId}` : null,
  connection: 'connecting', // 全局事件流连接态：connecting → live / reconnecting
  submitError: null,
  now: Date.now(),
  drafts: {},                // 会话草稿镜像（runId → 文本；真身在 setDraft 侧的 map）
  artifacts: { groups: [] }, // deploy/ + rpm/ 全量产物（目录分组，全局不属于任何 run）
  artifactCache: {},         // 产物内容多槽缓存（relPath → 条目+content），关标签页不清
  artifactSel: {},           // 批量下载勾选集（relPath → true，随清单刷新剪枝）
  artifactZipping: false,    // zip 打包请求进行中（按钮防重复触发）
}

// 时长走针仅在控制面会话执行期间（挂起与终态冻结，终态由事件求和定格）
setInterval(() => {
  if (state.runs[controlRunId()]?.status === RUNNING) set({ now: Date.now() })
}, 1000)

function set(patch) {
  state = { ...state, ...patch }
  if ('tabs' in patch || 'activeKey' in patch || 'lastSessionKey' in patch) persistTabs()
  listeners.forEach((l) => l())
}

function setRun(runId, patch) {
  const run = state.runs[runId]
  if (!run) return
  state = { ...state, runs: { ...state.runs, [runId]: { ...run, ...patch } } }
  listeners.forEach((l) => l())
}

export function subscribe(l) {
  listeners.add(l)
  return () => listeners.delete(l)
}
export const getState = () => state
export function useRunState() {
  return useSyncExternalStore(subscribe, getState)
}

// 控制面（header/输入条）绑定的会话：激活的是会话标签页 → 它；是文件
// 或空 → 记住的最后激活会话标签页。无会话标签页（服务端彻底无会话）为 null。
export function controlRunId() {
  return tabState.controlRunId(state.tabs, state.activeKey, state.lastSessionKey)
}

// 控制面会话（useControlRun 的数据源钩子；消息流视图在 App 按 tabs 派生）
export function useControlRun() {
  const s = useRunState()
  return s.runs[controlRunId()] ?? null
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const err = new Error(data.detail || `HTTP ${resp.status}`)
    err.status = resp.status
    err.detail = data.detail
    throw err
  }
  return data
}

let errorTimer = null
function fail(message) {
  clearTimeout(errorTimer)
  set({ submitError: message })
  errorTimer = setTimeout(() => state.submitError && set({ submitError: null }), 4000)
}

// 409 家族提示条文案：命中判定值给人话提示，其余如实透传服务端 detail
const conflictText = (detail) => `409 — ${detail}：${CONFLICT_HINT[detail]}`
function conflictMessage(err) {
  return err.status === 409 && CONFLICT_HINT[err.detail]
    ? conflictText(err.detail)
    : err.detail || err.message
}

// ---------- 事件通道：全局流 + 快照 ----------

// 事件落地（广播帧与快照重放同一归途）：整批交给纯归并入口按 seq
// 寻址、去重、排序并重算派生状态。阶段推进与收尾类事件顺手触发产物
// 清单刷新（幂等无害）。
function ingestEvents(runId, events) {
  const session = state.runs[runId]
  if (!session || !events.length) return
  setRun(runId, mergeSessionEvents(session, events))
  if (events.some((event) => REFRESH_EVENT_TYPES.includes(event.type))) refreshArtifacts()
}

// 广播帧 → 事件落地：帧带 run_id/seq/ts，先过「该 run 打开着标签页」守卫
// （未打开的丢弃——侧栏态势由摘要轮询驱动）。ts 在帧顶层（快照路径则是
// data 里已并入），统一并进 payload——归并入口读 payload.ts
function onBroadcastFrame(e) {
  let frame
  try {
    frame = JSON.parse(e.data)
  } catch {
    return
  }
  if (!openRunIds().has(frame.run_id)) return
  ingestEvents(frame.run_id, [{
    seq: frame.seq,
    type: frame.type,
    payload: { ...frame.payload, ts: frame.ts },
  }])
}

// 打开着的会话标签页的 runId 集（分发守卫；loadRuns 剪枝前对恢复标签页
// 宽进——不存在的 run 的帧会被 ingestEvents 的存在性守卫拦下）
function openRunIds() {
  const ids = new Set()
  for (const t of state.tabs) if (t.kind === 'session') ids.add(t.runId)
  return ids
}

// SSE 文本 → 事件数组：id/event/data 三行成帧，`:` 开头注释行（心跳）跳过。
// 单帧 data 非法 JSON 只丢那一帧（外部输出不保证，坏一帧不放大成整批丢失）
function parseSseEvents(text) {
  const events = []
  for (const block of text.split('\n\n')) {
    if (!block || block.startsWith(':')) continue
    let seq = null
    let type = null
    let data = null
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) seq = Number(line.slice(3).trim())
      else if (line.startsWith('event:')) type = line.slice(6).trim()
      else if (line.startsWith('data:')) data = line.slice(5).trim()
    }
    if (seq == null || !type || data == null) continue
    try {
      events.push({ seq, type, payload: JSON.parse(data) })
    } catch {
      // 坏帧丢弃：下一帧继续
    }
  }
  return events
}

// 拉一次快照补历史：per-run 端点按 Last-Event-ID 重放、重放完即断，重叠
// 事件由纯归并入口的 seq 去重吸收。run 已不在（服务端重启丢了空会话等）
// 静默作罢——摘要轮询会把它从列表剪掉。
async function loadSnapshot(runId) {
  const run = state.runs[runId]
  if (!run) return
  const lastSeq = run.maxSeq ?? 0
  try {
    const resp = await fetch(`/api/runs/${runId}/events`, { headers: { 'Last-Event-ID': String(lastSeq) } })
    if (!resp.ok) return
    const events = parseSseEvents(await resp.text())
    if (!events.length) return
    ingestEvents(runId, events)
  } catch {
    // 快照失败不打断使用：全局流仍在，断线恢复或下次打开再补
  }
}

// 对打开的会话标签页逐个重拉快照（onopen 恢复与 loadRuns 首屏恢复共用）
function refreshOpenSnapshots() {
  for (const runId of openRunIds()) loadSnapshot(runId)
}

// 全局流：应用启动即建一条、永不主动关闭。断线由浏览器自动重连，
// 恢复（onopen，含首次连上）对打开的会话标签页逐个重拉快照追平——
// 断线期间错过的事件全靠快照补，重连本身不带断点。
const globalStream = new EventSource('/api/stream')
for (const type of EVENT_TYPES) globalStream.addEventListener(type, onBroadcastFrame)
globalStream.onopen = () => {
  set({ connection: 'live' })
  refreshOpenSnapshots()
}
globalStream.onerror = () => {
  if (globalStream.readyState !== EventSource.CLOSED) set({ connection: 'reconnecting' })
}

// ---------- HTTP ----------

// run 对象的唯一构造点：服务端摘要（loadRuns/轮询）与新建/Fork 响应共用
// 同一形状，字段差异由 overrides 给出
function makeRun(overrides) {
  return {
    runId: null,
    status: null,
    stage: null,
    firstPrompt: null,
    title: null,
    resumedFrom: null,
    events: [],
    result: null,
    startedAt: null,
    endedAt: null,
    lastEventAt: null,
    maxSeq: 0,
    ...overrides,
  }
}

// 摘要列表拉取与合并（loadRuns 首屏与轮询共用）：新会话补进 runs，order
// 以服务端为源覆盖。返回列表 order（失败返回 null，调用方各自善后）
async function fetchSummaries() {
  const resp = await fetch('/api/runs')
  if (!resp.ok) return null
  const { runs } = await resp.json()
  const map = {}
  const order = []
  for (const s of runs ?? []) {
    map[s.run_id] = mergeSummary(state.runs[s.run_id] ?? makeRun({ runId: s.run_id }), s)
    order.push(s.run_id)
  }
  set({ runs: { ...state.runs, ...map }, order })
  return order
}

// 启动加载：拉全量 run 摘要恢复会话列表（服务重启后经 transcript 重放，
// 全部可续聊、ENDED 只读回看）；刷新恢复的标签集里不在列表的会话剪掉，
// 无存续标签（或全被剪空）时首屏打开最新一条。尽力而为，
// 失败从空开始。存续标签页的历史由快照补齐（实时事件走全局流）。
export async function loadRuns() {
  try {
    const order = await fetchSummaries()
    if (!order?.length) return
    // 存续会话标签页里在列表的保留（服务端重启丢了空会话等则剪掉；文件
    // 标签页防御性保留——启动恢复时本就没有）；全剪空（列表换代等）退回
    // 首屏开最新一条
    const liveTabs = state.tabs.filter((t) => t.kind !== 'session' || !!state.runs[t.runId])
    if (liveTabs.some((t) => t.kind === 'session')) {
      // 剪枝后记忆失效（记住的会话被剪掉）时换记末位会话标签页
      const remembered = liveTabs.some((t) => tabState.tabKey(t) === state.lastSessionKey)
      const fallbackKey = tabState.tabKey(liveTabs[liveTabs.length - 1])
      const patch = { tabs: liveTabs }
      if (!remembered) patch.lastSessionKey = fallbackKey
      if (!liveTabs.some((t) => tabState.tabKey(t) === state.activeKey)) patch.activeKey = fallbackKey
      set(patch)
      refreshOpenSnapshots()
    } else {
      // 无存续会话标签页或全被剪空：回到首屏开最新一条
      set({ tabs: [], activeKey: null, lastSessionKey: null })
      selectRun(order[0])
    }
  } catch {
    // 历史加载失败不打断使用：界面从空会话开始
  }
}

// 摘要 → run 的合并（loadRuns 与轮询共用同一形状）
function mergeSummary(run, s) {
  const session = {
    ...run,
    firstPrompt: s.first_prompt,
    resumedFrom: s.resumed_from,
    startedAt: s.started_at * 1000,
  }
  return mergeSessionSummary(session, {
    status: s.status,
    stage: s.stage,
    title: s.title ?? null,
    endedAt: s.ended_at ? s.ended_at * 1000 : null,
    lastEventAt: s.last_event_at ? s.last_event_at * 1000 : null,
  })
}

// 摘要轮询：驱动非查看中标签页的状态点与排序（全局流只覆盖打开的标签
// 页，他人会话或重启新会话只有列表最知道）。轻字段覆盖，不动 events。
setInterval(() => pollSummaries(), 5000)

async function pollSummaries() {
  try {
    await fetchSummaries()
  } catch {
    // 轮询失败静默：SSE 在的标签页不受影响，下个周期再试
  }
}

// 新会话落位（新建/Fork 共用）：run 注册、标签页尾插并切为查看中，再拉一次
// 快照补齐开卷事件与转录历史（广播帧可能先于 run 落位到达被守卫丢弃，
// 快照才是历史的确定入口；Last-Event-ID= 已有最大 seq，去重吸收重叠）
function adoptNewRun(run) {
  set({ runs: { ...state.runs, [run.runId]: run }, order: [run.runId, ...state.order], submitError: null })
  applyTabState(tabState.openSession(state.tabs, state.activeKey, run.runId))
  loadSnapshot(run.runId)
}

// 新建 = 一步创建空会话（READY），无中间表单；新建不受其他会话执行影响
export async function createRun() {
  try {
    const data = await postJson('/api/runs', {})
    adoptNewRun(
      makeRun({
        runId: data.run_id,
        status: data.status,
        resumedFrom: data.resumed_from ?? null,
        startedAt: Date.now(),
      })
    )
  } catch (err) {
    fail(`新建会话失败：${conflictMessage(err)}`)
  }
}

// Fork = 从控制面会话（READY/ENDED）分叉新会话：事件流转录、标题继承
// （转录历史经 adoptNewRun 的快照补齐——转录不带 session.started）
export async function cloneRun() {
  const src = state.runs[controlRunId()]
  if (!src) return
  try {
    const data = await postJson(`/api/runs/${src.runId}/clone`, {})
    adoptNewRun(
      makeRun({
        runId: data.run_id,
        status: data.status,
        resumedFrom: data.resumed_from ?? null,
        startedAt: Date.now(),
      })
    )
  } catch (err) {
    fail(`Fork 失败：${conflictMessage(err)}`)
  }
}

// 停止：打断控制面会话的当前回合（只作用它，不误停别人）
export async function stop() {
  const run = state.runs[controlRunId()]
  if (!run || run.status !== RUNNING) return
  try {
    await postJson(`/api/runs/${run.runId}/stop`, {})
  } catch (err) {
    fail(`停止失败：${err.message}`)
  }
}

// 向控制面会话发指令：执行中发送由服务端 409（turn_in_progress）拒绝，
// 想改方向先显式停止。返回是否投递成功（失败时输入由调用方保留）。
export async function send(text) {
  const trimmed = (text ?? '').trim()
  const run = state.runs[controlRunId()]
  if (!run || !trimmed) return false
  if (!isOperable(run.status)) {
    // 只读会话（已结束）不静默吞掉输入，给出出路提示
    fail('该会话只读（已结束）——「+ 新建」或 Fork 此会话后继续')
    return false
  }
  try {
    const data = await postJson(`/api/runs/${run.runId}/messages`, { text: trimmed })
    setRun(run.runId, { status: data.status })
    return true
  } catch (err) {
    fail(`发送失败：${conflictMessage(err)}`)
    return false
  }
}

// 结束控制面会话（显式、不可逆；执行中或挂起均可）
export async function endRun() {
  const run = state.runs[controlRunId()]
  if (!run || !isOperable(run.status)) return
  try {
    await postJson(`/api/runs/${run.runId}/end`, {})
  } catch (err) {
    fail(`结束会话失败：${err.message}`)
  }
}

// ---------- 标签页动作（决策归 tabState 纯模块，store 只当状态容器） ----------

// tabState 结果并入状态；激活的是会话标签页时记住它（控制面记忆——
// 之后激活文件标签页不换对象）
function applyTabState({ tabs, activeKey }) {
  const active = tabs.find((t) => tabState.tabKey(t) === activeKey)
  const patch = { tabs, activeKey }
  if (active?.kind === 'session') patch.lastSessionKey = activeKey
  set(patch)
}

// 激活标签页（点击标签 / 列表行），不产生服务端动作
export function activateTab(key) {
  const t = state.tabs.find((x) => tabState.tabKey(x) === key)
  if (!t) return
  const patch = { activeKey: key }
  if (t.kind === 'session') patch.lastSessionKey = key
  set(patch)
}

// 打开（或激活既有）会话标签页并拉快照补历史：不创建会话、不产生服务端
// 动作（loadRuns 首屏恢复与列表/标签点击共用）；已开着的标签页不重拉
export function selectRun(runId) {
  const run = state.runs[runId]
  if (!run) return
  const existed = openRunIds().has(runId)
  applyTabState(tabState.openSession(state.tabs, state.activeKey, runId))
  if (!existed) loadSnapshot(runId)
}

// 关闭标签页 = 只关视图：会话标签页的历史留在内存（会话仍在列表可重开，
// 重开时快照按 Last-Event-ID 只补缺口），文件标签页内容缓存保留
export function closeTab(key) {
  const prevTabs = state.tabs
  applyTabState(tabState.closeTab(state.tabs, state.activeKey, key))
  if (state.tabs === prevTabs) return // 拦截：没有标签被关
  // 关掉的是记住的会话标签页且回退目标不是会话（记忆悬空）→ 换记末位
  if (state.lastSessionKey === key && !state.tabs.some((t) => tabState.tabKey(t) === state.lastSessionKey)) {
    const last = state.tabs.filter((t) => t.kind === 'session').at(-1)
    if (last) set({ lastSessionKey: tabState.tabKey(last) })
  }
}

// ---------- 产物 ----------

// 清单组 → 组内全部文件的根前缀相对路径（勾选/下载的寻址形态）
export function groupRelPaths(group) {
  return group.files.map((f) => (group.dir ? `${group.dir}/${f.name}` : f.name))
}

// 勾选集剪枝：清单刷新后消失的文件移出勾选（否则 zip 请求会带上已
// 不存在的路径——服务端会跳过，但计数与按钮文案先骗了人）
function pruneSelection(groups) {
  const listed = new Set()
  for (const g of groups) for (const p of groupRelPaths(g)) listed.add(p)
  const next = {}
  for (const p of Object.keys(state.artifactSel)) if (listed.has(p)) next[p] = true
  return next
}

// 清单刷新：阶段推进/终态事件触发（无 run 参数，全局镜像）
export async function refreshArtifacts() {
  try {
    const resp = await fetch('/api/artifacts')
    if (!resp.ok) return
    const data = await resp.json()
    set({ artifacts: data, artifactSel: pruneSelection(data.groups) })
  } catch {
    // 清单刷新是尽力而为：失败不打断会话观察，下次阶段事件再试
  }
}

// 勾选单个产物（行内复选框）
export function toggleArtifactSel(relPath) {
  const next = { ...state.artifactSel }
  if (next[relPath]) delete next[relPath]
  else next[relPath] = true
  set({ artifactSel: next })
}

// 批量勾选/取消一组路径（组头全选、卡头全选用）
export function setArtifactSel(relPaths, on) {
  const next = { ...state.artifactSel }
  for (const p of relPaths) {
    if (on) next[p] = true
    else delete next[p]
  }
  set({ artifactSel: next })
}

export function clearArtifactSel() {
  set({ artifactSel: {} })
}

// ---------- 输入草稿 ----------

// 每枚会话标签页独立草稿（runId → 文本），切标签页不丢输入中的字；发送
// 成功后由调用方清空。入 state 容器：受控输入的字必须驱动重渲染，否则
// 下一次外来渲染（轮询/SSE/时长针）会用旧 value 把 DOM 里的字冲掉
const drafts = {}

export function draftOf(runId) {
  return drafts[runId] ?? ''
}

export function setDraft(runId, text) {
  drafts[runId] = text
  set({ drafts: { ...drafts } })
}

export function clearDraft(runId) {
  delete drafts[runId]
  set({ drafts: { ...drafts } })
}

// ---------- 产物文件标签页 ----------

// 打开产物文件标签页：已有则只激活；新则插当前激活标签页右侧并按需拉取
// 内容进多槽缓存（relPath → 条目+content）。二进制产物（清单带 binary
// 标记，如 rpms/ 下的 .rpm 包）不拉内容，占位视图元信息来自清单条目。
// relPath 形如 "rpm/nginx/1.25.3/nginx-rpm-result.md"；逐段编码（整段
// encode 会把 / 也编码）。内容缓存与标签页独立——关标签页不清缓存，
// 重开瞬开。
export async function openArtifact(relPath, entry) {
  if (entry?.binary) {
    const cut = relPath.lastIndexOf('/')
    set({
      artifactCache: {
        ...state.artifactCache,
        [relPath]: {
          dir: cut > 0 ? relPath.slice(0, cut) : '',
          name: entry.name,
          stage: entry.stage ?? null,
          size: entry.size ?? null,
          binary: true,
        },
      },
    })
  } else if (!state.artifactCache[relPath]) {
    try {
      const resp = await fetch(`/api/artifacts/file/${relPath.split('/').map(encodeURIComponent).join('/')}`)
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok) {
        fail(`打开产物失败：${data.detail || `HTTP ${resp.status}`}`)
        return
      }
      set({ artifactCache: { ...state.artifactCache, [relPath]: data } })
    } catch (err) {
      fail(`打开产物失败：${err.message}`)
      return
    }
  }
  applyTabState(tabState.openFile(state.tabs, state.activeKey, relPath, entry?.name))
}

// 单文件下载：服务端带附件头，临时 <a> 触发浏览器下载（不离开当前页）
export function downloadArtifact(relPath) {
  const a = document.createElement('a')
  a.href = `/api/artifacts/download/${relPath.split('/').map(encodeURIComponent).join('/')}`
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// 批量下载：勾选集 POST 到 zip 端点，blob 经 objectURL 触发下载；文件名
// 取服务端 Content-Disposition（auto-image-artifacts-<n>-<时间戳>.zip）
export async function downloadArtifactZip() {
  const paths = Object.keys(state.artifactSel)
  if (!paths.length || state.artifactZipping) return
  set({ artifactZipping: true })
  try {
    const resp = await fetch('/api/artifacts/zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    })
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}))
      fail(`打包下载失败：${data.detail || `HTTP ${resp.status}`}`)
      return
    }
    const disposition = resp.headers.get('Content-Disposition') || ''
    const match = disposition.match(/filename="?([^";]+)"?/)
    const url = URL.createObjectURL(await resp.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = match ? match[1] : 'auto-image-artifacts.zip'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (err) {
    fail(`打包下载失败：${err.message}`)
  } finally {
    set({ artifactZipping: false })
  }
}

// 启动即恢复任务列表（含服务重启后经 transcript 重建的历史）与产物清单
// （loadRuns 无历史时提前 return，产物首刷不能依赖它）
loadRuns()
refreshArtifacts()
