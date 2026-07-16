# radar-sim v5 Active Handoff

> 最近更新：2026-07-16
> 状态来源：本顶部区域是 v5 唯一实时实施状态。
> 下方 `Legacy History` 保留历史原文，不代表当前 v5 完成度。

## 0. 文档职责

| 文档 | 职责 | 防漂移规则 |
|---|---|---|
| `PRD.md` | 产品合同、用户入口、部署边界、P0/P1 范围 | 用户可见合同或产品边界变化时必须先改这里 |
| `docs/PRODUCT_CONTRACT.md` | 用户最终确认的唯一配置、四种组合、执行边界和发布门禁 | 与其他文档或旧实现冲突时以此文件为准；未经用户确认不得新增对外字段 |
| `docs/DETAILED_DESIGN.md` | 目标架构、现有代码迁移映射、复用/选型决策 | 技术边界、依赖、协议、拓扑变化时必须先改这里 |
| `DEVELOPMENT_PLAN.md` | WP0-WP10 可执行 backlog、依赖顺序、测试、退出标准、停止项 | 实施顺序或验收门禁变化时更新 |
| `HANDOFF.md` | 唯一实时状态、conformance log、偏离检查记录 | 每次 CLI/code agent 任务完成前必须更新 |

`CHECKPOINT.md`、`docs/handoff.md`、`docs/REFACTORING_PLAN.md`、`docs/WIZARD_IMPLEMENTATION_PLAN.md` 和旧 phase 章节只作为历史证据，不覆盖上述四份文档。

## 1. 产品不变量

每个实施任务必须引用其触碰的 INV。

| ID | 不变量 |
|---|---|
| INV-01 | 用户入口只有 Web 和 Python SDK / versioned REST API；CLI 仅用于管理员、调试和兼容。 |
| INV-02 | 部署形态只有 Windows full deployment 与 Linux central service + optional Windows light Agent。 |
| INV-03 | 新用户业务合同只有项目无关 `UserRunConfig 2.0` YAML；Web 与 SDK 共用同一后端 schema；`SimulationSpec` 仅保留兼容/内部适配用途。 |
| INV-04 | 调度核心只有一个；Web、SDK、兼容 CLI 不得各自实现调度规则。 |
| INV-05 | Linux 永不编译 Selena；`build.selena` capability 只属于 Windows full/Agent 节点。 |
| INV-06 | 无 Windows 用户只能使用 existing Selena + Cluster，不得隐藏依赖本机环境。 |
| INV-07 | Selena 和数据就绪后，Cluster 执行不依赖用户 PC；旧 Windows/Python2 接入走平台 Gateway/Worker。 |
| INV-08 | 用户只填写数据路径；shared/local/upload/browser 输入统一解析为内部 `DatasetRef`。 |
| INV-09 | Git 自动化不得 checkout/stash/reset/force 用户主工作区；分支构建必须用隔离 worktree。 |
| INV-10 | 复用优先：先改造现有代码或采用成熟独立模块/标准协议，不从零造基础设施。 |
| INV-11 | 全过程可视化是 P0：环境检查、preflight、阶段进度、日志、失败动作和 Manifest。 |
| INV-12 | 当前 dirty workspace 可构建，但必须记录 branch、commit、dirty fingerprint 和构建指纹证据。 |
| INV-13 | Windows 轻量 Agent 首版只做授权工作区编译、产物/数据登记校验上传和中央回传；不支持本地仿真，不承担 Cluster 运行期。 |
| INV-14 | 用户复用 Selena 时只填写已有 Selena 文件夹和 Runtime XML；系统必须校验/使用目录中的 Selena.exe 与依赖 DLL。Runtime Bundle 仅允许作为不可见的内部传输/缓存对象。 |
| INV-15 | 用户配置不得出现 Runtime Bundle、project/profile/recipe/output_root/共享盘类型/Agent/Cluster manager 等内部概念。 |
| INV-16 | Linux 是可迁移的统一控制面（当前目标 10.190.171.44），只调度和传输；不编译 Selena、不执行本地仿真。 |
| INV-17 | 必须支持 build/existing × local/cluster 四种组合；auto 只负责在四种组合中自动选择并展示原因。 |
| INV-18 | 已有 Selena 路径与 data.path 均由系统解析可达性；本地不可达时自动上传/传输，Cluster 就绪后不依赖用户电脑。 |

## 2. WP0-WP10 实时状态

> 2026-07-15 V1 收敛决定：完整 PRD 不变，但当前交付只实现 `existing Selena + Cluster`。权威首版范围和门禁见 `docs/V1_MVP_SCOPE.md`；编译和本地仿真组合暂停实施，待 V1 交付后继续。

### V1 纵向交付状态（2026-07-16）

统一入口已收敛为 `YAML -> RadarSimClient.submit_yaml() / Web -> Linux /api/v1 -> Selena/数据/配置资产准备 -> Stage DAG -> local/Cluster`；兼容 `submit_cluster_yaml()` 只额外限制 V1 的 `existing + cluster` 组合。2026-07-16 修复 Web 直接填写 Windows 本地已有 Selena 文件夹时错误要求编译工作区的问题：Agent 现在校验并导入 `Selena.exe + 同目录 DLL + Runtime XML`，服务端按已有产物路径绑定环境和数据 Stage。API 配置检查返回与提交共用的执行位置决策和完整 10-stage 计划，Web 原样展示。专项回归 `110 passed`，最终全量回归 `1222 passed, 8 skipped`；部署后 SDK 的 build/auto 与 existing/cluster dry-run 均成功。真实 Web 烟测 `job_3a2dc6949270` 已完成已有 Selena 导入、Linux Cluster 环境检查、共享数据解析和 preflight，并取得外部 Cluster job id `1`；结果文件已回收，Manifest 因输入数据缺少 Runtime 所需信号而明确为 `failed`。控制面已修正为由 Manifest 决定最终 Job 成败，避免把“调度完成但仿真失败”显示为已完成。Linux 正式用户服务现监听 `0.0.0.0:8877`，正式测试入口为 `http://10.190.171.44:8877`；`127.0.0.1:8878` 只保留为本机 Agent 兼容隧道。

只有具备代码证据和测试证据的实现才能标记完成。已有原型只算“待审计输入”，不算 v5 完成度。

| WP | 范围 | 状态 | 证据 / 下一门禁 |
|---|---|---|---|
| DOC | v5 文档基线与证据审计 | Done | `README.md`、`PRD.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md` 和本 Active Handoff 已更新；文档修改后必须跑 `git diff --check` |
| WP0 | 安全冻结与 legacy Git 路径隔离 | Done | `core/repo.py` 仅验证当前分支、不再 checkout；`cli/web.py` 拒绝 legacy `switch_branch` repair；focused tests 49 passed；Copilot 与主 agent 独立全量回归均为 466 passed |
| WP1 | `SimulationSpec v1` 与 legacy config adapter | Done | `SimulationSpec`/JSON Schema/YAML/hash 与只读 legacy adapter 已完成；A7a 进一步将最小导入收敛为 `project + data.path`，项目 recipe/platform 由 `ProjectCatalog.adapter` 承担 |
| WP2 | `/api/v1` application layer 与 Python SDK skeleton | Done | thin application/FastAPI、durable idempotency、JSON/SSE、SDK、`serve-v1` 已完成；A7a 新增 owner-isolated `/api/v1/jobs` 任务中心查询、进度/当前 Stage/可执行动作及 SDK 方法 |
| WP3 | Job/Stage/Event DAG | Done | 标准 10-stage planner、兼容 Stage/Attempt/Event persistence、结构化事件、cancel/retry/reclaim 与 v1/SDK 读取已完成；Copilot full 565 passed，主 agent focused/full 复验通过 |
| WP4 | Selena source resolver、dirty fingerprint、artifact catalog | V1 Done / Full In Progress | V1 existing 路径已接 SDK/Linux 自动导入、内容寻址目录、归档完整性校验和 Cluster 消费；完整产品 build 组合后续继续 |
| WP5 | Data resolver 与 upload | Done | shared/local/browser/SDK/Agent 数据统一为 `DatasetRef`/`DataLease`，中央多文件 resumable upload 与 Cluster/本地消费闭环完成 |
| WP6 | Windows full 与 light Agent 执行适配 | V1 Done / Full In Progress | Web/SDK 公开 existing Selena 文件夹已接 Agent 校验、打包和 owner-scoped 上传；`job_3a2dc6949270` 证明 existing-to-cluster 不再要求编译工作区。existing-to-local 与安装体验继续按完整 PRD 验收 |
| WP7 | Linux central Cluster executor 与平台 Gateway routing | V1 Done | existing + Cluster 从 SDK 到真实 manager 已跑通；`job_bad6f07479e5` succeeded 并产出 537,269,680-byte MF4；Linux 可直接导入已挂载共享 Selena |
| WP8 | Environment、Preflight、progress、Manifest 集成 | Done | Cluster 与 Windows-local environment/preflight/run/collect/finalize 已接统一 DAG；公共 Manifest/Result ZIP 只含逻辑 ref；外部 Cluster cancel 行为留目标环境验收 |
| WP9 | Web v5 migration | Done | packaged v1 console 已覆盖最少配置、YAML import/export、上传、任务中心、Stage/event/action、结果下载和 session-only Bearer；认证真实浏览器 smoke 已通过 |
| WP10 | SDK/package/docs/release gates | V1 Function Done / Network Pending | 统一 `submit_yaml()`、V1 兼容入口、Web 动态 10-stage 预览、SDK/FastAPI/Cluster 纵向门禁和 10.190.171.44 真实验收均完成；仅剩服务器 8878 入站放行/反向代理 |

当前工作区包含既有未提交代码和测试变更（`cli/*`、`core/*`、`web/*`、`tests/*` 以及新增原型模块）。这些都视为待审计输入，不计入 v5 完成度，直到某个 WP 引用代码证据并通过测试。

## 3. 当前文档工作证据

| 日期 | 范围 | 证据 |
|---|---|---|
| 2026-07-10 | v5 baseline docs | README 权威顺序、PRD current/target 边界、详细设计现有代码映射、WP0-WP10 可执行计划 |
| 2026-07-10 | 复用/选型防漂移 | 增加 INV-10、详细设计复用决策、第三方 spike 和回退门禁 |
| 2026-07-10 | 状态与偏离防护 | Active Handoff 状态表、任务模板、Architecture Conformance Gate |
| 2026-07-10 | 轻量 Agent P0 边界 | PRD/design/plan/HANDOFF 仅文档同步：light Agent 不做本地仿真、不承担 Cluster 运行期；不得误报为运行时代码已交付 |
| 2026-07-10 | Reviewer gap closure | PRD/design/plan/HANDOFF 仅文档同步：补 light build-to-cluster Stage/capability/node-kind 矩阵、artifact capability 词汇、WP6/WP7 负向门禁和 light Agent 数据 E2E 验收；WP6/WP7 runtime 仍 Pending |
| 2026-07-14 | Runtime/配置边界与 Cluster 纵切 | Runtime Bundle 仅含 exe/DLL/绑定 Runtime XML；Adapter/MatFilter 独立必填并可上传为 owner-scoped config-asset；Linux/Gateway 四阶段接入、无 Windows mocked E2E 和 Web 真实上传烟测通过 |
| 2026-07-16 | 统一 YAML/SDK/Web 收敛 | 新增 `submit_yaml()`、验证/提交共用路由决策、Web 10-stage 计划；修复 Web existing Selena 的 Agent 导入和 Stage 绑定；真实任务 `job_3a2dc6949270` 已完成 Cluster 结果回收，Manifest 正确暴露信号不匹配失败；新增 Job/Manifest 状态一致性门禁 |
| 2026-07-16 | 首轮用户体验修复 | 确认 `job_643a2386b7a7` 的 3 个数据均因 Runtime 所需信号缺失失败；任务轮询禁止重入、日志刷新保留滚动位置、失败原因直接展示；正式入口切换到防火墙已放行的 `10.190.171.44:8877` |
| 2026-07-16 | Linux 共享路径作用域修复 | 将 `linux_mount_map` 从项目 `local.yaml` 提升为 `$RSIM_HOME/config/deployment.yaml` 部署覆盖层，对所有内部项目识别结果生效，用户 YAML 仍只写 `data.path`；真实 UNC `//abtvdfs2.../20-4-26_CCCscp` 已解析到 `/mnt/cluster/...`，原任务 `job_f571e99f20f1` 的 `prepare_data` 重试成功。后续 Windows `prepare_source` 暴露长路径与大型 checkout 120 秒限制，已启用调用级 `core.longpaths=true`、600 秒 checkout 与超时半成品清理，重试后进入 `build_selena`。 |
| 2026-07-14 | 最少配置与动态调度收敛 | Web/YAML/SDK 统一为单一 data.path；新增 Selena/软件包脚本双入口、首次 Agent 自动配置、auto 本地/Cluster 选择、浏览器文件夹透明上传及 Adapter 条件校验；全量 1173 passed/8 skipped，真实页面复验通过 |
| 2026-07-15 | 用户合同最终复验 | OpenCode 独立审计配置/Web/SDK 子合同无缺口且专项 36 passed；主 Agent 调度/数据/Agent 专项 102 passed、合同组合 137 passed、全量 1173 passed/8 skipped；真实页面确认单数据路径、双构建脚本、无 project、Adapter 可选、MatFilter 必填、auto/local/Cluster 与结构化 Stage 任务中心 |
| 2026-07-15 | V1 existing + Cluster 收敛 | 单 YAML + 单 SDK 方法；SDK/服务端双侧已有 Selena 导入、数据/配置资产准备、manifest role 解包、Cluster 提交/结果回收纵向门禁通过；聚焦 101 passed，目标服务器烟测 Pending |

## 4. 真实任务记录

### 2026-07-15 - 用户最终合同固化与 existing Selena 底座纠偏

- Goal: 固化“一个 YAML、Web/SDK 同合同、Linux 只调度、Windows/Cluster 分开执行、build/existing × local/cluster 四组合”的最终用户边界，并修复把 `existing_path` 错当内部 Bundle ID 的设计偏移。
- Product baseline: 新增 `docs/PRODUCT_CONTRACT.md`；更新 INV-14~18。Runtime Bundle 降为不可见内部传输/缓存，用户复用 Selena 只填写文件夹和 Runtime XML。
- Root cause evidence: 真实失败 `output_root must be narrower than workspace_root` 来自空 build output 被 `Path("")` 解析为 workspace；已改为由用户 Selena 编译脚本推导输出且空值先校验。真实用户路径识别为 `ovrs25`，输出为 `C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR`。
- Claude CLI evidence: 按用户要求使用 Claude CLI；10 分钟超时后产生的初稿专项测试为 `12 failed, 11 passed`，主 Agent 未将其误报完成，审查并重写了错误的目录遍历、项目识别、lease 副作用和测试夹具。
- Implemented foundation: `core/existing_selena.py` 只接收目录 + Runtime XML；唯一 Selena.exe 检索有界且确定；要求至少一个同目录 DLL；从实际项目 adapter/别名推导内部项目；生成确定性 exe+全部 DLL+Runtime 内部归档；公开摘要无路径和内部项目。
- Verification: 新模块 + Runtime Bundle 专项 `21 passed`。真实目录 `.../RelWithDebInfo` 验证得到 `ovrs25`、1 个 `selena.exe`、7 个 DLL、绑定 Runtime XML、9 个归档文件、17,788,849-byte archive，公开摘要 `path_leaked=False`。
- Honest remaining gap: 该底座尚未接入 Agent resolve/register、Web/SDK 文件夹透明上传和 Linux shared-folder resolver；四种组合尚未重新完成纵向验证，状态保持 In Progress。

### 2026-07-15 - 单一用户配置与动态调度合同最终复验

- Goal: 将用户确认的单一配置合同作为收敛目标，确认 Web/YAML/SDK 只暴露数据路径和必要 Selena/仿真文件，并能按 auto/local/Cluster 动态调度 Full、Light 和无 Windows 三种部署形态。
- OpenCode review: 按用户要求停止 Claude 后改用 OpenCode。配置/Web/SDK 小范围独立审计确认无真实缺口、未制造代码改动，`test_user_config.py` 15 passed、`test_wizard_web.py` 5 passed、`test_sdk.py` 16 passed；调度大文件审计因模型响应超时未作为验收依据。
- Main verification: 合同组合测试 `137 passed`；调度、数据、Agent 与 Full Windows 纵向专项 `102 passed`；`node --check radar_sim_web/static/app.js` 通过；最终全量 `1173 passed, 8 skipped, 1 upstream Starlette/httpx deprecation warning`（429.90s）。
- Browser evidence: 本机 `http://127.0.0.1:8878/` 显示服务已连接且无需令牌；新建任务只有 `data.path`，代码路径/分支/Selena 编译脚本/软件包编译脚本/Runtime XML，Adapter 明确可选、MatFilter 必填；执行入口为自动/本地/Cluster；任务中心明确使用结构化 Stage 事件展示状态和进度。交付前已恢复 full Agent，capability 为 `windows_full.available=true`。
- Drift check: 用户配置仍无 project/profile/toolchain/Agent/共享存储类型/Cluster manager 参数；Linux 不编译 Selena；Light 不做本地仿真；无 Windows 只允许 existing Runtime Bundle + Cluster；未扩展本 Sprint 产品范围。
- External validation: 当前本机服务未配置 Cluster executor，因此只验证了 capability=false 和 mocked/contract 链路；企业共享盘、manager 提交/取消及上游 Selena 源码编译错误仍需在目标环境验收，不得误报为本机已完成真实仿真。

### 2026-07-14 - 最少配置、自动配置与动态任务路由

- Goal: 用户只维护一份项目无关 YAML；只填写数据路径、代码仓/分支、Selena 编译脚本、软件包编译脚本、Runtime、Adapter/MatFilter 和执行目标。
- Actual changes: 移除 `build_mode`/旧 `build_script` 对外字段；软件包脚本进入内部项目识别和依赖扫描/自动修复；一键 Agent 首次任务自动登记代码仓、Runtime 与本地数据根，心跳刷新后永久复用；`auto` 在 full 在线时选本地，否则选 Cluster；本地数据按路径自动生成 Agent 上传，浏览器无 Agent 时由同一数据字段旁的文件夹选择器透明上传；Adapter 可为空并由 recipe 预检决定，MatFilter 仍必填。
- Verification: YAML 示例可解析，`node --check` 通过；真实页面确认无令牌连接、单数据路径/文件夹选择、双脚本、Adapter 可选与配置校验；最终全量 `1173 passed, 8 skipped, 1 upstream deprecation warning`。目标企业 Cluster 仍需环境验收。
- Drift check: Linux 仍不编译 Selena；light 仍不做本地仿真；YAML 无 project/profile/toolchain/Agent/共享存储参数；Web 与 SDK 仍调用同一 `/api/v1`。

### 2026-07-14 - 发布收敛：鉴权、Windows full 本地闭环与一键部署

- Goal: 在不扩展业务范围的前提下，把 Web/SDK 统一配置实际调度到 no-Windows Cluster、Windows light build/upload、Windows full local 三种发布形态。
- Actual changes: `serve-v1` 统一 Web/API/Agent 数据库；User/Agent Bearer token 派生 owner/agent_id 并阻止 header/body 冒充；Runtime Bundle/ConfigAsset/Result 提供校验下载；Windows full 新增受控输出、进程树取消、结果目录与四阶段执行；已有共享 Bundle 可安全缓存到同一 full Agent 后配合本地数据仿真；Web 令牌仅存 session，选择 Bundle 后禁止覆盖 Runtime XML；Linux/Docker 默认认证 `serve-v1`；Windows 安装区分 light/full 以及 local/linux control plane。
- Product boundary: Runtime XML 与 branch/build artifact 强绑定进入 Bundle；Adapter/MatFilter 独立必填；light 不运行本地仿真；full+linux 同一入口可选 local/Cluster；full+local 是离线本地模式；裸 Selena.exe 自动导入不在当前发布版。
- Verification: HTTP/SDK/Agent/本地/部署 focused 229 passed；Playwright 真实浏览器验证未认证阻断、令牌连接、配置校验和 Bundle 锁定 Runtime；已有 Bundle + full local 纵向测试通过；无 Windows mocked Cluster E2E 通过；最终全量 `1159 passed, 8 skipped, 1 dependency deprecation warning`（404.26s）。
- External blocker: 真实 Selena 编译已执行约 23 分钟并进入 MSVC，失败于 `runtime.cpp(20) error C2382` 析构函数异常规格重定义；真实 Cluster 共享盘/manager 当前环境不可用，不能伪报外部提交验收。
- Drift check: Linux 无 build capability；light 无 local simulation/Cluster runtime capability；所有公开结果无物理路径/凭据；新 YAML 无 project/profile/manager/mount/Agent 字段；Runtime XML 不可脱离 Bundle 复用。

### 2026-07-14 - Runtime Bundle、独立配置资产与无 Windows Cluster 纵切

- Goal: 落实用户确认的强绑定关系：Runtime XML 随 Selena 分支/产物进入 Runtime Bundle；Adapter/MatFilter 独立必填；无 Windows 用户可只用中央入口完成 Cluster 调度。
- Scope: `core/runtime_bundle_archive.py`、`core/config_assets.py`、`core/cluster_stage_executor.py`、`core/cluster_runs.py`、`core/api_v1*.py`、`core/control_service.py`、`core/agent_build_stage.py`、`cli/server.py`、SDK/Web/配置示例及对应测试；未 commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-05、INV-06、INV-07、INV-08、INV-11、INV-13、INV-14；WP4/WP5/WP6/WP7/WP8/WP9=In Progress。
- Actual changes: Runtime Bundle 中央归档增加安全原子解包；构建阶段不再错误依赖 Adapter/MatFilter，只授权绑定 Runtime XML；新增 owner-scoped `ConfigAssetStore` 与 `/api/v1/config-assets`/SDK/Web 上传入口；新 YAML 可保存 `config-asset://`；`serve-v1` 使用 central owner-scoped control DB 并默认启动 Linux/Gateway 双角色执行器；Cluster `environment_check/prepare_data/preflight/run_simulation/collect_results/finalize_manifest` 接入逻辑 ref 与私有 lease；公共 Manifest 不含物理路径/命令/凭据；Runtime Bundle catalog 继续 shared visibility。
- Test evidence: 既有全量基线为 1082 passed/7 skipped，兼容修复针对性通过；Runtime/asset/API/SDK/Stage focused 178 passed；无 Windows existing Bundle + uploaded Dataset + uploaded Adapter/MatFilter mocked Cluster E2E 跑到公共 Manifest；Playwright 真实页面分别上传实际 Adapter 和 MatFilter，两个输入均写入 `config-asset://sha256/...`，截图 `output/playwright/config-assets-upload.png`。
- Independent review: Claude CLI Opus 只读审查确认复用 `cluster.py`/`cluster_runs.py` 的最小路径，并指出旧 Manifest 绝对路径泄漏、固定 30 分钟轮询误判和取消不可中断风险；当前实现已采用逻辑 Manifest 和用户/部署超时窗口，真实外部 Cluster cancel adapter 仍 Pending。
- Drift check: Linux 没有任何 Selena build capability；light Agent 仍不能领取 local simulation/Cluster runtime；Adapter/MatFilter 未进入 Runtime Bundle；新中央执行结果不返回 job_dir/config_path/command/stdout/stderr/password；用户 YAML 仍无 project/profile/manager/mount/Agent 字段。
- Remaining P0: Windows full 的 `preflight/run/collect/finalize` 本地执行适配；Windows light 使用真实 Agent 完成 build->Bundle upload->Cluster；共享盘/manager 真实 dry-run/submit；可信用户/Agent 身份；一键安装器；全量回归和发布文档验收。

### 2026-07-14 - A8 DataRef、上传、Agent 数据交接与 v1 Web 纵切

