# 统一接口 v1

## 2026-10-28 浏览器原始证据与结构化交接

- 保持 `schema_version=1` 和既有 Application/Port/ToolResult 签名。新增公共 DTO 统一在 `core/contracts.py`：BrowserDiagnostic、BrowserPageState、InterfaceContract、HandoffFile、TaskHandoff；不把 Playwright 类型传到上层。
- 浏览器状态新增 `diagnostics`，包含 session_id/document_id、document_ready_state、navigation_status、errors 和 earlier_errors_omitted。`ready=true`只表示会话已打开；DOMContentLoaded/load 不表示业务已成功。主文档导航开始时清空当前文档错误，跨标签分别保留；旧事件不删除。
- 页面创建时被动监听 pageerror、console error、requestfailed、HTTP >=400，保存 `browser.diagnostic` 和 `.clearact/browser/diagnostics.jsonl`。完整消息/stack留原记录，回执每条message最多600字、不携带完整stack、最多8条；内存最多64种当前文档错误。重复错误继续记录全部发生次数，但回执保留首次引用，避免事件编号变化伪装成进展。历史索引最多保留4条观察，明确不是当前页面健康判定。
- BrowserDiagnostic 提供原始 event_id、session/document/sequence、类型、URL、HTTP状态、资源类型和原生console位置（行列为零基），pageerror保留原生stack。结构化URL去掉查询、fragment及userinfo；错误正文和stack保留原文，不承诺全文脱敏。用户手动操作没有所属分支时仅写JSONL。工具异常仍返回原异常，额外附上已捕获diagnostics，无自动检查、注入错误监听、重载或重试。
- `task_plan.contracts=[]` 可选，单条InterfaceContract为 `name/provider_task/signature/consumers=[]/invariants=""`。名称唯一、提供者和消费者引用有效任务ID；不插入依赖、不自动调度。旧interfaces文本与旧计划保持有效。
- `task_checkpoint.interfaces` 使用同一InterfaceContract结构；子任务只能声明自身为提供者。`unresolved` 可选字符串数组。全部保留模型来源和verified=false。交接取本分支最近显式interfaces和unresolved；后续普通/运行时断点不抹掉它们，显式空数组可清除。每项来源断点分别记录坐标；计划回退明确标为interface_source=plan，不能称为实际验证接口。
- `task.handoff` 保存完整TaskHandoff：任务/分支、完整报告run.ended坐标、接口来源、未完成项、断点及存在的证据坐标。此处证据ID有效只表示事件存在，不表示证明成立。compact view把未完成项限制到850字、接口约1300字（单签名400、约束200），证据最多12个，截断标记context_excerpt。报告保留头1000/尾500字，完整原文仍可history_read。父回执保留result/files/working_state等旧键。
- HandoffFile 描述分配文件的最终存在性/字节/哈希，并增加before_sha256/changed；该哈希差异不证明语义正确，不覆盖命令/MCP的全部外部副作用。后续任务接收的文件另附current_sha256/matches_handoff，版本不符即旧报告可能过时。
- 依赖交接保留dependency_handoffs；共享分配文件或contracts消费者另外收到related_handoffs，标明basis，不改变任务依赖和完成状态。每个任务使用当前计划的最近一次结果，避免以旧成功掩盖后续失败。当前版本哈希在交接时读取一次，不添加模型调用或文件轮询。
- `task.assignment.handoff_branches` 明确授予本次交接来源分支的只读history_read访问，覆盖完整报告/stack取回；不允许任意兄弟或父分支读取。主代理本来已有委派子分支访问。历史索引保留交接及原文坐标，前端给新事件提供中文标签；旧历史无这些字段时仍可读取。

## 2026-10-27 可靠性兼容增量

- 后续检查预算补充：RecoveryState增加默认空observations和started_event_id=0；context_view只向模型投影重复观察，完整计数仍持久化。Settings/SettingsRequest新增unchanged_result_limit=4、最小2。blocked_rounds兼容字段同时计入不变观察后的无进展诊断；并非所有计入项都表示实际动作被阻止。语义见[补充说明](changes/2026-10-27-check-budget.md)。

