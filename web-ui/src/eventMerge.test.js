import { describe, expect, it } from 'vitest'
import { mergeSessionEvents, mergeSessionSummary } from './eventMerge.js'

const event = (seq, type, payload = {}) => ({
  seq,
  type,
  payload: { ts: seq * 10, ...payload },
})

const initialSession = () => ({
  status: 'READY',
  stage: null,
  title: null,
  result: null,
  endedAt: null,
  lastEventAt: null,
  events: [],
  maxSeq: 0,
})

const observable = (session) => ({
  seqs: session.events.map((item) => item.seq),
  status: session.status,
  stage: session.stage,
  title: session.title,
  result: session.result,
  endedAt: session.endedAt,
  lastEventAt: session.lastEventAt,
  maxSeq: session.maxSeq,
})

const completeSnapshot = () => [
  event(1, 'session.started'),
  event(2, 'stage.changed', { stage: 'VERIFY' }),
  event(3, 'session.title_changed', { title: '验证 nginx' }),
  event(4, 'turn.completed', { result: '上一回合完成' }),
  event(5, 'turn.started'),
]

describe('mergeSessionEvents', () => {
  it('tail 先于快照到达时仍按 seq 去重、排序并重算会话状态', () => {
    const snapshot = completeSnapshot()

    let session = mergeSessionEvents(initialSession(), [snapshot[4]])
    session = mergeSessionEvents(session, snapshot)

    expect(observable(session)).toEqual({
      seqs: [1, 2, 3, 4, 5],
      status: 'RUNNING',
      stage: 'VERIFY',
      title: '验证 nginx',
      result: '上一回合完成',
      endedAt: null,
      lastEventAt: 50_000,
      maxSeq: 5,
    })
  })

  it('快照先于 tail 且 tail 重复时得到完全相同的事件事实与派生字段', () => {
    const snapshot = completeSnapshot()

    let tailFirst = mergeSessionEvents(initialSession(), [snapshot[4]])
    tailFirst = mergeSessionEvents(tailFirst, snapshot)
    let snapshotFirst = mergeSessionEvents(initialSession(), snapshot)
    snapshotFirst = mergeSessionEvents(snapshotFirst, [snapshot[4]])

    expect(observable(snapshotFirst)).toEqual(observable(tailFirst))
  })

  it('断线 tail 有空洞且乱序时以最大 seq 为游标，快照补齐后无重复无空洞', () => {
    const snapshot = completeSnapshot()
    const later = event(6, 'agent.message', { text: '继续处理中' })

    let session = mergeSessionEvents(initialSession(), [later, snapshot[2], snapshot[4]])
    expect(session.maxSeq).toBe(6)

    session = mergeSessionEvents(session, [...snapshot, later])
    expect({ seqs: session.events.map((item) => item.seq), maxSeq: session.maxSeq }).toEqual({
      seqs: [1, 2, 3, 4, 5, 6],
      maxSeq: 6,
    })
  })

  it('摘要给出的 ENDED 在历史事件不含终态收尾时保持不可操作', () => {
    const ended = initialSession()
    ended.status = 'ENDED'
    ended.endedAt = 75_000

    const session = mergeSessionEvents(ended, [event(5, 'turn.started')])

    expect({ status: session.status, endedAt: session.endedAt }).toEqual({
      status: 'ENDED',
      endedAt: 75_000,
    })
  })

  it('摘要补尚未加载的新事实，但不能覆盖更新的实时事件', () => {
    const cached = mergeSessionEvents(initialSession(), [
      event(1, 'session.started'),
      event(2, 'stage.changed', { stage: 'GUIDE' }),
      event(3, 'session.title_changed', { title: '旧标题' }),
      event(4, 'turn.completed', { result: '上一回合完成' }),
    ])
    const summaryAhead = mergeSessionSummary(cached, {
      status: 'RUNNING',
      stage: 'INSTALL',
      title: '最新标题',
      endedAt: null,
      lastEventAt: 50_000,
    })
    const eventAhead = mergeSessionSummary(
      mergeSessionEvents(cached, [event(6, 'turn.started')]),
      {
        status: 'READY',
        stage: 'GUIDE',
        title: '旧标题',
        endedAt: null,
        lastEventAt: 40_000,
      },
    )

    expect({
      summaryAhead: observable(summaryAhead),
      eventAhead: observable(eventAhead),
    }).toEqual({
      summaryAhead: {
        seqs: [1, 2, 3, 4],
        status: 'RUNNING',
        stage: 'INSTALL',
        title: '最新标题',
        result: '上一回合完成',
        endedAt: null,
        lastEventAt: 50_000,
        maxSeq: 4,
      },
      eventAhead: {
        seqs: [1, 2, 3, 4, 6],
        status: 'RUNNING',
        stage: 'GUIDE',
        title: '旧标题',
        result: '上一回合完成',
        endedAt: null,
        lastEventAt: 60_000,
        maxSeq: 6,
      },
    })
  })
})