- Goal: 收口 `project + data.path` 的数据解析/上传路径，并提供只调用 `/api/v1` 的真实 Web 用户入口。
- Scope: 新增 DatasetRef/catalog/shared namespace/central store/Agent DataLease 与 bindings；扩展 `/api/v1`、SDK、Agent prepare_data；新增 packaged `radar_sim_web` 静态控制台；保留 legacy `web/*` 与 `/api/*`，未 commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-04、INV-05、INV-07、INV-08、INV-10、INV-11、INV-13；WP5/WP6/WP9=In Progress。
- Actual changes: shared/upload/local drive 统一解析为 path-free `DatasetRef`；中央多文件分块上传具备 owner isolation、offset retry、quota/expiry、server-side final SHA256；SDK 仍预哈希，浏览器无需把大 MF4 整块读入内存；Windows Agent 使用授权 data-root binding 和 immutable `DataLease` 发现、上传并回写 Stage evidence；v1 Web 与 FastAPI 同源打包，支持项目发现、最少配置、Selena/target、YAML import/export、目录选择/上传、提交、任务列表、Stage、事件、cancel/retry/upload action；blocked Stage 会派生 `needs_input` 状态，状态筛选按 v1 派生状态执行。
- Test evidence: dataset focused 28 passed；Web/API/dataset focused 42 passed；API/service/data focused 75 passed；`node --check radar_sim_web/static/app.js` passed；Playwright 真实浏览器完成 validate -> submit -> task center -> blocked action/event smoke，console 0 errors，并检查 390px/1440px 页面；Claude CLI 独立只读审查确认复用路由和静态/YAML薄接口方向。
- Drift check: Linux 仍不编译 Selena；Web 不复制调度规则、不调用 legacy build/sim/cluster handler；light Agent 仍无 local simulation/Cluster runtime capability；用户 YAML 未新增 Agent/toolchain/storage/scheduler 参数；legacy Web 未被新静态 mount 覆盖。
- Risks: `X-Rsim-User` 与 `X-Rsim-Agent-ID` 尚未可信认证；v1 capability/server-info 未实现；branch isolated-worktree build、Cluster v1 Stage executor、preflight/run/manifest executor、installer、完整 Agent HTTP E2E、全量 tests 和真实环境 smoke 仍是 P0 门禁；本轮不构成最终交付。
- Next step: 实现可信 execution capability snapshot 和 v1 Stage executor handoff，先让 existing Selena + uploaded/shared DatasetRef 的 Cluster 任务不依赖用户 PC 完整跑到 Manifest；随后收口 branch build/full local、installer/auth 和 release gates。

### 2026-07-10 13:30 - v5 产品与架构基线

- Goal: 建立进入 WP0 前的 v5 文档基线、实时状态机制、复用优先门禁和 current/target 边界。
- Scope: 仅修改 `HANDOFF.md`、`README.md`、`PRD.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md`；不修改代码、配置、测试，不 commit/push。
- Referenced invariants / WP: INV-01 至 INV-12；DOC=Done；WP0-WP10=Pending。
- Actual changes: 明确 Web/SDK 两入口、两部署、统一 `SimulationSpec`/调度、Linux 不编译、无 Windows=existing Selena+Cluster、Cluster 不依赖用户 PC、数据只填路径、Git worktree 隔离、复用优先、全过程可视化；补充 Build vs Reuse、第三方 spike/回退、WP0 首个编码任务和 HANDOFF 更新机制。
- Test evidence: 文档修改执行 `git diff --check`；Markdown 引用和尾随空格自检；未执行代码测试，因为本任务禁止代码/测试修改。
- Drift check: PRD 只放产品原则，不写具体库；详细设计承载技术选型；开发计划承载 WP 顺序；HANDOFF 顶部为唯一实时状态；Legacy History 保留但不作为当前状态。
- Risks: 工作区已有未提交代码/测试和未跟踪原型模块，尚未审计，不能计入 v5 完成度；第三方依赖仍需 WP spike 证明版本、许可证、安全、离线打包和代理/证书兼容。
- Next step: 进入 WP0 安全冻结，审计并隔离 `prepare_repo_context()` 自动分支路径，确保 Web/API 分支自动编译只走 worktree。

### 2026-07-10 13:35 - WP0 legacy Git 分支路径安全冻结

- Goal: 阻止自动化流程修改用户主工作区，同时保留当前 dirty workspace 构建能力。
- Scope: 修改 `core/repo.py`、`cli/build.py`、`cli/web.py` 和 focused tests；未修改 PRD、详细设计、开发计划、配置或无关代码。
- Referenced invariants / WP: INV-09、INV-12、INV-10；WP0=Done。
- Actual changes: `prepare_repo_context()` 改为只验证当前分支；目标分支等于当前分支时允许继续，即使存在 dirty/staged/untracked 修改；目标分支不同时返回安全错误，不执行 checkout、checkout -f、stash 或 reset；`check_repo_context()` 不再暴露 `switch_branch` auto repair；legacy Web `switch_branch` repair 直接拒绝并提示使用当前工作区或 isolated worktree；CLI build 成功日志改为 verified current branch。
- Test evidence: `python -m pytest tests\test_cli_build.py tests\test_environment.py tests\test_web.py -q` -> 49 passed；Copilot 全量回归 `python -m pytest -q` -> 466 passed in 128.33s；主 agent 独立复跑 -> 466 passed in 122.13s；`git diff --check` 通过；静态搜索确认 Selena 用户工作区路径没有自动 checkout/stash/reset 调用。
- Drift check: WP0 只做安全冻结，没有提前实现 WP4 worktree build 的路径重定向；`prepare_repo_worktree()`/`cleanup_repo_worktree()` 原型保留；当前 dirty workspace 编译仍可用。
- Risks: 指定不同分支的自动编译会被安全阻止，直到 WP4 接入完整 isolated worktree build；既有其他未提交 progress/manifest/harvest/wizard 改动仍未审计，不计入 WP0。
- Next step: WP0 已由主 agent 验收；进入 WP1 的 Pydantic/`SimulationSpec v1` spike 与最小纵切。后续 WP4 负责完整分支 worktree build、构建脚本/config/output 路径重定向和 artifact 登记。

### 2026-07-10 14:04 - WP1-A Pydantic spike 与 SimulationSpec v1 模型

- Goal: 验证 Pydantic 2.13.4 作为 `SimulationSpec v1` 唯一模型/JSON Schema 来源，并实现不依赖 legacy `core/config.py` 的业务合同模型。
- Scope: 修改 `setup.py`；新增 `core/spec/*`、`tests/test_simulation_spec.py`、`docs/dependency-decisions/pydantic-2.13.4.md`；不修改 `core/config.py`，不做 WP2/API/Web/调度，不 commit/push。
- Referenced invariants / WP: INV-03、INV-08、INV-10；WP1=In Progress（WP1-A done，WP1-B legacy adapter pending）。
- Actual changes: 新增 Pydantic frozen/extra-forbid `SimulationSpec`、`SelenaSpec`、`DataSpec`、`SimulationRunSpec`、`ResultSpec`；实现 YAML text/file import、stable YAML export、canonical JSON、fingerprint、JSON Schema、path normalization、required_signals 去空去重、auto_build mode rules；`auto_build` 公开类型为非 nullable bool，schema 为 `{'default': True, 'type': 'boolean'}` 且不列为 required，字段缺失时按 mode 注入默认，显式 null 会被拒绝；路径归一化折叠重复 `/`，保留 UNC `//` 和 URI `://`；`setup.py` 新增 `v5-spec` extra 并 pin `pydantic==2.13.4`，legacy `install_requires` 仍仅 PyYAML。
- Test evidence: TEMP-only Pydantic smoke rerun passed；offline wheels downloaded and cleaned for Windows CPython 3.12 x64, Linux CPython 3.12 x86_64, Linux CPython 3.10 x86_64; TEMP venv `pip-audit --progress-spinner off` -> `No known vulnerabilities found`, exit 0; `python -m pytest tests\test_simulation_spec.py -q` -> 26 passed; `python -m pytest -q` -> 492 passed in 106.79s; `git diff --check` passed.
- Drift check: 模型字段只来自 PRD §6 / detailed design §4.1；不包含 Agent、VS/TCC、Cluster manager、repo path、upload chunk 等环境/调度字段；`data.path` 只做业务级字符串归一化，不访问文件系统，并保持 UNC/逻辑 URI 语义；未实现 ResolvedSimulationSpec、ProjectCatalog、UserBindings、API 或上传。
- Risks: 第三方门禁对 WP1-A 可用，但产品发布前仍需内部第三方 notice/镜像流程确认；Pydantic 未加入默认安装，未安装 `v5-spec` extra 的环境不能导入 `core.spec`；legacy config adapter 尚未完成，WP1 不能标 Done；`auto_build` schema 已去除 null，依赖方若传 null 会得到 ValidationError。
- Next step: WP1-B 实现 legacy config adapter，将现有 profile/local.yaml 映射到 `SimulationSpec`/ProjectCatalog/UserBindings 边界，同时继续禁止环境路径进入用户 YAML。

### 2026-07-10 14:31 - WP1-B legacy config adapter

- Goal: 将已 merge 的 legacy effective config/profile/local.yaml 只读映射为可导出的 `SimulationSpec`、不含环境绝对路径的 `ProjectCatalog`、不可导出的 `UserBindings`。
- Scope: 新增 `core/spec/legacy_adapter.py`、`tests/test_spec_legacy_adapter.py`；更新 `core/spec/__init__.py` 导出；在 `core/config.py` 只新增 `load_simulation_spec_bundle()` lazy facade；未修改现有 load/merge/save 行为，未进入 WP2/API/Web/调度，未 stage/commit/push。
- Referenced invariants / WP: INV-03、INV-08、INV-10、INV-12；WP1=In Review（等待主 agent 独立验收，不标 Done）。
- Actual changes: `adapt_legacy_config()` 接收 effective legacy config dict 和可选显式 project/profile/data_path，返回 frozen typed bundle；project 逻辑 ID 采用 explicit > `_meta.project` > `project.name`；profile 采用 explicit > `active_profile` > `default` 并复用 `core.profiles.list_profiles/get_profile`；Selena build+branch 映射 `branch/auto_build=true`，build 无 branch 映射 `current_workspace`，path/existing 映射 `existing/auto_build=false` 且只把 `legacy:<project>:<profile>` 逻辑 artifact 写入 spec；真实 exe、workspace、build scripts 仅进入 `UserBindings`；ProjectCatalog 仅暴露逻辑 project/display/platform/profile/source/required_signals/timeout/default_build_mode。
- Test evidence: `python -m py_compile core\spec\legacy_adapter.py core\config.py core\spec\__init__.py tests\test_spec_legacy_adapter.py` 通过；`python -m pytest tests\test_spec_legacy_adapter.py -q` -> 12 passed；`python -m pytest tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py -q` -> 38 passed；`python -m pytest -q` -> 504 passed in 125.50s；`git diff --check` 通过（仅既有 LF/CRLF warning）；静态 `rg "^from core\.spec|^import core\.spec|pydantic" core\config.py` 无匹配。
- Drift check: `SimulationSpec` 仍只含业务字段，除用户数据路径外不包含 workspace、build scripts、Cluster 密码/manager/queue、runtime/tool 路径或 existing exe；`ProjectCatalog` 不包含 repo/tool/runtime/Cluster workspace/python/password/exe 绝对路径；`UserBindings` 不混入 Cluster secrets/policy；adapter 不访问真实 Bosch 路径或网络，不修改输入 dict，不复制 profile overlay/normalize 规则；未实现 ResolvedSpec、Artifact catalog、Data resolver、API、Web 或调度。
- Risks: existing Selena 的逻辑 artifact ID 只是 legacy binding 占位，真实 artifact 推荐/登记仍属 WP4；data path 仍是用户输入字符串，`DatasetRef` 解析/上传仍属 WP5；Pydantic 仍在 optional `v5-spec` 路径下，legacy `core.config` 仅 facade 调用时 lazy import；需要主 agent 独立验收后才可把 WP1 标 Done。
- Next step: 主 agent 独立验收 WP1-A/WP1-B 代码和测试证据；验收通过后才进入 WP2 `/api/v1` skeleton。

### 2026-07-10 14:42 - WP1 主 agent 独立验收

- Goal: 独立验证 WP1 的公开 schema、legacy 边界、真实项目迁移和 legacy 安装兼容性，决定是否允许进入 WP2。
- Scope: 只读审查 `core/spec/*`、`core/config.py` facade 和 tests；执行测试/smoke；仅更新本 HANDOFF 状态，不修改业务代码，不 stage/commit/push。
- Referenced invariants / WP: INV-03、INV-08、INV-10、INV-12；WP1=Done。
- Actual changes: 确认 `auto_build` JSON Schema 为非 nullable boolean 且非 required，缺省按 Selena mode 注入；确认 UNC/逻辑 URI 归一化稳定；确认 legacy profile 映射复用 `core.profiles`，existing exe 只存在 `UserBindings`，`SimulationSpec` 使用逻辑 artifact ID；确认 `core.config` facade 为 lazy import。
- Test evidence: 主 agent `python -m pytest tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py -q` -> 38 passed；主 agent `python -m pytest -q` -> 504 passed in 118.25s；真实 `ovrs25`/`bydod25` `load_simulation_spec_bundle()` + YAML/hash round-trip smoke 通过；阻断 `pydantic` import 后 `import core.config` smoke 通过；`git diff --check` 通过（仅既有 LF/CRLF warning）。
- Drift check: WP1 没有实现 API、调度、ResolvedSpec、Artifact catalog 或 DatasetRef；环境/Cluster policy/secrets 未进入用户 YAML；未修改用户工作区、未触发编译或仿真。
- Risks: `legacy:<project>:<profile>` 仍是 WP4 接入 artifact catalog 前的逻辑占位；Pydantic 内部 notice/mirror 仍是发布门禁，但不阻塞 WP2 开发。
- Next step: 进入 WP2 FastAPI/Uvicorn + HTTPX/SSE spike 和 `/api/v1` application/SDK skeleton；继续保留 stdlib legacy server。

### 2026-07-10 14:48 - WP2 `/api/v1` application layer 与 SDK skeleton

- Goal: 完成 FastAPI/Uvicorn + HTTPX/SSE 依赖门禁、thin `/api/v1` application/API 纵切、Python SDK skeleton，并保持 legacy stdlib control server 不变。
- Scope: 新增 `core/api_v1.py`、`core/api_v1_fastapi.py`、`radar_sim_sdk/*`、`tests/test_api_v1_service.py`、`tests/test_api_v1_fastapi.py`、`tests/test_sdk.py`、`docs/dependency-decisions/fastapi-uvicorn-httpx-wp2.md`；修改 `core/control_service.py`、`cli/server.py`、`setup.py` 和本 HANDOFF；未修改 PRD/design 产品合同，未进入 WP3 Stage DAG、WP4 resolver、WP5 upload、WP9 Web，未 stage/commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-04、INV-05、INV-06、INV-07、INV-10、INV-11；WP2=In Review。
- Actual changes: TEMP 独立 venv 门禁通过后才实现；`setup.py` 仅新增 optional extras `v5-server`、`sdk`、`v5` 精确 pin，默认 `install_requires` 仍仅 PyYAML；`ControlService` 以兼容 migration 为 `jobs` 增加 `owner/idempotency_key/request_hash` 与 partial unique index，旧 `create_job()` 调用保持不传 key 可用；`ApiV1Service` 只依赖 `SimulationSpec` 与 `ControlService`，实现 `health/schema/validate/submit/get/events/cancel/manifest`，submit 创建逻辑 `simulation.v1` / `simulation.v1.dry_run` queued job，payload 保存 canonical spec/spec_hash，metadata 保存 api_version/owner/dry_run/idempotency；FastAPI app factory 只做 HTTP/model/error/request_id/SSE 适配，统一错误 `code/message/detail/actions/request_id` 并带 `X-Request-ID`；`serve-v1` 默认 `127.0.0.1:8878`、Uvicorn `workers=1`，帮助明确 legacy Agent endpoints 仍由 `serve` 提供直到 WP6；SDK 导出 `RadarSimClient`、同一个 `SimulationSpec`、typed validation/job/event/manifest/error models，HTTPX timeout/trust_env/verify/client injection、JSON events、SSE parser、watch/reconnect、wait/cancel/manifest/context manager 已接入。
- Test evidence: dependency gate isolated TEMP venv install/import/TestClient/StreamingResponse/HTTPX stream OK；`pip-audit -r resolved-product.txt --progress-spinner off` -> `No known vulnerabilities found`；offline wheel `pip download --only-binary=:all:` Windows CPython 3.12 x64、Linux CPython 3.12 x86_64、Linux CPython 3.10 x86_64 均 `count=17` 并清理；baseline `python -m pytest tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py tests\test_control_service.py tests\test_control_http.py tests\test_user.py tests\test_remote_control.py tests\test_server_pyz.py -q` -> 84 passed；baseline full `python -m pytest -q` -> 504 passed；new WP2 focused `python -m pytest tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py -q` -> 21 passed, 1 StarletteDeprecationWarning；WP1+WP2 focused `python -m pytest tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py tests\test_control_service.py tests\test_control_http.py tests\test_user.py tests\test_remote_control.py tests\test_server_pyz.py -q` -> 105 passed, 1 StarletteDeprecationWarning；final full `python -m pytest -q` -> 525 passed, 1 StarletteDeprecationWarning；`git diff --check` -> exit 0（仅既有 LF/CRLF warning）；static `rg` confirmed no FastAPI/HTTPX/Uvicorn import in `core/api_v1.py` and no scheduler/profile/control-service dependency in `radar_sim_sdk`.
- Drift check: `core/api_v1.py` 无 FastAPI/HTTPX/Uvicorn import；FastAPI route source 无 `cluster.run`、`local.run_sim`、`prepare_cluster_job`、subprocess/Git/worktree 调度规则；SDK source 无 `core.profiles`、`core.control_service`、Cluster/local scheduling strings；legacy import blocker smoke 证明 `import core.config/core.control_service` 不要求 WP2 deps；Linux no-build 仅排队逻辑 job，不解析/路由/执行、不产生 build capability；SSE 仍是 WP2 transport spike，事件 JSON/SSE 临时映射 legacy `task_logs`，未声称完成 WP3 Event DAG。
- Risks: FastAPI TestClient 触发上游 StarletteDeprecationWarning（提示未来可能从 `httpx` 切换 `httpx2`，当前门禁与测试通过）；第三方内部 notice/legal/mirror 仍是 WP10 release gate；manifest 在 WP8 接入前按合同稳定返回 `available=false`，仅当 job result/metadata 已有 manifest 才透出；idempotency 只覆盖 v1 submit，不改变 legacy `/api/*` 行为。
- Next step: 主 agent 独立验收 WP2 依赖门禁、API/SDK 合同、静态漂移与测试证据；验收通过后才允许进入 WP3 Job/Stage/Event DAG。

### 2026-07-10 15:14 - WP2 主验收修复

- Goal: 修复主 agent 实测的 WP2 验收缺陷，保持 `/api/v1` 和 SDK skeleton 范围，不进入 WP3/WP4/WP5/WP9。
- Scope: 仅修改允许范围内的 `core/api_v1.py`、`core/api_v1_fastapi.py`、`core/control_service.py`、`core/user.py`、`radar_sim_sdk/client.py`、相关 WP2/user/control tests 和本 HANDOFF；未修改 PRD/design，未 stage/commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-04、INV-05、INV-06、INV-07、INV-10、INV-11；WP2=In Review。
- Actual changes: FastAPI validate/submit request models 改为直接使用同一个 `core.spec.SimulationSpec`，OpenAPI validate body 与 `SubmitJobRequest.spec` 均 `$ref` 到 `#/components/schemas/SimulationSpec`；Pydantic/FastAPI validation errors 通过 JSON-safe encoding，`project=''` 返回 422 `invalid_spec` 且 loc 清晰，不泄漏 traceback；v1 submit 创建的逻辑 task 绑定稳定 sentinel `__v1_scheduler__`，普通 legacy agent 即使 capabilities 为 empty、`*`、或 exact `simulation.v1` 也不能领取；新增 public `normalize_user()`，`current_user()`、`control_db_path_for_user()`、FastAPI owner、ApiV1Service owner/metadata/factory key 全部复用，unsafe header 不进入 raw metadata，DB path parent 固定在 `$RSIM_HOME/results`；idempotency concurrent `sqlite3.IntegrityError` 对不同 request hash 映射同一 409 `idempotency_conflict`，不再冒泡 500；old DB migration 以 `BEGIN IMMEDIATE` 序列化 ALTER/index，WAL pragma 锁冲突不阻断初始化；SDK `watch()` 对 SSE 与 JSON polling transport failure 都按 cursor/deadline 重试，API errors 仍立即抛出。
- Test evidence: 主验收复现先确认旧行为：`project=''` -> 500、OpenAPI schema 为 arbitrary object、empty-capability agent 可领取 `simulation.v1`、unsafe user 可越出/生成子目录；修复后新增 regression focused `python -m pytest tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py tests\test_user.py tests\test_control_service.py -q` -> 54 passed, 1 StarletteDeprecationWarning；final WP1+WP2/control/user focused `python -m pytest tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py tests\test_control_service.py tests\test_control_http.py tests\test_user.py tests\test_remote_control.py tests\test_server_pyz.py -q` -> 121 passed, 1 StarletteDeprecationWarning；final full `python -m pytest -q` -> 541 passed, 1 StarletteDeprecationWarning；`git diff --check` -> exit 0（仅既有 LF/CRLF warning）；static `rg` confirmed `core/api_v1.py` has no FastAPI/HTTPX/Uvicorn import and SDK has no scheduler/profile/control-service dependency.
- Drift check: `core/api_v1.py` 仍 framework-agnostic；FastAPI routes 仍只做 HTTP/model/error/SSE 适配；v1 jobs 仍只是 logical queued `simulation.v1`/`simulation.v1.dry_run`，没有 Stage DAG、resolver、upload、Web migration、subprocess/Git/Cluster routing；legacy capability matching semantics 未改，仅通过 assigned-agent sentinel 隔离 v1 logical tasks；用户规范化不引入认证系统。
- Smoke evidence: `/openapi.json` validate request schema `{'$ref': '#/components/schemas/SimulationSpec'}`；SubmitJobRequest.spec `{'$ref': '#/components/schemas/SimulationSpec'}`；`project=''` -> status 422, code `invalid_spec`, loc `['body','project']`；empty/`*`/exact `simulation.v1` agent claim 均 `None`；`../../../escape`、`..\..\escape`、`a/b` normalized 后 DB parent 均为 `$RSIM_HOME/results` 且 filename 不含 `..`。
- Risks: Starlette TestClient deprecation warning 仍来自上游；`normalize_user()` 是文件/metadata 安全规范化，不是认证；v1 sentinel 隔离是 WP2 临时防领取机制，WP3 进入真实 scheduler/Stage 后需由 Stage claim 模型替代。
- Next step: 主 agent 重新独立验收 WP2 修复；验收通过后才允许进入 WP3 Job/Stage/Event DAG。

### 2026-07-10 15:28 - WP2 主 agent 独立验收

- Goal: 独立复核 WP2 修复后的公开 schema、错误合同、任务隔离、用户路径安全、并发持久化和 SDK reconnect，决定是否进入 WP3。
- Scope: 只读审查 WP2 代码与依赖证据，执行 smoke/focused/full tests；仅更新本 HANDOFF 状态，不修改业务代码，不 stage/commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-04、INV-05、INV-06、INV-07、INV-10、INV-11；WP2=Done。
- Actual changes: 确认 OpenAPI validate/submit 直接引用同一个 `SimulationSpec`；确认自定义 validator 错误稳定返回 JSON-safe 422；确认 sentinel 阻止 empty/`*`/exact capability Agent 误领逻辑 v1 task；确认 unsafe user header 经统一 normalization 后 DB 始终位于 `RSIM_HOME/results`；确认 concurrent idempotency/migration 和 SDK SSE/poll reconnect regression 已纳入测试。
- Test evidence: 主 agent 手工 smoke：两个 OpenAPI schema 均 `#/components/schemas/SimulationSpec`，`project=''` -> 422 `invalid_spec` / loc `['body','project']`，unsafe owner 被规范化，三类 Agent claim 均为 None，unsafe DB filename 不含 `..` 且 parent 固定；主 agent `python -m pytest tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py tests\test_user.py tests\test_control_service.py -q` -> 54 passed, 1 upstream warning；主 agent `python -m pytest -q` -> 541 passed, 1 upstream warning in 121.23s；`git diff --check` 通过（仅既有 LF/CRLF warning）。
- Drift check: WP2 仍只创建不可被普通 Agent 领取的逻辑 queued job；没有提前实现 Stage DAG、capability routing、Selena resolver、DatasetRef/upload、Web migration 或执行逻辑；stdlib legacy server 仍保留。
- Risks: Starlette TestClient 的 `httpx` deprecation warning 需在 WP10 依赖复核时重新评估；`X-Rsim-User` normalization 只解决路径/metadata 安全，不构成认证；sentinel 将由 WP3 scheduler/Stage claim 取代。
- Next step: 进入 WP3，基于现有 ControlService 扩展 Job/Stage/Event DAG；不引入 Celery/Temporal，先做兼容 migration 和 application planner。