- `ToolResult.error_kind="unknown"`为可选增量，错误类别validation/deterministic/transient/unknown/blocked不等于副作用结论。`tool.started/returned.executed=false`明确本次没有执行。原ToolResult(content,is_error)和HTTP输入保留兼容，schema_version仍为1。
- `core.ToolFact`统一成功调用的有界参数、结果摘录、call_event_id/result_event_id/branch_id和truncated；归档和父子交接复用。截断事实不代替原文，可用history_read取回。
- `core.RecoveryState`统一started_at、失败签名/内容版本/错误/原文坐标、blocked_rounds、输出预算及估算标志、paused/reason。`reliability.updated`持久化完整状态；diagnosing/blocked/paused/resolved/resumed记录状态转换。不新增数据库表或另一套HTTP运行结果。
- `RunResult.status=stopped`且error以`Recovery paused:`开头表示有界补救暂停；区别于用户取消。子任务此状态交回主代理，用户取消仍向上传递。新用户轮次可打开新预算，自动容量续跑及同计划子任务重开保留预算。
- `Settings`和`SettingsRequest`共用默认repeated_failure_limit=2、recovery_blocked_rounds=2、recovery_timeout_seconds=600、recovery_output_tokens=32768。已有保存设置缺字段时使用默认值。前端保留这些设置，尚无专用控件。
- `task_checkpoint`保留state/next_steps必填，新增可选problem/changed_method/success_criterion/resolved/evidence_event_ids。恢复声明只接受问题后、当前断点前的成功执行回执引用；这是模型证据声明，不是独立验收。未知模型输出使用UTF-8字节上界并标估算，RunResult.usage仍保留null。
- `agent.finished.working_state`增加同一成功事实、RecoveryState、最近断点和完整报告坐标。父子工具签名、Application公开方法及ModelProvider不改变；Runner.run仅增加内部可选allow_recovery_resume参数。

以下各日期条目反映当时实现，当前对无变化失败的有界处理以本节及RULES为准。

## 2026-10-23 容量与工作状态修复

- `core.TextRange` 统一文件与历史的字符分页结果。`file_read/history_read` 保留旧参数和结果开头 `characters/offset`，增加 `next_offset/eof`，到末尾或越界时返回 `next_offset=null; eof=true`。坐标仍是字符数，非行号或UTF-8字节数；不自动读取下一页、不拦截重复调用。

- `core.RequestFootprint` 保存 `model_key/tools_hash/message_hashes`。`context.projected.request_footprint` 与 `model.returned.context_event_id/response_message_hash` 关联一次请求和实报用量；哈希不包含密钥，历史记录不修改。
- `context.budget` 优先使用同模型、同工具定义、同完整消息前缀的服务实报输入，加新增消息的保守界限及余量；首个新增 assistant 可用对应实报输出估算。没有匹配或用量未知时使用字节保守界限，未知不填零。一次容量续跑提示消失时保留其已测成本。
- `ContextBuilder` 末尾新增可选 `model_key=""`，旧构造调用兼容。`Settings.tokenizer_key` 是兼容锚点，不是精确 tokenizer。
- `context.compacted` 增加 `index_as_of/retained_message_ids/target_reached/working_set_preserved`。保留最近完整事务和最近成功写入所属完整事务；索引固定到归档时点，同一分支显式文件恢复使索引刷新到恢复事件。没有归档时也向该分支下次请求提供恢复事实；跨子分支恢复对父上下文同步尚未验证。60% 为软目标，必要内容超出总窗口仍停止。
- 文件写入/恢复事件新增可选 `bytes/text_tail`，文本尾部最多1200字；删除恢复的值为 null。完整正文继续在原始消息和快照中保存。
- `PlannedTask.deliverable` 可选，默认空字符串，旧计划可读。`task.assignment` 保存子代理实际分配；子请求增加依赖任务的短报告和文件坐标。计划及子结果增加 `ready_task_ids`，子结果增加 `pending_task_ids/deliverable`；不代表验收通过，不自动执行下一项。
- `file_list` 接受工作区绝对根目录和 `.`；已有工作区及记录目录保护不变。`ModelProvider`、Application 和 HTTP v1 签名未改变，schema_version 仍为 1。

## 2026-10-22 长任务扩展

以下增量保留既有调用兼容性，schema_version 仍为 1；容量估算与工作状态以本页2026-10-23修复说明为准。

