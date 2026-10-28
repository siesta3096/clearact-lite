# 开发与验收资料

当前容量、归档与任务分配修复见 [2026-10-23-context-repair.md](docs/changes/2026-10-23-context-repair.md)。`scripts/verify_context_repair.py` 只重放旧失败现场，不调用模型；真实游戏、协议替身、全部失败和用量分别保存。

长任务批次见 [2026-10-22-long-work.md](docs/changes/2026-10-22-long-work.md)。`scripts/evaluate_long_work.py` 保存真实本地参数组合、累计 token/单次窗口边界及完整原始游戏任务；`scripts/verify_long_frontend.py` 验证压缩、续跑、子记录、父子恢复的真实 Edge 界面，但模型是协议替身。各次现场独立，替身成功不算真实模型验收。

本目录集中保存测试、真实运行痕迹、失败和用量，以及每轮修改说明、解释图和文件树。Agent 本体在项目根目录的 `src/clearact_lite/`，运行时不依赖本目录。

| 位置 | 内容 |
| --- | --- |
| `tests/` | 自动化行为和契约测试 |
| `scripts/` | 真实模型验收脚本 |
| `fixtures/` | 可重复使用的完整任务原文；不包含旧项目实现或旧验收成绩 |
| `records/evals/` | 各次隔离工作目录、原始事件、快照、产物、用量和测试报告 |
| `records/layout-relocation-inventory.json` | 搬迁前后路径和内容哈希 |
| `docs/ARCHITECTURE.md` | 当前完整文件树、模块表和总运行流程 |
| `docs/CONTRACTS.md` | 统一接口和兼容约定 |
| `docs/WORKFLOW.md` | 修改前、中、后的协作要求 |
| `docs/RULES.md` | 当前实际系统提示、技术约束与未加入的审查体系 |
| `docs/changes/` | 按修改批次保存的解释图、当时的文件树及验证索引 |
| `.cache/` | Ruff、pytest 等开发工具缓存 |

本地前端实现位于 `src/clearact_lite/web/`，HTTP 适配器位于 `src/clearact_lite/gateway/`。本目录只保存它们的测试和开发资料。

前端修改说明见 [2026-10-22-frontend.md](docs/changes/2026-10-22-frontend.md)。`scripts/verify_frontend.py` 用 Edge 区分协议替身交互与真实本地模型任务；`scripts/verify_launcher.py` 验证实际 Windows 启动行为。所有现场、截图、失败、用量和日志写入 `records/evals/` 的独立目录。

每轮至少有一份独立修改说明，包含解释图、当时文件树、验证路径和未验证项。当前架构文档持续更新，历史文件树单独保留，不被当前版本覆盖。

本轮新增功能说明见 [2026-10-22-capabilities.md](docs/changes/2026-10-22-capabilities.md)。`scripts/verify_capabilities.py` 区分 UI 替身验证、真实本地模型贯通、完整大任务和明确反馈复测；`scripts/mcp_fixture.py` 是真实协议本地测试服务，不参与 Agent 运行。测试失败和首次算错的记录保留，不能仅展示纠正后的通过。

## 历史位置映射

```text
tests/    → development/tests/
scripts/  → development/scripts/
docs/     → development/docs/
data/     → development/records/
```

实际记录、快照、原始报告保留搬迁前字节。其中绝对路径及首轮源码哈希指向当时位置，使用清单查找搬迁后的文件。重定位测试脚本及活跃文档中的路径不算重写历史测试结果。

原始运行记录、缓存由根 `.gitignore` 排除；测试代码、脚本和修改文档可随代码维护。验证命令见项目根 [README](../README.md)。

2026-10-25：[CSGO质量目标FPS真实尝试](docs/changes/2026-10-25-fps-attempt.md)，Agent本体不改，独立结果与待确认方案单列。

2026-10-25：[通用可靠性计划（待确认）](docs/RELIABILITY_PLAN.md)：按需验证、主代理审查、有界补救与无效绕圈止损；当前仅方案，未接入运行时。