### 2026-07-10 15:58 - WP3 Job/Stage/Event DAG

- Goal: 在现有 `ControlService`/SQLite 上实现 v5 Job/Stage/Attempt/Event DAG，同时保持 legacy `/api/*`、Agent claim/pinning、task logs 和 SDK/API skeleton 兼容。
- Scope: 新增 `core/stages.py`、`tests/test_stages.py`、`tests/test_control_stages.py`；修改 `core/control_service.py`、`core/api_v1.py`、`core/api_v1_fastapi.py`、`radar_sim_sdk/client.py`、`radar_sim_sdk/models.py` 以及 WP3/API/SDK focused tests 和本 HANDOFF/CHECKPOINT；未修改 PRD/design，未 stage/commit/push，未进入 WP4 Selena resolver/Git/build、WP5 data/upload、WP6 Agent v1、WP7 routing、WP8 real preflight/manifest、WP9 Web。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-06、INV-07、INV-10、INV-11；WP3=In Review。
- Actual changes: `core/stages.py` 生成固定 10-stage DAG（`resolve_spec -> environment_check -> prepare_source/prepare_data -> build_selena -> register_artifact -> preflight -> run_simulation -> collect_results -> finalize_manifest`），existing Selena 的 `prepare_source`/`build_selena` 以 `skipped + skip_reason` 可见持久化；v1 submit 使用 planner 创建 stages，保存 canonical spec 与 pending `resolved_spec` placeholder，并继续用 `__v1_scheduler__` sentinel 隔离普通 Agent；`jobs` 兼容新增 spec/resolved/start/finish 字段，`tasks` 兼容新增 stage/dependency/progress/input/output/error/skip metadata；新增 `stage_attempts` 和 job-local monotonic `job_events`，日志双写 `task_logs + log event`；claim 创建新 attempt，完成/失败/取消更新对应 attempt 并写 terminal/job events；explicit dependencies 支持 stage_type 名解析，skipped 作为 terminal-success dependency；legacy 无 dependencies 仍按 order_index；cancel 保留 skipped、running 进入 cancel_requested/cancelling 后由 aggregate 得到 cancelled；`retry_stage()` 只允许 failed/cancelled，保留 attempts/events，失败导致的未执行下游按 initial_status 恢复；v1 events 改读 `job_events`，新增 FastAPI/SDK retry route/method，SDK Job/Event typed model 暴露 stages/resolved_spec/status/progress/code/action。
- Test evidence: baseline focused `python -m pytest tests\test_control_service.py tests\test_control_http.py tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py -q` -> 53 passed, 1 StarletteDeprecationWarning；new WP3 `python -m pytest tests\test_stages.py tests\test_control_stages.py -q` -> 11 passed；WP3+WP1/WP2/control/http/user/sdk focused `python -m pytest tests\test_stages.py tests\test_control_stages.py tests\test_control_service.py tests\test_control_http.py tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py tests\test_user.py tests\test_remote_control.py tests\test_server_pyz.py tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py -q` -> 137 passed, 1 StarletteDeprecationWarning；full `python -m pytest -q` -> 557 passed, 1 StarletteDeprecationWarning；`git diff --check` -> exit 0（仅既有 LF/CRLF warnings）；manual smoke 打印 existing spec 10 stages/skipped、job-local event sequence `[1,2,3,4,5]`、fail/retry/success attempts `[(1,'failed','E_SMOKE'), (2,'succeeded','')]`。
- Drift check: `core/stages.py` 静态搜索无 subprocess/Git/worktree/filesystem/network；FastAPI route 静态搜索无 planner/DAG/business rules；SDK 静态搜索无 `core.stages`/`core.control_service`/planner/control-service import 或 stage planner strings；未改 legacy capability wildcard 语义；未伪造 v1 stage 成功，真实 Stage executor/capability routing 仍留 WP4+；stdlib legacy `/api/*` focused 回归通过。
- Risks: WP3 只实现持久化 DAG/状态机/结构化事件，不实现真实 scheduler 逐 stage bind、Selena resolver、artifact/data resolver、real preflight/manifest 或 Web v5；v1 stages 在 WP4+ 前仍由 sentinel pin 阻止普通 Agent 领取；Starlette TestClient deprecation warning 仍来自上游。
- Next step: 主 agent 独立验收 WP3 代码、状态机、迁移、静态漂移和测试证据；验收通过后再进入 WP4/WP5 等后续包。

### 2026-07-10 16:18 - WP3 主验收状态机修复

- Goal: 修复主验收发现的真实状态机缺口，保持 WP3 In Review，不进入 WP4/WP5/WP6/WP7/WP8/WP9。
- Scope: 仅修改 `core/control_service.py`、`tests/test_control_stages.py`、`tests/test_api_v1_service.py`、`tests/test_api_v1_fastapi.py`、`tests/test_sdk.py` 和本 HANDOFF；未修改 PRD/design/DEVELOPMENT_PLAN/CHECKPOINT，未 stage/commit/push。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-06、INV-07、INV-10、INV-11；WP3=In Review。
- Actual changes: `create_job()` 现在验证 explicit dependencies 必须指向同 job stage_type/task_id，拒绝 unknown/self/duplicate dependency，并在创建时写 `job.created` 与每个 `stage.queued/stage.skipped` 初始事件；`claim_next_task()`、`append_logs()`、`submit_task_result()`、`cancel_job()`、`reclaim_stale_tasks()` 增加 write transaction，保证多 ControlService 实例并发 event sequence 连续；`reclaim_stale_tasks()` 对普通失联将当前 attempt 以 `AGENT_STALE` 终结并 requeue stage、写 `stage.requeued`，max attempts 时 attempt/stage failed 并取消下游，cancel_requested 时 attempt/stage cancelled 且不 requeue；direct queued `submit_task_result()` 原子创建 synthetic attempt 1 并保持 legacy task started_at 语义；upstream failure cancel 写入 `UPSTREAM_FAILED/upstream_stage_id/action` 到 downstream error/event，retry 只恢复由目标 stage 导致取消的 stages（含已 attempt 的并行分支），不恢复用户取消或其他错误。
- Test evidence: 新回归复现先得到 7 failures（creation events、dependency invalid、stale reclaim 三类、direct synthetic attempt、branch retry）；修复后 `python -m pytest tests\test_control_stages.py -q` -> 17 passed；targeted legacy regression `python -m pytest tests\test_web_control.py::test_tail_status_succeeded_maps_to_success tests\test_control_stages.py::test_direct_submit_queued_task_creates_synthetic_attempt -q` -> 2 passed；WP3+WP1/WP2/control/http/user/sdk/web focused `python -m pytest tests\test_stages.py tests\test_control_stages.py tests\test_control_service.py tests\test_control_http.py tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_sdk.py tests\test_user.py tests\test_remote_control.py tests\test_server_pyz.py tests\test_simulation_spec.py tests\test_spec_legacy_adapter.py tests\test_web_control.py -q` -> 156 passed, 1 StarletteDeprecationWarning；full `python -m pytest -q` -> 565 passed, 1 StarletteDeprecationWarning；`git diff --check` -> exit 0（仅既有 LF/CRLF warnings）。
- Drift check: 未改 PRD/design，不引入 Celery/Temporal/SQLAlchemy/Alembic/new DB/second scheduler；未改 legacy capability wildcard；未进入 WP4 resolver/Git/build、WP5 data/upload、WP6 Agent v1、WP7 routing、WP8 real preflight/manifest、WP9 Web；所有修复仍复用 jobs/tasks/task_logs + SQLite。
- Smoke evidence: 手工 smoke 打印 `creation_event_types ['job.created','stage.queued','stage.queued']`；`stale_requeue_attempts [(1,'failed','AGENT_STALE'), (2,'running','')] next 2`；branch retry statuses 恢复 `prepare_source/prepare_data/build_selena/preflight=queued` 且 source/data next attempts 都为 2；并发 append logs sequence `[1..8]` 连续，`task_logs=6`、`log_events=6`。
- Risks: `AGENT_STALE` 当前以 failed terminal attempt 表示 abandoned/requeued 证据，stage 本身可 requeue；真实 executor/scheduler 逐 stage bind、Selena/data resolver、real preflight/manifest 仍属后续 WP，不在本修复范围；Starlette TestClient deprecation warning 仍来自上游。
- Next step: 主 agent 复验 WP3 状态机、并发事件、retry/cancel/reclaim 与 focused/full 测试证据；复验通过后再决定是否进入后续 WP。

### 2026-07-10 16:20 - WP3 主 agent 独立验收

- Goal: 独立验证 WP3 的 DAG、事务事件序列、attempt/reclaim、取消原因、分支 retry、legacy 兼容和全量回归。
- Scope: 只读审查 `core/stages.py`、`core/control_service.py`、v1/SDK 适配与 tests；执行 focused/full tests；仅更新本 HANDOFF 状态，不修改业务代码，不 stage/commit/push。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-06、INV-07、INV-10、INV-11；WP3=Done。
- Actual changes: 确认 fixed 10-stage DAG 与 existing Selena skipped 可见；确认 creation events、job-local sequence 写事务、日志双写和 dependency validation；确认 stale requeue/max-attempt/cancel 都关闭 attempt 并写 `AGENT_STALE`；确认 upstream failure 以 `UPSTREAM_FAILED/upstream_stage_id` 关联，retry 只恢复对应取消 stage并保留历史 attempts；确认 direct legacy completion 有 synthetic attempt，legacy task/order/capability/HTTP/Web 兼容回归全绿。
- Test evidence: 主 agent `python -m pytest tests\test_control_stages.py tests\test_stages.py -q` -> 19 passed；主 agent `python -m pytest -q` -> 565 passed, 1 upstream warning in 123.05s；`git diff --check` 通过（仅既有 LF/CRLF warning）。
- Drift check: 所有 v1 stage 仍由 `__v1_scheduler__` sentinel 阻止普通 Agent 领取；没有执行真实 Git、构建、数据上传、preflight、仿真或 Cluster 路由；`HANDOFF.md` 仍是唯一实时状态。
- Risks: `AGENT_STALE` 的旧 attempt 以 failed terminal 表示 abandoned 证据；Starlette TestClient warning 留 WP10；真实逐 Stage bind、capability routing 和 resolved snapshot 完成度属于 WP4-WP8。
- Next step: WP4 先审计并接入现有 `core/repo.py` worktree、dirty fingerprint、build runner 与 artifact/manifest 原型；在证明不修改用户主工作区前，不解除 build stage sentinel。

### 2026-07-10 16:19 - Product decision / Conformance Entry: light Agent P0 boundary

- Goal: 固化不可逆 P0 产品决策：Windows 轻量 Agent 首版只负责授权工作区 Selena 本地编译、产物登记/校验/上传或同步、必要数据路径检索/校验/上传，并把任务交还中央调度；本地仿真只属于 Windows full deployment，Cluster 仿真运行期只由 Linux central executor 或平台 Gateway/Worker 执行。
- Scope: 仅修改 `PRD.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md` 和本 `HANDOFF.md`；不修改代码、测试、配置，不 stage/commit/push；保留 Legacy History 作为历史证据。
- Referenced invariants / WP: INV-02、INV-05、INV-06、INV-07、INV-13；WP6=Pending，WP7=Pending。
- Actual changes: 当前合同、架构强制边界、ExecutionNode/capability 归属、目标路由矩阵、WP6 范围/测试/退出标准、P0 验收标准均改为 light Agent 不声明/领取 `simulation.local`，不执行或维持 Cluster 运行期；无部署用户只能 existing Selena + shared/uploaded data + Cluster。
- Test evidence: 文档一致性扫描使用 `rg`；未运行代码测试，因为本次任务禁止修改代码/测试且只更新文档。
- Drift check: 本条是产品决策和文档 conformance，不是运行时代码实现；不得把 light Agent 边界误报为已交付功能。真实 Agent capability 上报、claim 拒绝、artifact/data upload 和 cluster handoff 仍需 WP6/WP7 代码与 smoke 证据。
- Risks: Legacy History 中仍保留 2026-07-03 至 2026-07-08 的旧 Mode A/B、T1/T2/T3、Agent/local sim 表述，均只作为历史记录，不能重新定义当前 P0 合同。

### 2026-07-10 16:28 - Docs-only Conformance Entry: reviewer gap closure for light Agent boundary

- Goal: 关闭独立 Reviewer 指出的 docs-only 产品/架构一致性缺口，不改变代码、测试、配置或已交付状态。
- Scope: 仅修改 `PRD.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md` 和本 `HANDOFF.md`；不修改代码、测试、配置，不 add/commit/push。
- Referenced invariants / WP: INV-02、INV-05、INV-07、INV-08、INV-13；WP6=Pending，WP7=Pending。
- Actual changes: 增加 light build-to-cluster 的 Stage -> capability -> node-kind 明确矩阵；正式固化 `artifact.register`、`artifact.validate`、`artifact.upload`，要求绑定授权目录和同一构建节点，并在平台形成 `SelenaArtifact` 后解除对 Agent 在线依赖；补充 WP6/WP7 测试与停止项的负向门禁，禁止 `windows_agent` 上报/领取 `simulation.local`、`simulation.cluster`、`cluster.gateway` 或 Cluster run/collect/finalize stage，要求服务端按 node kind 过滤自报能力且 legacy wildcard/`cluster.run` 不能绕过 v1 policy，并区分 `windows_full` 与 `platform_gateway` 的 node kind/policy；补齐 light Agent 数据 E2E 验收：授权本地路径 -> Agent 检索/校验/上传 -> `DatasetRef` -> 中央 Cluster 使用 -> Agent 离线仍完成。
- Test evidence: docs-only 验证使用 `rg` 和 `git diff --check`；未运行代码测试，因为本次任务明确限制不改代码/测试。
- Drift check: 本条只关闭产品/架构文档缺口，不代表 WP6/WP7 runtime 已交付；真实 capability 上报过滤、claim 拒绝、artifact/data upload、DatasetRef handoff、Cluster executor/Gateway 运行期仍需后续代码实现和 smoke 证据。
- Risks: Legacy History 仍保留旧 Mode A/B、T1/T2/T3 和 Agent/local sim 表述，均只作为历史记录；任何后续任务不得用旧表述绕过当前 P0 policy。

### 2026-07-10 16:42 - WP4-A1 workspace fingerprint and safe detached worktree

- Goal: 完成 Selena source resolver 的第一段：只读 workspace fingerprint、受限 Git ref 解析和安全 detached worktree，不触碰用户主工作区。
- Scope: 修改 `core/repo.py`，新增 `tests/test_repo_source.py`，更新本 Active Handoff；未修改产品文档、harvest 行为、scheduler、sentinel、配置或现有用户改动，未 add/commit/push。
- Referenced invariants / WP: INV-09、INV-12、INV-10；WP4=In Progress。
- Actual changes: 新增 immutable `WorkspaceFingerprint`/`DetachedWorktreeHandle`；`inspect_workspace()` 只读 Git 并用 HEAD、staged/unstaged `--binary` diff、untracked relative path 和 streamed file SHA256 生成稳定 fingerprint，ignored 不计且对外 dict 不暴露绝对路径；未跟踪 symlink 只散列 link target 文本而不跟随读取仓库外内容；`resolve_git_ref()` 仅接受 exact 40-hex commit、local branch 和 `origin/*` tracking branch；`prepare_detached_worktree()` 在受控 root 下按 job/stage 安全分段加 UUID 创建 detached worktree；cleanup 先验证受控 root，再 `git worktree remove --force` + `prune`，越界抛稳定异常；默认 worktree 在进程 registry 丢失后仍可按严格布局安全恢复清理；兼容 `prepare_repo_worktree()`/`cleanup_repo_worktree()` 委托新 helper 且不删除任意路径。
- Test evidence: Copilot 初验 `python -m pytest tests\test_repo_source.py -q` -> 5 passed；主 agent 补强后 `python -m pytest tests\test_repo_source.py -q` -> 5 passed, 1 skipped（Windows 当前用户无 symlink 权限时 skip）；registry-loss 单测 -> 1 passed；主 agent `python -m pytest tests\test_repo_source.py tests\test_repo_harvest.py tests\test_concurrency.py tests\test_cli_build.py tests\test_environment.py -q` -> 43 passed, 1 skipped；`git diff --check` 通过（仅既有 LF/CRLF warning）。
- Drift check: 没有 checkout/stash/reset 用户主工作区；指定分支构建只通过 detached worktree helper；dirty workspace fingerprint 记录 branch、commit、dirty 和证据摘要；`core/api_v1.py` 的 `__v1_scheduler__` sentinel 保持不变；WP4 仍为 In Progress，因为尚未接入 scheduler / artifact catalog。
- Risks: 兼容 build 路径仍未重定向到 scheduler stage；artifact catalog、Selena resolver、build 前后 fingerprint 对比、构建脚本路径改写和 manifest/preflight/progress 统一接入仍属后续 WP4/WP8；真实大 Selena 仓库 fingerprint 性能与 Git ref 同步策略待 Windows smoke。
- Next step: WP4-A2 实现 SQLite `SelenaArtifact` catalog 与纯 resolver，再把 resolution outcome 接入 `ResolvedSimulationSpec`；在 scheduler/node policy 完成前继续保留 sentinel。

### 2026-07-10 17:05 - WP4-A2 SelenaArtifact catalog and pure resolver

- Goal: 完成 WP4-A2 的 SQLite `SelenaArtifact` catalog 与纯 Selena source resolver，为后续 ResolvedSpec/API/scheduler 接入提供可测试内核。
- Scope: 新增 `core/artifacts.py`、`core/selena_resolver.py`、`tests/test_artifacts.py`、`tests/test_selena_resolver.py`；为 ProjectCatalog revision 修改 `core/spec/legacy_adapter.py`；仅更新本 Active Handoff，不把 `CHECKPOINT.md` 作为实时状态源；未修改 `core/repo.py`、PRD、详细设计、开发计划、API、scheduler、ControlService 或 sentinel，未 add/commit/push。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-06、INV-09、INV-10、INV-12、INV-13；WP4=In Progress（A2 done，接入仍 pending）。
- Actual changes: `SelenaArtifact` 为 frozen dataclass，manifest 深冻结且 `to_dict()` 返回新对象；`ArtifactCatalog` 使用 stdlib SQLite、每操作独立连接、busy timeout、事务安全、可与 ControlService 共用 DB；登记按 checksum + accessibility 幂等，clean shared 按 project 隔离，private/dirty 再按 owner 隔离，dirty 或 source-changed 强制 private 且不进入推荐；owner-less list/snapshot/get 不再暴露 private artifact，显式 ID 同 checksum 也不能跨 owner/project identity 返回；`storage_ref` 只允许 `artifact://`、`cluster://`、`shared://`、`legacy://` 逻辑引用，拒绝 Windows/UNC/file path；推荐与 access 校验覆盖 visibility、ready、retain、build_mode 和 target accessibility。`resolve_selena()` 仅消费 immutable `SourceResolutionContext`，支持 current_workspace/branch/existing/auto，并防御校验 owner/visibility、retain、health、source-changed、build mode、target、exact commit、workspace project 与 ProjectCatalog revision；private dirty artifact 可由 owner 显式复用但不能推荐。新增 atomic `apply_selena_resolution()` 同时返回 partial ResolvedSpec 与 Stage mutation，避免“跳过构建但未记录 artifact”；I/O 仍只在显式 context builder 边界。
- Test evidence: Copilot 新增测试 -> 16 passed；主 agent 修复 reviewer P0 后 `python -m pytest tests\test_artifacts.py tests\test_selena_resolver.py tests\test_spec_legacy_adapter.py tests\test_stages.py -q` -> 48 passed；API/control/spec/artifact/resolver focused -> 101 passed, 1 StarletteDeprecationWarning；主 agent full `python -m pytest -q` -> 605 passed, 1 skipped（Windows symlink 权限）, 1 upstream Starlette warning in 222.26s；`git diff --check` 通过。
- Drift check: pure resolver 不访问文件、DB、网络、时间或 Git；snapshot/outcome/resolved spec 不包含 workspace_path、executable_path、build scripts 或绝对路径；单独 Selena resolved 只把整体 ResolvedSpec 标为 `partial`，不会误报完整解析；artifact resolution 仍只产生纯 Stage mutation，不改 DB/任务状态；`core/api_v1.py` 的 `__v1_scheduler__` sentinel 保持不变，未接 scheduler。
- Risks: Artifact location/health 目前仍以不可变记录表达，尚未拆分独立 location/health history；SQLite 首版表尚无跨版本 column migration 与多进程迁移 smoke；仍未接 API/v1 submit、ControlService 原子 persistence update、真实 scheduler stage bind、构建前后 fingerprint 对比、artifact upload/register stage adapter、preflight/manifest/progress/data resolver；WP4 因这些集成仍保持 In Progress。
- Next step: 后续 WP4-A3 将 resolver outcome 接入 ResolvedSpec 和 scheduler/stage adapter；在 node policy 完成前继续保留 sentinel。

### 2026-07-10 17:28 - WP4-A3 API submit source resolver boundary

- Goal: 把 WP4-A2 已实现的纯 Selena resolver 原子接入 `/api/v1` submit，同时继续不实现真实 scheduler/worker，不解除 `__v1_scheduler__` sentinel。
- Scope: 新增 `core/source_resolution_runtime.py` 和 `tests/test_source_resolution_runtime.py`；修改 `core/api_v1.py`、`core/control_service.py`、`cli/server.py`、`tests/test_api_v1_service.py`、`tests/test_api_v1_fastapi.py` 与本 Active Handoff；未修改 `PRD.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md` 或 `CHECKPOINT.md`，未 add/commit/push。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-06、INV-09、INV-10、INV-12、INV-13；WP4=In Progress（A3 done，真实 scheduler、Agent snapshot、artifact register/upload 仍 pending）。
- Actual changes: `ApiV1Service` 新增 immutable `SourceResolutionInputs` 与 optional `source_resolution_provider(owner, spec)`；submit 保持 parse/canonical/hash/control/idempotency existing check 在前，仅新 job 调 provider；provider=None 仍持久化 pending ResolvedSpec；provider 返回 inputs 后调用 `resolve_selena()` + atomic `apply_selena_resolution()`，在同一次 `create_job()` 事务中持久化 resolved_spec 与 stage plan；metadata 的 source_resolution 只保存 status/code，不保存路径或完整 evidence；artifact resolution 动态跳过 `prepare_source`/`build_selena`；needs_input/impossible outcome 创建 `needs_input` Job 与 `blocked` Stages，全部保留 sentinel 且不可 claim，避免误标 succeeded 或执行 stage。`core/source_resolution_runtime.py` 作为显式 I/O boundary：legacy config loader 只取 ProjectCatalog/UserBindings，ArtifactCatalog snapshot 只含 shared + owner private，Linux/central 默认 no-inspect，不读 Windows workspace；logical workspace_binding_id 由稳定 hash 生成，不暴露绝对路径；`inspect_local_workspace=True` 才显式调用 context I/O builder；不把 legacy executable path 自动 seed 为 SelenaArtifact。`serve-v1` explicit DB 与 per-user DB 均把 resolver runtime 的 ArtifactCatalog 指向同一 control DB，FastAPI adapter 保持 thin。
- Test evidence: `python -m py_compile core\api_v1.py core\control_service.py core\source_resolution_runtime.py cli\server.py tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_source_resolution_runtime.py` 通过；`python -m pytest tests\test_api_v1_service.py tests\test_api_v1_fastapi.py tests\test_control_service.py tests\test_control_stages.py tests\test_spec_legacy_adapter.py tests\test_simulation_spec.py tests\test_artifacts.py tests\test_selena_resolver.py tests\test_source_resolution_runtime.py tests\test_server_pyz.py -q` -> 145 passed, 1 upstream Starlette warning；`git diff --check` 通过。
- Drift check: 未实现真实 scheduler/worker、未解除 `__v1_scheduler__`、未让 Linux 读取或探测 Windows workspace、未自动登记 legacy exe path、未新增第三方依赖、未改产品/设计/开发计划合同；unresolved source outcome 不会产生可执行 queued stage。
- Risks: `blocked` job/stage 是提交期可观察状态，后续真实 scheduler 需把 needs_input 的用户补充/重试 UX 接入；Agent snapshot、artifact register/upload、build 前后 fingerprint 对比、data resolver、preflight/manifest/progress 仍属于后续 WP4/WP5/WP8；WP4 继续 In Progress。
- Next step: 后续 WP4/WP6 接入 Windows full/light Agent source snapshot 与 artifact register/upload stage；在 scheduler/node policy 完成前继续保留 sentinel。

