// 从会话状态派生展示数据（布局自理），与服务端 first_prompt / title 语义对齐

// 会话状态中文（header 状态、会话列表第二行共用；判定值与服务端状态机一致，
// 单处维护）
export const RUN_STATUS_LABEL = { RUNNING: '执行中', READY: '等待指令', ENDED: '已结束' }
export const STAGE_LABEL = { GUIDE: '生成指南', INSTALL: '远程安装', VERIFY: '只读验证', ARCHIVE: '打包归档', BUILD: 'RPM 构建' }

// 标签页状态词（tab 的 title 与控制面胶囊共用）：状态点形状之外再给文字，
// 状态不只靠形状/颜色传达。判定值与 tabDot 同源。
export const TAB_DOT_LABEL = { running: '执行中', ready: '等待指令', failed: '最近回合失败', ended: '已结束' }
export function tabStatusLabel(run) {
  return TAB_DOT_LABEL[tabDot(run)] ?? ''
}

// 首条指令原文：服务端摘要的 firstPrompt 优先（重启找回的历史在事件回放前就有名字），
// 否则取事件流首条 user.message。空会话（含尚未回放的历史）返回 null。
export function firstPromptText(run) {
  if (!run) return null
  return run.firstPrompt ?? run.events.find((e) => e.type === 'user.message')?.payload.text ?? null
}

// 任务名 = LLM 标题（服务端 title / 事件流 session.title_changed）优先，
// 回退首条指令截断（生成中/失败/老会话）
export function firstPromptPreview(run, max = 18) {
  if (run?.title) return run.title.length > max ? run.title.slice(0, max) + '…' : run.title
  const first = firstPromptText(run)
  if (first == null) return '(空会话)'
  const t = String(first).trim().replace(/\s+/g, ' ')
  return t.length > max ? t.slice(0, max) + '…' : t
}

// Fork 标记：「⑂ Fork 自『任务名』」；来源会话不在（列表缺它）时不标
export function resumeMark(run, runs) {
  if (!run?.resumedFrom || !runs?.[run.resumedFrom]) return ''
  return ` ⑂ Fork 自『${firstPromptPreview(runs[run.resumedFrom])}』`
}

// 累计执行时长：各回合（user.message → 回合收尾）求和，扣除等待输入的
// 空档；执行中的回合以 now 收口。终态（ENDED）与挂起同口径——事件 ts
// 是真实发生时刻（重放/Fork 透传源时刻），求和即定格，无需 wall-clock
// 兜底；终态未闭合的回合（异常边界）不计。
export function activeSeconds(run, now) {
  let total = 0
  let turnStart = null
  for (const ev of run.events) {
    if (ev.type === 'user.message') turnStart = (ev.payload.ts ?? run.startedAt / 1000)
    if ((ev.type === 'turn.completed' || ev.type === 'turn.stopped' || ev.type === 'turn.failed' || ev.type === 'turn.interrupted') && turnStart != null) {
      total += Math.max(0, (ev.payload.ts ?? turnStart) - turnStart)
      turnStart = null
    }
  }
  if (turnStart != null && run.status !== 'ENDED') {
    total += Math.max(0, (now || Date.now()) / 1000 - turnStart)
  }
  return Math.floor(total)
}

