# ClearAct Lite

2026-10-23 修复容量估算与归档工作状态：优先参考匹配请求的服务实报用量，保留最近写入和当前完整事务，60%整理目标让位于必要工作内容；计划增加增量交付物及依赖交接，文件/历史读取共用字符游标与EOF。修改图、完整结构和本轮独立验证见 [修复说明](development/docs/changes/2026-10-23-context-repair.md)。真实复杂任务结果与自动测试分开记录。

在独立目录重建的本地优先执行系统。当前为 **0.1.0 架构基础版**，仅红色完全控制模式：模型选择工具和步骤，程序负责协议、执行、记录、停止和文件证据。

本地工作台支持话题、模型设置、阶段与文本流、工具记录、修改预览、文件恢复、历史分支和交付下载。本轮新增 **MCP、用户与模型共用的浏览器、文件上传及 PDF/Word/Excel 内容读取**，均通过原有统一入口接入。尚无独立命令执行工具，Office 当前提供读取。复杂任务表现以本轮独立验收记录为准，模型正常结束不等于完成用户目标。

新增长任务支持：默认在估算上下文占用达到 90% 时归档完整旧事务；模型可保存文件计划、调用独立上下文子代理、分段追加或局部修改文件，并在输出截断后从最后完整动作继续。模型保持加载，不执行截断参数。子代理目前串行、只有一层；总 token 不保证减少。详见 [本轮长任务与参数测试](development/docs/changes/2026-10-22-long-work.md)。

- [开发与验收资料总入口](development/README.md)
- [完整架构、逐文件职责、总流程与模块表](development/docs/ARCHITECTURE.md)
- [统一接口及兼容约定](development/docs/CONTRACTS.md)
- [协作流程与每轮修改解释要求](development/docs/WORKFLOW.md)
- [结构调整解释图和文件树](development/docs/changes/2026-10-22-layout.md)
- [前端接入解释图、文件树与验证](development/docs/changes/2026-10-22-frontend.md)
- [本轮能力接入解释图、完整树与真实验证](development/docs/changes/2026-10-22-capabilities.md)
- [实际规则与限制清单](development/docs/RULES.md)

## 本地运行

**双击根目录 `clearact.cmd` 即可启动本地工作台并自动打开浏览器。**默认地址为 `http://127.0.0.1:8875`；启动失败会保留窗口和错误信息。正常使用期间保留服务窗口，按 Ctrl+C 停止。

需要 Python 3.12+。正式安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

启动脚本优先使用项目 `.venv` 中的 Python，否则使用 PATH 中的 Python。本机已具备依赖时可直接启动。手工启动或改用其他端口：

```powershell
.\clearact.cmd web --open
.\clearact.cmd web --port 8876 --open
```

默认工作区为 `workspaces/default/`；可从页面底部切换文件夹，在左下角“模型与设置”保存接口配置。下面的命令行用法仍然可用：

```powershell
$env:CLEARACT_BASE_URL = 'http://localhost:1235/v1'
$env:CLEARACT_MODEL = 'qwen/qwen3.8-27b'
# 服务启用鉴权时，在当前终端设置 CLEARACT_API_KEY，不写入源码。
.\clearact.cmd --workspace E:\clearact\clearact-lite\workspaces\my-work run '创建 hello.txt，内容为 hello，读取核对并登记交付'
```

`--workspace` 是本轮工具可操作的根目录，必须放在子命令之前。目录会自动创建。运行输出含分支 ID；`finished` 只表示模型结束本轮，不代表独立验收通过。

```powershell
.\clearact.cmd --workspace E:\clearact\clearact-lite\workspaces\my-work history <分支ID>
.\clearact.cmd --workspace E:\clearact\clearact-lite\workspaces\my-work report <分支ID>
.\clearact.cmd --workspace E:\clearact\clearact-lite\workspaces\my-work run '继续修改' --branch <分支ID>
.\clearact.cmd --workspace E:\clearact\clearact-lite\workspaces\my-work fork <分支ID> --at <完整事务末尾事件ID>
.\clearact.cmd --workspace E:\clearact\clearact-lite\workspaces\my-work restore <分支ID> <file.written事件ID>
```

