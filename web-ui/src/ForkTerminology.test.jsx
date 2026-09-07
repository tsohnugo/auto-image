import { readFileSync } from 'node:fs'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fixture = vi.hoisted(() => ({ status: 'READY' }))

vi.mock('./store.js', () => ({
  useRunState: () => ({
    tabs: [{ kind: 'session', runId: 'run_1' }],
    activeKey: 'session:run_1',
  }),
  useControlRun: () => ({
    runId: 'run_1',
    status: fixture.status,
    firstPrompt: '部署 nginx',
    title: null,
    events: [],
  }),
  draftOf: () => '',
  isOperable: (status) => status === 'READY' || status === 'RUNNING',
  send: vi.fn(),
  stop: vi.fn(),
  cloneRun: vi.fn(),
  clearDraft: vi.fn(),
  setDraft: vi.fn(),
  activateTab: vi.fn(),
}))

import ChatBar from './components/ChatBar.jsx'

beforeEach(() => {
  fixture.status = 'READY'
})

describe('Fork 术语', () => {
  it('操作条在 READY、RUNNING、ENDED 都只用 Fork 表达分叉动作', () => {
    for (const status of ['READY', 'RUNNING', 'ENDED']) {
      fixture.status = status
      const html = renderToStaticMarkup(<ChatBar />)
      expect(html).toContain('Fork')
      expect(html).not.toContain('克隆')
    }
  })

  it('按钮、错误提示、来源标记与 ENDED 出路不再保留旧称', () => {
    for (const file of ['App.jsx', 'components/ChatBar.jsx', 'derive.js', 'store.js']) {
      const source = readFileSync(new URL(file, import.meta.url), 'utf-8')
      expect(source, file).not.toContain('克隆')
    }
  })
})