export function fmtActive(run, now) {
  if (!run.startedAt) return '--:--'
  const s = activeSeconds(run, now)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const pad = (n) => String(n).padStart(2, '0')
  // 小时档：几十分钟级的长部署（dist-upgrade + 安装 + 验证 + 制镜像）
  // 是常态，mm:ss 顶到 452:10 已不可读
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`
}

// 更新时间（具体日期+时间）：最后一条 user.message 的 ts——会话随用户
// 发消息推进，agent 回复是它的响应；无事件时回退会话创建时刻
export function lastUserMessageAt(run) {
  for (let i = run.events.length - 1; i >= 0; i--) {
    if (run.events[i].type === 'user.message') return (run.events[i].payload.ts ?? 0) * 1000
  }
  return null
}

export function fmtLastActivity(run) {
  const ms = lastUserMessageAt(run) ?? run.startedAt
  if (!ms) return '—'
  const d = new Date(ms)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// 产物大小：B / KB / MB / GB（清单 size 字段的展示形态；rpm 包几十 MB 是
// 常态，KB 一级顶到五位数不可读）
export function fmtSize(bytes) {
  if (bytes == null) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let v = bytes
  let u = 0
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024
    u++
  }
  return u === 0 ? `${v} ${units[u]}` : `${v.toFixed(1)} ${units[u]}`
}

// ---------- 标签栏（多会话视图） ----------

// 最后活动时刻：事件 ts（全局流 / 快照 / 摘要轮询同步进 run）优先，无活动
// 的新会话回退创建时刻——新建因此天然排最前
export function lastActivityAt(run) {
  return run?.lastEventAt ?? run?.startedAt ?? 0
}

// 标签状态点：● 执行中 / ○ 等待指令 / ◆ 最近回合失败 / ■ 已结束。
// 失败标记由事件流倒序判定：最后一条回合开卷/收尾洗掉它，只有落在流尾的
// turn.failed 才标 ◆；未回放的会话（仅轮询摘要，无事件）只剩状态可用。
export function tabDot(run) {
  if (!run) return 'ready'
  if (run.status === 'RUNNING') return 'running'
  if (run.status === 'ENDED') return 'ended'
  for (let i = run.events.length - 1; i >= 0; i--) {
    const t = run.events[i].type
    if (t === 'turn.failed') return 'failed'
    if (t === 'turn.started' || t === 'turn.completed' || t === 'turn.stopped' || t === 'turn.interrupted') return 'ready'
  }
  return 'ready'
}

// 当前执行中的会话数（顶部运行计数：并行负载与并发上限预判）
export function runningCount(runs) {
  return Object.values(runs).filter((r) => r.status === 'RUNNING').length
}

// 相对时间（会话列表右侧）：刚刚 / N 分钟前 / N 小时前 / N 天前；无活动
// 时刻（异常边界）落空串
export function fmtAgo(ms, now = Date.now()) {
  if (!ms) return ''
  const diff = Math.max(0, now - ms)
  const min = Math.floor(diff / 60_000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

// RUNNING 标题提示的会话集：排除控制面会话（正控制着的无需提醒）
export function runningOthers(runs, controlId) {
  return Object.values(runs).filter((r) => r.status === 'RUNNING' && r.runId !== controlId)
}

// ---------- 产物目录树（清单平铺分组 → 嵌套树） ----------

// 服务端清单组键（"rpm/httpd/2.4.57" 形）按 / 拆段逐级挂载：节点.path 即
// 组键（勾选/下载的寻址前缀），files 为该目录自身组内文件，count 为子树
// 文件总数。子目录次序取首见次序——组间已按最新落盘降序，各级天然「最新
// 在前」；返回根层节点（deploy、rpm……），空清单返回 []。
export function artifactTree(groups) {
  const roots = []
  const byPath = new Map()
  for (const g of groups) {
    let node = null
    let path = ''
    for (const seg of g.dir.split('/')) {
      path = path ? `${path}/${seg}` : seg
      let child = byPath.get(path)
      if (!child) {
        child = { name: seg, path, dirs: [], files: [] }
        byPath.set(path, child)
        const holder = node ?? { dirs: roots }
        holder.dirs.push(child)
      }
      node = child
    }
    if (node) node.files = g.files
  }
  const tally = (n) => n.files.length + n.dirs.reduce((sum, d) => sum + tally(d), 0)
  for (const r of roots) r.count = tally(r)
  return roots
}

// 产物文件总数（「产物 · N」的 N，面板头唯一显示位）：清单平铺分组求和
export function artifactFileCount(groups) {
  return groups.reduce((n, g) => n + g.files.length, 0)
}

// 节点子树全部文件的根前缀相对路径（目录行三态勾选 / 卡头全选的勾选集）
export function subtreeRels(node) {
  const out = node.files.map((f) => `${node.path}/${f.name}`)
  for (const d of node.dirs) out.push(...subtreeRels(d))
  return out
}

// 默认展开路径集：最新一组（groups[0]，服务端按落盘时间降序）及其各级
// 祖先——沿用旧平铺形态「默认展开最新一组」的行为，未手动动过的目录才生效
export function defaultOpenPaths(groups) {
  const open = new Set()
  const first = groups[0]?.dir
  if (!first) return open
  let path = ''
  for (const seg of first.split('/')) {
    path = path ? `${path}/${seg}` : seg
    open.add(path)
  }
  return open
}
