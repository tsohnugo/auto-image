# web — 部署会话 Web 服务端

浏览器会话式入口：新建空会话 → 输入部署指令 → SSE 实时看 agent 事件流。
事件通道两条：全局流（`GET /api/stream`，一条连接广播全部会话实时事件、
常驻心跳保活）+ per-run 快照（`GET /api/runs/{run_id}/events`，按
Last-Event-ID 重放历史、重放完即断）。
多会话并行（并发上限 `WEB_MAX_PARALLEL_RUNS`，默认 10，数执行中回合——
新建、Fork、标题生成不占名额）；SDK 连接按回合开合，挂起会话零 CLI 进程。
会话三态 READY / RUNNING / ENDED：回合完成、停止、失败都回 READY 可续聊；
ENDED 只来自用户显式结束（不可续聊只能 Fork，墓碑入册）。执行中发送 409
`turn_in_progress`（想改方向先显式停止）；Fork READY/ENDED 源会建立独立
目标身份，并以 `resume=源身份 + fork_session=true` 生成独立 transcript。
会话为 `ClaudeSDKClient` 真实现（`web/sdk.py` 经工厂注入）；测试注入脚本化
假实现（`web/fake.py`），不触网、不启动真 SDK。

## 运行（本机直跑，单进程单 worker）

```bash
# Ubuntu 24.04 起 pip 受 PEP 668 管控，二选一：
pip install --break-system-packages -r web/requirements.txt   # 装进系统（本机现状）
python3 -m venv .venv && . .venv/bin/activate \
  && pip install -r web/requirements.txt                      # 或 venv 隔离
python -m web            # 默认 127.0.0.1:8123
WEB_PORT=8765 python -m web             # 换端口
WEB_HOST=0.0.0.0 WEB_PORT=8123 python -m web   # 外部可访问（见下）
WEB_STATE_PATH=/tmp/x.json python -m web       # 簿记隔离（同 HOME 多实例并行）
```

默认只监听 127.0.0.1（无认证服务，能访问即能触发真实云操作）。
需要外部机器的浏览器访问时，`WEB_HOST=0.0.0.0` 绑定全部网卡，
经 `http://<本机IP>:<端口>/` 访问——暴露面由运行者的网络策略
（安全组/防火墙）控制，风险自担。

前端两种打开方式：

- 生产形态：`cd web-ui && npm run build` 后直接访问 `http://127.0.0.1:8123/`（FastAPI 挂载 `web-ui/dist`）；
- 开发形态：`cd web-ui && npm run dev` 后访问 `http://127.0.0.1:5173/`（`/api` 由 Vite 代理到 FastAPI 的 8123 端口）。

## 前端交互边界

- 主区是一层混合标签栏，会话与产物文件同栏；左侧「会话 | 产物」只负责
  导航。侧栏会话按最后活动降序，打开的标签保持自己的工作区次序。
- header 与底部操作条绑定最后激活的会话。激活文件标签页时，输入框折叠为
  会话作用对象行，需先点回会话再输入；若该会话正在执行，「■ 停止」仍可用。
- 快照与全局实时流都按 per-run `seq` 寻址、排序、去重并重算派生态；摘要
  只补尚未加载的事实，不能用旧状态覆盖更新事件。
- RPM 等二进制产物在 Markdown 解析前分流，只显示元数据、不可预览说明与
  原始文件下载；Markdown 与 JSON 仍按文本方式显示。

## 并行运行约定（产物冲突，人工规避）

产物目录 `deploy/{{software}}/{{version}}` 全实例共享、无服务端隔离（指令
文本里的软件/版本表述不可穷尽，服务端拦截必漏且给「会拦」的错觉）。并行
会话时的约定（真部署实测，同软件同版本并行 7 个产物文件全量覆盖）：

- **同软件 + 同版本不要同时运行**：后写入者覆盖先写入者（指南、meta、
  结果、清单全部），以最后落盘内容为准，无合并。
- 同目录并行时 verify 读 output_dir 全目录，会看到对方中途落盘的文件；
  交叉读取的结论只对各自的机器安装有效，产物归属已乱。
- 同一目标机器 / SSH alias / ECS / 镜像任务不要交叉使用——两路对同一台
  机器并发安装会产生半成品系统。
- 前端「正在跑：…」RUNNING 标题提示是唯一防线，新建会话时肉眼避开。

## 测试（主缝：HTTP 进、SSE 出）