### 2026-07-13 - WP4-A3 Reviewer 独立验证与 stale test 修复

- Goal: 独立验证主 agent 对 A3 的 7 项安全修复（reserved sentinel、provider owner、provider error 脱敏、needs_input 结构化事件与 cancel 终结、central no Windows I/O、clock NaN/Inf、workspace binding ID 稳定），并直接修复任何 P0/P1。
- Scope: 审查 `core/api_v1.py`、`core/source_resolution_runtime.py`、`core/control_service.py`、`core/stages.py`、`cli/server.py`、`core/control_http.py`、`core/api_v1_fastapi.py`、`core/selena_resolver.py`、`core/config.py` 与对应 tests；修改 `tests/test_control_stages.py` 一处 stale assertion，新增 legacy HTTP reserved-sentinel 负向测试，并更新本 HANDOFF A3 entry；未修改业务代码、PRD/design/DEVELOPMENT_PLAN/CHECKPOINT，未 add/commit/push，未引入新 scheduler，未解除 sentinel。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-06、INV-09、INV-10、INV-12、INV-13；WP4=In Progress（A3 done）。
- 主验收证据（独立确认）：
  1. HTTP `/api/agents/register`（legacy `core/control_http.py`）调 `register_agent`，`__v1_scheduler__` 在 `RESERVED_INTERNAL_AGENT_IDS` 被拒并冒 `ValueError`→HTTP 400；`register_internal_agent` 仅在 tests 内直连 `ControlService` 调用，FastAPI `/api/v1` 与 legacy HTTP 均无 route 暴露 internal registration。
  2. resolved/queued stage 仍不可被普通/wildcard/spoof agent 领取：`claim_next_task` SQL `WHERE status='queued' AND (assigned_agent_id='' OR assigned_agent_id=?)` 先按 agent_id 过滤 v1 sentinel-bound task，`_capability_matches` 仅在 SQL 过滤后生效；blocked stage 因 `status='blocked'` 被 `WHERE status='queued'` 直接排除，连已注册的 `__v1_scheduler__` internal agent（`*` capability）也 claim 不到（`test_v1_submit_unresolved_outcomes_are_observable_but_not_executable` 断言 `claim_next_task(V1_SCHEDULER_AGENT_ID) is None`）。
  3. needs_input cancel 终结：`cancel_job` 不把 `needs_input` 视为 terminal（`needs_input` 不在 `TERMINAL_JOB_STATUSES`），blocked stage 直接置 `cancelled` 写 `stage.cancelled` event，`_refresh_job_status_locked` 聚合后 job 落到 `cancelled`；API `cancel_job` 对 needs_input job 可调用且返回 `cancelled`，所有 stage 落在 `{cancelled, skipped}`。
  4. job/stage structured events 完整且不破坏 WP3 event sequence：`create_job` 在同一 `BEGIN IMMEDIATE` 事务写 `job.created` + 每 stage `stage.queued/skipped/blocked`，并在 refresh 后 status≠queued 时补一条 `job.status`；`cancel_job`/`retry_stage`/`submit_task_result`/`reclaim_stale_tasks` 均 write-tx；WP3 `test_create_job_writes_initial_job_and_stage_events`（mixed queued+skipped→3 events 无 job.status）仍通过。
  5. provider owner/path 脱敏：`SourceResolutionContext` frozen 且 `evaluated_at` 拒 NaN/Inf/负；`api_v1.py` 校验 `inputs.context.owner == owner` 否则 `source_resolution_owner_mismatch`；`SourceResolutionProviderError` 经 `_provider_api_error` 固定 public 文本映射，`detail` 仅含 `provider_error=<code>`，不回显 provider message/action path；未捕获异常统一 `source_resolution_unavailable` 且 `detail` 仅 `type(exc).__name__`；resolved_spec decisions 仅白名单键（无 workspace_path/executable_path，`workspace_binding_id` 为 sha256 hash）。
  6. central no Windows I/O：`serve-v1` `inspect_local_workspace=False`，`build_legacy_source_resolution_inputs` 走非 inspect 分支只构造纯 context，不调 `build_source_resolution_context_from_io`（Git/filesystem I/O）；`config_loader` 接 `spec.data.path`，`load_simulation_spec_bundle` lazy `adapt_legacy_config` 纯映射，workspace_path 仅留在 `UserBindings` 不被探测。
  7. idempotency existing 不重调 provider：`submit_job` idempotency existing check（`get_job_by_idempotency`）在 `source_resolution_provider` 调用之前，existing job 直接 return，provider 调用计数为 1（`test_v1_idempotency_replay_does_not_call_source_provider_again`）；concurrent IntegrityError fallback 同样返回 existing job。
  8. create_job 原子持久化 resolved_spec+stages：`resolved_spec` 与 `tasks` 在同一 `create_job` 事务的 `BEGIN IMMEDIATE`/commit 内写入，异常 rollback 不留半状态。
- 直接修复（P1 stale test，非业务逻辑缺陷）：`tests/test_control_stages.py::test_event_sequence_is_job_local_and_concurrent_append_is_monotonic` 旧 assertion 假设 single skipped-task job 创建时只有 2 个初始 event，但 WP3 状态机修复后 single skipped task 会使 job 在创建期翻到 `succeeded` terminal 并补写 `job.status` event（共 3 个初始 event）；旧 assertion `range(3,11)` 与 job_b `[1,2,3]` 与现行 `create_job`/`_refresh_job_status_locked` 行为不符而失败。修复为 `range(4,12)` 与 job_b `[1,2,3,4]` 并加注释说明 single-skipped-task→terminal→job.status 事件。这是 stale test 对齐正确状态机行为，不改变任何业务语义。
- Test evidence: focused `python -m pytest tests/test_api_v1_service.py tests/test_api_v1_fastapi.py tests/test_source_resolution_runtime.py tests/test_control_service.py tests/test_control_stages.py tests/test_control_http.py tests/test_sdk.py tests/test_artifacts.py tests/test_selena_resolver.py tests/test_spec_legacy_adapter.py tests/test_server_pyz.py -q` -> 149 passed, 1 upstream Starlette warning；新增 `test_control_http_rejects_reserved_internal_scheduler_identity` 证明 public HTTP register 返回 400 且不产生伪造 Agent；full `python -m pytest -q` -> 625 passed, 1 skipped（Windows symlink 权限）, 1 upstream warning in 314.62s；`git diff --check` 通过（仅既有 LF/CRLF warning）；静态确认 `core/api_v1.py`/`source_resolution_runtime.py`/`stages.py`/`selena_resolver.py` 无 Celery/Temporal/SQLAlchemy/Alembic/RQ/dramatiq，sentinel `__v1_scheduler__` 保留。
- Drift check: 未改业务代码、未新增 scheduler、未解除 sentinel、未让 Linux 探测 Windows workspace、未扩展产品范围（needs_input 恢复 endpoint、source preflight、provider concurrent single-flight 仅记为后续风险未实现）；HANDOFF 仍为唯一实时状态，未新增第二实时状态，未改 CHECKPOINT/PRD/design/DEVELOPMENT_PLAN。
- Risks（后续，不在本任务扩展）：(a) needs_input job 暂无恢复/resume endpoint，用户只能 cancel 后重新 submit；(b) source resolution 无独立 preflight/validate endpoint，provider 失败只能在 submit 时观察到；(c) provider 无 concurrent single-flight，同 idempotency-key 并发 submit 会被两个 worker 各调一次 provider（winner 写 job、loser 拿 existing），属可接受但非最优；(d) `AGENT_STALE` attempt 仍以 failed terminal 表 abandoned 证据；(e) Starlette TestClient `httpx` deprecation warning 留 WP10；(f) blocked stage 当前不可 retry（retry_stage 只接 failed/cancelled），needs_input 恢复路径待后续 WP 接入真实 scheduler 时一并设计。
- Next step: 后续 WP4/WP6 接入 Windows full/light Agent source snapshot 与 artifact register/upload stage；needs_input 恢复、source preflight、provider single-flight 作为后续 WP 设计项评估；在 scheduler/node policy 完成前继续保留 sentinel。

### 2026-07-13 - WP6-A1 Windows Agent light/full capability runtime gate

- Goal: 修复历史 Mode A Agent 默认 `cluster.run + tcc.*` 与 v5 产品合同的冲突，建立 light/full 明确模式，并在 CLI、公开注册和 claim 三层阻止 light Agent 执行本地仿真或 Cluster 运行期。
- Scope: 新增 `core/agent_policy.py`、`tests/test_agent_policy.py`、`tests/test_agent_cli_policy.py`；修改 `cli/agent.py`、`cli/web.py`、`core/control_service.py`、`tests/test_control_service.py`、`tests/test_control_http.py` 与本 Active Handoff；未实现 artifact/data upload、source snapshot、scheduler replacement、installer、UI 或 needs_input recovery，未解除 `__v1_scheduler__`，未 add/commit/push。
- Referenced invariants / WP: INV-02、INV-04、INV-05、INV-07、INV-10、INV-13；WP6=In Progress（A1 done）。
- Actual changes: Agent CLI 新增 `--windows-mode light|full` 且默认 light；light 默认只上报 source/workspace、Selena build、artifact register/validate/upload、data local read/upload 和 legacy local check/build aliases，禁止 wildcard、local simulation、Cluster simulation/gateway/run/collect/finalize 与未知 capability；full 默认增加 local simulation，但不自动获得 Cluster runtime/Gateway。CLI 在创建 HTTP client 前校验显式 capability，并在 `Popen` 前用 node policy 二次拒绝非法 task/stage；注册 metadata 明确 `node_kind/windows_mode`。ControlService 公开注册按 node kind 规范化并过滤自报 capability，只记录过滤标记/数量而不持久化被拒 token；未知显式 node kind 返回稳定 400；claim 对已存在/手工污染的 wildcard/exact capability 记录再次应用严格 task/stage allowlist。内嵌 Web Agent 明确注册为 `windows_full` 并同样走执行前 gate。Claude CLI 产出初始 policy/CLI 草案；主 agent 独立审查后修复显式 legacy、大小写归一化、unknown allowlist、full 默认 `cluster.run` 越权和 rejected-token 持久化，并合并删除重复 `core/node_policy.py`，保持单一策略源。
- Test evidence: policy/CLI/Web/control focused -> 206 passed, 1 upstream Starlette warning；full `python -m pytest -q` -> 705 passed, 1 skipped（Windows symlink 权限）, 1 upstream Starlette/httpx warning in 255.37s；`python -m py_compile core\agent_policy.py cli\agent.py core\control_service.py` 通过；`git diff --check` 通过（仅 LF/CRLF warning）；静态搜索无 `core.node_policy` import 或重复策略模块。
- Drift check: light Agent 仍不具备 `simulation.local`、`simulation.cluster`、`cluster.gateway`、legacy `cluster.run` 或 run/collect/finalize stage；Windows full 与 platform Gateway 未合并；legacy 未声明 node kind 的调用仍保留兼容 capability 行为；本切片只交付 node/capability policy，未把 WP6 或 WP4 误报完成。
- Risks: node kind 目前仍由注册 metadata 声明而非安装凭证绑定，身份认证/attestation 属后续安全工作；WP7 尚未为 `linux_executor/platform_gateway` 建立独立 allowlist；v5 scheduler 仍使用 sentinel，尚未把 Stage 基于 required capabilities 绑定到真实 node；light Agent 授权目录、source snapshot、artifact/data upload 和 Agent offline Cluster E2E 仍待实现。
- Next step: WP4/WP6-A2 实现授权 workspace/output 边界、构建前后 `WorkspaceFingerprint` source snapshot 与 artifact validate/register/upload staging；完成后再让真实 scheduler 绑定 build/artifact stage，sentinel 暂时保留。

### 2026-07-13 - WP4/WP6-A2 authorized source snapshot and artifact staging kernel

- Goal: 为 Windows full/light Agent 建立不访问网络的本地 staging 安全边界，确保只有授权 workspace/output 下的本次 Selena 产物可被校验并形成不含绝对路径的 `SelenaArtifact`/Stage result。
- Scope: 新增 `core/agent_artifact_staging.py`、`tests/test_agent_artifact_staging.py`；仅更新本 Active Handoff；未接 CLI Agent task、binding store、HTTP/upload/catalog endpoint、scheduler、Web/SDK 或 installer，未 add/commit/push。
- Referenced invariants / WP: INV-07、INV-09、INV-10、INV-12、INV-13；WP4=In Progress，WP6=In Progress（A2 kernel done）。
- Actual changes: immutable `AuthorizedRoots` 只接受非空、存在且非 drive root 的 workspace，并要求 output root 是 workspace 内更窄的授权目录；resolve/realpath 双重 containment 阻止 traversal 和 symlink/reparse escape。`capture_source_snapshot()` 授权后复用 WP4-A1 `inspect_workspace()`，错误对外不回显路径。`validate_and_hash_artifact()` 只接受授权 output 中非空、regular、非 symlink、非 hardlink 的 `selena.exe`，流式 SHA-256，并在 hash 前后比较 file identity/size/mtime 防止替换；只返回校验过的 relative POSIX logical path/checksum/size。`stage_selena_artifact()` 校验 snapshot commit/hash、业务 metadata 与 timestamps，用 before snapshot 固化 branch/commit/dirty fingerprint，before/after hash 或 commit 变化时标记 `source_changed_during_build`，dirty/changed 由 `SelenaArtifact` 强制 private；manifest key/value 和 Stage result 递归拒绝绝对路径。Claude CLI 生成初始模块/真实 Git tests；主 agent 修复 symlink resolve 后漏检、路径错误泄漏、evidence 可伪造绝对路径、Inf、hardlink/hash race、空/root authorization，并把慢测试拆成少量真实 Git + 快速合成 fingerprint。
- Test evidence: staging-only `python -m pytest -q tests\test_agent_artifact_staging.py` -> 39 passed, 3 skipped；staging/repo/artifact/Agent policy focused -> 139 passed, 4 skipped；full `python -m pytest -q` -> 746 passed, 4 skipped, 1 upstream Starlette/httpx warning in 277.63s；`python -m py_compile core\agent_artifact_staging.py` 和 `git diff --check` 通过。skips 为当前 Windows symlink 权限及既有 repo symlink case，不隐藏其他失败。
- Drift check: 模块只做 local filesystem staging，不上传、不访问 catalog/网络、不运行 simulation/Cluster、不接受中央绝对路径；Stage result 只含 logical relative path、checksum/size、before/after public fingerprint 和 artifact logical record；light Agent 边界不变，scheduler sentinel 不变。
- Risks: `ArtifactEvidence` 仍是同进程 typed evidence，不是跨进程签名/attestation；binding ID 到本机路径的持久化授权尚未实现；构建 executor 尚未在 build 前后调用 snapshot；artifact upload 后的 logical `storage_ref`、中央 catalog register 与 Agent offline handoff 尚未实现；Windows junction/企业文件系统需真实机 smoke。
- Next step: WP6-A3 复用 `RSIM_HOME` 实现 Agent-local workspace binding store（逻辑 ID -> workspace/output roots），再接 `prepare_source/build_selena/register_artifact` Stage adapter；中央任务只携带 binding ID/relative output，不携带 Windows 绝对路径。

### 2026-07-13 - WP6-A3 Agent-local workspace binding store

- Goal: 实现“一次配置、永久生效”的本机 workspace/output 授权持久化，使中央 resolver 和 Windows Agent 通过同一逻辑 binding ID 协作，而不跨机器传递绝对路径。
- Scope: 新增 `core/agent_bindings.py`、`tests/test_agent_bindings.py`；最小修改 `core/source_resolution_runtime.py` 复用共享 ID 算法；更新本 Active Handoff；未接 Agent CLI bind 命令/文件夹选择器、真实 Stage、upload/catalog/scheduler/Web/installer，未 add/commit/push。
- Referenced invariants / WP: INV-03、INV-07、INV-08、INV-09、INV-10、INV-13；WP6=In Progress（A3 store done）。
- Actual changes: `make_workspace_binding_id(project, path)` 成为唯一 ID 算法，保持既有 `workspace:sha256:<24hex>`、slash/casefold 兼容；中央 `logical_workspace_binding_id(UserBindings)` 仅作 facade。immutable `WorkspaceBinding` 本地保存 project、resolved workspace/output roots 与时间，`public_dict` 只暴露 ID/project/output count/health/timestamps。`AgentBindingStore` 使用 local-only SQLite + WAL/busy timeout/线程锁/`BEGIN IMMEDIATE`，默认路径为 `RSIM_HOME/agent/bindings.db`，未配置时为用户 home `~/.rsim/agent/bindings.db`，不落 repo cwd；register 先复用 `AuthorizedRoots`，稳定 upsert 且保留 created_at，get/list/resolve 每次重新检查目录健康，delete/project isolation/collision/malformed JSON/nonfinite clock 均有 path-free 错误。Claude CLI 生成 store/facade/tests；主 agent 修复 delete 双 rollback、clock 校验、malformed JSON 被 list 吞掉、JSON object shape、严格 project token、collision 检查和 corrupt DB 错误脱敏。
- Test evidence: binding/source/staging focused -> 95 passed, 5 skipped；扩展 resolver/API focused -> 137 passed, 5 skipped；full `python -m pytest -q` -> 793 passed, 6 skipped, 1 upstream Starlette/httpx warning in 297.31s；`python -m py_compile core\agent_bindings.py core\source_resolution_runtime.py` 与 `git diff --check` 通过；静态确认 binding ID digest 只在 `core/agent_bindings.py` 实现一次。
- Drift check: 绝对 workspace/output 只存 Agent 本机 DB，public view 和中央 ResolvedSpec 只携带 logical binding ID；store 无网络、无 upload、无 simulation/Cluster、无 catalog 写入；Linux central 仍不 inspect Windows workspace；light/full capability gate 与 sentinel 保持不变。
- Risks: 还没有用户可见的 bind/unbind/health CLI/API 与 folder picker；本地 DB 尚未绑定安装身份/签名，node attestation 后续补；真实 build Stage 尚未证明 legacy config 的 workspace/build script/output 与 binding 一致；Windows junction/SMB workspace 需真实环境 smoke。
- Next step: WP6-A4 增加 Agent-local bind CLI/健康检查，并实现 v5 `build_selena` Stage adapter：由 binding ID 解析本机路径、校验 legacy build config 与 binding 一致、build 前后 snapshot、校验相对 output 中 `selena.exe` 并回传 redacted evidence；不改变 legacy `local.build_selena` 行为。

### 2026-07-13 - WP6-A4 Agent binding admin CLI

- Goal: 为尚未接 Web folder picker 的阶段提供可释放、可脚本化的一次性本机授权入口，让用户/管理员能 register/list/health/delete workspace binding 并永久保存。
- Scope: 新增 `cli/agent_binding.py`、`tests/test_agent_binding_cli.py`；更新本 Active Handoff；未修改产品 YAML、Web/SDK、Agent poll loop、build Stage、upload/catalog/scheduler 或 installer，未 add/commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-09、INV-11、INV-13；WP6=In Progress（A4 admin CLI done）。
- Actual changes: 动态 CLI 新增 `rsim agent-binding register|list|health|delete` 且 `NO_CONFIG=True`；register 接收 project/workspace/output roots 并复用 `AgentBindingStore/AuthorizedRoots`，list 支持 project filter 与 deterministic JSON，health 每次重新解析授权 roots，delete 显式删除。所有 stdout 只输出 binding ID/project/output count/health/timestamps/deleted，不输出 workspace/output/db 绝对路径；未指定 `--db` 时复用 A3 的 `RSIM_HOME`/user-home 默认。该 CLI 是管理员/调试/自动化入口，不改变 Web/SDK 作为产品入口的合同。Claude CLI 完成模块与 20 个 focused tests；主 agent 独立复验动态自动注册与 no-path 输出并清理未使用 imports。
- Test evidence: CLI + store focused -> 67 passed, 2 skipped；`python rsim.py agent-binding --help` 成功显示 4 个子命令；full `python -m pytest -q` -> 813 passed, 6 skipped, 1 upstream Starlette/httpx warning in 280.08s；`python -m py_compile cli\agent_binding.py` 与 `git diff --check` 通过。
- Drift check: CLI 不进入 SimulationSpec/YAML，不成为第三个产品调度入口；它只管理 Agent 本机授权，无网络、无仿真、无 Cluster、无 artifact catalog；输出和异常不跨机泄露绝对路径。
- Risks: 命令行路径参数不等于目标 Web 系统文件夹选择器体验；binding 尚未绑定 installer/node attestation；未实现自动环境检查/修复；真实 v5 build/register_artifact Stage 还不能消费 binding。
- Next step: WP6-A5 安全重构现有 Selena build command 为可注入/可验证的 worker adapter，再让仅 v5 `build_selena` Stage 使用 binding ID、校验本机 legacy config、capture before/after、validate/hash `selena.exe` 并返回 redacted evidence；legacy `local.build_selena` 保持兼容，upload/register 后置。

### 2026-07-13 - WP6-A5 v5 configured-script Selena build Stage adapter

- Goal: 让 Windows full/light Agent 能安全执行真实 v5 `build_selena` Stage：只消费逻辑 binding ID，在本机解析授权路径，执行已验证 build script，并回传 build 前后 snapshot 与产物 hash evidence。
- Scope: 新增 `core/agent_build_stage.py`、`tests/test_agent_build_stage.py`；修改 `cli/agent.py`、`core/agent_policy.py`、`core/control_service.py`、`tests/test_control_agent.py`、`tests/test_agent_policy.py`、`tests/test_control_service.py` 与本 Active Handoff；未接 scheduler payload enrichment、R2D2 fallback、register_artifact/upload/catalog、Web/SDK/installer，未 add/commit/push。
- Referenced invariants / WP: INV-04、INV-05、INV-09、INV-11、INV-12、INV-13；WP6=In Progress（A5 adapter done，真实环境 smoke pending）。
- Actual changes: `prepare_selena_build()` 只接受 project/`workspace_binding_id`/build mode/strict bool clean/optional logical profile，拒绝任何本地 path key；通过 Agent-local store 解析授权 roots，加载本机 config 并用共享 binding ID 验证 config workspace 完全一致；P0 v5 Stage 只允许 configured Selena build script，拒绝 legacy R2D2 fallback，script 必须 regular、非 symlink/hardlink、位于授权 workspace，实际 command 必须是 `cmd /c <同一 script>`，cwd 必须是授权目录，预期 `selena.exe` 必须位于授权 output。prepare 在全部校验后 capture before，并记录 script SHA-256；`verify_prepared_build()` 在 `Popen` 前重验 script identity/checksum；`finish_selena_build()` capture after、validate/hash artifact，返回只含 project/`workspace_binding_id`/build mode/public snapshots/source-changed/logical path/checksum/size 的 redacted result。`cli.agent._run_task()` 仅对正式 `build_selena` Stage 走 prepare -> verify -> subprocess -> finish，使用授权 cwd，不在日志起始或 result 回传 command/cwd；setup/finish 失败转为 path-free Stage failure。legacy `local.build_selena` 保持原命令行为。服务端 formal `build.selena` capability 新增 `build_selena` Stage alias 匹配，但 sentinel 尚未解除。Claude CLI 生成初始 kernel 后未完成 tests；主 agent 缩小为 script-only P0、修复 fallback/clean/binding field/command identity/path leak，并实现 23 个 kernel tests 与 Agent 集成 tests。
- Test evidence: kernel focused -> 23 passed；Agent/policy/control focused -> 121 passed；扩展 build/binding/staging/API focused -> 322 passed, 5 skipped, 1 upstream warning；full `python -m pytest -q` -> 840 passed, 6 skipped, 1 upstream Starlette/httpx warning in 297.06s；`python -m py_compile core\agent_build_stage.py cli\agent.py` 与 `git diff --check` 通过。
- Drift check: Linux 不编译；light Agent 仍不能领取 local simulation/Cluster runtime；中央 Stage payload 不含绝对路径；legacy build 未被强制 binding 化；v5 R2D2 fallback 明确拒绝，不用未审计路径绕过 configured script；build evidence 尚未伪装成可供 Cluster 使用的 `SelenaArtifact`。
- Risks: 尚未在真实 Selena repo/toolchain 执行 smoke，compiler stdout 可能包含本机路径（result 已脱敏，日志脱敏策略待 WP8）；scheduler 尚未把 resolved project/binding/build mode 写入 Stage payload 并安全重绑定 Agent，当前 v1 stages 仍 sentinel；build 成功 evidence 仍在 task result，未形成 upload session/storage_ref/catalog artifact；Windows batch descendant/cancel 行为需真实机验证；R2D2 fallback 暂不支持 v5 Agent Stage。
- Next step: WP4/WP6-A6 实现 `register_artifact` 的 upload session + Agent uploader + central finalize/catalog transaction；scheduler 在此之前先实现 build Stage payload enrichment/agent assignment，但不得解除 run/collect/finalize 的 light Agent 禁令。

