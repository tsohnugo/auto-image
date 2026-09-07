// 底部常驻对话输入条（控制面之一）：作用于最后激活的会话标签页。左缘
// 常显作用对象胶囊（状态点 + mono 任务名）；激活文件标签页时输入折叠为
// 作用对象行——点它切回该会话标签页再输入（作用对象先确认，就着产物
// 清单也不会把指令误发进另一个挂起会话），执行中回合的「■ 停止」在
// 折叠态仍在手边。右侧单按钮按状态变形，回车永远等价于点它：
//   执行中   -> ■ 停止（stop 自带会话判定，只停控制面会话；输入同步
//               禁用，想改方向先停止——执行中发送被服务端 409
//               turn_in_progress 拒）
//   等待指令 -> 发送
// 「⑂ Fork」常驻：READY/ENDED 可点、RUNNING 禁用——Fork 基于 transcript
// 续接，执行中的上下文是过时的（服务端 409 同源）。新建会话入口在标签栏
// 「+ 新建」。草稿按 runId 独立存 map，切标签页不丢字。
import * as store from '../store.js'
import { firstPromptPreview, tabDot } from '../derive.js'
import { tabKey } from '../tabState.js'

export default function ChatBar() {
  const s = store.useRunState()
  const run = store.useControlRun()
  const runId = run?.runId ?? null
  const text = store.draftOf(runId)
  const activeTab = s.tabs.find((t) => tabKey(t) === s.activeKey) ?? null
  // 激活的就是会话标签页时作用对象显而易见（胶囊窄形态常显左缘）；
  // 激活文件标签页时输入折叠为宽形态胶囊——先确认作用对象再输入
  const targetOn = activeTab?.kind === 'session'

  // 输入只由控制面会话状态决定（并行不受其他会话执行影响）；
  // 执行中禁用输入（无排队错觉），停止是显式按钮
  const canInputHere = !!run && store.isOperable(run.status) && run.status !== 'RUNNING'
  const isTerminal = !!run && !store.isOperable(run.status)

  const send = async () => {
    if (!text.trim()) return
    if (await store.send(text)) store.clearDraft(runId)
  }
  // 停止只打断后续动作：已提交的云操作不可撤销（与消息流提示一致）
  const act = () => (run?.status === 'RUNNING' ? store.stop() : send())

  let btnLabel, btnDisabled
  if (run?.status === 'RUNNING') {
    btnLabel = '■ 停止'
    btnDisabled = false
  } else if (canInputHere) {
    btnLabel = '发送 ⏎'
    btnDisabled = !text.trim()
  } else {
    btnLabel = isTerminal ? '会话已结束' : '无会话'
    btnDisabled = true
  }

  const placeholder = run
    ? isTerminal
      ? '会话已结束——点「⑂ Fork」基于此上下文创建新分支'
      : run.status === 'RUNNING'
        ? '回合执行中——想改方向点「■ 停止」打断后再输入'
        : '输入部署指令：软件 + 文档链接 + 目标机器…'
    : '点标签栏「+ 新建」开始一个部署会话'

  // 作用对象胶囊（窄 = 常显标识；宽 = 折叠态的输入位替身），点击切回
  // 该会话标签页
  const target = (wide) => (
    <button
      className={`chat-target${wide ? ' wide' : ''}`}
      onClick={() => runId && store.activateTab(tabKey({ kind: 'session', runId }))}
      title={`输入发送到此会话 · ${runId}${targetOn ? '' : '（点击切回它的标签页）'}`}
    >
      <span className={`va-tab-dot ${tabDot(run)}`} />
      <span className="chat-target-name">
        {wide ? `输入将作用于「${firstPromptPreview(run, 24)}」——点击切回会话` : firstPromptPreview(run)}
      </span>
    </button>
  )

  const forkBtn = (
    <button
      className="chat-continue"
      onClick={() => store.cloneRun()}
      disabled={!run || run.status === 'RUNNING'}
      title={
        run?.status === 'RUNNING'
          ? '回合执行中不能 Fork（上下文仍在变化）——回合结束后可用'
          : `从『${firstPromptPreview(run)}』的上下文创建独立 Fork`
      }
    >
      ⑂ Fork
    </button>
  )

  // 折叠态（激活的是文件标签页）：输入位换成作用对象行；「■ 停止」在
  // 执行中保留——看产物时停止仍在手边
  if (run && !targetOn) {
    return (
      <div className="chat-bar">
        {target(true)}
        {forkBtn}
        {run.status === 'RUNNING' && (
          <button className="chat-send" onClick={act}>
            {btnLabel}
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="chat-bar">
      {run && target()}
      {forkBtn}
      <input
        value={text}
        disabled={!canInputHere}
        placeholder={placeholder}
        title={run?.status === 'RUNNING' ? '回合执行中不能输入——想改方向点「■ 停止」' : undefined}
        onChange={(e) => store.setDraft(runId, e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && !btnDisabled && act()}
      />
      <button className="chat-send" onClick={act} disabled={btnDisabled}>
        {btnLabel}
      </button>
    </div>
  )
}