- core 新增 PlannedFile / PlannedTask / TaskPlan；文件职责、任务依赖和共享接口使用同一数据定义。ports 新增 DelegationPort.plan/execute。
- 工具共十五项，新增 file_edit、task_plan、agent_task、task_checkpoint。history_read 新增可选 branch_id（仅自己的已委派子代理）；省略 event_id 可分页列事件目录。读取 assistant 工具调用时返回完整参数 JSON。
- file_edit.append 要求 expected_bytes（UTF-8 字节），可选 expected_sha256；replace 要求 old_text 唯一匹配，每次完整写入先做快照。参数不匹配返回错误，不自动重放。
- task_plan 校验 ID、路径及依赖无环；模型决定执行顺序。PlannedTask 新增可选 tools，null 使用默认文件/文档/历史/断点工具，模型可显式指定其他已注册工具。子代理接收完整项目文件职责表，避免复制其他任务的实现。agent_task 当前串行、单层，文件写工具只写分配路径；不是操作系统沙箱。
- 父 RunResult 的 model_calls/usage 包含子代理；任一调用未知则相应用量保持 null。子代理有独立完整历史，父消息只接收短报告、文件元信息和分支坐标。
- Application.branches 隐藏子代理话题；Application.report 新增 children/child_changes，后者保留原 branch_id，页面在原分支上显式恢复。记录下载路由不变。
- Settings / SettingsRequest 新增 auto_compact、compact_threshold、compact_target、auto_continue，默认 true/.9/.6/true。触发占用率 =（保守输入估算 + 输出预留）/ 配置容量；累计 usage 不参与。压缩只归档闭合事务，用户原文不删除；仍用全分支原始消息校验工具 ID 唯一。
- 新事件：context.compacted（截止坐标、前后估算和固定用户消息），capacity.continuation（输出到限后的容量续跑指令），task.plan/checkpoint（模型计划及工作状态），agent.identity/started/progress/finished（父子关系、进度与结果）。
- 历史索引与原 system 合并为请求开头的一条 system，兼容本地模板。索引是事实摘录和坐标，不保证语义完整；原始 system 和原始历史不改写。
- 容量续跑重新请求已加载模型，不执行半截参数，不自动重放已完成动作。普通工具/服务/验收错误不触发此续跑；受原整轮超时和用户停止控制。

## 单一入口与依赖方向

命令行和本地网关只调用 `Application`；`bootstrap.py` 是唯一默认装配点。`core` 不依赖任何具体执行器、模型 SDK、存储或界面。`Settings` 是唯一运行配置结构，HTTP 输入在网关边界校验并转换一次。

| Application 方法 | 输入 | 输出与约束 |
| --- | --- | --- |
| `await run(prompt, branch_id=None, on_event=None, on_delta=None, attachment_ids=None)` | 文本、可选分支、观察者及上传 ID | `RunResult`；新参数在末尾且可选，旧调用保持有效 |
| `stop()` | 无 | bool；取消当前任务，不自动重试工具 |
| `events(branch_id)` | 分支 ID | 该分支可见的完整 `Event` 列表，含祖先截至分叉点的历史 |
| `fork(branch_id=None, at_event=None)` | 可选父分支、可选事件 ID | 无父分支时创建空分支；拒绝在未闭合工具事务处分叉；不改变文件 |
| `branches()` | 无 | `BranchInfo` 列表；从已有事件派生标题与状态，无数据库结构变更 |
| `artifact(branch_id, event_id)` | 登记交付事件 ID | 文件名和原始字节；拒绝下载未登记或内容已变的文件 |
| `restore(branch_id, change_event)` | `file.written` 事件 ID | 显式恢复单文件；内容冲突则拒绝；保留旧历史 |
| `report(branch_id)` | 分支 ID | 版本化字典：运行结果、登记产物和全部事件 |
| `upload(name, content)` | 文件名、原始 bytes | 附件元信息；保存原始 blob、工作副本和哈希，运行中拒绝 |
| `await browser_state/control/image(...)` | 控制参数及可选分支 | 状态、操作结果或 JPEG；与模型共享 BrowserPort，手动动作留痕 |
| `mcp_status()` / `await mcp_connect(...)` | 可选服务名和断开标志 | 脱敏配置、连接状态；不改变运行策略 |
| `await configure_mcp(servers)` | 服务配置映射 | 空闲时替换；旧会话先关闭 |
| `reconfigure(settings, provider)` | 新配置、模型适配器 | 同工作区空闲时更新，保留 MCP/浏览器会话 |
| `await aclose()` | 无 | 同一事件循环关闭浏览器、MCP 和存储；运行未结束时拒绝 |
| `close()` | 无 | 无活动异步资源时兼容原同步关闭；资源已启动则要求使用 aclose |