```bash
python web/tests/test_api.py        # ASGI 主缝（假会话驱动）
python web/tests/test_artifacts.py  # 产物端点（临时目录造桩）
python web/tests/test_events.py     # 事件存储、快照与全局订阅
python web/tests/test_fixture_isolation.py # 通用 fixture 的生产依赖哨兵
python web/tests/test_history.py    # 列表摘要、可续聊约束、假 transcript 驱动的重启重放
python web/tests/test_normalize.py  # 消息映射与阶段推导纯函数断言
python web/tests/test_redact.py     # 事件出口脱敏（形状正则 + 已知值清单）
python web/tests/test_sdk.py        # options 契约（身份、Fork、系统提示词、无值守写权限）
python web/tests/test_state.py      # 身份映射、墓碑与 Fork 来源簿记
python web/tests/test_title.py      # 标题生成（prompt/清洗/一次性会话/幂等/写回）
python web/tests/test_transcript_times.py # transcript 时刻读取
cd web-ui && npm test && npm run build    # 前端完整测试与生产构建
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 应用工厂、API 路由（含 `GET /api/runs` 列表）、SSE 通道两条（全局流常驻广播 + per-run 快照：id=seq、Last-Event-ID 重放、重放完即断）、启动接线（重放恢复 + 残留 CLI 告警） |
| `runs.py` | 会话状态机（READY/RUNNING/ENDED 三态、无全局门禁）、回合计数（`WEB_MAX_PARALLEL_RUNS`）、Fork（内部 `clone` 路由）/end 校验与 409 判定收敛（turn_in_progress / session_running / parallel_limit_reached / session_not_active） |
| `events.py` | 进程内事件存储：seq 递增、快照重放、全局订阅唤醒 |
| `session.py` | 回合执行（send 起回合级 asyncio.Task，SDK 连接只包住一个回合；停止意图覆盖连接建立前与 query 前的启动窗口） |
| `normalize.py` | SDK 消息 → 内部事件映射、阶段推导 |
| `artifacts.py` | deploy/ + rpm/ 多根全量产物浏览（目录分组 + 最新落盘排序，约定文件带阶段徽标）、内容读取、单文件下载与批量 zip、路径约束 |
| `redact.py` | 事件出口脱敏（运行时已知值清单 + AK/SK、密码字段、私钥块形状正则） |
| `rebuild.py` | 服务重启后的恢复（单一流程）：全量 transcript 按 session 粒度重放 + state 簿记叠加——身份映射命中的沿用原 run_id，墓碑会话标 ENDED；其余重放会话 READY 可续聊，未收尾回合（transcript 推导 turn_open）补 `turn.interrupted` 不伪造完成 |
| `state.py` | 恢复簿记（`~/.auto-image-web/state.json`，全量原子替换）：墓碑（用户 ENDED 的 session_id 集合）+ 身份映射（run_id ↔ session_id）+ Fork 来源镜像（session_id → 来源 run_id），仅此三样（stage/title/first_prompt 从 transcript 重放推导）；损坏降级为空簿记重放，不阻断启动 |
| `title.py` | 会话标题 LLM 生成（Codex 同构，research/codex-session-title.md）：首条指令到达即起一次性无工具会话生成，成功落 run.title + `session.title_changed` 事件 + transcript custom-title 行；失败静默维持截断标题；Fork 会话继承源标题不再生成 |
| `sdk.py` | ClaudeSDKClient 生产实现：目标身份/上下文来源/Fork 启动意图、options 全配、消息形状适配、工厂、历史读取包装 |
| `fake.py` | 脚本化假会话（默认剧本含敏感样例），测试注入用 |

## SDK 与浏览器实测记录（claude-agent-sdk 0.2.144 + CLI 2.1.220）

接入真实现时逐项实测的结论，均为实际运行观察、非文档推断：

1. **消息形状**：`receive_response` 产出 dataclass（`AssistantMessage` 等），
   `sdk.to_dict` 适配成 CLI JSON 形状 dict 后进 `normalize_message`，
   与假剧本同一条映射路径。Assistant / Result / partial 直接携带的
   `session_id` 及 System init 内的同名字段都会保留，供回合尽早确认身份；
   每回合终止于 `ResultMessage`。SDK 本身允许同连接继续 `query`，Web 则在
   回合收尾后关闭连接，下一回合以自身 `session_id` 新建 resume 连接。
2. **子 agent thinking 转发**（方案风险点一）：`forward_subagent_text=True`
   下子 agent 的 thinking 块**会**随文本一并转发（parent_tool_use_id 非空的
   assistant 消息里实测出现 ThinkingBlock），子 agent 思维链在前端可见。
3. **子 agent 工具名**：CLI 现名 `Agent`（system init 的工具注册表里仍可见
   旧名 `Task`）。阶段推导两者都接受（`normalize.SUBAGENT_TOOL_NAMES`），
   真实 `Agent` 调用带四类 subagent_type 时已实测发出 `stage.changed`。
4. **tools 必须显式给 `claude_code` 预设**：SDK 不配 `tools` 时 CLI 基础
   工具集不含子 agent 工具（agent 自查工具目录无 Task/Agent），四阶段
   流水线无从推进——`--tools default` 后才有。
5. **interrupt 行为**：回合执行中 `interrupt()` 后消息流**自然终止**
   （`receive_response` 迭代器结束），尾随一条 `subtype="error_during_execution"`、
   `is_error=True`、`result=None` 的 Result（后接的 UserMessage 为被中断
   工具的错误 tool_result，照常走 tool_finished 映射）；同一会话随后
   `query` 续聊正常，打断前的上下文保留。服务端以自己的 `stop_requested`
   标记区分 `turn.stopped` 与 `turn.completed`，不解析该 Result 的文案。
6. **联网链路**（方案风险点三）：SDK 会话内内置 `WebFetch` 被域名安全校验
   拦截（"Unable to verify if domain ... is safe to fetch"）、`WebSearch`
   在权限层被拒——与仓库 CLAUDE.md 记录一致。已按方案经 options 的
   `mcp_servers` 接入既有 exa MCP，并因非交互会话无人批准 MCP 工具而
   配 `allowed_tools=["mcp__exa-search__*"]` 放行；复测抓取 nginx.org
   成功。内置工具中只读 Bash 随 `claude_code` 预设放行；写路径需
   `permission_mode`（见第 8 条）。
7. **回合上限**：`max_turns=200` 是终局语义：Result 的错误 subtype（兜底
   判定：只有 `success` 是正常完成）以 `turn.failed` 收尾、会话回 READY
   可续聊（重试 = 下一条指令新连接）。**无 wall-clock 超时**（曾有 3600 秒
   上限，已删）：单回合即完整部署流水线，四阶段串行 + 云操作轮询（IMS 制
   镜像）可超小时级，服务端主动掐断会把已提交的云操作留在中间态。

8. **无值守会话的写权限**（真部署实测）：`claude_code` 工具预设只放行
   只读 Bash，Write 与 Bash 写路径一律被权限系统拦截（guide 只能把指南
   全文以文本返回）——options 必须配 `permission_mode="bypassPermissions"`。
   信任边界由运行形态承担：只监听 127.0.0.1 + 系统提示词任务边界。
9. **凭据值的两层脱敏**（真部署实测）：形状正则防不住自然语言内联
   （`` password `pcb…@@` ``、`password is set (…)` 出现在 thinking），
   redact 层除形状外还维护运行时已知值清单（启动时从 scope.yaml 登记
   ak/sk/password 值，任意上下文整值遮蔽）；实测华为云 SK 为 38 位大小写
   混合，非注释曾以为的 40 位小写。
10. **子 agent 的异步派发失稳**（真部署实测）：CLI 的 Agent 工具支持异步
   启动，模型可能派发后结束回合「等通知」——通知无处投递、回合提前收尾；更早一轮还出现过并发派发上百次 guide 的调度风暴（20 实例
   触发 429，agent 自行终止后恢复）。系统提示词以执行纪律约束：至多一个
   子 agent 在跑、派发后 TaskOutput 阻塞等待、四阶段完成才收尾回合。
11. **interrupt 的终止边界**（干预语义实测，脚本经 `web.sdk` 工厂走生产路径）：
   回合执行中的本地 Bash 子进程**随打断被终止**（实测 `sleep 222` 在
   interrupt 后即刻消失），CLI 子进程保留、连接可续聊。已提交的云操作
   （HTTP API 类：创建 ECS、制镜像等）不受任何影响——打断只作用于后续
   动作，界面在 `turn.stopped` 块与停止按钮上如实提示「已提交的云操作
   不受停止影响，无法撤销」。
12. **cancel（断连）的终止边界**：回合执行中直接断开 SDK 连接（= run_task
   取消后 `__aexit__` 的路径）后 3 秒内：CLI 子进程**全部退出、无残留**
   （配合服务重启语义中的 pgrep 告警兜底），正在执行的本地 Bash 子进程
   同样被终止（实测 `sleep 333` 消失）。远程命令经 ssh 转发：客户端进程
   被杀断开连接，远端进程是否终止取决于远端 shell 配置，**不保证**——
   按「已提交的云操作不可撤销」对待。断连后以 `resume=session_id` 新建
   会话实测可续接，上下文完整（能复述被打断前的指令）。
13. **重启重放的 transcript 形状**（历史列表实测，本机 119 条真实会话、
   全量重放约 2 秒）：`get_session_messages` 只回可见的 user/assistant 链
   （isMeta / isSidechain 已滤），user 行 content 可为字符串（含 CLI 命令
   包装）或块列表（tool_result 回填），无 Result 消息——回合边界由「下一
   条真实用户输入」推导、回合汇总取该回合最后一条 agent 文本；重放的
   run 状态 READY（可续聊可 Fork），流无终态收尾事件、快照重放完即断
   等待续聊。`list_sessions(directory=项目根)` 的 first_prompt 即任务名来源。
14. **服务重启的恢复**（state 簿记 + 真 SDK 实测）：簿记只存墓碑（用户
   ENDED 的 session_id）、身份映射（run_id ↔ session_id）与 Fork 来源镜像
   （session_id → 来源 run_id），每次状态变更即全量原子写。首回合一经接受
   便在异步任务启动和首次落盘前预分配 UUID；从未接受回合、没有 transcript
   的空会话仍不恢复。重启后全量
   transcript 重放：映射命中的以原 run_id 恢复可聊——send 起的回合以
   自身 session resume 新连接（实测恢复后发消息，agent 记得重启前的
   约定）；墓碑会话保持 ENDED 不复活。未收尾回合（末回合无下一条输入
   收口）补 `turn.interrupted` 不自动重跑（已提交的云操作不可重复执行）。
   簿记损坏/缺失一律降级为无墓碑无映射的重放，不阻断启动。kill -9 实测：
   崩溃窗口内丢失的最后一次状态变更由 transcript 存在性校验兜底（读不到
   即丢弃）。
15. **按回合开合的连接生命周期**（真部署并行实测，两路 nginx/redis 全
   流水线 + 调研回合）：
   - **每回合 CLI 启动开销 5-7 秒**（指令发出 → 首条 assistant 响应，
     transcript 时间戳实测；含 CLI 冷启动 + exa MCP stdio 握手），续聊
     回合同量级——按回合开合没有摊薄启动的复用红利，也换来回合间零
     进程；秒级成本对分钟级部署流水线可忽略。
   - **挂起会话零 CLI 进程**：全实例无 RUNNING 回合时，web 进程名下
     CLI 子进程为 0（ps 归属实测）；执行中每回合恰一个 CLI 主进程 +
     一个回合级 exa MCP（npm exec）子进程，标题生成的一次性会话约 8
     秒即退、不常驻。
   - **异常回合后新连接续聊完整**：kill -9 服务截断的回合，重启重放呈
     `turn.interrupted`，随后 resume 同 session 发消息实测可续接（agent
     记得截断前在做什么）；interrupt 停止后的回合续聊同样完整。回合异
     常不污染 session 身份——身份在首回合接受时预分配，并由最早携带
     session_id 的 SDK 消息确认；不一致会让回合失败而不会改写目标身份。
   - **并行资源形态**：双部署回合并行 = 两个独立 CLI 进程树（互不共享
     MCP/连接），内存开销随执行中回合线性增长，并发上限即资源护栏。
16. **Fork 的 transcript 归属**（受控真实 SDK 复验）：生产 adapter 以
    `session_id=目标`、`resume=源`、`fork_session=true` 建立首个 Fork 回合。
    源 SID `9e27cff8-fa01-46f9-97a6-a683a733badc` 与 Fork SID
    `10925496-3941-4bf8-8076-32ad8d13915a` 对应两份不同 JSONL；两边共享
    分叉前的 BASE 指令，分叉后源只含 SOURCE_ONLY/RESTART_SOURCE，Fork
    只含 FORK_ONLY/FORK_FOLLOW/RESTART_FORK。Fork 后续回合和服务重启后
    都只 resume 自身 SID；结束源会话再重启，源保持 ENDED、Fork 保持 READY，
    两个原 run_id 均稳定且没有 `run_hist_*` 副本。
17. **SDK 启动窗口内立即停止**（浏览器 + slow-enter adapter）：发送成功后
    在 `__aenter__` 尚未放行时停止，放开连接后 `query_calls=0`、
    `mock_cloud_actions=0`、`interrupt_calls=0`。事件恰为
    `session.started → turn.started → user.message → turn.stopped`，会话回
    READY，下一条指令正常完成；这证明停止意图由回合握手消费，而非依赖
    当时是否已有可中断连接。已提交的云操作不可撤销边界不变。
18. **快照与实时流收敛**（真实浏览器）：tail→旧 snapshot 与
    snapshot→tail 两种到达顺序都严格收敛为 seq `[1,2,3,4,5]`，断开全局
    SSE 后由快照补齐至 `[1..9]`，重复为 0；状态与服务端一致。客户端按
    per-run seq 寻址、排序、去重并重算派生态，快照游标取最大已见 seq。
19. **二进制产物**（真实浏览器）：无 `content` 的 RPM 在 Markdown 解析前
    分流，显示文件名、`12.0 KB`、不支持在线预览说明与下载动作；下载得到
    12,292 bytes，魔数 `ed ab ee db`。Markdown 与 JSON 视图保持原行为。

- CLI stderr 对本环境网关模型名报 `[claude-code:unrecognized_model]`
  警告，不影响会话执行，服务日志如实记录。
- `include_partial_messages=True` 带来大量 partial/system 消息
  （`thinking_tokens` 估算等），`to_dict` 只保留其中可用的 session_id；无
  `type` 的结果仍不进事件流。
