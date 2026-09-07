// tabState 纯模块规则钉子：开-关-激活-控制面-落盘过滤全部在这里冻结，
// UI 切片押在这块地基上。只断言外部可见行为（开了什么、关后激活谁、
// 控制面是谁、落盘剩什么），不断言内部实现。
import { describe, expect, it } from 'vitest'
import { closeTab, controlRunId, openFile, openSession, persistableTabs, tabKey } from './tabState'

const s = (runId) => ({ kind: 'session', runId })
const f = (relPath, name) => ({ kind: 'file', relPath, name })
const keys = (tabs) => tabs.map(tabKey)

describe('tabKey', () => {
  it('会话标签页 key 为 session:<runId>，文件标签页为 file:<relPath>', () => {
    expect(tabKey(s('run_1'))).toBe('session:run_1')
    expect(tabKey(f('deploy/nginx-install.md', 'nginx-install.md'))).toBe('file:deploy/nginx-install.md')
  })
})

describe('openSession', () => {
  it('已有该会话标签页：不重插，只激活', () => {
    const tabs = [s('run_1'), s('run_2')]
    const r = openSession(tabs, 'session:run_2', 'run_1')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'session:run_2'])
    expect(r.activeKey).toBe('session:run_1')
  })

  it('新会话标签页尾插并激活（不管当前激活的是什么）', () => {
    const tabs = [s('run_1'), f('deploy/nginx-install.md', 'nginx-install.md'), s('run_2')]
    const r = openSession(tabs, 'file:deploy/nginx-install.md', 'run_9')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'file:deploy/nginx-install.md', 'session:run_2', 'session:run_9'])
    expect(r.activeKey).toBe('session:run_9')
  })

  it('不修改入参数组', () => {
    const tabs = [s('run_1')]
    openSession(tabs, 'session:run_1', 'run_2')
    expect(keys(tabs)).toEqual(['session:run_1'])
  })
})

describe('openFile', () => {
  it('已有该文件标签页：不重插，只激活', () => {
    const tabs = [s('run_1'), f('deploy/nginx-install.md', 'nginx-install.md'), s('run_2')]
    const r = openFile(tabs, 'session:run_1', 'deploy/nginx-install.md', 'nginx-install.md')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'file:deploy/nginx-install.md', 'session:run_2'])
    expect(r.activeKey).toBe('file:deploy/nginx-install.md')
  })

  it('新文件标签页插当前激活标签页右侧并激活', () => {
    const tabs = [s('run_1'), s('run_2'), s('run_3')]
    const r = openFile(tabs, 'session:run_2', 'deploy/nginx-verify.md', 'nginx-verify.md')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'session:run_2', 'file:deploy/nginx-verify.md', 'session:run_3'])
    expect(r.activeKey).toBe('file:deploy/nginx-verify.md')
  })

  it('name 缺省取 relPath 末段', () => {
    const r = openFile([s('run_1')], 'session:run_1', 'rpm/nginx/1.25.3/result.md')
    expect(r.tabs[1].name).toBe('result.md')
  })

  it('无激活标签页（tabs 非空、activeKey 未命中）时尾插', () => {
    const r = openFile([s('run_1')], null, 'deploy/a.md', 'a.md')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'file:deploy/a.md'])
    expect(r.activeKey).toBe('file:deploy/a.md')
  })

  it('不修改入参数组', () => {
    const tabs = [s('run_1')]
    openFile(tabs, 'session:run_1', 'deploy/a.md', 'a.md')
    expect(keys(tabs)).toEqual(['session:run_1'])
  })
})