观察者异常会记录为 `observer.failed`，不会导致工具重放。文本 delta 实时通知，不逐 token 写 SQLite；完整返回存档，正常停止或异常时保存已收集部分。强制结束进程可能丢失尚未落盘的流式片段。

## 公共数据

定义统一位于 `core/contracts.py`，不得在 UI、工具或模型适配器中复制定义。

| 数据 | 含义 |
| --- | --- |
| `Message` | role、正文、工具调用及关联 ID、可选 reasoning_content；原始记录保留 |
| `ToolCall` | id、工具名、JSON 对象参数 |
| `ToolSpec` | name、description、JSON Schema parameters |
| `ToolResult` | content、is_error；所有工具返回同一结构 |
| `ModelReply` | assistant 消息、finish_reason、Usage、原始响应 |
| `Usage` | input_tokens/output_tokens；未上报为 null |
| `Event` | id、branch_id、kind、payload、UTC created_at、schema_version |
| `RunResult` | 分支、运行状态、回答、调用次数、用量、耗时、错误和 schema_version |
| `BranchInfo` | id、parent_id、title、status、updated_at；任务列表使用，不改变消息契约 |
| `PlannedFile / PlannedTask / TaskPlan` | 文件职责、任务依赖、共享接口、可选tools和deliverable；模型保存的统一计划 |
| `RequestFootprint` | model_key、tools_hash、message_hashes；用于匹配实报输入锚点，不是tokenizer |
| `TextRange` | total_characters、offset、next_offset、eof、text；文件/历史统一字符分页结果 |

`finished` 表示模型以正常结束标记结束；其他状态为 `stopped`、`failed`、`context_limit`、`output_limit`。文件登记和任务验收不使用这一状态混合表示。

每次 assistant 工具调用之后必须有对应 tool 消息；ID 非空且在分支消息中唯一。完整事务验证通过后才执行工具。参数错误、未知工具和工具异常以错误结果回传模型，由模型决定下一步；运行时不追加补救提示或自动重发。

## 扩展接口

| Port | 实现约定 |
| --- | --- |
| `ModelProvider.complete(messages, tools, on_delta)` | 异步返回 `ModelReply`；供应商字段只在适配器内转换 |
| `Tool.execute(arguments, context)` | 异步返回 `ToolResult`，工具自带 `ToolSpec`；注册时检查 Schema，调用时校验参数 |
| `EventStore` | append/events/fork/close；保持历史顺序和分叉截点 |
| `SnapshotStore` | write_text/write_bytes/restore；写入前保存原始字节和预览证据；二进制预览仅显示大小变化 |
| `ToolContext` | workspace、branch_id、store、snapshots、emit；工具不直接调用 Runner |
| `AsyncResource` | is_open/aclose；同一事件循环显式释放资源 |
| `BrowserPort` | state/control/screenshot；页面和工具共享一个实例，不暴露 Playwright 类型给上层 |
| `MCPPort` | overview/connect/list_tools/call/disconnect/configure；SDK 类型留在 integrations 中 |

新增工具只需实现 `Tool` 并在装配点注册，不改 Runner。当前十五项：`file_list`、`file_read`、`file_write`、`file_edit`、`document_read`、`history_read`、`artifact_register`、`task_plan`、`agent_task`、`task_checkpoint`、`browser_open`、`browser_read`、`browser_action`、`mcp_list_tools`、`mcp_call_tool`。远端 MCP Schema 不展开成每次请求的固定工具集合，而由 mcp_list_tools 按需分页获取，mcp_call_tool 在边缘校验并转换为统一 ToolResult。

附件没有另造一套 Message：`attachment.linked` 保存原始输入和文件元信息，现有 user 消息附加简短路径引用。模型按需读取，完整文件不自动塞入上下文。HTTP 用 Pydantic DTO，内部仍使用既有契约；MCP 配置归一化在 integrations/config.py。模型/工具消息、事件和 RunResult 的 schema_version 仍为 1。

## 事件、兼容和恢复