运行中用 Ctrl+C 停止；API 使用 `Application.stop()`。当前文件操作是短暂的同步原子操作，执行其中一项时，取消会在下一个异步让出点生效。文件恢复会核对当前内容；手工修改后的冲突不会被覆盖。

## 配置与记录

运行依赖还包含 `mcp`、`playwright`、`python-multipart`、`pypdf`、`python-docx`、`openpyxl`，共十项；存储使用 Python 自带 SQLite。网页使用原生 HTML/CSS/JS，无 Node 构建或 CDN。工作台只监听本机，启动前端不调用模型；关闭后不会自动重放任务。

前端配置存放在项目根目录 `.clearact/web-settings.json`，包含本机使用的密钥，已被 Git 忽略。页面不会回显密钥，留空保存会保留已有值，也可点击清除。已只读复用旧项目中必要的本地 Qwen 连接信息初始化本机配置；没有迁移旧任务、附件或运行框架。环境变量用于初始值，已保存的页面配置优先。

环境变量：`CLEARACT_BASE_URL`、`CLEARACT_MODEL`、`CLEARACT_API_KEY`、`CLEARACT_CONTEXT_TOKENS`、`CLEARACT_MAX_OUTPUT_TOKENS`、`CLEARACT_REASONING_EFFORT`。其他设置通过统一的 `Settings` 对象提供。思考强度默认 `none`，设为空串时不发送该兼容字段。当前适配器要求服务支持流式 Chat Completions 及 `stream_options`，不自动试错重发。

完整事件与快照保存在所选工作目录的 `.clearact/`；原始模型响应、工具参数和文件内容可能包含私有信息。它们由 `.gitignore` 排除，不自动上传。

## 验证

测试代码、验收脚本、所有实际测试记录、修改说明和文件树统一在 `development/`。Agent 本体位于 `src/clearact_lite/`，实际使用的工作文件可另放 `workspaces/`。

每次使用新的隔离测试目录，避免覆盖旧失败现场：

```powershell
New-Item -ItemType Directory -Force development/records/evals | Out-Null
$testStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python -B -m pytest -q --basetemp "development/records/evals/pytest-$testStamp" --junitxml "development/records/evals/pytest-$testStamp.xml"
ruff check src development/tests development/scripts
ruff format --check src development/tests development/scripts
$env:PYTHONPATH = 'src'
python -B development/scripts/evaluate_foundation.py
```

最后一项会调用所配置模型，只用于小文件真实冒烟，保存成功、失败和逐调用用量。请指向本地服务。完整能力验证入口见下方，不把小样例成绩当作复杂任务通过。

网页端验收使用本机 Edge；Playwright 也是 Agent 浏览器的运行依赖，`psutil` 仅用于验收进程清理：

```powershell
python -m pip install -e '.[dev,ui-test]'
python -B development/scripts/verify_frontend.py
python -B development/scripts/verify_frontend.py --real-model
python -B development/scripts/verify_launcher.py
```

第一项是页面与协议替身验证，第二项调用真实模型验证多文件任务与续聊，第三项验证实际 Windows 启动脚本并结束它创建的测试服务。真实模型所需鉴权可以通过 `CLEARACT_API_KEY` 提供；验收工作区与配置在 `development/records/` 下隔离，不读取页面保存的用户密钥。

## 本轮新增能力的用法

