export const SESSION_STATUS = Object.freeze({
  RUNNING: 'RUNNING',
  READY: 'READY',
  ENDED: 'ENDED',
})

const { RUNNING, READY, ENDED } = SESSION_STATUS

const READY_EVENTS = new Set([
  'turn.completed',
  'turn.stopped',
  'turn.failed',
  'turn.interrupted',
])

function orderedUniqueEvents(existing, incoming) {
  const bySeq = new Map()
  for (const event of existing) {
    if (!bySeq.has(event.seq)) bySeq.set(event.seq, event)
  }
  for (const event of incoming) {
    // seq 对应不可变事件；两路冲突时保留已经落地的事实。
    if (!bySeq.has(event.seq)) bySeq.set(event.seq, event)
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
}

function latestLoadedEventAt(events) {
  let latest = null
  for (const event of events) {
    const timestamp = event.payload?.ts
    if (typeof timestamp !== 'number' || !Number.isFinite(timestamp)) continue
    latest = Math.max(latest ?? -Infinity, timestamp * 1000)
  }
  return latest
}

/**
 * 合并单个会话的事件事实，并从完整有序事件统一派生展示状态。
 * 摘要字段只在尚未见到对应事件时作为初值。
 */
export function mergeSessionEvents(session, incoming = []) {
  const events = orderedUniqueEvents(session.events ?? [], incoming)
  let status = session.status
  let stage = session.stage
  let title = session.title
  let result = session.result
  let endedAt = session.endedAt
  let lastEventAt = session.lastEventAt

  for (const event of events) {
    const { type, payload = {} } = event
    if (type === 'stage.changed') stage = payload.stage
    if (type === 'session.title_changed') title = payload.title
    if (type === 'turn.completed') result = payload.result
    if (type === 'turn.started') status = RUNNING
    if (READY_EVENTS.has(type)) status = READY
    if (type === 'session.ended') {
      status = ENDED
      if (payload.ts != null) endedAt = payload.ts * 1000
    }
    if (payload.ts != null) lastEventAt = payload.ts * 1000
  }

  // 墓碑会话的历史可能没有 session.ended；摘要的 ENDED 必须保持单向。
  if (session.status === ENDED) status = ENDED

  return {
    ...session,
    events,
    status,
    stage,
    title,
    result,
    endedAt,
    lastEventAt,
    maxSeq: events.reduce((max, event) => Math.max(max, event.seq), 0),
  }
}

/**
 * 把服务端摘要并入已缓存事件。摘要的最后活动晚于已加载事件，说明对应的
 * 新事件尚未加载，此时摘要暂时提供权威展示值；若本地事件更新，则事件
 * 派生值优先，避免在途旧摘要覆盖实时 tail。ENDED 在两路间都保持单向。
 */
export function mergeSessionSummary(session, summary) {
  const merged = mergeSessionEvents(session)
  const loadedEventAt = latestLoadedEventAt(merged.events)
  const summaryCoversUnloadedEvents =
    merged.events.length === 0 ||
    (summary.lastEventAt != null &&
      (loadedEventAt == null || summary.lastEventAt >= loadedEventAt))
  const withFreshestFacts = summaryCoversUnloadedEvents
    ? {
        ...merged,
        status: summary.status,
        stage: summary.stage,
        title: summary.title,
        endedAt: summary.endedAt,
        lastEventAt: summary.lastEventAt,
      }
    : merged

  if (session.status === ENDED || merged.status === ENDED || summary.status === ENDED) {
    return { ...withFreshestFacts, status: ENDED }
  }
  return withFreshestFacts
}