- 当前 schema_version 为 1。数据库版本不匹配时明确拒绝打开，不静默转换或清理数据。
- 同一主版本优先增加有默认值的可选字段。删除字段、改名、改变语义或签名时，必须显式升级契约，提供迁移或适配并更新契约测试。
- `kind` 是可扩展字符串，消费者遇到未知事件应忽略其展示，保留原始事件。现有公共事件见下表。
- 公共接口集中只能减少接口漂移，不能保证将来永远不用升级。HTTP v1 接口见下表；事件与运行结果复用 core 的序列化结构。
- 进程崩溃留下未闭合工具事务时，不自动重放；续聊会拒绝不完整消息，可显式从事务前的完整事件点分支。当前没有崩溃后自动恢复界面。

| 事件族 | 用途 |
| --- | --- |
| `run.configuration/started/ended` | 非密钥配置、运行开始/结果 |
| `message` | 完整原始消息，模型请求从这些消息投影 |
| `context.projected` | 估算值、投影字节数、被摘录工具结果的原文事件坐标 |
| `model.started/returned/partial/invalid_response/interrupted_response` | 调用次数、上报用量、原始响应和失败片段 |
| `tool.started/returned/exception` | 实际工具名称、参数、结果及错误 |
| `file.write_prepared/written` | 写入前的原始内容哈希、预览和写入后哈希 |
| `file.restore_prepared/restored` | 显式恢复的准备与结果 |
| `artifact.registered` | 文件存在性、大小和哈希；不代表内容验收 |
| `observer.failed` | 展示消费者异常，与执行错误分开 |
| `attachment.linked` | 原始用户 prompt、附件 ID/名称/工作区路径/大小/类型/哈希 |
| `browser.action/manual` | 模型或用户实际操作与结果；所有浏览器动作另有 JSONL 审计 |
| `mcp.response` | 服务名、工具名、SDK 原始结果；图片/音频另存文件，完整结果不丢弃 |

网页从真实事件展示阶段、工具结果和修改预览，通过 SSE 接收实时事件与正文 delta。完整消息、原始模型响应和 diff 可从下载记录读取。模型的私有推理字段只保留在原始记录，不作为阶段文案展示。

## 本地 HTTP v1

| 路由 | 用途 |
| --- | --- |
| `GET /api/state` | 脱敏模型配置、工作区、分支和活动运行 |
| `POST /api/settings` | 保存模型与工作区设置；空闲时生效；密钥不回显 |
| `POST /api/model/probe` | 测试已保存接口的 `/models`；不触发生成 |
| `POST /api/runs` | 接受任务，返回 202 和分支 ID |
| `POST /api/uploads` | multipart 单文件上传，201 返回元信息；任务引用 attachment_ids |
| `GET /api/browser/state` | 共享会话状态，不主动打开浏览器 |
| `POST /api/browser/action` | arguments 使用 browser_action 相同 Schema，可选 branch_id 记录手动操作 |
| `GET /api/browser/image` | 共享页面实时 JPEG；预览用，不自动发给模型 |
| `GET/POST /api/mcp` | 获取脱敏配置或保存 servers 映射 |
| `POST /api/mcp/{server}/connect` | 显式连接/重连；disconnect=true 断开 |
| `POST /api/stop` | 停止并等待当前任务记录完成 |
| `GET /api/branches/{id}` | 下载该分支的完整报告 |
| `POST /api/branches/{id}/fork` | 从完整事务边界创建分支 |
| `POST /api/branches/{id}/restore` | 通过已有 Application 显式恢复文件 |
| `GET /api/branches/{id}/files/{event}` | 下载已登记且哈希仍一致的文件 |
| `GET /api/stream?after={sequence}` | SSE 短期实时帧；重连支持 Last-Event-ID |

SSE 帧为 `{sequence, kind, payload}`；kind 为 event/delta/state/error/browser。event.payload 复用 `Event`，delta 为分支 ID 与文本，browser 用于刷新共享会话状态。内存帧环只保存最近 1000 帧，数据库才是完整持久历史；断线或刷新后从报告重新加载。正文 delta 不逐 token 写数据库，沿用执行器的完整返回/正常中断记录方式。

变更请求携带页面的 `X-ClearAct-Token`；网关只监听 127.0.0.1，拒绝跨源请求，未配置 CORS。输入校验错误不回显请求体或密钥。运行中拒绝设置、工作区切换、分支创建、文件恢复、上传和 MCP 配置变更；停止及手动浏览器操作可用。MCP env/headers 从响应排除，编辑时 null 保留已有值，{} 清除。