- 输入框旁的附件按钮支持选择、拖入或粘贴文件。上传后发送任务，模型收到文件路径，按需用 `file_read` / `document_read` 读取，不自动展开全文。默认每文件上限 64MB，可用 `CLEARACT_UPLOAD_MAX_MB` 调整。原始上传字节保留，工作副本允许模型编辑。
- 顶部“浏览器”打开共享预览，可访问地址、点击、滚动、输入文字或按键。模型操作同一会话；关闭面板保留会话，“结束浏览器”才关闭。使用独立浏览器上下文，不继承个人 Edge 登录态。Windows 优先使用已安装 Edge；其他环境或无 Edge 时需先执行 `python -m playwright install chromium`。
- 左栏“MCP 工具”新增或导入 `mcpServers` JSON，支持 stdio、Streamable HTTP、SSE，提供连接/断开按钮。密钥放在 env/headers，本地保存但不回显；编辑时留空保留，填写 `{}` 清除。配置不自动执行任务，模型按需发现和调用工具。stdio 的命令及依赖需已安装。
- PDF、DOCX、XLSX/XLSM 支持内容提取；CSV/JSON/文本用文件读取。扫描 PDF 未接 OCR，旧 `.doc/.xls` 不支持解析，图片当前保留为附件但未接多模态模型输入。

MCP 配置保存在项目 `.clearact/mcp-servers.json`，与模型设置一起被 Git 忽略。MCP 外部服务和浏览器网页的副作用记录下来，但无法用本地文件快照全部撤销。

没有附加审查模型、风险审批、关键词任务分类、搜索配额、重复操作拦截或基于验收反馈的自动补救轮次。用户另行明确授权了容量压缩、输出到限续跑及文件计划/子代理工具。仍有协议、路径、超时和并发约束，见 [规则清单](development/docs/RULES.md)。

## 长任务与参数测试

模型设置的高级参数提供自动压缩开关、触发比例（默认 0.9）、目标比例（默认 0.6）和输出到限续跑开关。占用率是保守估算的输入加输出预留除以配置容量，不是实际 tokenizer 的精确 90%。累计 usage 不参与容量触发。保留用户原文；如果用户要求本身太长，压缩后仍放不下会明确停止。

多文件任务由模型调用 `task_plan` 保存每个文件职责和公共接口，再调用 `agent_task` 执行计划项。子代理不继承主代理源码消息，接收用户要求、共享接口、完整文件职责表和分配任务；默认使用文件类工具，模型可在计划的 `tools` 中指定其他可用工具。返回短报告、状态、文件哈希和完整历史分支。主代理负责整合；运行时不会额外插入验收指令。正常结束不等于验收通过。页面执行记录可下载子代理完整记录，修改预览包含子代理文件并可显式恢复。

`file_edit` 的 append 模式要求当前 UTF-8 字节数，避免重发片段造成重复；replace 模式要求原文唯一匹配。每次完整写入先做快照。输出截断只重新发起容量续跑请求，不自动修复普通工具、服务或验收错误；不会自动重放网页提交等副作用。服务崩溃后的自动重载未实现。

```powershell
$env:PYTHONPATH = 'src;development/scripts'
$env:PYTHONUTF8 = '1'
python -B development/scripts/evaluate_long_work.py --mode matrix
python -B development/scripts/evaluate_long_work.py --mode boundary
python -B development/scripts/evaluate_long_work.py --mode resume
python -B development/scripts/evaluate_long_work.py --mode large --output 16384
python -B development/scripts/verify_long_frontend.py
```

所有模式写入新的隔离目录。matrix 是少量配置组合的真实本地比较，不代表全局最优；boundary 区分累计 token 与单次容量，并测试本机服务越界响应；resume 在一次完整写入后人为限制真实下一轮输出为 1 token 来验证续跑；large 使用完整原始游戏任务，仍须独立路线验收。页面验证使用协议替身，单独标识。

新增验收脚本仅允许本地模型，隔离保存记录；真实模式在内存中读取本机模型连接配置，不输出密钥：

```powershell
$env:PYTHONPATH = 'src;development/scripts'
$env:PYTHONUTF8 = '1'
python -B development/scripts/verify_capabilities.py --mode ui
python -B development/scripts/verify_capabilities.py --mode real
python -B development/scripts/verify_capabilities.py --mode large
```

`ui` 使用协议替身驱动 Agent，同时实际操作 Edge 和 MCP；`real` 调用本地模型贯通四种附件、MCP、网页提交与交付，并独立核对值；`large` 使用完整原始游戏任务，从空目录运行，模型结束后仍需独立验收。明确反馈复测是手工执行 `--feedback-case <原轮记录目录>`，不属于 Agent 自动补救功能。
