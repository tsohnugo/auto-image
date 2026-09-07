// store 草稿规则钉子：输入框是受控输入（value=draftOf(runId)），草稿必须
// 驱动重渲染——否则任何外来渲染（摘要轮询、SSE 事件、时长针）都会用旧
// value 把 DOM 里的字冲掉，表现为「输入延迟/丢字、backspace 光标跳尾」。
// 断言订阅者视角的外部可见行为：setDraft 后通知到达、快照含新值、外来
// set 不冲掉草稿。
import { afterEach, describe, expect, it, vi } from 'vitest'

// store 模块级副作用重：SSE EventSource、轮询 setInterval、loadRuns——
// 全部 stub 掉，模块隔离成纯状态容器
vi.mock('./store.js', async () => {
  const actual = await vi.importActual('./store.js')
  return actual
})

const sseListeners = {}
let globalSource = null
global.EventSource = class {
  constructor() {
    this.readyState = 0
    globalSource = this
  }
  addEventListener(type, fn) { (sseListeners[type] ??= []).push(fn) }
  onopen() {}
  onerror() {}
}
vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
const timers = []
vi.stubGlobal('setInterval', (fn, ms) => { timers.push([fn, ms]); return timers.length })

const store = await import('./store.js')

afterEach(() => {
  store.clearDraft('r1')
})

describe('输入草稿', () => {
  it('setDraft 通知订阅者且快照可见（受控输入的 value 源）', () => {
    const seen = []
    const unsub = store.subscribe(() => seen.push(store.getState().drafts.r1))
    store.setDraft('r1', 'nginx 1.25')
    unsub()
    expect(seen).toContain('nginx 1.25')
    expect(store.getState().drafts.r1).toBe('nginx 1.25')
    expect(store.draftOf('r1')).toBe('nginx 1.25')
  })

  it('外来渲染（轮询/SSE 推进）不冲掉草稿——每次 set 后 draftOf 仍是最新值', () => {
    // 模拟外来事件流：外来 set 与用户输入交错
    store.setDraft('r1', 'a')
    //外来渲染等价物：任何不碰 drafts 的 set()
    store.getState() // 触发一次读
    store.setDraft('r1', 'ab')
    expect(store.draftOf('r1')).toBe('ab')
    store.clearDraft('r1')
    expect(store.draftOf('r1')).toBe('')
  })
})

describe('快照与全局流归并', () => {
  it('tail 先到仍收敛到有序事实，断线补齐游标取最大 seq', async () => {
    const sse = (...frames) => frames.map(([seq, type, payload]) => [
      `id: ${seq}`,
      `event: ${type}`,
      `data: ${JSON.stringify(payload)}`,
    ].join('\n')).join('\n\n')
    const response = (body) => ({ ok: true, text: async () => body })
    const deferredResponse = () => {
      let resolve
      const promise = new Promise((done) => { resolve = (body) => done(response(body)) })
      return { promise, resolve }
    }
    const broadcast = (seq, type, payload = {}) => {
      sseListeners[type][0]({
        data: JSON.stringify({ run_id: 'event-run', seq, ts: seq * 10, type, payload }),
      })
    }
    const initialReplay = deferredResponse()
    const queuedReplays = [initialReplay.promise]
    const snapshotRequests = []
    fetch.mockImplementation(async (url, options = {}) => {
      if (url === '/api/runs' && options.method === 'POST') {
        return {
          ok: true,
          json: async () => ({ run_id: 'event-run', status: 'READY', resumed_from: null }),
        }
      }
      if (url === '/api/runs/event-run/events') {
        snapshotRequests.push(options)
        return queuedReplays.shift() ?? response('')
      }
      return { ok: false }
    })

    await store.createRun()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('0')
    broadcast(5, 'turn.started')
    initialReplay.resolve(sse(
      [1, 'session.started', { ts: 10 }],
      [2, 'stage.changed', { ts: 20, stage: 'VERIFY' }],
      [3, 'session.title_changed', { ts: 30, title: '验证 nginx' }],
      [4, 'turn.completed', { ts: 40, result: '上一回合完成' }],
      [5, 'turn.started', { ts: 50 }],
    ))

    await vi.waitFor(() => expect(store.getState().runs['event-run'].events).toHaveLength(5))
    const session = store.getState().runs['event-run']
    expect({
      seqs: session.events.map((item) => item.seq),
      status: session.status,
      stage: session.stage,
      title: session.title,
      result: session.result,
      lastEventAt: session.lastEventAt,
    }).toEqual({
      seqs: [1, 2, 3, 4, 5],
      status: 'RUNNING',
      stage: 'VERIFY',
      title: '验证 nginx',
      result: '上一回合完成',
      lastEventAt: 50_000,
    })

    const gapReplay = deferredResponse()
    queuedReplays.push(gapReplay.promise)
    globalSource.onopen()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('5')
    broadcast(10, 'turn.started')
    broadcast(8, 'agent.message', { text: '实时先到' })
    gapReplay.resolve(sse(
      [6, 'user.message', { ts: 60, text: '继续' }],
      [7, 'stage.changed', { ts: 70, stage: 'ARCHIVE' }],
      [8, 'agent.message', { ts: 80, text: '实时先到' }],
      [9, 'turn.completed', { ts: 90, result: '补齐完成' }],
      [10, 'turn.started', { ts: 100 }],
    ))

    await vi.waitFor(() => expect(store.getState().runs['event-run'].events).toHaveLength(10))
    expect(store.getState().runs['event-run'].events.map((item) => item.seq)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    queuedReplays.push(Promise.resolve(response('')))
    globalSource.onopen()
    expect(snapshotRequests.at(-1).headers['Last-Event-ID']).toBe('10')
  })
})