describe('closeTab', () => {
  it('最后一枚会话标签页：拦截，原状态返回', () => {
    const tabs = [s('run_1'), f('deploy/a.md', 'a.md')]
    // 先关掉文件，只剩一枚会话
    const r1 = closeTab(tabs, 'file:deploy/a.md', 'file:deploy/a.md')
    const r2 = closeTab(r1.tabs, r1.activeKey, 'session:run_1')
    expect(keys(r2.tabs)).toEqual(['session:run_1'])
    expect(r2.activeKey).toBe('session:run_1')
  })

  it('关激活的文件标签页 → 激活左邻', () => {
    const tabs = [s('run_1'), s('run_2'), f('deploy/a.md', 'a.md'), s('run_3')]
    const r = closeTab(tabs, 'file:deploy/a.md', 'file:deploy/a.md')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'session:run_2', 'session:run_3'])
    expect(r.activeKey).toBe('session:run_2')
  })

  it('关激活的文件标签页（激活在首位，无左邻）→ 激活右邻', () => {
    const tabs = [f('deploy/a.md', 'a.md'), s('run_1')]
    const r = closeTab(tabs, 'file:deploy/a.md', 'file:deploy/a.md')
    expect(r.activeKey).toBe('session:run_1')
  })

  it('关激活的会话标签页 → 激活右邻', () => {
    const tabs = [s('run_1'), s('run_2'), s('run_3')]
    const r = closeTab(tabs, 'session:run_2', 'session:run_2')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'session:run_3'])
    expect(r.activeKey).toBe('session:run_3')
  })

  it('关激活的会话标签页：右邻是文件标签页也照激活它', () => {
    const tabs = [s('run_1'), s('run_2'), f('deploy/a.md', 'a.md')]
    const r = closeTab(tabs, 'session:run_2', 'session:run_2')
    expect(keys(r.tabs)).toEqual(['session:run_1', 'file:deploy/a.md'])
    expect(r.activeKey).toBe('file:deploy/a.md')
  })

  it('关激活的末位会话标签页（无右邻）→ 激活左邻', () => {
    const tabs = [s('run_1'), s('run_2')]
    const r = closeTab(tabs, 'session:run_2', 'session:run_2')
    expect(keys(r.tabs)).toEqual(['session:run_1'])
    expect(r.activeKey).toBe('session:run_1')
  })

  it('多枚文件全关后激活仍在的会话标签页，不落空', () => {
    let tabs = [s('run_1'), f('deploy/a.md', 'a.md'), f('deploy/b.md', 'b.md')]
    let activeKey = 'file:deploy/a.md'
    for (const k of ['file:deploy/a.md', 'file:deploy/b.md']) {
      const r = closeTab(tabs, activeKey, k)
      tabs = r.tabs
      activeKey = r.activeKey
    }
    expect(keys(tabs)).toEqual(['session:run_1'])
    expect(activeKey).toBe('session:run_1')
  })

  it('关非激活标签页：激活态不动', () => {
    const tabs = [s('run_1'), s('run_2')]
    const r = closeTab(tabs, 'session:run_2', 'session:run_1')
    expect(keys(r.tabs)).toEqual(['session:run_2'])
    expect(r.activeKey).toBe('session:run_2')
  })

  it('key 不存在：原状态返回', () => {
    const tabs = [s('run_1')]
    const r = closeTab(tabs, 'session:run_1', 'session:run_x')
    expect(keys(r.tabs)).toEqual(['session:run_1'])
    expect(r.activeKey).toBe('session:run_1')
  })
})

describe('controlRunId', () => {
  it('激活的是会话标签页 → 它', () => {
    const tabs = [s('run_1'), s('run_2')]
    expect(controlRunId(tabs, 'session:run_2', 'session:run_1')).toBe('run_2')
  })

  it('激活的是文件标签页 → 最后激活的会话标签页（记住的那枚，非序列末位）', () => {
    const tabs = [s('run_1'), s('run_2'), f('deploy/a.md', 'a.md')]
    // 开着 run_2 时激活过 run_1 再去看文件：控制面仍是 run_1
    expect(controlRunId(tabs, 'file:deploy/a.md', 'session:run_1')).toBe('run_1')
  })

  it('无激活（空）→ 最后激活的会话标签页', () => {
    const tabs = [s('run_1'), s('run_2')]
    expect(controlRunId(tabs, null, 'session:run_2')).toBe('run_2')
  })

  it('记忆 key 指向已不存在的标签页（防御）→ null', () => {
    const tabs = [s('run_1'), f('deploy/a.md', 'a.md')]
    expect(controlRunId(tabs, 'file:deploy/a.md', 'session:run_x')).toBe(null)
  })

  it('无会话标签页 → null', () => {
    expect(controlRunId([], null, null)).toBe(null)
  })
})

describe('persistableTabs', () => {
  it('file: 前缀被滤掉，只剩会话标签页次序', () => {
    const tabs = [s('run_1'), f('deploy/a.md', 'a.md'), s('run_2')]
    const r = persistableTabs(tabs, 'session:run_2', 'session:run_2')
    expect(r.openTabs).toEqual(['run_1', 'run_2'])
  })

  it('激活的是会话标签页：viewRun 是它', () => {
    const tabs = [s('run_1'), s('run_2')]
    expect(persistableTabs(tabs, 'session:run_2', 'session:run_2').viewRunId).toBe('run_2')
  })

  it('激活的是文件标签页：viewRun 落到最后激活的会话标签页', () => {
    const tabs = [s('run_1'), s('run_2'), f('deploy/a.md', 'a.md')]
    expect(persistableTabs(tabs, 'file:deploy/a.md', 'session:run_1').viewRunId).toBe('run_1')
  })

  it('记忆 key 失效（防御）：viewRun 回落数组首枚会话标签页', () => {
    const tabs = [s('run_1'), s('run_2'), f('deploy/a.md', 'a.md')]
    expect(persistableTabs(tabs, 'file:deploy/a.md', 'session:run_x').viewRunId).toBe('run_1')
  })

  it('无会话标签页：viewRun 为 null', () => {
    expect(persistableTabs([], null, null)).toEqual({ openTabs: [], viewRunId: null })
  })
})
