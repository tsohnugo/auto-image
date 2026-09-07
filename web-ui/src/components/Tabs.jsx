// 混合标签栏（只压主区上方，不横跨侧栏）：会话标签页（独立状态点 ● 执行中 /
// ○ 等待指令 / ◆ 最近回合失败 / ■ 已结束 + 标题）与文件标签页（文件图标 +
// 文件名，hover 显全路径）同栏混排，整栏一个横向滚动容器——「+ 新建」与
// 计数/提示随标签一起滚（竖向锁定，永不出现竖向滚动条；悬停栏上滚轮
// 直接横滚，见下方原生非被动监听）。点击只切激活（不产生服务端动作）；
// 关闭只关视图（会话仍在列表，重开时快照补历史；文件内容缓存保留）。
// 最后一枚会话标签页的 × 置灰（控制面永远要有对象，title 说明缘由）。
// 运行计数与「正在跑」提示是人工规避同软件同版本并行冲突的唯一防线。
// 方向键沿序列切换，title 带状态词——状态不只靠 8px 形状传达。
import { useEffect, useRef } from 'react'
import * as store from '../store.js'
import { firstPromptPreview, resumeMark, tabDot, tabStatusLabel, runningCount, runningOthers } from '../derive.js'
import { tabKey } from '../tabState.js'

export default function Tabs() {
  const s = store.useRunState()
  const barRef = useRef(null)
  const sessionCount = s.tabs.filter((t) => t.kind === 'session').length
  const running = runningCount(s.runs)
  const others = runningOthers(s.runs, store.controlRunId())

  // 滚轮转横向：竖向锁定后，悬停标签栏的竖向滚轮推动标签序列（两端到头
  // 不拦默认）。React 合成 wheel 是被动监听、preventDefault 无效，须挂
  // 原生非被动。deltaMode 行/页（Firefox）归一成像素。
  useEffect(() => {
    const el = barRef.current
    if (!el) return
    const onWheel = (e) => {
      if (e.deltaY === 0) return
      const d = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * el.clientWidth : e.deltaY
      const max = el.scrollWidth - el.clientWidth
      const next = Math.max(0, Math.min(max, el.scrollLeft + d))
      if (next === el.scrollLeft) return
      e.preventDefault()
      el.scrollLeft = next
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // 方向键沿标签序列移动激活（tablist 的选中跟随焦点）
  const moveTab = (key, dir) => {
    const idx = s.tabs.findIndex((x) => tabKey(x) === key)
    const next = s.tabs[(idx + dir + s.tabs.length) % s.tabs.length]
    if (!next) return
    store.activateTab(tabKey(next))
    document.querySelector(`[data-key="${tabKey(next)}"]`)?.focus()
  }

  return (
    <div className="va-tabsbar" role="tablist" ref={barRef}>
      {s.tabs.map((t) => {
        const key = tabKey(t)
        const on = key === s.activeKey
        const run = t.kind === 'session' ? s.runs[t.runId] : null
        const closeable = t.kind === 'file' || sessionCount > 1
        return (
          <div
            key={key}
            data-key={key}
            className={`va-tab${on ? ' on' : ''}`}
            role="tab"
            aria-selected={on}
            aria-controls="va-tab-body"
            tabIndex={0}
            onClick={() => store.activateTab(key)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                store.activateTab(key)
              }
              if (e.key === 'ArrowRight') {
                e.preventDefault()
                moveTab(key, 1)
              } else if (e.key === 'ArrowLeft') {
                e.preventDefault()
                moveTab(key, -1)
              }
            }}
            title={
              t.kind === 'file'
                ? t.relPath
                : `${tabStatusLabel(run)} · ${firstPromptPreview(run, 60)} · ${t.runId}${resumeMark(run, s.runs)}`
            }
          >
            {run ? (
              <>
                <span className={`va-tab-dot ${tabDot(run)}`} />
                <span className="va-tab-name">{firstPromptPreview(run)}</span>
              </>
            ) : (
              <>
                <span className="va-tab-ico">{s.artifactCache[t.relPath]?.binary ? '📦' : '📄'}</span>
                <span className="va-tab-name">{t.name}</span>
              </>
            )}
            {closeable ? (
              <button
                className="va-tab-close"
                title={t.kind === 'file' ? '关闭文件标签页' : '关闭标签页（不影响会话执行，列表里可重新打开）'}
                aria-label={`关闭 ${t.kind === 'file' ? t.name : firstPromptPreview(run)} 标签页`}
                onClick={(e) => {
                  e.stopPropagation()
                  store.closeTab(key)
                }}
              >
                ×
              </button>
            ) : (
              <button
                className="va-tab-close"
                disabled
                tabIndex={-1}
                title="最后一枚会话标签页不可关闭——输入条（控制面）需要对象"
              >
                ×
              </button>
            )}
          </div>
        )
      })}
      <button
        className="va-tab-new"
        onClick={() => store.createRun()}
        title="新建空会话（与其他会话执行互不影响）"
      >
        + 新建
      </button>
      <span className="va-spacer" />
      {running > 0 && (
        <span className="va-run-count" title="当前执行中的回合数（并发上限内的并行负载）">
          运行中 {running}
        </span>
      )}
      {others.length > 0 && (
        <span
          className="va-run-hint"
          title="正在执行的会话——避免同软件同版本并行（产物目录共享，结果会互相覆盖）"
        >
          正在跑：{others.map((r) => firstPromptPreview(r)).join('、')}
        </span>
      )}
    </div>
  )
}
