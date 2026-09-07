// 左侧侧栏：顶部小 tab「会话 | 产物」切两块面板（默认产物，切过之后
// localStorage 记住选择）。会话面板 = 全部会话仪表盘（含 ENDED 与重启
// 恢复的历史，服务端最后活跃降序平铺），点行开成（或激活既有）会话
// 标签页——历史会话由此第一次可达。产物面板 = 原产物卡内容原样迁入
// （工具行/复选框/zip/单文件下载/默认展开最新组），点文件开成（或激活
// 既有）文件标签页——多槽内容缓存，消息流不再被顶走。数据零新增请求：
// 会话列表即摘要轮询已拉的全量，产物即清单刷新。
import { useState } from 'react'
import * as store from '../store.js'
import { tabKey } from '../tabState.js'
import StageBadge from './StageBadge.jsx'
import {
  RUN_STATUS_LABEL, STAGE_LABEL, firstPromptPreview, lastActivityAt, tabDot, fmtAgo,
  fmtSize, artifactTree, subtreeRels, defaultOpenPaths, artifactFileCount,
} from '../derive.js'

// 会话列表行：状态点 + 标题（LLM 标题优先/首条指令截断回退/空会话占位）+
// 右侧相对时间；第二行小字状态中文与当前阶段（ENDED 只显「已结束」，
// 不再显阶段）。当前控制面会话高亮；已开标签页的会话弱标记（标题前小点）。
function SessionRow({ run, on, open }) {
  const title = firstPromptPreview(run, 40)
  const sub =
    run.status === 'ENDED'
      ? RUN_STATUS_LABEL.ENDED
      : [RUN_STATUS_LABEL[run.status], run.stage && (STAGE_LABEL[run.stage] ?? run.stage)]
          .filter(Boolean)
          .join(' · ')
  return (
    <div
      className={`va-sess-row${on ? ' on' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => store.selectRun(run.runId)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          store.selectRun(run.runId)
        }
      }}
      title={title}
    >
      <span className={`va-tab-dot ${tabDot(run)}`} />
      <div className="va-sess-main">
        <div className="va-sess-title">
          {open && <span className="va-sess-open" title="已开为标签页" />}
          <span className="va-sess-name">{title}</span>
          <span className="va-sess-ago">{fmtAgo(lastActivityAt(run))}</span>
        </div>
        <div className="va-sess-sub">{sub}</div>
      </div>
    </div>
  )
}

// 会话面板：全部会话按服务端序（order 即最后活跃降序）平铺，不加搜索/
// 分组/排序控件——列表保持简单，最近的总在最上。「已开标签页」标记从
// tabs 派生，不另立状态
function SessionPanel({ order, runs, controlId, openIds }) {
  return (
    <div className="va-side-panel">
      {order.map((id) => {
        const run = runs[id]
        if (!run) return null
        return (
          <SessionRow
            key={id}
            run={run}
            on={id === controlId}
            open={openIds.has(id)}
          />
        )
      })}
      {order.length === 0 && <div className="va-side-empty">暂无会话——点标签栏「+ 新建」</div>}
    </div>
  )
}

// 目录树节点：目录行（箭头 + 三态勾选 + 目录名 + 子树文件数）+ 本目录文件
// 行 + 子目录递归（缩进 + 竖参考线）。目录行勾选作用于子树全部文件（三态：
// 全选 / 部分半选 / 无）；展开状态由父级 toggles 字典集中管理，未动过的
// 目录落到 defaultOpen（最新一组所在路径自动展开）。文件行「已开标签页」
// 弱标记与高亮从 tabs 派生，不另立状态。
function ArtDir({ node, toggles, setToggles, defaultOpen, openFiles, activeRel }) {
  const s = store.useRunState()
  const open = toggles[node.path] ?? defaultOpen.has(node.path)
  const rels = subtreeRels(node)
  const selCount = rels.reduce((n, p) => n + (s.artifactSel[p] ? 1 : 0), 0)
  const allOn = rels.length > 0 && selCount === rels.length
  const some = selCount > 0 && !allOn
  const toggleOpen = () => setToggles({ ...toggles, [node.path]: !open })
  return (
    <div className="va-art-group">
      <div
        className="va-art-dir-head"
        role="button"
        tabIndex={0}
        onClick={toggleOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleOpen()
          }
        }}
        title={node.path}
      >
        <span className="va-art-dir-arrow">{open ? '▾' : '▸'}</span>
        <input
          type="checkbox"
          className="va-art-check"
          checked={allOn}
          ref={(el) => {
            if (el) el.indeterminate = some
          }}
          onChange={() => store.setArtifactSel(rels, !allOn)}
          onClick={(e) => e.stopPropagation()}
          title="勾选本目录（含子目录）全部文件"
        />
        <span className="va-art-name">{node.name}</span>
        <span className="va-art-count" title="本目录（含子目录）文件数">{node.count}</span>
      </div>
      {open && (
        <div className="va-art-children">
          {node.files.map((f) => {
            const rel = `${node.path}/${f.name}`
            const opened = openFiles.has(rel)
            return (
              <div
                key={f.name}
                role="button"
                tabIndex={0}
                className={`va-art-item${activeRel === rel ? ' on' : ''}`}
                onClick={() => store.openArtifact(rel, f)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    store.openArtifact(rel, f)
                  }
                }}
                title={rel}
              >
                <input
                  type="checkbox"
                  className="va-art-check"
                  checked={!!s.artifactSel[rel]}
                  onChange={() => store.toggleArtifactSel(rel)}
                  onClick={(e) => e.stopPropagation()}
                />
                {f.stage ? <StageBadge stage={f.stage} /> : null}
                <span className="va-art-name">{f.name}</span>
                {opened && <span className="va-art-opened" title="已打开为标签页" />}
                <span className="va-art-size">{fmtSize(f.size)}</span>
                <button
                  className="va-art-dl"
                  title="下载此文件"
                  onClick={(e) => {
                    e.stopPropagation()
                    store.downloadArtifact(rel)
                  }}
                >
                  ⤓
                </button>
              </div>
            )
          })}
          {node.dirs.map((d) => (
            <ArtDir key={d.path} node={d} toggles={toggles} setToggles={setToggles} defaultOpen={defaultOpen} openFiles={openFiles} activeRel={activeRel} />
          ))}
        </div>
      )}
    </div>
  )
}

// 产物面板：deploy/ + rpm/ 全量镜像，目录树形态（服务端平铺分组派生成嵌套
// 树，子目录按最新落盘在前），点击文件开成（或激活既有）文件标签页。约定
// 命名的带阶段徽标，非约定的（.v1 备份、杂项）无徽标平铺。每行可勾选
// （目录行/面板头可整棵子树全选），单文件 ⤓ 下载、勾选集一键打包 zip 下载。
// 行与目录头是 div 而非 button：内部还嵌复选框与下载按钮，交互件不嵌套。
function ArtifactPanel({ openFiles, activeRel }) {
  const s = store.useRunState()
  const groups = s.artifacts.groups
  const [toggles, setToggles] = useState({})
  const roots = artifactTree(groups)
  const fileCount = artifactFileCount(groups)
  const selCount = Object.keys(s.artifactSel).length
  return (
    <div className="va-side-panel">
      <div className="va-art-panel-head">产物 · {fileCount}</div>
      {fileCount > 0 && (
        <div className="va-art-tools">
          <button onClick={() => store.setArtifactSel(roots.flatMap(subtreeRels), true)}>
            全选
          </button>
          <button onClick={() => store.clearArtifactSel()} disabled={selCount === 0}>
            清空
          </button>
          <button
            className="va-art-zip"
            onClick={() => store.downloadArtifactZip()}
            disabled={selCount === 0 || s.artifactZipping}
            title="勾选的产物打包成一个 zip 下载"
          >
            {s.artifactZipping ? '打包中…' : `下载 zip${selCount ? ` (${selCount})` : ''}`}
          </button>
        </div>
      )}
      {roots.length === 0 && <div className="va-side-empty">deploy/ · rpm/ 下暂无产物</div>}
      {roots.map((r) => (
        <ArtDir key={r.path} node={r} toggles={toggles} setToggles={setToggles} defaultOpen={defaultOpenPaths(groups)} openFiles={openFiles} activeRel={activeRel} />
      ))}
    </div>
  )
}

// 侧栏本体：pin 开合钮在 App 内（骑缝移动），本组件只承载两面板与切换。
// 面板选择持久化 localStorage——刷新后仍是切过的面板（首次默认产物）。
// 两面板的「当前对象」标记都从 tabs 派生：会话面板高亮控制面会话，
// 产物面板高亮激活的文件标签页（弱标记则覆盖全部已开文件）。
const SIDE_PANEL_KEY = 'va-side-panel'
function readPanel() {
  try {
    return localStorage.getItem(SIDE_PANEL_KEY) === 'sessions' ? 'sessions' : 'artifacts'
  } catch {
    return 'artifacts'
  }
}

export default function SidePanel() {
  const s = store.useRunState()
  const [panel, setPanel] = useState(readPanel)
  const openIds = new Set(s.tabs.filter((t) => t.kind === 'session').map((t) => t.runId))
  const openFiles = new Set(s.tabs.filter((t) => t.kind === 'file').map((t) => t.relPath))
  const activeTab = s.tabs.find((t) => tabKey(t) === s.activeKey)
  const switchPanel = (p) => {
    setPanel(p)
    try {
      localStorage.setItem(SIDE_PANEL_KEY, p)
    } catch {
      // 存储不可用（隐私模式等）：只丢面板选择存活，不影响使用
    }
  }
  return (
    <aside className="va-side" id="task-side">
      <div className="va-side-tabs" role="tablist" aria-label="侧栏面板">
        <button
          role="tab"
          aria-selected={panel === 'sessions'}
          aria-controls="va-side-panel"
          className={panel === 'sessions' ? 'on' : ''}
          onClick={() => switchPanel('sessions')}
        >
          会话
        </button>
        <button
          role="tab"
          aria-selected={panel === 'artifacts'}
          aria-controls="va-side-panel"
          className={panel === 'artifacts' ? 'on' : ''}
          onClick={() => switchPanel('artifacts')}
        >
          产物
        </button>
      </div>
      <div className="va-side-panel-wrap" id="va-side-panel" role="tabpanel">
        {panel === 'sessions' ? (
          <SessionPanel order={s.order} runs={s.runs} controlId={store.controlRunId()} openIds={openIds} />
        ) : (
          <ArtifactPanel openFiles={openFiles} activeRel={activeTab?.kind === 'file' ? activeTab.relPath : null} />
        )}
      </div>
    </aside>
  )
}