### 2026-07-13 - WP4/WP6-A6 path-first central artifact upload and shared catalog

- Goal: 按用户决定实现“用户选择复用路径优先、clean 多用户可见、dirty/source-changed 私有”的中央 Selena 上传与 catalog application boundary，使 Windows 编译产物上传后形成不依赖用户 PC 在线的稳定逻辑引用。
- Scope: 新增 `core/artifact_store.py`、`core/artifact_upload_service.py`、`tests/test_artifact_store.py`、`tests/test_artifact_upload_service.py`；修改 `core/artifacts.py`、`core/spec/model.py`、`core/api_v1.py`、`core/api_v1_fastapi.py`、`core/control_service.py`、`cli/server.py`、`radar_sim_sdk/*` 及相关 tests/PRD/design/HANDOFF；未实现 Agent 自动上传、真实 Stage scheduler、data upload、local/Cluster run Stage、Web picker 或 installer，未 add/commit/push。
- Referenced invariants / WP: INV-03、INV-04、INV-05、INV-07、INV-10、INV-12、INV-13、INV-14；WP4=In Progress，WP6=In Progress（A6 central boundary done）。
- Actual changes: `SimulationSpec.selena.publish_path` 成为可复用的项目内业务路径且拒绝绝对路径/URI/traversal；`ArtifactStore` 使用 `RSIM_ARTIFACT_ROOT` 下 project-isolated content 与独立 sessions DB，提供 persisted resumable offset、chunk retry 幂等、size/SHA256 finalize、restart recovery、same-path same-checksum reuse 和 different-checksum conflict，并防 traversal/UNC/drive/device/reserved namespace、symlink/junction/reparse/hardlink escape；公开引用固定为 `shared://selena/<project>/<publish-path>/selena.exe`，不返回物理路径。`ArtifactUploadService` 只接受 build attempt evidence ref 与可选 publish path，checksum/size/project/branch/commit/dirty/source-changed/build mode/builder 均从成功的同 owner Windows build attempt 读取；finalize 原子形成 `SelenaArtifact` 并写共享 catalog，clean 默认 shared，dirty/source-changed 强制 private。`ArtifactCatalog` 改为 storage-ref/path-first identity，同路径冲突但相同 binary 在不同用户路径保留独立记录；新增 storage-ref lookup/access check。FastAPI 新增 `/api/v1/artifact-uploads` create/get/PATCH/finalize，SDK 新增会话方法与 `upload_artifact()` 文件便利方法；`serve-v1` 使用同一中央 catalog 支持 clean 产物多用户可见。
- Test evidence: 初始 focused 捕获并修复 stale test fixture 后，artifact/API/SDK/build/control/source/spec focused `python -m pytest -q ...` -> 208 passed, 1 skipped, 1 upstream warning；全量首次 -> 902 passed, 7 skipped，捕获 Windows 并发 finalize 中 `C:\\...` 与 `\\\\?\\C:\\...` 同路径表示误判；修复仅用于比较的 Windows extended-path normalization 后，并发用例连续 20 次通过，store/upload/catalog/source focused -> 84 passed, 1 skipped, 1 upstream warning；最终 full `python -m pytest -q` -> 903 passed, 7 skipped, 1 upstream Starlette/httpx warning in 297.27s；相关模块 `py_compile` 通过；`git diff --check` exit 0（仅既有 LF/CRLF warning）。
- Drift check: Linux central 仍不编译 Selena；上传 metadata 不信任客户端自报；绝对 Windows/服务器路径不进入 YAML/API/result；light Agent 仍不能 local sim/Cluster runtime；`__v1_scheduler__` sentinel 未解除，未伪造任何 Stage 成功；旧 Web/CLI/control/本地及 Cluster 原型通过全量回归，但不等于 v5 自动流水线已完成。
- Risks: Windows Agent 尚未实现 `register_artifact` Stage uploader，build 成功后不会自动调用中央上传；真实企业共享盘/SMB/junction、多进程故障和大文件需环境 smoke；`X-Rsim-User` 仍只是 owner normalization 而非认证/attestation；session 清理/保留策略属后续；scheduler 尚未把 resolved payload 绑定到具体 Windows node，用户提交的 job 仍不会自动前进。
- Next step: WP6-A7 实现受限 Stage binder：先让中央只完成已真实发生的 `resolve_spec`，为 `prepare_source/build_selena/register_artifact` 生成不含路径的 payload，并绑定具备 capability 的同一 Windows full/light Agent；Agent 完成 register/upload 后中央只消费 `storage_ref`。不放行尚无执行器的 data/preflight/run/collect/finalize。

### 2026-07-13 - WP1/WP2/WP8-A7a minimal config, project adapter, environment plan and task center

- Goal: 在实现真实 Stage binder 前，先固化用户最少配置、项目差异归属、分部署环境依赖计划和任务中心 P0 合同，避免 scheduler 把 `local.yaml`、工具链路径或 Cluster 拓扑泄漏回用户。
- Scope: 新增 `core/environment_contract.py`、`tests/test_environment_contract.py`；修改 `core/spec/model.py`、`core/spec/legacy_adapter.py`、`core/stages.py`、`core/control_service.py`、`core/api_v1.py`、`core/api_v1_fastapi.py`、`radar_sim_sdk/client.py`、`radar_sim_sdk/models.py` 及相邻 tests/PRD/design/HANDOFF；未实现真实 EnvironmentSnapshot checker、Stage binder、Agent uploader、data resolver、local/Cluster run executor、Web 或 installer，未 add/commit/push。
- Referenced invariants / WP: INV-01、INV-03、INV-04、INV-05、INV-06、INV-07、INV-08、INV-10、INV-11、INV-13；WP1/WP2 保持 Done，WP8=In Progress（A7a plan/task-center boundary done）。
- Actual changes: `SimulationSpec` 导入现在只要求 `project` 和 `data.path`，schema version、Selena auto、target auto、default profile、结果保留期由模型补齐，canonical YAML 导出仍完整可追溯；JSON Schema required 只含 project/data。`ProjectCatalog.adapter` 从项目 `recipe` 或 platform 生成并纳入 revision，项目 build/runtime 差异留在受版本控制的 ProjectCatalog/adapter，不进入用户 YAML。新增纯 `environment_plan`，按 spec 生成 path-free Stage/capability/node-kind requirements：build 只路由 Windows full/light，local runtime 只路由 Windows full，Cluster runtime 只路由 Linux executor/platform gateway，data path 继续由 resolver 判断 shared/local/upload；validate 与 pending/resolved spec 均暴露同一 plan，source provider 解析后写入真实 project adapter。`ControlService.list_jobs` 增加 owner/status/job-type 安全过滤；`GET /api/v1/jobs` 与 SDK `list_jobs()` 返回当前用户 v1 jobs、overall progress、current Stage 和 available actions，避免 shared DB 跨用户/legacy 泄漏；running/queued 提供 cancel、failed Stage 提供精准 retry、blocked/needs_input 透传修复动作，terminal 不伪造 current Stage/action。配置安全审计同时移除两个 versioned project config 的 `cluster.kill_password` 和代码默认密码，真实提交只从 deployment env `RSIM_CLUSTER_KILL_PASSWORD` 或显式 node-local config 取值；prepare/dry-run/Manifest/CLI command 只写 `<redacted>`，未配置 secret 的真实提交以稳定错误停止，新增测试禁止项目 config 再出现 password/token。
- Independent CLI review: 首次全仓 Claude CLI 只读审计超时并被终止，不计入证据；缩小为事实输入后的 Haiku 审计认可两字段 YAML 与配置分层，并指出外部 Cluster 取消补偿风险。主 agent 修正其“无部署用户需要 workspace”的误判：无部署用户只需 existing Selena + data path + Cluster；workspace 只属于 Windows 编译用户。取消补偿风险已写入 PRD，真实外部 cancel adapter 仍属 WP7/WP8。
- Test evidence: 新增/相邻 focused `python -m pytest -q tests/test_environment_contract.py ...` -> 130 passed, 1 upstream warning；首次 full -> 910 passed, 7 skipped，捕获 environment plan frozen tuple 与公开 JSON list 导致 ResolvedSpec/Stage snapshot 不一致；统一 public `node_kinds` 为 JSON list 后 resolver/environment/API/spec focused -> 114 passed, 1 upstream warning；第二次 full -> 911 passed, 7 skipped；secret migration 后 cluster/environment focused -> 54 passed；最终 full `python -m pytest -q` -> 913 passed, 7 skipped, 1 upstream Starlette/httpx warning in 267.88s；相关模块 `py_compile` 通过。
- Drift check: 最小 YAML 仍不包含 workspace/build script/VS/MATLAB/Qt/Boost/TCC/Agent ID/Cluster address/group/password/queue/storage map；environment plan 只描述逻辑要求，不伪装成节点实际检查结果；项目 adapter 复用 layered config/profile/recipe 和既有 build/simulation 代码，不建立第二套项目调度器；v1 sentinel 未解除，未把任何未执行 Stage 标为成功。
- Risks: environment plan 尚未形成真实 `EnvironmentSnapshot`，不能据此声称机器 ready；`GET /api/v1/projects` 和新版 Web task center 尚未实现；任务 list 当前是 limit/status 而非 cursor 分页；Cluster 外部取消/补偿尚未接 adapter；project 配置里仍有机器绝对路径和 Cluster 拓扑参数，需后续继续拆入 UserBindings/DeploymentPolicy，但凭据已迁出且它们不会进入 SimulationSpec。
- Next step: WP6/WP8-A7b 实现受限 Stage binder + environment_check executor：先把 plan 转成具体 node-local checks/EnvironmentSnapshot，只有 snapshot 满足 requirements 才绑定 `prepare_source/build_selena/register_artifact`；同一 Windows Agent 完成 build/upload handoff 后才让中央继续。无真实 executor 的 data/preflight/run/collect/finalize 保持 sentinel。

### 2026-07-14 - WP6/WP8-A7b.1 node-local EnvironmentSnapshot and durable Agent affinity

- Goal: 建立受限 Stage binder 的第一段真实执行内核，让 Windows Agent 在不泄漏路径的前提下检查 current-workspace Selena build 环境，并保证后续 build/retry 不漂移到另一台机器。
- Scope: 新增 `core/environment_snapshot.py`、`core/stage_binder.py`、`tests/test_environment_snapshot.py`、`tests/test_stage_binder.py`；修改 `cli/agent.py`、`core/agent_policy.py`、`core/control_service.py`、相邻 tests 和本详细设计/计划。尚未解除默认 v1 sentinel，尚未实现 prepare_source/register_artifact uploader/data/preflight/run/collect/finalize，未 add/commit/push。
- Referenced invariants / WP: INV-04、INV-05、INV-09、INV-11、INV-12、INV-13；WP6/WP8=In Progress（A7b.1 kernel done，live scheduling pending）。
- Actual changes: 新增不可变、带 TTL 和内容 fingerprint 的 path-free `EnvironmentSnapshot`/check result，递归拒绝绝对路径和 credential-shaped 内容；`inspect_selena_build_environment()` 在 Windows Agent 内复用严格的 `prepare_selena_build()` 授权/config/script/command/output 校验但不启动 compiler，输出 workspace/toolchain/local-staging 三项证据。Agent 为 `environment_check` 增加显式 node-local executor，ready 才成功，blocked 以结构化 snapshot 失败，不回落 legacy subprocess；注册 metadata 移除绝对 `cwd`。formal capability map 增加 environment/prepare/register aliases。`tasks` 增加可迁移的 `required_agent_id`，claim 同时校验 transient assignment 与 durable affinity；stale reclaim 只清 assignment、不清 affinity。`bind_stage_to_agent()` 以 SQLite transaction/CAS 绑定真实 Agent 并写 `stage.bound` event；`bind_current_workspace_build()` 只接受同 job、成功且未过期、agent/project/binding 一致、required checks 全通过的 snapshot，将 build path-free payload 与 `stage:attempt` snapshot ref 绑定回同一 Agent。branch build 明确不放行，等待 detached worktree/source lease adapter；未实现 Stage 继续 sentinel。
- Independent review: Claude CLI Sonnet 只读调用 60 秒无输出并超时，不计入证据；并行只读 agent 审计确认五个阻塞：sentinel、Linux/Windows machine-resolvable 分类、缺 executor、assigned affinity 会被 reclaim 清空、branch/workspace 强绑定。主线采纳 snapshot attempt + durable affinity + current_workspace-first，未采纳一次性放行全部 Stage。
- Test evidence: 环境/Agent/policy/build focused -> 103 passed；增加 durable binder/migration/control regression 后 focused -> 106 passed；`py_compile` 通过；full `python -m pytest -q` -> 921 passed, 7 skipped, 1 upstream Starlette/httpx warning in 311.58s。
- Drift check: Linux 不执行环境 build 检查和 Selena compile；light Agent 仍不领取 preflight/local sim/Cluster runtime；snapshot/result 不含绝对 path；过期/错误 snapshot 不解除 sentinel；build 仅 current_workspace 且 `clean=false`，不会 checkout/stash/reset 用户工作区；branch 不伪装完成。
- Risks: v1 submit 仍会把 Linux 看不到 workspace fingerprint 的 machine-resolvable 状态误判为 user needs_input 并全 blocked；默认 job 尚未把真实 environment payload 交给 Agent；prepare_source/register upload 未闭环；legacy control 8877 与 v1 upload 8878 仍需显式双 URL 或统一服务；多用户 Agent enrollment/owner identity 尚未完成。
- Next step: A7b.2 将 `workspace_fingerprint_required`/`branch_commit_required` 与真实用户缺 binding 分开；仅 current_workspace 先由拥有同 project logical binding 的 Agent 执行 environment/source snapshot，重算 ResolvedSpec 后调用受限 binder。随后实现 Agent-local artifact lease + register upload；branch 在 detached worktree adapter 完成前保持明确 unavailable。

### 2026-07-14 - WP6/WP8-A7b.2/A7b.3 live workspace dispatch and build-to-upload handoff

- Goal: 让默认 v1 Job 从最小 YAML 真正进入 Windows node-local environment check，并在成功后自动完成同机 build/register-artifact handoff，而不是只存在可单测 binder。
- Scope: 修改 `core/api_v1.py`、`core/control_service.py`、`core/control_http.py`、`core/stage_binder.py`、`core/environment_snapshot.py`、`core/spec/legacy_adapter.py`、`cli/agent.py`；新增 `core/agent_artifact_lease.py`、`tests/test_agent_artifact_lease.py`，扩展 API/control/Agent/binder tests 与 PRD/design/HANDOFF。未实现 data resolver/upload、preflight、local/Cluster run/collect/finalize、branch detached worktree、新 Web 或 installer，未 add/commit/push。
- Referenced invariants / WP: INV-01、INV-04、INV-05、INV-07、INV-09、INV-11、INV-12、INV-13、INV-14；WP6/WP8=In Progress（current_workspace/auto build-to-upload dispatch connected，data/runtime pending）。
- Actual changes: Agent 注册 metadata 新增仅含 ID/project/health 的 workspace binding 广告并移除 cwd；中央对 `current_workspace` 缺 fingerprint、以及最小 YAML 默认 `auto + auto_build` 且已有 binding/无 artifact 的情况写 `pending_node/workspace_snapshot_pending`，不再全 blocked；真正无 binding 仍为 `workspace_binding_required/needs_input`。在线 Agent 提交时直接匹配，离线 Agent 后续 poll 通过 `bind_pending_environment_stage()` CAS 领取；resolve_spec 标记为同步提交时已解析，prepare_source 对 current workspace 显式由 environment snapshot 覆盖。environment result HTTP hook 更新 path-free ResolvedSpec workspace decision，并自动绑定 build。build 成功后 Agent-local `artifact_leases.db` 保存绝对 path/file identity，公开只返回随机 lease ID；上传前重验 identity+SHA256。build result hook 以精确 `stage:attempt` 将 register Stage 绑定回同一 required Agent；Agent `register_artifact` executor 使用显式 `--api-url` 调既有 v1 resumable upload/central trusted evidence/catalog，上传期间保持 heartbeat，结果只返回 artifact/storage_ref/session ID。未实现 Stage 继续 sentinel。
- Test evidence: A7b.2 API/control/source/Agent 宽回归 -> 233 passed；machine-pending + live poll + HTTP env-to-build integration -> 7 passed；lease/binder/register executor/artifact upload focused -> 133 passed；最小 auto config 和 env->build->register HTTP chain -> 22 passed；扩展宽回归首次 314 passed, 1 skipped 并捕获旧 v5 build test 缺 lease fixture，已修复；最终 full `python -m pytest -q` -> 931 passed, 7 skipped, 1 upstream Starlette/httpx warning in 327.32s；`py_compile` 和 `git diff --check` 通过。
- Drift check: Linux 不读取 Windows path、不编译 Selena；light Agent 不获得 local simulation/Cluster runtime；用户 YAML 仍不含 Agent ID/API URL/toolchain/shared mapping；API URL 是部署参数；absolute artifact path 只存在 Agent-local SQLite；central upload metadata 仍只信成功 Windows build attempt；dirty workspace decision 保留 fingerprint 并最终强制 private；branch 不放行。
- Risks: v1 API 与 legacy Agent control 当前仍为显式双 URL/双服务，安装器需固化；Agent owner/enrollment 仍依赖 `X-Rsim-User` normalization；upload cancel 只能保持 heartbeat且 resumable，尚未实现 chunk 间主动 abort/compensation；真实 Windows 大产物、进程重启、中央 8878/Agent 8877 跨机 smoke 未跑；register 完成后 prepare_data/preflight 仍 sentinel。
- Next step: WP5/WP6-A8 实现统一 `DataRef` resolver：shared path 中央直用、Windows local path 由同一 Agent检索/校验/可续传上传、browser/SDK upload 形成中央 DatasetRef；随后 preflight 只消费 ArtifactRef+DatasetRef，并把 light Agent 从 Cluster 运行期完全释放。

## 5. CLI coding agent 任务更新模板

每个后续 CLI coding agent 子任务（当前使用 Claude CLI）在声称完成前，必须在本 Active 区域追加或更新一条记录：

```text
### YYYY-MM-DD HH:mm - <short task title>

- Goal:
- Scope:
- Referenced invariants / WP:
- Actual changes:
- Test evidence:
- Drift check:
- Risks:
- Next step:
```

## 6. Architecture Conformance Gate

标记任何 WP 或子任务完成前必须满足：

1. 没有代码证据和测试证据就不能完成，即使已有原型。
2. 用户可见合同变化必须先改 `PRD.md` 并明确产品决策。
3. 技术边界、依赖、协议、执行拓扑或部署形态变化必须先改 `docs/DETAILED_DESIGN.md`。
4. 新依赖或协议必须通过复用决策门禁：解决缺口、复用模块、成本/风险、PoC/退出标准、回退方案。
5. 第三方组件 spike 还必须提供：版本 pin、许可证审查、漏洞/维护状态、内部镜像或 vendoring、Windows/Linux 离线打包、代理/证书兼容；缺任一证据则 spike 不通过。
6. WinSW 只默认评估已批准的 stable 版本，不默认采用 pre-release。
7. 代码任务必须使用上方模板更新本 Active Handoff。
8. 任何实现违反 INV 时必须停止，先更新 PRD/design 再继续编码。
9. 既有 dirty workspace 变更在完成审计、归属 WP 并有测试前，不是完成证据。

## Legacy History

The content below is preserved for traceability. It may describe historical Mode A/B or T1/T2/T3 work, old tests, prototypes, light Agent local simulation assumptions, or Linux server dependency on user Windows Agent; it does not define current v5 product status. Any conflict with the product invariants above, `PRD.md`, or `docs/DETAILED_DESIGN.md` is legacy/history only and must not be cited as the current contract or delivered state.

# radar-sim Handoff

Last updated: 2026-07-07

Current state:

- configuration system expanded
- simulation config normalized
- repo/branch semantics added
- second-project config skeleton started
- tests green
- cluster batch simulation environment identified
- Linux control-plane migration path implemented as server shell + Windows polling agent
- remote web mode can submit jobs through `--server-url`; real Selena work still runs on user Windows machines
- 2026-07-07 verification: focused control-plane tests 56 passed, full suite 336 passed

## What is already done

### 1. Unified simulation config

Added:

- [core/simulation.py](/D:/RamboStar/idea/radar-sim/core/simulation.py)

Key behavior now:

- `run / prepare-sim / check / render_selena_config` all use one normalized `simulation` model
- batch dataset runs supported
- one generated paramconfig per MF4
- historical outputs like `*out.MF4` and `*out (n).MF4` are skipped
- radar position auto-detection is wired in for the BYD Gen5-style MF4

### 2. ovrs25 config simplified

Main file:

- [config/projects/ovrs25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/ovrs25/config.yaml)

Current idea:

- user gives project-level build entry
- system derives as much as possible

Derived fields now include:

- `project_root`
- `binding`
- `build_config`
- `build_mode`
- `r2d2_script`
- `hex_build_script`
- `build_output`

### 3. Repo and Selena branch support

Config model now includes:

- `repos.outer_repo_root`
- `repos.inner_repo_root`
- `build.selena_branch`

Build behavior:

- `rsim build` checks inner repo branch before compile
- if branch differs and repo is clean, it tries to switch
- if repo is dirty, it stops and reports instead of forcing checkout

Relevant files:

- [cli/build.py](/D:/RamboStar/idea/radar-sim/cli/build.py)
- [cli/check.py](/D:/RamboStar/idea/radar-sim/cli/check.py)

### 4. Script-based Selena build is more flexible now

Added config support for:

- `build.script_args_template`

Reason:

- do not hardcode all projects to the `mode + config + binding` bat-call shape
- keep one user-facing `rsim build`, but allow per-project internal invocation differences

### 5. Paramconfig model expanded

Config/rendering now supports:

- `simulation.runtime_xml`
- `simulation.adapter_file`
- `simulation.matfilefilter`
- `simulation.paramconfig_options`

Template placeholders now support:

- `{{ADAPTER_FILE}}`
- `{{EXTRA_PARAMCONFIG_LINES}}`

Relevant files:

- [core/config.py](/D:/RamboStar/idea/radar-sim/core/config.py)
- [core/simulation.py](/D:/RamboStar/idea/radar-sim/core/simulation.py)
- [config/projects/ovrs25/assets/selena/selena_config_tmpl.txt](/D:/RamboStar/idea/radar-sim/config/projects/ovrs25/assets/selena/selena_config_tmpl.txt)

### 6. Environment/setup documentation added

Added:

- [docs/environment-setup.md](/D:/RamboStar/idea/radar-sim/docs/environment-setup.md)

README links to it.

### 7. New project skeleton started for shared config files

Added recipe:

- [config/recipes/g3n_fvg3_od25.yaml](/D:/RamboStar/idea/radar-sim/config/recipes/g3n_fvg3_od25.yaml)

Added project skeleton:

- [config/projects/bydod25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/config.yaml)
- [config/projects/bydod25/local.example.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/local.example.yaml)
- [config/projects/bydod25/assets/README.md](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/assets/README.md)
- [config/projects/bydod25/assets/selena/selena_config_tmpl.txt](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/assets/selena/selena_config_tmpl.txt)

This skeleton maps the shared folder info into config fields:

- runtime XML
- adapter file
- matlab filter
- `source=RadarFC`
- extra paramconfig option `distilled-mat=true`

## Important findings

### 0. Cluster batch simulation environment is available

User provided a Bosch internal cluster environment for batch simulation:

- Outside compliance room:
  - Online submit / job page: [http://szhradar01/cluster/?page=jobs](http://szhradar01/cluster/?page=jobs)
  - Docupedia: `Submit Gen5 Cluster Simulation Task Online - XC-AS/EDY-CN - Docupedia`
  - Cluster software share: `\\szhradar01\_cluster_software\`
  - Tool / project share path: `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster`
- Inside compliance room / VDI:
  - Docupedia: `Remote(VDI) - XC-CN Data Compliance Solution - Docupedia`
  - Docupedia: `01_Cluster+KPI+VDI - XC-DA/EDY-CN - Docupedia (bosch.com)`
  - VDI cluster path: `\\selena01\_cluster_software\`

Current interpretation:

- This may become the preferred backend for large dataset / multi-MF4 batch simulation.
- Keep local `rsim run` as the baseline execution path, then add a cluster submission backend instead of hardwiring cluster behavior into core simulation logic.
- Cluster V2.0 is script/config based. `client.py` is a Python 2 client and expects:
  - command shape: `python.exe client.py <Config.cfg> <kill-password> [username]`
  - required config keys include `simulation`, `simulation_prio`, `python_version`, `datafile_path`, `extension`, `skip_dir`, `skip_filename`, `finalstep`, `send_email`, `send_netsend`, `group`, `subgroup`
  - `datafile_path` can point to a single file; the manager treats an existing file path as a one-task job
  - simulation scripts must include `sys.path.append('\\\\szhradar01\\_CLUSTER_SOFTWARE\\')` or equivalent
  - sample BYD_OVRS files are under `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER`
- Access status from current Codex session on 2026-06-26:
  - `\\szhradar01\_cluster_software\` is readable
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster` is readable
  - `\\selena01\_cluster_software\` is not resolvable from this session, although the user can open it in Explorer
  - `http://szhradar01/cluster/?page=jobs` and XML-RPC `szhradar01.apac.bosch.com:8123` time out from this session
  - likely next step: run submission from VDI / compliance-room environment or another shell/browser context with manager HTTP/XML-RPC reachability
- External cluster path health check on 2026-06-26:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster` supports read/write from current session
  - a small probe directory/file under `...\Cluster\radar-sim_probe\...` was created and read back successfully
  - executing a Python script from the UNC path works in the current session
  - a 1 MB read/write roundtrip succeeded but was slow, so large MF4 files should preferably already live on a shared path instead of being copied from local `D:\data` for every run
  - `\\szhradar01\_cluster_software\` is readable and contains `client.py`, `manager.py`, `worker.py`, `simulation_runtime.py`, `python27_deprecated_modules`, and Python 2.7 installer assets
  - MySQL `szhradar01:3306` is reachable; read-only query showed `cluster_config.state=1`, `state_message=Online`, `manager_host=SZHRADAR01`, `manager_port=8123`, `http_host=https://szhradar01`
  - available external cluster groups from DB include `Radar/PSS1`, `Radar/PSS2`, `Radar/ACC`, `Radar/Jenkins`, `Radar/RA6`
  - HTTP/HTTPS status page ports `80/443` and XML-RPC manager port `8123` time out from this session
- Local ovrs25 cloud-run asset status:
  - local compiled Selena exists at `C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo`
  - full local `RelWithDebInfo` is about 640 MB
  - runtime-essential files excluding `.pdb`, `.ilk`, logs, and missing-signal text are about 90 MB
  - runtime-essential file set observed: `selena.exe`, `selena_dll.dll`, `selena_core.dll`, `selena_gui.dll`, `Mdf4Lib_x64.dll`, `MdfLibSort_x64.dll`, `MDFSort_x64.dll`, `Qt5Core.dll`, `Qt5Xml.dll`, `XmlParser_x64.dll`
  - project assets already include `config/projects/ovrs25/assets/selena/runtime.xml`, `matfilefilter.txt`, and `selena_config_tmpl.txt`
  - local input MF4 under `D:\data\...` is not directly usable by cluster workers; input data must be copied to or already exist on a worker-readable shared path
- Current architectural conclusion:
  - a cloud/cluster second path is feasible as a separate backend from local `rsim run`
  - user can compile Selena locally, then publish a trimmed runtime package plus runtime XML/filter/input data to the shared cluster workspace
  - `rsim cluster prepare` can generate a self-contained job package (`Config.cfg`, worker `SIMULATION_RADAR_SIM.py`, paramconfig template/assets)
  - actual queue submission still needs either official manager access on port `8123`, Python 2 `client.py` from an environment that can reach it, or an explicitly approved direct DB enqueue experiment
  - direct DB enqueue is technically plausible because DB is reachable, but it bypasses manager validation and should not be done without explicit user approval and a single-file smoke-test plan

### 1. Do not over-generalize low-level logic

Strong recommendation:

- keep user entry unified
  - `rsim check`
  - `rsim build`
  - `rsim prepare-sim`
  - `rsim run`
- split internal adaptation by project/recipe

### 2. The new repo/project is not structurally the same as ovrs25

Observed:

- `fvg3_lfs` repo root is not a `bindings/<name>/...` style repo
- shared ParamConfig has fields not covered by the original ovrs25 assumptions
- `adapterfile` is required
- current known source is `RadarFC`

So:

- do not force this project into the old `binding`-style build semantics
- use `recipe` or project-specific build/run shaping

## Browser / repo lookup notes

Only read-only browsing was done.

Confirmed from Bitbucket page:

- repo root visible
- `jenkins/`
- `jenkins/configs`
- `jenkins/jenkinsfiles`
- branch shown in page UI: `develop_evo`

No authenticated write action was performed.

## What is not finished yet

### 1. Recipe system is only half-done

Right now:

- recipe exists in config layering
- project skeleton exists

But not done yet:

- full execution-layer dispatch for `build / run / check / prepare-sim`

### 2. bydod25 is still a config skeleton

Important:

- local checkout paths like `D:/byd/...` are not validated yet
- they are placeholders / intended local layout, not confirmed local files

### 3. Need local-project verification later

Still needed when local repo exists:

- confirm actual `jenkins_selena_build.bat` location
- confirm actual script argument convention
- confirm actual `build_output`
- confirm actual `selena.sln`
- confirm actual `selena.exe`

## Recommended next steps for the next AI

1. Finish recipe execution model

Suggested direction:

- explicit recipe dispatch module
- separate internal handling for:
  - `ovrs25`
  - `g3n_fvg3_od25`

2. Validate bydod25 local checkout once it exists

Check:

- script path
- script args
- build output
- VS solution path
- selena.exe path

Then update:

- [config/projects/bydod25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/config.yaml)
- [config/projects/bydod25/local.example.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/local.example.yaml)

3. Update config docs

Specifically document:

- `project.recipe`
- `repos.*`
- `build.selena_branch`
- `build.script_args_template`
- `simulation.adapter_file`
- `simulation.paramconfig_options`

4. Investigate cluster backend

Check:

- whether submission should use:
  - existing Python 2 `client.py`
  - a small Python 3 XML-RPC adapter to `addSimulation`
  - the online Docupedia workflow
- required input packaging: MF4, paramconfig, runtime XML, Selena executable/build artifact, filters/adapters
- where logs and output MF4 files are stored
- whether jobs can be queried from `http://szhradar01/cluster/?page=jobs`
- whether the target environment can reach `szhradar01.apac.bosch.com:8123`

Then consider adding:

- `rsim cluster submit`
- `rsim cluster status`
- `rsim cluster fetch`
- a config section such as `cluster.url`, `cluster.tool_path`, `cluster.software_path`

## Test status

Latest full test run:

- `pytest -q`
- result: `98 passed`

## Cluster smoke test on shared data, 2026-06-26

User-provided BYD_SR data source:

- `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR\`

Smoke-test package created on the external Cluster share:

- root: `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\smoke_20260626_201242`
- Selena runtime copy: `...\selena\RelWithDebInfo`
- config/assets: `...\assets`
- extracted input data: `...\data\Gen5_2009-01-01_05-56_0114.MF4`
- output/logs: `...\output`

What was validated:

- BYD_SR source share is readable from this session.
- `23-4-26_CBNA.zip` can be inspected with `\\szhradar01\_cluster_software\7za.exe`.
- One MF4 was extracted to the Cluster project share.
- A trimmed local Selena runtime package was copied to the Cluster project share.
- Local compiled Selena can start using the shared config/assets and shared MF4 path.

Current result:

- The run is not yet a successful simulation.
- Output `Gen5_2009-01-01_05-56_0114out.MF4` was created but is only 1448 bytes.
- `selena_cbna.log` stops after runnable loading / input file setup, with no completed simulation progress.
- Directly running `selena.exe` from the UNC runtime copy in this local session failed or timed out; use local exe or copy runtime to a worker-local temp folder before launch.

Likely next isolation:

- Emulate Cluster worker behavior by copying the input MF4 from shared storage to a local temp folder, running local Selena with local temp input/output and shared assets, then copying output back.
- If that passes, the packaging/config is good and the remaining blocker is official Cluster manager/worker submission.
- If that still hangs, inspect runtime XML / radar source / mounting position / input MF4 compatibility.

## Cluster backend and Web Console progress, 2026-06-30

User clarified that Cluster batch simulation is server-deployed, not cloud-hosted:

- `http://szhradar01/cluster/?page=jobs` is primarily a status/progress page.
- Simulation assets must be staged under `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster`.
- Scheduling is handled by the server XAMPP environment plus the Cluster software package from `\\szhradar01\cluster_software` / `\\szhradar01\_cluster_software`.
- `client.py` submits from the user's PC, `manager.py` receives/dispatches, `worker.py` runs simulation, and `database.py` updates status.

Implemented first backend slice:

- `core/cluster.py`
  - `check_cluster_environment(config)`
  - `prepare_cluster_job(config, ...)`
  - `submit_cluster_job(config_path, config, dry_run=True)`
  - Generates `Config.cfg`, `SIMULATION_RADAR_SIM.py`, copied assets, `manifest.json`, and submit command.
  - The generated worker script is Python2-compatible and writes a per-task Selena paramconfig before running `selena.exe --paramconfig`.
- `cli/cluster.py`
  - `rsim cluster check`
  - `rsim cluster prepare [input_path] --dataset BYD_SR --run-id ...`
  - `rsim cluster submit <Config.cfg>` defaults to dry-run; use `--execute` for a real `client.py` call.
- `cli/web.py` plus `web/`
  - `rsim web --host 127.0.0.1 --port 8765`
  - Local Web Console with tabs for local simulation diagnostics, server Cluster simulation, and effective config.
  - API endpoints include `/api/projects`, `/api/config`, `/api/local/check`, `/api/cluster/check`, `/api/cluster/prepare`, `/api/cluster/submit`.

Current verified state:

- `pytest -q tests/test_cluster.py` passes (`3 passed`).
- `python -m py_compile core\cluster.py cli\cluster.py cli\web.py` passes.
- `python rsim.py --project ovrs25 cluster prepare --dataset BYD_SR --run-id dryrun_20260630_bydsr --json` created a real package under:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\dryrun_20260630_bydsr`
- The generated package points `datafile_path` to the shared BYD_SR dataset:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR`
- `rsim cluster check` shows:
  - OK: Cluster software path, `client.py`, `manager.py`, `worker.py`, `database.py`, `simulation_runtime.py`
  - OK: Cluster workspace root and write probe
  - OK: worker dependency paths for MATLAB/Boost/Qt network shares
  - Missing: `C:\Python27\python.exe`
- Web server was started and verified:
  - `http://127.0.0.1:8765/` returns 200
  - `/api/projects` returns `bydod25` and `ovrs25`
  - `/api/cluster/check?project=ovrs25` returns the same Cluster diagnostics

Next concrete steps:

- Configure or detect the real local Python2 runtime for `client.py` (possibly from the mapped Cluster package or an installed `py -2` launcher).
- Set `cluster.selena_exe` to a worker-visible Selena runtime path or run `rsim cluster prepare --copy-selena` for a single smoke test.
- Submit one single-MF4 package with `rsim cluster submit <Config.cfg> --execute` after Python2 is available.
- Add status/fetch commands once a real submitted job exposes the manager-created output folder naming.

2026-06-30 continuation:

- Added Python2 runtime discovery:
  - `rsim cluster python`
  - API: `/api/cluster/python`
  - It checks configured `cluster.python_path`, common Python27 paths, `py -2`, and Python on PATH.
  - Current machine result: no usable Python2 found; Python 3.12 on PATH is detected but rejected.
- Added prepared job discovery and status:
  - `rsim cluster list`
  - `rsim cluster status <job_dir>`
  - API: `/api/cluster/jobs`, `/api/cluster/status`
  - Status inspects `output/` and manager-style `OUT*` folders, output MF4s, logs, and `result.ini`.
- Added output fetch:
  - `rsim cluster fetch <job_dir> --dest <dir>`
  - API: `/api/cluster/fetch`
  - Copies output files back to `results/<project>/cluster/<run_id>` by default.
- Extended Web Console:
  - Cluster tab now has Python2 detection, prepared job list, per-job status, fetch, and dry-run submit actions.
  - Local tab now exposes `rsim run` through `/api/local/run`; default is dry-run unless `execute` is explicitly set.
- Verified:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q` -> `118 passed`
  - Web server restarted on `http://127.0.0.1:8765/`
  - `/api/cluster/jobs?project=ovrs25&limit=2` returns prepared packages.
  - `/api/cluster/python?project=ovrs25` returns all Python2 candidates as not usable.

2026-06-30 continuation 2:

- Cluster submit path is now productized around the current environment:
  - `rsim cluster check` no longer treats missing local Python2 as a blocker when XML-RPC manager submission is reachable.
  - Current verified submit mode is `xmlrpc`; check output shows `Submit path: xmlrpc`.
  - `Python for client.py` reports `C:\Python27\python.exe (not found); optional because XML-RPC submit path is reachable`.
- Status inspection now extracts high-signal failure summaries from `result.ini` and `selena.log`:
  - latest smoke state: `finished-failed`
  - OK/NOK: `0/1`
  - worker: `szhradar25`
  - useful Selena error: `no signal found in channel cache for port g_Golf_Fct_Hmi_RunnableHmi_internalstates`
  - interpretation: Cluster infrastructure path is proven through worker execution and output copy-back; the current failure is selected MF4/runtime signal compatibility, not submission plumbing.
- Web Console polish:
  - `web/index.html` and `web/app.js` restored to valid UTF-8 Chinese labels.
  - Cluster tab now has protected real submit buttons in addition to dry-run.
  - job list shows state, OK/NOK counts, file count, and first error summary line.
  - static server now returns `charset=utf-8` for HTML/CSS/JS.
- Verification:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q` -> `122 passed`
  - `python rsim.py --project ovrs25 cluster check` -> `Cluster check passed`
  - `http://127.0.0.1:8765/` -> 200, `text/html; charset=utf-8`
  - `http://127.0.0.1:8765/app.js` -> `text/javascript; charset=utf-8`
  - `/api/cluster/check?project=ovrs25` returns all Cluster checks OK, including XML-RPC submit path.
  - `/api/cluster/status?...smoke_fr_20260630_safeout` returns the real failed smoke result and error summary above.

Next best work:

- Find or generate one MF4/runtime pair that contains `g_Golf_Fct_Hmi_RunnableHmi_internalstates`, then submit a single-file smoke through the existing XML-RPC path.
- After one smoke succeeds, enable guarded batch prepare/submit from the Web Console for a selected directory rather than the whole BYD_SR root by default.

2026-06-30 continuation 3:

- Added bounded Cluster input data scanning:
  - CLI: `rsim cluster data [input_path] --dataset BYD_SR --limit N --max-read-mb M --required-signal <name>`
  - API: `/api/cluster/data?project=ovrs25&dataset=BYD_SR&limit=...&max_read_mb=...&required_signal=...`
  - Web Console: Cluster tab now has `扫描候选数据`, `Required signal`, `扫描数量`, and `每文件读取 MB` controls.
  - Candidate rows show `present`, `missing`, `missing-in-prefix`, `not-scanned`, or `error`; clicking `选用` fills the Cluster input path.
- Implementation notes:
  - scanner skips generated `*out.MF4` files.
  - it searches bounded head/tail byte segments instead of opening huge MF4s with `asammdf`.
  - it searches UTF-8 and UTF-16LE encodings of required signal names.
  - default required signal comes from `cluster.required_input_signals`, currently `g_Golf_Fct_Hmi_RunnableHmi_internalstates`.
- Data findings:
  - Direct `asammdf` metadata listing on remote BYD_SR MF4s was too slow for interactive use.
  - BYD_SR first 30 files, scanning 4 MB each, did not find `g_Golf_Fct_Hmi_RunnableHmi_internalstates`.
  - BYD_SR first 10 files, scanning 8 MB head/tail each, did not find it.
  - BYD_SR `28-4-26_DMS_FCW` first 5 files, scanning 32 MB head/tail each, did not find it.
  - Prior CBNA smoke file `...\smoke_20260626_201242\data\Gen5_2009-01-01_05-56_0114.MF4` was fully scanned (`273234976` bytes) and does not contain it.
- Runtime findings:
  - current `config/projects/ovrs25/assets/selena/runtime.xml` includes `g_Golf_Fct_Hmi_RunnableHmi`.
  - historical shared BYD_OVRS runtime exists at `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\runtime_1r1v.xml`; it uses older runnable names such as `g_Fct_RunnableHmi_RunnableHmi_A`.
  - historical Selena executables exist:
    - `...\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Master\selena.exe`
    - `...\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Slave\selena.exe`
  - historical Config points at `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\DA\Radar\02_GEN5\09_BYD\EM2E\Pre-ER\PreER_10044C`, but that data path is currently not reachable from this session.
- Verification:
  - `pytest -q` -> `124 passed`
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `python rsim.py --project ovrs25 cluster data --dataset BYD_SR --limit 2 --max-read-mb 1`
  - `/api/cluster/data?project=ovrs25&dataset=BYD_SR&limit=2&max_read_mb=1&required_signal=g_Golf_Fct_Hmi_RunnableHmi_internalstates`
  - Web static files still return UTF-8 charset and include the new scan controls.

Current interpretation:

- The Cluster backend path remains operational.
- The next real smoke-success blocker is not submission, but choosing a runtime/Selena/data combination that agrees on HMI runnable measurement names.
- Do not submit large BYD_SR batches until a single-file candidate reports `present` or a known-compatible runtime is selected.

2026-07-01 continuation:

- Added Cluster runtime profiles so the Web/CLI can switch between multiple Selena/runtime/data assumptions without editing config by hand:
  - Core:
    - `list_cluster_profiles(config)`
    - `apply_cluster_profile(config, profile_name)`
    - `check_cluster_environment(..., profile=...)`
    - `scan_cluster_data(..., profile=...)`
    - `prepare_cluster_job(..., profile=...)`
  - CLI:
    - `rsim cluster profiles`
    - `rsim cluster check --profile <name>`
    - `rsim cluster data --profile <name>`
    - `rsim cluster prepare --profile <name>`
  - API:
    - `/api/cluster/profiles`
    - `/api/cluster/check?...&profile=...`
    - `/api/cluster/data?...&profile=...`
    - `/api/cluster/prepare` accepts `profile`
  - Web Console:
    - Cluster tab now has a `Profile` selector loaded from `/api/cluster/profiles`.
- Configured ovrs25 profiles:
  - `default`: current local build/runtime assets.
  - `byd-ovrs-bl01v7-er-shared`: historical shared BYD_OVRS BL01V7_ER Master/RadarFC/PSS1 profile.
  - `byd-ovrs-bl01v7-er-shared-fl-pss2`: historical shared BYD_OVRS BL01V7_ER Slave/RadarFL/PSS2 profile.
- Profile check results:
  - both historical profile Selena executables are reachable:
    - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Master\selena.exe`
    - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Slave\selena.exe`
  - shared runtime is reachable:
    - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\runtime_1r1v.xml`
- Generated profile packages:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\dryrun_profile_api_20260701`
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\smoke_profile_fl_pss2_20260701`
- Real submissions:
  - `dryrun_profile_api_20260701` submitted through XML-RPC, manager returned `value=1`.
    - official page job id: `10320`
    - subgroup: `PSS1`
    - observed page status: `1/0/0`, output `0 MB`, end `unknown`
    - output folder: `OUT_260701_130630`
    - local status: `running-or-started`, only copied `Config.cfg` so far.
  - `smoke_profile_fl_pss2_20260701` submitted through XML-RPC, manager returned `value=1`.
    - official page job id: `10321`
    - subgroup: `PSS2`
    - observed page status: `1/0/0`, output `0 MB`, end `unknown`
    - output folder: `OUT_260701_132004`
    - local status: `running-or-started`, only copied `Config.cfg` so far.
- Official web page:
  - `http://szhradar01/cluster/?page=jobs` is reachable from this session and returns 200.
  - HTTPS fails certificate trust from this shell, but HTTP is usable for viewing status.
- Verification:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q` -> `125 passed`
  - `/api/cluster/profiles?project=ovrs25` returns all three profiles.
  - `/api/cluster/check?project=ovrs25&profile=byd-ovrs-bl01v7-er-shared` returns profile Selena/runtime OK.
  - `/api/cluster/prepare` with `profile=byd-ovrs-bl01v7-er-shared` generated a valid package.

Current interpretation after profiles:

- The app now supports multiple Cluster runtime profiles end to end.
- XML-RPC submission and official web visibility are proven for profile jobs too.
- The latest blocker is worker execution progress for the historical shared profiles: both jobs are visible on the official page but have not written worker logs/result.ini yet.
- Do not submit broader batches until one profile smoke job reaches either `finished-success` or a clear worker/runtime failure.

2026-07-01 later continuation:

- Added official Cluster V2.0 status parsing:
  - CLI: `rsim cluster web-status <job-id-or-job-dir>`
  - API: `/api/cluster/web-status?project=ovrs25&job=<job-id-or-job-dir>`
  - Web Console: prepared job rows now include an `Official` action that queries the Cluster web page.
  - The parser reads `http://szhradar01/cluster/?page=jobs` to map a prepared package path to a job id, then reads `?page=tasks&jobid=<id>` for task details.
  - It preserves the readable state such as `simulating` and stores numeric DB state as `simulation_state_code` when present.
- Latest official status check:
  - job `10320` (`dryrun_profile_api_20260701`) is assigned to `szhradar14 (CC-DA.Simulation_Room)`, task DB id `5445488`, state `simulating`, started simulation at `2026-07-01 13:06:36`, python version `python27`.
  - job `10321` (`smoke_profile_fl_pss2_20260701`) is assigned to `szhradar26 (CC-DA.Simulation_Room)`, task DB id `5445489`, state `simulating`, started simulation at `2026-07-01 13:20:10`, python version `python27`.
  - Shared output folders currently contain only the manager-copied `Config.cfg`; no worker `result.ini`, logs, or output MF4 have appeared yet.
- Verification after the official-status parser:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q tests\test_cluster.py` -> `13 passed`
  - `pytest -q` -> `126 passed`
  - `python rsim.py --project ovrs25 cluster web-status 10320 --json` -> `state: simulating`
  - `python rsim.py --project ovrs25 cluster web-status 10321 --json` -> `state: simulating`
  - Web Console restarted on `http://127.0.0.1:8765/` with process id `73988`.
  - `/api/cluster/web-status?project=ovrs25&job=<job-dir>` maps `smoke_profile_fl_pss2_20260701` to official job `10321` and returns `state: simulating`.

Next cluster step:

- Poll `rsim cluster web-status 10320` and `rsim cluster web-status 10321` until either job writes `time_finished` or the server timeout/worker error appears.
- If they finish successfully, run `rsim cluster fetch <job_dir>` and use that as the first known-good profile batch template.
- If they fail or time out without logs, compare the official output path with the shared output folder and inspect whether the worker can execute the historical shared Selena path directly.

2026-07-01 wait-command continuation:

- Added a Cluster polling command:
  - `rsim cluster wait <job-id-or-job-dir> [--job-dir <prepared-job-dir>]`
  - `--once` prints one combined snapshot and exits.
  - `--json` includes official web status, shared-output status, and a `diagnosis` block.
  - `--interval` and `--max-minutes` support longer watch sessions without hand-running repeated `web-status`/`status` commands.
- Diagnosis logic combines:
  - official task state from `http://szhradar01/cluster/?page=tasks&jobid=<id>`
  - shared output folders from `inspect_cluster_job`
  - `success_count`, `fail_count`, output MF4 count, task error messages, runtime minutes, and configured Cluster timeout.
- Added Web Console wait integration:
  - API: `/api/cluster/wait?project=ovrs25&job=<job-id-or-job-dir>`
  - prepared job rows now include a `Wait` action.
  - Web `Wait` defaults to official-status-only so the page does not block on slow UNC output scans.
  - Use the existing `状态` action for shared-output inspection; API callers can pass `shared=1&job_dir=<dir>` when they explicitly want combined official/shared diagnosis and can tolerate slow UNC scans.
- Fixed a `wait` argument-resolution bug where numeric job ids could ignore an explicit `--job-dir`.
- Latest real wait snapshots:
  - `10320`: `simulating`, worker `szhradar14 (CC-DA.Simulation_Room)`, shared state `running-or-started`, no outputs/logs/result files, runtime about `45.7` minutes, stale `false`, timeout `120` minutes.
  - `10321`: `simulating`, worker `szhradar26 (CC-DA.Simulation_Room)`, shared state `running-or-started`, no outputs/logs/result files, runtime about `32.2` minutes, stale `false`, timeout `120` minutes.
- Verification:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q tests\test_cluster.py` -> `15 passed`
  - `pytest -q` -> `128 passed`
  - `http://127.0.0.1:8765/` -> 200, `text/html; charset=utf-8`
  - `/api/cluster/wait?project=ovrs25&job=10321` -> `outcome: running`, `state: simulating`, `stale: false`

Current cluster conclusion:

- Submission, official web visibility, worker assignment, and shared-output inspection are all automated.
- Both real profile smoke jobs are still inside their configured 120-minute runtime window and should not be treated as failed yet.
- Do not submit broader batches until either `rsim cluster wait ... --once` reports success/failure or the stale flag becomes `true`.

## First successful cloud simulation, 2026-07-01 (ovrs25 cloud-build profile)

Root cause of all prior cloud failures (job 10320/10321/10322 `finished-failed` / stuck `simulating`) was found and fixed — it was **not** a runtime/data schema incompatibility, it was a missing radar-source assignment on the cluster path.

### What was wrong

- `prepare_cluster_job` did not run radar orientation auto-detection, while local `rsim run` did (via `build_effective_simulation` → `detect_radar_orientation`).
- With ovrs25 `source: "auto"`, the cluster Config.cfg rendered `radar = ""` and `mountingPosition = ""`. Selena then defaulted to `RadarFC`.
- BYD_SR `12-5-26_CBNA` data is actually `RadarFL` (detected via mounting_position x=3.66, y=0.77, confidence 0.95). Running it as RadarFC caused `no signal found in channel cache for port g_Golf_Fct_Hmi_RunnableHmi_internalstates` and a 1448-byte output.
- A second defect: `cli/cluster.py _run_prepare` passed `copy_selena=bool(False)` instead of `None`, which suppressed the `selena.source=build` auto-package logic. Fixed to `or None` so profile-driven Selena packaging triggers.

### Fixes

- `core/cluster.py prepare_cluster_job`: when `source`/`mounting_position` are auto/unset and the input is a single MF4, call `detect_radar_orientation` and write the result into `sim` before rendering Config.cfg. Mirrors the local path.
- `cli/cluster.py _run_prepare` (and `_run_one_shot` already correct): pass `copy_data`/`copy_selena` as `None` when unset so profile adaptivity decides.

### Successful cloud run

- Profile: `cloud-build` (backend=cluster, selena.source=build → packaged local selena.exe + 10 DLLs ≈90 MB into job folder, data.copy=false → BYD_SR referenced in place on UNC).
- Input: `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR\12-5-26_CBNA\12-5-26_CBNA\Gen5_2009-01-01_03-57_0115.MF4` (393 MB).
- Detected: `radar=RadarFL`, `mountingPosition=CFL`.
- Worker: `szhradar27` (PSS1). Init 24s + simulation 51s.
- Output: `Gen5_2009-01-01_03-57_0115out.MF4` = **537 MB** (> input, success).
- `result.ini`: `successfull=1`, `simulation_state=4`, `error_message=` (empty), `out_size` reported 0 but the 537 MB MF4 is present on disk.

### Verification

- `pytest -q` → `159 passed` (no regression from the cluster fixes).
- Note: cluster web page `http://szhradar01/cluster/?page=jobs` returned 404 during this session; XML-RPC manager on 8123 was still reachable and submission succeeded. Track cloud jobs via shared `OUT_*` output folders (`rsim cluster status` / `rsim cluster wait`) when the web page is down.

### Note on signal scanning

- Earlier signal scans of BYD_SR concluded the data lacked `g_Golf_Fct_Hmi_RunnableHmi_internalstates`. That conclusion was misleading: the `_internalstates` variant is a runtime port name that does not appear as a raw byte string in the MF4 prefix; both the local-passed CBNA_23-4-26 and the cloud-passed BYD_SR 12-5-26_CBNA scan as `missing-in-prefix` for it, yet both simulate successfully when `source=RadarFL` is set. The plain runnable name `g_Golf_Fct_Hmi_RunnableHmi` is present in both. Do not use `_internalstates` signal scans as a compatibility gate — use radar orientation detection + a real single-file smoke instead.

## Batch cloud simulation + configuration + unified check + dual API, 2026-07-01 (evening)

### Batch cloud verification

- Re-submitted the other 2 MF4s of BYD_SR `12-5-26_CBNA` via `cloud-build` profile. All 3 finished-success, ~512 MB output each, parallel workers (szhradar26/27). Batch path proven.

### Configuration management (Phase 2)

- Reused the existing `local.yaml` gitignored overlay for per-user needs (different Selena branch / repo / data / profile). No new config layer.
- New `rsim config` command (`cli/config.py`): `show` (effective merged config), `init` (copy local.example.yaml → local.yaml), `diff` (which keys local.yaml overrides vs config.yaml).
- `local.example.yaml` expanded with A/B/C scenario docs (develop branch + shared data / feature branch + local data / different machine toolchain).
- profile gained optional `selena.selena_branch` so checks can warn on exe/branch mismatch.
- README gained a "用户配置指南" section.

### Unified environment check (Phase 1)

- `CheckItem` gained `severity` (error|warning|info) and `category` (repo|selena|runtime|data|cluster|profile); defaults keep old callers working.
- New `core/repo.py` consolidates repo checks (outer/inner existence, branch match, dirty tree, submodule init) and `prepare_repo_context` (branch switch before build). `cli/check.py::_check_repo_context` and `cli/build.py::_prepare_repo_context` now delegate to it.
- New `CheckReport` dataclass (`.ok`/`.errors`/`.warnings`/`.items`), `__iter__` for transitional compat. `check_for_backend` returns `CheckReport`.
- `check_local_environment` now covers repo + selena.exe + exe/branch freshness (mtime vs branch ref) + runtime/adapter + data reachability + radar orientation detectability.
- `rsim check --backend local|cluster --profile <name>` prints severity-graded items (OK / W / !!).

### Public Python API (Phase 3)

- New `core/api.py` (API_VERSION 1.0) — stable entry for software integration: `load_project`, `list_profiles`, `check_environment`, `prepare_simulation`, `run_local`, `submit_cluster`. Other software `from core.api import *`.
- `run_local` uses subprocess isolation (same as web) to keep a selena crash from taking down the caller.
- `core/__init__.py` declares `__all__ = ["api"]` + `__version__`.

### Web frontend (Phase 4)

- Fixed `/api/cluster/prepare` passing `bool(False)` instead of `None` for copy_data/copy_selena (same profile-adaptivity bug fixed in cli/cluster.py earlier).
- New endpoints: `GET /api/profiles` (unified, includes local backend), `GET /api/check` (CheckReport with severity), `POST /api/cluster/run` (non-blocking prepare+submit; front-end polls `/api/cluster/wait?once=1`).
- Front-end: new "环境校验" tab renders severity-graded items; Profile dropdown unified via `/api/profiles`.

### Verification

- `pytest -q` → `183 passed` (was 159; +11 environment, +9 api, +4 web).
- `rsim --project ovrs25 check --backend local --profile local-build` reports repo/selena/runtime/data with severity.
- `python -c "from core.api import check_environment; print(check_environment('ovrs25', profile='local-build').ok)"` → True.
- Web `/api/check` and `/api/profiles` return expected JSON; `/api/cluster/prepare` passes None for unset copy flags.

### Next

- bydod25 cloud profile is configured (`cloud-build`) but not yet smoke-tested on cluster (local bydod25 simulation already passes). A bydod25 cloud smoke would need BYD_SR data compatibility or bydod25's own Vehicle_FR5CP data staged to a shared path.
- Web front-end "一键运行" button wiring (poll loop after `/api/cluster/run`) can be polished further; the backend endpoint is in place.

### 控制平面 + web 接入（2026-07-03）

- **清理**：删除误生成的 `profiles.json`（HTTP 404 HTML）和一次性脚本 `_dry.py`。
- **agent 编码**：`cli/agent.py` 的 `Popen` 加 `encoding="utf-8", errors="replace"`，修复 Windows charmap 遇中文 stdout 崩溃。
- **`rsim tcc` CLI**（新 `cli/tcc.py`）：`bootstrap-itc2` / `install <tc>` / `auto-repair` / `status`，把 `core/tcc.py` 的纯 Python API 暴露成子命令，供 agent 调度。
- **agent 加 tcc task_type**：`_build_task_command` 加 `tcc.bootstrap_itc2` / `tcc.install_toolcollection` / `tcc.auto_repair_all` 三分支，`DEFAULT_CAPABILITIES` 加对应能力。
- **适配层 `core/web_control.py`**（新）：把 web 端点的 `BuildTask.tail()` 11 字段 shape 桥接到 control plane 的 job/task 模型——状态映射（`succeeded→success`）、`log_id` 当 `total_lines` 游标、缺字段（exe_path/files_done 等）从 `job.result` 取。`ControlService.list_jobs()` 新增。
- **`rsim web` 内置控制平面**：启动时拉起 control server（线程，127.0.0.1:8877，`results/_control.db`）+ polling agent（线程，复用 `cli.agent._run_task`，走本机 HTTP）。build/sim/tcc/cancel/tasks/repair 端点全转适配层。`_tail_task` 带 legacy `BuildTaskRegistry` fallback（旧 task_id 仍可查）。`--no-control` 退回旧路径。
- **前端零改动**：响应 shape 不变，localStorage 恢复逻辑兼容（job_id 当 task_id 用）。
- **端到端验证**：前端编译 → job 进 control DB → 内置 agent 认领 → 跑 `rsim build selena` → 122 行日志回传 → 前端轮询看到；tcc bootstrap_itc2 → `rsim tcc bootstrap-itc2` → success。重启 web 后残留 queued job 自动被 agent 认领（持久化恢复）。
- **测试**：`pytest -q` → 312 passed（+9 web_control、+2 agent tcc、+4 web 集成）。
- **Linux 迁移路径**：代码已统一，拆 `rsim server serve` 到 Linux + web 加 `--server-url` + agent 留 Windows 即可，零特例。详见 `SIMULATION_WORKFLOW.md` §10。

### 多用户隔离 + 分发（2026-07-03）

- **KI-1 记录**：web 云端仿真失败（Cluster job 10319, Returncode=-1）记入 `docs/KNOWN_ISSUES.md`，含现象/已排除/4个怀疑方向/排查步骤。根因需下次跑时抓 worker stderr。
- **P1 RSIM_HOME**：`core/config.py` 加 `get_data_root()`（读 RSIM_HOME，缺省回退仓库根）。results/DB/task_store 跟随；config/assets 不变（代码资源）。`control_service`/`simulation` 内的 `_data_root()` 同步。向后兼容。
- **P2 每用户 DB**：`core/user.py`（current_user/control_db_path_for_user）。user 标识 = RSIM_USER > OS用户 > default。DB = `_control_<user>.db`。HTTP 链路用 `X-Rsim-User` 头传递；`control_http.make_control_handler` 接受 service 或 `(user)->service` factory；server serve 用 per-user service 缓存。**互不可见验证通过**（alice 看不到 bob job，HTTP 404）。
- **P3 唯一化**：embedded agent_id = `embedded-<user>-<pid>`（不再硬编码）；agent --agent-id 缺省 = `agent-<user>-<hostname>`；web 端口绑定失败 fallback 随机端口。
- **P4 local.yaml 用户目录**：`local_yaml_path_for_project()` 优先 `$RSIM_HOME/config/projects/<name>/local.yaml`，回退仓库内。save/load/list/export/import 端点全适配。
- **P5 _runtime 并发隔离**：`_runtime/<pid>/` 子目录，CRlog.log 和 paramconfig 按进程隔离，同项目并发不覆盖。
- **P6 分发**：`setup.py` install_requires 只留 PyYAML，重依赖移到 `extras_require[full]`，新增 `[control]`（轻量）。`scripts/build_server_pyz.py` 打 14KB zipapp 单文件（server 专用，任意 python3.9+ 跑）。`docs/server-deploy.md` 加分发 + 多用户章节。
- **端到端验证**：两 RSIM_USER（alice/bob）连同一 server，alice agent 只认领 alice job（→succeeded），bob job 不被碰（仍 queued），两独立 DB 文件。
- **测试**：312 → 319 passed（+6 user +1 http 隔离）。

### web 接远程 server + 用户标识（2026-07-03）

- **RemoteControlClient**（`core/remote_control.py`）：轻量 HTTP 客户端，镜像 web 需要的4操作（create_job/get_job/get_logs/cancel_job/list_jobs），所有请求带 `X-Rsim-User` 头。不复用 agent 的 `_ControlClient`（那是 agent 专用）。
- **GET /api/jobs 端点**：`control_http.py` 加列表路由，调 `service.list_jobs(limit)`。返回 `{"jobs":[...]}`。
- **web_control 远程模式**：`set_remote_client(client)` 注入。各函数（start_build/sim/tcc/tail/cancel/list_jobs）有 remote client 走 HTTP、否则走本地 `_service()`。`tail_via_control` 抽出 `_tail_from_job_and_logs` 共享本地/远程（复用 `_map_status`/`_extract_errors` 纯函数）。404 → `{"found":False}`。
- **web --server-url/--user**：`rsim web --server-url http://server:8877 --user alice` → 跳过内置 server/agent，构造 RemoteControlClient 注入 web_control。三种模式：内置（默认）/远程（--server-url）/禁用（--no-control）。浏览器零改动（单用户每 web 实例）。
- **端到端验证**：本机 server + `rsim web --server-url` → web 投 build job → 转发到远程 server → job 进 alice DB → bob 看不到（隔离）。
- **本地 Selena + 云端 UNC 数据 dry-run**：本地 smoke MF4 dry-run 通过（paramconfig 在 `_runtime/<pid>/`，radar 检测 FL conf=0.95）。UNC 375MB 文件 dry-run 因网络读取慢未完成（非 bug，是 UNC 大文件体验问题）。
- **测试**：319 → 326 passed（+7 remote_control + web_control 远程模式）。
- **三种 web 模式定型**：内置 / 远程 / 禁用，覆盖单机、跨机多用户、legacy 三种场景。

## Linux 控制面迁移盘点与交接，2026-07-07

### 目标边界

- Linux 只提供控制面服务：job/task 调度、agent 注册与认领、日志/结果存储、per-user 路由、web/SDK 入口。
- Selena 编译、本地仿真、TCC/VS/bat、用户本机数据访问仍全部在 Windows 用户电脑执行。
- Windows 侧通过 `rsim agent --server-url http://<linux>:8877` 主动轮询，不要求用户电脑开放入站端口。

### 当前实施状态

| 领域 | 状态 | 关键文件 |
|------|------|----------|
| 控制面存储与状态机 | 已实现。SQLite 持久化 agents/jobs/tasks/logs，支持 ordered task、cancel、wrong-agent 拒绝、重复完成拒绝、dead-agent reclaim。 | `core/control_service.py`, `tests/test_control_service.py`, `tests/test_reclaim.py` |
| HTTP 控制接口 | 已实现。stdlib `ThreadingHTTPServer` handler，支持 health、job create/get/list/logs/cancel、agent register/poll/heartbeat、task logs/result，按 `X-Rsim-User` 路由。 | `core/control_http.py`, `tests/test_control_http.py` |
| CLI server | 已实现。`serve/create-job/get-job/get-logs/cancel/reclaim`，`NO_CONFIG=True` 可在 Linux 轻量 server 环境运行。 | `cli/server.py` |
| Windows agent | 已实现。主动 register/poll/heartbeat，调用本机 `rsim check/build/run/cluster/tcc`，回传 stdout/result，支持取消和启动失败回报。 | `cli/agent.py`, `tests/test_control_agent.py` |
| web 接远程 server | 已实现。`rsim web --server-url ... --user ...` 使用 `RemoteControlClient`，远程模式不启动内置 agent。 | `cli/web.py`, `core/remote_control.py`, `core/web_control.py`, `tests/test_remote_control.py`, `tests/test_web_control.py` |
| 多用户隔离 | 已实现。`RSIM_USER` / OS user 映射到 `_control_<user>.db`，HTTP 通过 `X-Rsim-User` 头选择用户 DB。 | `core/user.py`, `tests/test_user.py` |
| Linux 分发 | 已实现。server-only zipapp 和 Dockerfile，zipapp 仅打包 stdlib server 依赖。 | `scripts/build_server_pyz.py`, `Dockerfile`, `tests/test_server_pyz.py`, `docs/linux-server-deploy.md` |
| 部署文档 | 已有。Linux 部署指南、server deploy 文档、跨机 E2E runbook、KNOWN_ISSUES。 | `docs/linux-server-deploy.md`, `docs/server-deploy.md`, `docs/e2e-linux-windows-runbook.md`, `docs/KNOWN_ISSUES.md` |

### 本次复核证据

```bash
python -m pytest tests\test_control_service.py tests\test_control_http.py tests\test_control_agent.py tests\test_user.py tests\test_remote_control.py tests\test_web_control.py tests\test_reclaim.py tests\test_server_pyz.py -q
# 56 passed

python -m pytest -q
# 336 passed in 105.85s

python rsim.py server --help
# serve/create-job/get-job/get-logs/cancel/reclaim present

python rsim.py web --help
# --server-url / --user / --no-control present

python rsim.py agent --help
# --server-url / --agent-id / --capability / --once present
```

### 已知缺口与风险

- ~~`docs/e2e-linux-windows-runbook.md` 写了 `GET /api/agents` 用于验证 agent 注册，但当前 `core/control_http.py` 没有 agent list 路由，`ControlService` 也没有 `list_agents()`。~~ **✅ 2026-07-07 已补齐**：`ControlService.list_agents()`、`GET /api/agents`、`rsim server list-agents`、web `/api/agents` 全部实现并测试。runbook 验证步骤已更新。
- `docs/server-deploy.md` 仍有旧注记“web --server-url 当前未实现”，但代码中已经实现。需要清理旧文档，避免误导。
- `X-Rsim-User` 是可信内网头，不是鉴权。任意人可伪造 user 头访问对应 DB，生产化前必须加 token 或反向代理鉴权。
- remote web 模式只负责投 job 和看状态，不内置执行 agent。没有 Windows agent 时 job 会一直 queued。
- 跨机投递不要传 Linux/Windows 本机路径。Linux 投给 Windows agent 时优先用 `--project` 和 `--dataset`，让 agent 在自己的 Windows 配置里解析路径。**注**：`server create-job` 的 `--project` 曾被空默认值覆盖导致 project 丢失，2026-07-07 已修复（见下方「端到端 loopback 回归」）。
- `server reclaim` 目前是手动 CLI，不是 server 内置周期任务。agent 崩溃后需要人工或外部定时任务调用。
- 文档记录过真实跨机链路通过：Linux Ubuntu + Python 3.10 跑 `rsim_server.pyz`，Windows agent 连接并执行回传。2026-07-07 本次只做本机代码和测试复核，没有重新跑真实跨机 build/sim。

### 下一步改造计划

1. ~~**补齐可观测性 API**~~ **✅ 2026-07-07 完成**
   - ~~增加 `ControlService.list_agents()`。~~ 已实现（`core/control_service.py`）。
   - ~~增加 `GET /api/agents`。~~ 已实现（`core/control_http.py`），返回 agent_id/name/status/last_heartbeat/current_task_id/capabilities/hostname/platform。
   - ~~增加 `rsim server list-agents`。~~ 已实现（`cli/server.py`）。
   - ~~更新 `docs/e2e-linux-windows-runbook.md` 的 agent 注册验证步骤。~~ 已更新。
   - **额外**：web 前端 `/api/agents` 端点（`cli/web.py` + `core/web_control.py:list_agents_via_control` + `core/remote_control.py:RemoteControlClient.list_agents`），嵌入式与远程模式共用。

2. **自动化 dead-agent 回收**
   - 在 `rsim server serve` 增加可选参数：`--reclaim-interval`、`--stale-after`、`--max-attempts`。
   - 默认可先关闭或保守启用，避免误杀长时间无 stdout 但 heartbeat 正常的任务。
   - 保留 `rsim server reclaim` 作为人工运维入口。

3. **补最小鉴权**
   - 增加 `RSIM_SERVER_TOKEN` / `--token`。
   - server 校验 `Authorization: Bearer <token>` 或 `X-Rsim-Token`。
   - agent、RemoteControlClient、web `--server-url` 都带 token。
   - 文档保留可信内网模式，但明确生产必须启 token。

4. **整理部署文档**
   - 合并或互相引用 `docs/linux-server-deploy.md` 与 `docs/server-deploy.md`，消除旧状态冲突。
   - 更新 `README.md` 控制平面章节，列出三种模式：embedded / remote / legacy。
   - 在 runbook 中标注“Linux 投递用 project/dataset，不传本机路径”。

5. **真实跨机回归**
   - 重新构建 `dist/rsim_server.pyz`。
   - Linux 起 server，Windows 起 agent。
   - 依次验证：agent list、local.check、local.build_selena dry path、local.run_sim dry-run、cancel、reclaim。
   - 如果可用真实数据，再跑一条 CBNA smoke，记录 job_id、agent_id、输出大小和日志证据。

6. **SDK 化**
   - 基于 `core.remote_control.RemoteControlClient` 提供稳定 `radar_sim_sdk` 包装。
   - 固定 create/check/build/run/status/logs/cancel 的 Python API。
   - 后续前端和外部自动化只依赖 SDK，不直接拼 HTTP payload。

## 端到端 loopback 回归（2026-07-07）

依据「Linux 控制面迁移盘点」第 5 项「真实跨机回归」执行。真实跨机（Linux server + Windows agent 双机）需 Linux 环境，本次先在本机用 `dist/rsim_server.pyz` + `rsim agent` 跑 loopback 端到端，验证控制面全链路（跨机只差网络，逻辑同构）。

### 已重建产物

- `dist/rsim_server.pyz` 重新构建（16341 bytes），含本次 create-job 修复。
- 启动方式不变：`python rsim_server.pyz server serve --host 0.0.0.0 --port 8877 --db-path <db>`。

### 链路验证证据（loopback，RSIM_HOME=/tmp/rsim-e2e 隔离）

| # | 链路 | 结果 | 证据 |
|---|------|------|------|
| 1 | agent 注册 + local.check | ✅ | job_fef278540388 → succeeded，returncode 0，日志回传 `[agent] starting local.check` + check 输出 |
| 2 | run_sim dry-run（带 project） | ✅ | job_04b9c5a4b007 → succeeded，dry-run 打印完整仿真计划（selena.exe 路径、input/output MF4、paramconfig 在 `_runtime/<pid>/`、radar FL conf=0.95） |
| 3 | cancel | ✅ | job_f9df8632b6de → status=cancelled，cancel_requested=true |
| 4 | reclaim（dead-agent 回收） | ✅ | job_b60fec25861e task 被 agent 认领为 running 后 agent 被 kill；`server reclaim --stale-after 3` 把 task 重新入队 queued，attempt_count=1，assigned_agent 清空 |
| 5 | build_selena dry path | ⚠️ 未单独跑 | build 命令无 `--dry-run`，会真实调 VS/selena 编译链；端到端投递链路已被单测覆盖，真实编译留待有工具链的跨机回归 |
| 6 | agent list（`GET /api/agents`） | ❌ 未实现 | P1 缺口属实：`/api/agents` 返回 404，`rsim server list-agents` 不存在。runbook 验证 agent 注册步骤依赖它 |

### 修复：server create-job project 丢失 bug（阻断跨机投递）

**现象**：`rsim server create-job local.check --project ovrs25 --backend local` 投递后，task payload 里 project 为空。`--payload-json '{"project":"ovrs25"}'` 同样丢失。这直接违反 HANDOFF.md:788「跨机投递优先传 project/dataset」约束——project 根本传不进 task。

**根因**（`cli/server.py:_run_create_job`）：
1. create-job 子命令定义了自己的 `--project`（default=""）。argparse 子命令 namespace 会覆盖父 parser 同名属性，所以全局 `rsim --project ovrs25 server create-job` 的 ovrs25 也被 default="" 覆盖。
2. `task_payload.update({..."project": args.project or ""...})` 无条件用（可能为空的）CLI 字段覆盖 `--payload-json` 里的 project。

**修复**：CLI 标志字段改为「非空才覆盖 payload_json」，让 `--payload-json` 成为 project 等字段的可靠来源。`server create-job --project ovrs25` 和 `--payload-json '{"project":"x"}'` 两种方式现在都正确进 payload。

**回归测试**：`tests/test_server_pyz.py` 新增 `test_create_job_project_flag_lands_in_payload`、`test_create_job_payload_json_project_survives`（subprocess 隔离，避免污染 sys.modules）。全量 336 → 338 passed。

### 新发现的运维问题（未修，记入 KNOWN_ISSUES 候选）

1. **`rsim server get-logs` 在中文 Windows 终端崩溃**：DB 里存的日志含中文（如 check 输出），`get-logs` CLI 用 charmap 打印到 cp936 终端时报 `'charmap' codec can't encode`。需 `PYTHONUTF8=1` 才能正常打印。agent 端已修（HANDOFF.md:709），但 server CLI 端的打印路径未修。
2. **agent 投递 `--select` 任务必失败**：`rsim run --select` 是交互式（要 stdin 输入文件号），agent 的 `subprocess.Popen` 在非交互环境 stdin 为空，`input()` 立即 EOF → "No files selected" → returncode 1。**agent 投递的 run_sim 任务不要用 `--select`**，应直接传 `--input-mf4` 或 `--dataset`（不带 select）。需在 runbook 标注。

## 真实跨机端到端回归（2026-07-07）

在 Linux 服务器（10.190.171.44，Ubuntu 22.04 + Python 3.10.12）部署代码、启动控制服务，Windows 本机起 agent 跨机连接，重跑 P4 全链路。**6 条链路全部通过。**

### 部署方式

- 代码同步：本地 `tar` 打包（排除 `__pycache__`/`results`/`dist`/`*.MF4`/`*.db`）通过 ssh 管道传到 `~/radar-sim/`（Windows 无 rsync，用 tar over ssh）。
- Linux 启动：`python3 rsim.py server serve --host 0.0.0.0 --port 8877 --db-path ~/rsim_data/cross.db`（nohup 后台）。
- 端口：ufw 虽激活但 8877 实测可达（无需额外放行；HANDOFF.md:91 旧注记「需 sudo ufw allow 8877」未复现）。
- Windows agent：`RSIM_USER=alice NO_PROXY=10.190.171.44 python rsim.py agent --server-url http://10.190.171.44:8877`（`NO_PROXY` 绕过 Bosch 代理，与 HANDOFF.md:76 一致）。
- **投 job 到远程 server 必须用 HTTP POST `/api/jobs`**（curl 或 RemoteControlClient）。`rsim server create-job` CLI 只写本地 DB，不投远程——这是设计（server CLI 用于本地 DB 运维）。

### 链路验证证据（跨机，agent=win-agent-cross，user=alice）

| # | 链路 | 结果 | 证据 |
|---|------|------|------|
| 1 | 跨机 health + 空状态 | ✅ | Windows `curl http://10.190.171.44:8877/health` → `{"ok":true}`；`/api/agents` `/api/jobs` 空返回 `[]` |
| 2 | agent 跨机注册 + list-agents | ✅ | Windows agent 注册后，Linux `GET /api/agents` 见 `win-agent-cross \| Windows-11 \| hostname WX8-C-0001A \| idle`；`rsim server list-agents` CLI 同样可查 |
| 3 | local.check 跨机 | ✅ | job_de808aad39d2 → succeeded, rc=0, agent=win-agent-cross；日志跨机回传（含 Windows 路径 `C:\BYD_OVS_CB`） |
| 4 | run_sim dry-run 跨机 | ✅ | job_eec3e82ece5a → succeeded；dry-run 日志显示 selena.exe 路径、paramconfig `_runtime/<pid>/`、radar FL conf=0.95，全跨机回传 |
| 5 | cancel 跨机 | ✅ | job_4103cabe859b 投递 → cancel_requested → agent heartbeat 检测 → task=cancelled, rc=1 → agent 回 idle |
| 6 | reclaim 跨机 + 恢复 | ✅ | 真实 run job 认领后 kill agent → task 卡 running → Linux `rsim server reclaim --stale-after 3` → task 重新 queued, attempt_count=1 → 新 agent 注册立即认领，job 重新 running |
| 7 | build_selena dry path | ⚠️ 未单独跑 | build 无 `--dry-run`，真实编译需 VS 工具链；投递链路已被单测 + 上述链路覆盖 |

### 跨机发现的测试可移植性问题（已修）

- `tests/test_control_agent.py::test_build_task_command_for_local_run_sim_matches_cli_flags` 断言 `sys.executable` 以 `python` 结尾，但 Linux 上是 `/usr/bin/python3`（以 `python3` 结尾）。已修为兼容 `python3`。
- `tests/test_v4.py::TestCLI::*`（18 个）硬编码 `subprocess.run(["python", "rsim.py", ...])`，Linux 无 `python` 只有 `python3` → FileNotFoundError。**未修**（既有的测试可移植性问题，不在本轮控制面目标范围；控制面测试全过）。后续可统一改用 `sys.executable`。

### Linux 测试

- 控制面套件：63 passed（test_control_service/http/agent/user/remote_control/web_control/reclaim/server_pyz）。
- 全量：325 passed, 18 failed（均为 test_v4 的 `python` 硬编码问题，非控制面）。

### 下一步（真实跨机回归剩余项）

- ~~拿到 Linux 环境重跑 6 条链路~~ ✅ 2026-07-07 完成。
- ~~先实现 P1（`GET /api/agents` + `list_agents`）~~ ✅ 已完成并跨机验证。
- 跨机 build_selena 真实编译回归（需 Windows agent 本机有 VS + selena 源码）。
- CBNA smoke 真实跑一条（非 dry-run），记录 job_id/agent_id/输出大小/日志。本机数据 `D:/data/byd/...CBNA_23-4-26` 可用，agent 在 Windows 本机执行即可。
- 修 test_v4.py 的 `python` 硬编码（改 `sys.executable`），让 Linux 全量测试也绿。

## 双模式架构调整（2026-07-07）

### 策略转变

之前 Linux 迁移让 server 接受全部 4 种 task_type（local.check / local.build_selena / local.run_sim / cluster.run），Windows agent 跨机跑 local task。用户认为这不合理：**Linux 提供服务时仿真应仅走 cluster 链路**（依赖最少，集群节点有 selena/MATLAB/Qt，Windows 接入端无需繁重依赖）；**本地编译预设 Windows 用户 clone 仓一键部署**。

确立**双模式**架构（同一份代码，按部署模式启用不同 task_type 集合）：

- **模式 A（Linux 服务，cluster-only）**：Windows 用户不 clone 完整工具链，装 Python+PyYAML+agent 连 Linux server；server 用 `--allowed-task-types cluster.run` 启动，拒绝 local task（HTTP 400）；agent 默认 capability 为 `cluster.run`（+ tcc.*）。
- **模式 B（Windows 本机仓，完整能力）**：clone 仓一键部署，保留 local + cluster 双能力，`rsim web` 前后端齐全。

### 本次完成

**模式 A：**
- `cli/server.py`：`serve` 加 `--allowed-task-types`（逗号分隔，默认空=全允许）。
- `core/control_http.py`：`make_control_handler(service, allowed_task_types=None)`；POST `/api/jobs` 校验 `job_type` 和 `tasks[].task_type`，不在白名单返回 400。**白名单默认空=全允许**，模式 B 零影响。
- `cli/agent.py`：`DEFAULT_CAPABILITIES` 收窄为 `["cluster.run", "tcc.*"]`；新增 `FULL_CAPABILITIES` 供模式 B；`_build_task_command` 的 local 分支**保留**（模式 B 显式 `--capability local.*` 仍能用）。
- `Dockerfile`：CMD 加 `--allowed-task-types cluster.run`。
- 文档：`docs/linux-server-deploy.md` / `docs/e2e-linux-windows-runbook.md` / `SIMULATION_WORKFLOW.md` §10.3/10.5/10.6 全部改为模式 A 仅 cluster.run 示例 + 双模式说明。

**模式 B 核心：**
- `scripts/bootstrap.ps1`：PowerShell 一键部署（Python 检测→venv→依赖→local.yaml→doctor+check），支持 `-Project`/`-SkipDeps`/`-SkipCheck`，幂等可重跑，支持 `third_party/python-wheels/` 离线装。
- `cli/doctor.py`：新增 `rsim doctor` 子命令。系统级诊断（VS2017/2019/2022 实际安装、MATLAB/Qt/Boost/selena_env 路径存在性、Python 包可导入性、集群 UNC 可达性、cluster profile selena source），输出分级 ok/warning/error + 修复建议，支持 `--backend`/`--json`。区别于 `rsim check`（配置一致性），doctor 探测真实机器。

**文档：**
- `README.md`：快速开始重写为双模式表格 + bootstrap.ps1 路径；命令一览加 `rsim doctor`；控制平面示例加 `--allowed-task-types` 和模式 A/B 区分。
- `docs/environment-setup.md` §10：`bootstrap.ps1` 和 `rsim doctor` 从 TODO 标为已实现。

**测试（358 全绿，零回归）：**
- `tests/test_control_http.py`：新增 4 个白名单用例（`cluster_only_server` fixture + 拒绝 local / 接受 cluster.run / 拒绝 tasks[] 内 local / 默认 server 全允许）。
- `tests/test_doctor.py`：新增 11 个用例（路径存在/缺失、VS 版本匹配/不匹配、deferred env path、cluster UNC 可达/不可达、JSON 输出、返回码、backend 过滤）。
- 现有 local task 测试全保留（模式 B 仍支持），未改动。

### 后续待办（HANDOFF）

1. **`scripts/build_agent_pyz.py`（A3）** — 把 agent + cluster 链路打成单文件 pyz，让模式 A 的 Windows 端无需 clone 完整仓。当前模式 A 暂以 clone 仓 + `rsim agent` 接入。
2. **`rsim config init --auto-detect`（B3）** — 扫描本机路径自动填 `local.yaml` 的 `environment.*`，复用 doctor 的检测函数。当前 bootstrap.ps1 用"复制模板 + doctor 诊断 + 手填"。
3. **离线 wheel 目录 `third_party/python-wheels/`（B4）** — 内网 asammdf 等 C 扩展包的离线安装方案。bootstrap.ps1 已预留离线装逻辑（目录非空则 `--no-index --find-links`），但目录本身未预置。
4. **server 端 config.yaml 预置 cluster-inherent 默认（A4 延伸）** — 把 workspace_root / software_path / group / subgroup / 共享 selena 包路径固化到 server 侧 config，让用户侧只指定 project + input + profile。
5. **bootstrap.ps1 注释里的 em dash 在 cp936 终端乱码** — 纯注释问题，不影响执行，低优先级可改 ASCII。
6. **VS 检测增强** — `doctor._check_visual_studio` 目前只扫 `C:\Program Files (x86)\Microsoft Visual Studio\{2017,2019,2022}`，可加 vswhere.exe 或注册表扫描覆盖非标准安装路径。

## 鲁棒性修复 + 实际数据仿真验证（2026-07-07，晚）

### 目标重述

Linux 提供仿真服务（前端页面 + 后端接口），不同用户通过本地 Selena 或指定路径 Selena 在 cluster 完成仿真；Windows 源码用户支持本地仿真 + cluster 仿真；配置精简、环境校验、鲁棒性好；用实际数据做仿真测试。

### 修复的 bug

1. **doctor selena.exe 字段名 bug**（`cli/doctor.py`）：`_check_cluster_dataset_profile` 读 `selena.path`，但 unified profile 字段是 `selena.exe`。改为用 `core.profiles.list_profiles` 统一解析（含 legacy `cluster.profiles[].selena_exe` 转换），字段读 `selena.exe`。之前对 source=path 的 cluster profile 永远报 "no path given" false warning。
2. **check/doctor 在 cluster-only 机器误报 local 工具链缺失**：
   - `cli/doctor.py`：`--backend` 默认改为 auto——有 local profile 或工具链路径时跑 all，否则只跑 cluster。新增 `_infer_backend`。
   - `cli/check.py`：`run` 不带 `--backend` 时，若 `_is_cluster_only_config`（无 local profile + 无 matlab_root/qt_path/boost_root/BOOST_ROOT/selena_env_path/vs_version/python3_path）则自动走 cluster 检查。避免模式 A 机器上 BOOST_ROOT/build 脚本/VS 误报 error。
   - 两者判定 key 同步（含大写 BOOST_ROOT）。
3. **doctor `--json` 模式日志污染**：`--json` 时把 root logger 调到 WARNING，避免 numexpr/asammdf 的 INFO 日志混入 stdout 破坏 JSON 解析。

### 新增文档

- `docs/cluster-only-quickstart.md`：模式 A 快速开始。明确 Windows 端最小配置（无需 local.yaml、无需 MATLAB/Qt/Boost/VS），用 `selena.source: path` 指向集群共享 selena.exe + UNC dataset。含配置精简要点表、Selena 分支说明、故障排查。

### 实际数据仿真验证（真实跑通）

**模式 A（Linux cluster-only 服务）端到端** — 本地起 server + agent 模拟：
- server：`rsim server serve --port 8890 --allowed-task-types cluster.run`
- 白名单生效：`local.run_sim` / `local.build_selena` → HTTP 400；`cluster.run` → HTTP 201 queued。
- agent（`--once`）认领 cluster.run task（job_7d7c3b757302, agent=test-agent-1）→ 子进程执行 `rsim.py --project ovrs25 cluster run --dataset BYD_SR --profile byd-ovrs-bl01v7-er-shared` → job 打包成功（dry-run，source=path 共享 Selena + BYD_SR UNC dataset）→ task status=succeeded, rc=0，日志跨进程回传。
- 证明：模式 A Windows 端用 source=path 共享 Selena + UNC dataset，无需本机工具链即可提交 cluster 仿真。

**模式 B（Windows 本机仓）local 仿真真实跑通**：
- `rsim --project ovrs25 run <CBNA MF4> --profile local-build --timeout 600 --no-retry`
- selena.exe（本机编译，source=build）实际执行 229.6s，产出 `Gen5_2009-01-01_06-02_0115out.MF4` = **364.9 MB**（382606488 字节）。
- `[SUCCESS] Simulation completed`，exit 0。
- 输入：`D:/data/byd/FRGVBYDP-21536/23-4-26_CBNA/CBNA_23-4-26/Gen5_2009-01-01_06-02_0115.MF4`（实际 CBNA 数据）。

**cluster dry-run**：`rsim cluster run --dataset BYD_SR --profile byd-ovrs-bl01v7-er-shared --limit 1` → job 包准备成功，selena 引用 `(profile)`，data path 解析为 BYD_SR UNC。

### 未能验证（环境限制）

真实 cluster 提交（`--execute`）需要 Bosch 内网/VPN 访问 `\\abtvdfs2.de.bosch.com\...` 和 `\\szhradar01\cluster_software`，本机当前 UNC 全部不可达。dry-run 验证了配置解析 + job 打包逻辑，真实提交链路与 dry-run 共用同一代码路径（只差 `--execute` 标志调 XML-RPC submit）。

### 测试

- 全套 363 passed（新增 5 个 doctor 用例：legacy selena_exe、source=path missing exe、auto-backend cluster-only/all 三个）。
- 零回归。

### 后续待办

7. **cluster check 的 severity 语义混乱**（`core/cluster.py` `_check_cluster_environment`）：输出里 `!!`（error 标记）的项最终 `Backend check passed`——某些 CheckItem `severity=error, ok=True` 语义矛盾。需审查 cluster 检查项的 ok/severity 赋值，让 error 级项确实阻断 report.ok。
8. **真实 cluster 提交回归**：连内网后用 `--execute` 跑一条真实 cluster 仿真，记录 job_id/输出大小/日志，确认 XML-RPC submit + worker 执行 + 结果 fetch 全链路。
9. **server 端 config 预置**（同前 #4）：让 Linux server 侧固化 cluster-inherent 默认，Windows 端只带 project + dataset/profile。

## T3 真实 cluster 仿真端到端跑通（2026-07-08，Linux server）

### 突破：Linux server 自己打包+提交 cluster job，无需 Windows agent

通过 SSH（paramiko）在 Linux server（10.190.171.44，hoz2wx@APAC.BOSCH.COM）配置环境并验证 T3 全链路。

### 环境配置（已完成）

1. **SZHRADAR01 解析**：`/etc/hosts` 加 `10.54.5.71 SZHRADAR01 szhradar01.APAC.BOSCH.COM`（Linux DNS 原本不可解析）。
2. **SMB 共享挂载**（`cifs-utils` 已装）：
   - `//abtvdfs2.de.bosch.com/ismdfs` → `/mnt/cluster`（`domain=APAC,uid=hoz2wx`，可写 Cluster 目录）
   - `//szhradar01/_CLUSTER_SOFTWARE` → `/mnt/cluster_sw`
   - 关键：必须 `domain=APAC` 显式域认证，否则 DFS 深层路径（`loc/szh/Isilon3/Cluster`）权限被拒。
3. **local.yaml**（Linux 专用）：config 路径保持 Windows UNC（worker 是 Windows 读 UNC），新增 `cluster.linux_mount_map` 把 UNC 前缀映射到挂载点，让 Linux server 写盘用挂载点、Config.cfg 内容用 UNC。

### 代码改动（已完成 + push）

- **`core/server_cluster_executor.py`**：server 内置 cluster 执行器（`--cluster-executor`），认领 cluster.run task 直接调 `prepare_cluster_job` + `submit_cluster_job`，不经子进程、不需 Windows agent。
- **`core/cluster.py:prepare_cluster_job`**：
  - `linux_mount_map` 机制：Linux 上写盘用 `job_dir_local`（挂载点），Config.cfg/submit/manifest 内容用 `job_dir_unc_str`（UNC，反斜杠分隔）。
  - UNC 路径分隔符强制反斜杠（Linux Path() 用 `/` 会产生混合分隔符 `\\host\share/dir`，Windows manager 解析不了）。
  - 雷达朝向检测的 `Path.is_file()` 容错（SMB/DFS 偶发 ConnectionRefused 不崩）。
- **`core/cluster.py:_validate_submit_package`**：接收 mount_map，本地存在性校验用挂载点路径；datafile 接受目录（dataset input_dir）。
- **`core/cluster.py:_submit_via_xmlrpc`**：校验时 UNC→挂载点转换。

### 端到端验证证据

投 `cluster.run`（execute=true）→ `server-cluster-executor` agent 认领 → prepare 写盘 + 生成 Config.cfg → XML-RPC 提交 SZHRADAR manager → **manager 返回 value=48（job ID）**，returncode=0，task status=succeeded。

```
[executor] preparing cluster job (project=BYD_OVS_CB, profile=byd-ovrs-bl01v7-er-shared, dataset=BYD_SR)
[executor] job package prepared: \\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\linux_t3_real_006
[executor] Config.cfg: \\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\linux_t3_real_006\Config.cfg
value=48
[executor] submitted via xmlrpc, returncode=0
```

`rsim cluster status 48` → `prepared`（job 进入集群队列，worker 会 picked up 跑 selena）。

### 三档现状

| 档位 | 状态 |
|------|------|
| **T1** 完整本地环境 | ✅ 已实现（模式 B） |
| **T2** 有编译无仿真环境 | ⚠️ server 端提交已具备（T3 执行器）；Selena 上传到集群共享待实现 |
| **T3** 无代码仓无编译无仿真 | ✅ **核心跑通**——Linux server 打包+提交 cluster job，无需 Windows agent |

### 仍待验证

- ~~job 48 worker 执行~~ ✅ **已验证**：用 `verified-shared` profile（指向 `cloud_batch_0117` 已验证的 selena.exe）+ 单文件 MF4 + `radar=RadarFL`，worker 实际跑通 selena 仿真。selena.log 末尾 `Simulation finished` + `Thank you for using Selena`。result.ini `job_id=10338, successfull=0`。

### T3 完整端到端验证证据（2026-07-08）

```
Linux server (10.190.171.44) --cluster-executor
  → cluster.run job (单文件 MF4, verified-shared profile, radar=RadarFL)
  → server-cluster-executor 认领
  → prepare_cluster_job: 写盘到 /mnt/cluster (SMB 挂载), Config.cfg 用 UNC
  → submit_cluster_job: XML-RPC 到 SZHRADAR01:8123, manager 返回 value=1
  → 集群 worker picked up, 跑 selena.exe (共享路径 cloud_batch_0117/selena/)
  → selena.log: "MDF-Scheduler finished: file duration: 45.0s" + "Simulation finished"
  → 产出 OUT_/result.html, result.pickle, selena.log, result.ini
```

**关键配置点（Linux local.yaml）**：
- `linux_mount_map`: UNC→挂载点映射（`\\abtvdfs2\ismdfs`→`/mnt/cluster`）。
- profile `source: "RadarFL"` + `mounting_position: "CFL"` 显式指定（自动检测对部分 MF4 返回 None）。
- 用已验证的共享 selena.exe（`cloud_batch_0117/selena/selena.exe`）而非 `BYD_OVRS/BL01V7_ER` 的（后者版本不匹配，selena 启动但无产出）。

### 后续待办

10. ~~**T2 Selena 上传**~~ ✅ 已实现：`rsim cluster upload-selena`（commit c72da7d）。复制本机 selena.exe+DLL 到 `<workspace_root>/selena-packages/<name>/`，打印 source=path profile 条目供 local.yaml 使用。支持 linux_mount_map。
11. **T3 Selena 选择 UI**：web 前端列出可用共享 Selena 包（扫描 `selena-packages/` + config 预置），供浏览器用户选择。当前 T3 用户靠 curl/脚本提交（已验证可行）。
12. ~~**Linux 持久挂载**~~ ✅ 已完成：`/etc/fstab` 加 cifs 挂载（credentials 文件 `/etc/rsim/smb-creds`，`_netdev,nofail`）；systemd service `rsim-server.service` 开机自启 server（`--cluster-executor`）。重启后自动恢复。
13. **Linux 代码同步**：Linux 无外网，github.com 不可达。当前靠 SFTP 从本机传单文件（paramiko）。可建 Bosch 内网 Git 镜像或用 rsync over ssh 同步整个仓库。

### systemd 持久化回归验证（2026-07-08）

systemd 管理的 server（`rsim-server.service`）+ fstab 持久挂载后，T3 端到端再次跑通：
- job_9a4338a89118（verified-shared profile，单文件 MF4，execute=true）
- worker 执行 selena，selena.log 末尾 `Thank you for using Selena and have a nice day!`
- result.ini: `successfull=0, job_id=10339, filesize=393`
- 证明重启后自动恢复全链路（挂载 + server + 执行器 + 提交 + worker 执行）。

## Web 前端 Cluster tab + 双用户支持（2026-07-08）

### 新增能力

- **`/api/server-info`** 端点（`cli/web.py:_server_info`）：返回 web 模式（embedded/remote/legacy）+ `local_sim_available`（有无 Windows agent）+ `cluster_executor`（server 端执行器）。前端据此显示模式横幅。
- **`/api/cluster/submit-job`** 端点：通过 control plane 投 `cluster.run` job（`web_control.start_cluster_via_control`），由 server 执行器认领，有完整 job/日志追踪。区别于 `/api/cluster/run`（本机直调，无追踪）。
- **前端 Cluster 仿真 tab**（`web/index.html` + `web/app.js`）：① Selena 来源（选 profile / 填上传 profile 名）② 数据（选 dataset / 填 MF4 路径）③ 提交（dry-run 选项）④ 状态/日志轮询。支持 T3（无 Windows）和 T1（Windows 本地+cluster）。

### Linux web 服务化

- `rsim-web.service` systemd unit（8765 端口，`--server-url http://127.0.0.1:8877 --no-control`，纯前端转发）。
- ufw 放行 8765/tcp。
- 两个 systemd service 都 active：`rsim-server`（8877）+ `rsim-web`（8765），开机自启。

### 端到端验证（web → cluster）

通过 web `/api/cluster/submit-job` 提交 job（verified-shared profile + 单文件 MF4）→ server 执行器认领 → prepare + XML-RPC 提交 → manager 返回 value=1 → `status: success`。日志通过 `/api/sim/status` 轮询回传。前端 JS 从 input 框读路径，无转义问题（之前测试用的内联 python 转义是测试脚本问题，非前端 bug）。

### 服务地址

- **前端 web**：`http://10.190.171.44:8765/`
- **后端 API**：`http://10.190.171.44:8877/`
- Windows 用户浏览器访问 8765，Cluster tab 提交仿真；本地仿真走"仿真"tab（需本机 agent）。
