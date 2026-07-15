# radar-sim v5 可执行发布计划

> 状态：当前计划
> 日期：2026-07-10
> 产品口径：`PRD.md` v5.0
> 架构依据：`docs/DETAILED_DESIGN.md`

> 2026-07-15 纠偏状态：以 `docs/PRODUCT_CONTRACT.md` 为权威基线。旧版把 Runtime Bundle 暴露给用户并要求先登记的路径不算交付；当前必须收敛 `existing_path + runtime_xml`、Web/SDK 透明上传/解析、build/existing × local/cluster 四组合和 10.190.171.44 真实验收。Linux 仍只调度与传输，不编译 Selena、不执行本地仿真。

## 0. 文档定位

本文是下一阶段编码和验收的唯一 backlog。旧 v3/v4/v5 phase 记录不再作为当前计划执行。

历史记录保留用途：

| 文件/章节 | 当前用途 |
|---|---|
| 本文末尾“历史阶段索引” | 说明旧计划已冻结，不再排入 backlog |
| `HANDOFF.md` | v5 唯一实时状态、conformance log 和偏离防护记录 |
| `CHECKPOINT.md`、`docs/handoff.md` | 历史交付与测试记录 |
| `docs/REFACTORING_PLAN.md`、`docs/WIZARD_IMPLEMENTATION_PLAN.md`、`docs/selena-source-tiers-design.md` | 历史方案输入，可作为证据，不作为产品合同 |

当前实现与目标架构的边界以 `PRD.md` 和 `docs/DETAILED_DESIGN.md` 为准；实现计划只接受能直接落到代码、测试和验收的工作包。

每个 WP 完成时必须更新 `HANDOFF.md` active section 的 WP 状态和 Architecture Conformance Gate。没有代码证据、测试证据和 HANDOFF conformance entry，不得把 WP 标记为完成。

## 1. 现状基线

真实代码中可复用的能力：

- `core/control_service.py`、`core/control_http.py`：legacy Job/Task/Agent/Log SQLite 控制面，支持 `assigned_agent_id` claim pinning、独立且不会在 stale reclaim 时丢失的 `required_agent_id` 节点亲和、task claim、日志、取消和 reclaim。
- `core/web_control.py`、`core/remote_control.py`：Web/远端控制兼容桥。
- `cli/server.py`、`cli/agent.py`：Linux/Windows 可运行的 legacy control server 与 polling agent。
- `core/server_cluster_executor.py`、`core/cluster.py`：server-side `cluster.run` executor 与 Cluster prepare/submit/wait/fetch 能力。
- `core/data.py`：MF4 发现、路径分类、访问检查和按需复制原型。
- `core/environment.py`、`cli/doctor.py`、`core/tcc.py`：环境检查和部分自动修复。
- `core/repo.py`：已有 `prepare_repo_worktree()`，但主构建路径仍有 `prepare_repo_context()` 原地 checkout 风险。
- `core/preflight.py`、`core/progress_parser.py`、`core/manifest.py`：Preflight、结构化进度、Manifest 原型。
- `web/*`、`cli/web.py`：现有 Web 仍走 legacy `/api/*` 和直接业务 handler，可迁移为 v1 API client。

尚未具备的 v5 发布能力：

- `SimulationSpec v1` schema、迁移器和 YAML round-trip。
- `/api/v1` application layer 和可发布 Python SDK。
- 统一 Job/Stage/Event DAG 与 Web/SDK 共用状态机。
- Web 全面停止直接配置写入、编译、仿真和 Cluster 调度。
- 分支自动编译唯一使用隔离 worktree。
- Dirty workspace fingerprint 接入当前工作区构建。
- Selena catalog、推荐、artifact 登记和共享可见性。
- Agent/SDK/browser 数据上传链路。
- Windows full / light Agent 一键安装与 companion 文件夹确认。

## 2. 发布原则

1. Web 与 SDK 是唯一用户入口；CLI 只保留管理员、调试和兼容用途。
2. 用户业务合同只有 `SimulationSpec` YAML，只包含业务参数。
3. Web、SDK 和兼容 CLI 都提交到统一后端调度器，不各自调度。
4. Selena 来源与仿真目标解耦，但受部署能力约束：`local` 仿真只属于 Windows full；轻量 Agent 首版只支持授权编译/上传后转 `cluster`。
5. Linux 永不编译 Selena；无 Windows 用户只能使用已有 Selena + Cluster。
6. Cluster 不依赖用户电脑；旧 Windows/Python2/共享盘链路由平台 Gateway/Worker 承担。
7. 分支自动编译必须使用 `git worktree`，不得 checkout/stash/reset/force 用户工作区。
8. 当前 dirty 工作区可以构建，但必须记录 branch、commit、dirty fingerprint 和构建前后 fingerprint。
9. 用户只填写数据路径；共享、本地、SDK 上传、浏览器上传均解析为内部 `DatasetRef`。
10. 环境检查、自动处理、阶段进度、日志、失败动作和 Manifest 是 P0。
11. 复用优先：先基于现有模块改造；确需引入成熟模块/标准协议时，必须先完成 `docs/DETAILED_DESIGN.md` 的 spike/回退决策，不能把依赖引入变成全栈重写。

## 3. 依赖顺序总览

```text
WP0 安全冻结
  -> WP1 SimulationSpec
  -> WP2 /api/v1 + SDK skeleton
  -> WP3 Job/Stage/Event DAG
  -> WP4 Selena source + artifact catalog
  -> WP5 Data resolver + upload
  -> WP6 Windows execution adapters
  -> WP7 Linux central + platform gateway routing
  -> WP8 Preflight/progress/manifest integration
  -> WP9 Web v5 migration
  -> WP10 Packaging/docs/release gates
```

不得跳过依赖包直接做 UI 或安装器；否则会继续把 current 与 target 混在一起。首个编码任务仍是 WP0 安全冻结，之后才进入 `SimulationSpec` 和 `/api/v1`。

## 4. 工作包

### WP0：安全冻结与旧路径隔离

**目标**：先阻断会破坏 v5 红线的旧自动路径，避免下一阶段继续放大风险。

| 项 | 内容 |
|---|---|
| 代码落点 | `core/repo.py`、`core/build_runner.py`、`cli/build.py`、`cli/web.py`、相关 tests |
| 主要任务 | 先完成复用决策：继续使用 Git CLI 和现有 `core/repo.py`，不引入新的 Git 抽象；随后标记并隔离 `prepare_repo_context()` 的自动分支编译用途；Web/API 自动分支编译只能走 worktree；保留 CLI 手工兼容路径时必须显式警告和测试保护；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | repo dirty/staged/untracked fingerprint 准备测试；禁止 `checkout -f`、`reset --hard`、自动 stash 的安全测试；现有 build/repo 测试 |
| 退出标准 | Web/API 分支构建没有任何原地 checkout；当前工作区构建仍可直接使用真实 dirty workspace；测试能证明主工作区 branch 和文件未改变 |
| 停止项 | 无法证明不修改用户工作区；需要用户手工 stash/checkout 才能继续 |

### WP1：`SimulationSpec v1` 与 legacy config adapter

**目标**：建立 Web/SDK/API 共用的唯一业务合同。

| 项 | 内容 |
|---|---|
| 依赖 | WP0 |
| 代码落点 | 新增 `core/spec/`；扩展 `core/config.py` 的只读 adapter；新增 `tests/test_spec_*.py` |
| 主要任务 | 第一项：Pydantic v2 spike（JSON Schema、字段错误、YAML/JSON/SDK round-trip），并通过第三方组件门禁（版本 pin、许可证、漏洞/维护状态、内部镜像或 vendoring、Windows/Linux 离线打包、代理/证书兼容）；不通过则回退 dataclass + 手写校验；随后实现 schema version、YAML load/dump、业务字段校验、legacy `config/projects/*`、profiles、local.yaml 到 `ProjectCatalog/UserBindings` 的映射；禁止环境路径写入用户 YAML；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | YAML round-trip；非法字段拒绝；legacy config 映射；路径 slash/backslash 归一；schema version mismatch |
| 退出标准 | 同一 `SimulationSpec` 可由文件、Web JSON 和 SDK model 得到相同 canonical JSON 与 hash |
| 停止项 | 需要把 VS/TCC/Agent/Cluster path 写进用户 YAML 才能提交 |

### WP2：`/api/v1` application layer 与 SDK skeleton

**目标**：提供稳定入口，Web 与 SDK 不再拼 legacy task payload。

| 项 | 内容 |
|---|---|
| 依赖 | WP1 |
| 代码落点 | 新增 `core/api_v1.py` 或等价 application service；`core/control_http.py`/`cli/web.py` v1 routes；新增 `radar_sim_sdk/`；API tests |
| 主要任务 | 第一项：FastAPI/Uvicorn thin-route spike 和 HTTPX SDK/SSE spike，并通过第三方组件门禁（版本 pin、许可证、漏洞/维护状态、内部镜像或 vendoring、Windows/Linux 离线打包、代理/证书兼容）；stdlib server 保留为 legacy adapter，调度器不得重写进 FastAPI route；不通过则回退 stdlib route + urllib/polling；随后实现 `validate`、`submit`、`get`、`events`、`cancel`、`manifest` 最小 API；统一错误 `code/message/detail/actions/request_id`；SDK `RadarSimClient` 和 `SimulationSpec.from_yaml()`；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | Web-style JSON 与 SDK 请求响应一致；错误码稳定；idempotency key；legacy `/api/*` adapter 不影响 v1 |
| 退出标准 | 不启动 Web 也可用 SDK 提交/查询一个 dry-run Job；同 spec 的 Web/SDK validate 结果一致 |
| 停止项 | SDK 需要复制调度规则；Web 仍必须调用 `/api/build/*` 或 `/api/cluster/*` 才能提交新任务 |

### WP3：Job/Stage/Event DAG

**目标**：把 legacy job/task 队列升级为 v5 可观测执行模型。

| 项 | 内容 |
|---|---|
| 依赖 | WP2 |
| 代码落点 | `core/control_service.py`、DB migration、新 `core/stages.py`/`core/events.py`、`core/web_control.py` |
| 主要任务 | 第一项：复用现有 `control_service/control_http` + SQLite 的 schema extension 方案，明确不引入 Celery/Temporal/SQLAlchemy/Alembic，除非 spike 证明现有模型无法满足；随后实现 Job、Stage、Attempt、Event、ResolvedSpec 持久化；stage dependency；skipped stage；sequence event；cancel/retry 状态机；legacy job view；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | DAG 生成；stage order；skipped 可见；event sequence；cancel/retry；agent claim pinning 回归 |
| 退出标准 | 一个请求能生成标准阶段：resolve、environment、source、build、artifact、data、preflight、run、collect、manifest；Web/SDK 读取同一状态 |
| 停止项 | 继续依赖日志文本推断任务最终状态；Stage 失败不能定位到错误码和 action |

### WP4：Selena source resolver、dirty fingerprint 与 catalog

**目标**：把 Selena 来源与仿真目标解耦，并可追溯登记。

| 项 | 内容 |
|---|---|
| 依赖 | WP3 |
| 代码落点 | `core/repo.py`、`core/build_runner.py`、新 `core/artifacts.py`、`core/manifest.py`、config adapter |
| 主要任务 | 第一项：复用 `core/repo.py` worktree、Git CLI、`core/manifest.py` 原型的集成 spike，不引入新的 artifact 服务产品；随后实现 `current_workspace` 前后 fingerprint；`branch` detached worktree；`existing` artifact lookup；artifact checksum、branch、commit、dirty、interface/signal manifest；推荐候选；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | current dirty 构建包含未提交修改；branch worktree 清理；并发 branch build 隔离；existing artifact 可访问性；dirty artifact 默认不共享 |
| 退出标准 | 六种组合中的 Selena 解析结果都进入 `ResolvedSimulationSpec`，且不修改主工作区 |
| 停止项 | 分支自动编译需要 checkout 主工作区；dirty 产物无法区分 clean 产物 |

### WP5：数据路径 resolver 与上传链路

**目标**：用户只提供路径，系统生成稳定 `DatasetRef`。

| 项 | 内容 |
|---|---|
| 依赖 | WP3 |
| 代码落点 | `core/data.py`、新 upload/session service、`cli/agent.py` stage adapter、SDK uploads、Web upload client |
| 主要任务 | 第一项：tus 协议 spike（tusd + Uppy/@uppy/tus + tus-py-client），并通过第三方组件门禁（版本 pin、许可证、漏洞/维护状态、内部镜像或 vendoring、Windows/Linux 离线打包、代理/证书兼容）；不通过则回退非断点 multipart 或 Agent/SDK 本地复制；随后实现共享命名空间识别；Agent 本地 path token；SDK 本地上传；浏览器文件夹上传；分片/断点/校验；DatasetRef 生命周期；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | UNC/shared direct；Windows local via Agent；SDK local upload；browser upload mock；large file resume；path traversal 防护 |
| 退出标准 | Cluster Stage 只消费 `DatasetRef/storage_ref`，不再靠原始 `D:\...` 或 `\\...` 字符串跨节点推断 |
| 停止项 | 中央 Web 试图直接读取浏览器本机绝对路径；上传失败后没有可恢复 session |

### WP6：Windows full 与 light Agent 执行适配

**目标**：Windows full 可离线完整运行并支持本地仿真；light Agent 只连接中央服务执行授权工作区编译、产物登记/校验/上传、必要数据检索/校验/上传，并把任务交还中央调度。

| 项 | 内容 |
|---|---|
| 依赖 | WP3、WP4、WP5 |
| 代码落点 | `cli/agent.py`、worker adapter、Windows service/installer scripts、companion loopback API、`core/environment.py` |
| 主要任务 | 第一项：PyInstaller one-folder + 已批准 WinSW stable + WiX Toolset spike，并通过第三方组件门禁（版本 pin、许可证、漏洞/维护状态、内部镜像或 vendoring、Windows/Linux 离线打包、代理/证书兼容）；不默认采用 WinSW pre-release；不通过则回退 ZIP + PowerShell/manual service；自动更新保留 P1；随后实现 Stage 协议；full 模式的本地编译、本地仿真、数据检索、上传；light Agent 的授权编译、`artifact.register`/`artifact.validate`/`artifact.upload`、数据检索/校验/上传和中央回传；文件夹选择确认；环境 snapshot；服务化启动/重连；full 模式内置 Web/API/scheduler/worker；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | agent register/heartbeat/claim；service restart resume；folder authorization；light Agent local build/upload dry/smoke；light Agent 拒绝/不上报 `simulation.local`、`simulation.cluster`、`cluster.gateway` 与 Cluster run/collect/finalize stages；服务端按 `node.kind` 过滤自报能力，legacy wildcard/`cluster.run` 不能绕过 v1 policy；light Agent 数据 E2E：授权本地路径 -> Agent 检索/校验/上传 -> `DatasetRef` -> 中央 Cluster 使用 -> Agent 离线仍完成；Agent offline 后 Cluster 继续；Windows full local sim dry/smoke；offline full smoke |
| 退出标准 | Windows full 断开中央服务仍能本机 Web 提交 build+local sim；light Agent 仅执行授权目录和绑定 workspace 内的 build/data/artifact staging，artifact capability 绑定同一构建节点和授权输出目录，完成后 Cluster 任务可由中央继续且不依赖 Agent 在线 |
| 停止项 | Agent 能静默读取任意路径；light Agent 执行本地仿真、领取 `simulation.local`/`simulation.cluster`/`cluster.gateway` 或 Cluster run/collect/finalize stage；Cluster 运行中依赖用户 Agent 持续在线；`windows_full` 与 `platform_gateway` 复用同一 node kind/policy |

### WP7：Linux central、Cluster executor 与平台 Gateway

**目标**：无 Windows 用户可零客户端完成已有 Selena + Cluster。

| 项 | 内容 |
|---|---|
| 依赖 | WP3、WP4、WP5 |
| 代码落点 | `core/server_cluster_executor.py`、`core/cluster.py`、Gateway adapter、deployment policy |
| 主要任务 | 第一项：复用 `core/server_cluster_executor.py` 和 `core/cluster.py` 的 Linux executor/Gateway routing spike，借鉴 GitLab Runner/Jenkins 节点能力握手但不引入整套产品；随后实现 Linux executor 能力探测；Cluster 共享路径映射；旧 Python2/Windows-only client 路由到平台 Gateway；external cluster job id 持久化；结果 fetch；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | Linux no-agent existing Selena + shared data dry/smoke；Linux no build capability；Gateway routing mock；Agent offline 后 Cluster 继续；UNC/linux mount map；`windows_agent` 不能上报/领取 Cluster runtime capability；`windows_full` 与 `platform_gateway` node kind/policy 隔离 |
| 退出标准 | Linux 进程不会触发 Selena 编译；无 Windows 用户能提交已有 Selena + Cluster；旧接入不可 Linux 运行时有平台 Gateway 路径；Cluster preflight/run/collect/finalize 不由 light Agent 执行 |
| 停止项 | P0 依赖无 Windows 用户的本机环境；把 Linux 配成 `build.selena`；legacy wildcard/`cluster.run` 绕过 v1 policy；`platform_gateway` 冒充 `windows_full` 或反向代领 |

### WP8：环境检查、Preflight、进度与 Manifest 接入

**目标**：P0 全过程可视化和可追溯。

| 项 | 内容 |
|---|---|
| 依赖 | WP3、WP4、WP5、WP6/WP7 对应执行路径 |
| 代码落点 | `core/environment.py`、`core/preflight.py`、`core/progress_parser.py`、`core/manifest.py`、Stage adapters |
| 主要任务 | 第一项：复用 `environment/tcc/preflight/progress_parser/manifest` 原型的 Stage-event integration spike，借鉴 MLflow metadata/artifact separation 但不部署 MLflow；随后实现动态环境检查；自动/确认/指导处理等级；Preflight 强校验；build/sim progress event；最终 Manifest 成功/失败/取消均生成；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | 缺工具/缺权限/缺数据 action；preflight pass/fail/degraded；build `[n/N]` 和 sim `Frame X/Y`；manifest immutability |
| 退出标准 | Web/SDK 能看到完整阶段、进度、失败原因、建议动作、日志和 Manifest |
| 停止项 | 任务只有“running”无阶段；失败只返回原始堆栈或日志片段 |

### WP9：Web v5 迁移

**目标**：Web 成为 v5 可视化入口，不再拥有调度逻辑。

| 项 | 内容 |
|---|---|
| 依赖 | WP2、WP3、WP8 |
| 代码落点 | `web/index.html`、`web/app.js`、`web/styles.css`、`cli/web.py` static host/v1 proxy |
| 主要任务 | 第一项：复用 vanilla `web/*` 的 v1 API client spike；不通过才重新评估 React，P0 不默认重写；随后实现新建任务页；YAML/form 双向同步；validate preview；任务中心；环境页；artifact/dataset/resource view；legacy config/wizard 下沉为迁移工具；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | DOM/API contract tests；Web 与 SDK 同 spec 同结果；no direct `/api/build/*` for new flow；browser upload UI；error actions |
| 退出标准 | 用户可从 Web 导入/修改/导出 `SimulationSpec`，提交后看到同一 Job DAG 和 Manifest |
| 停止项 | Web 新流程仍直接调用 subprocess、Git、Cluster 或写项目 config |

### WP10：SDK、安装包、文档与发布门禁

**目标**：交付可安装、可验证、可运维的 P0。

| 项 | 内容 |
|---|---|
| 依赖 | WP0-WP9 |
| 代码落点 | `radar_sim_sdk/`、packaging scripts、README、deployment docs、runbooks |
| 主要任务 | 第一项：汇总 WP1/WP2/WP5/WP6 spike 结果和回退方案，确认未发生全栈重写；随后完成 Python SDK packaging；Windows full installer；light Agent installer；Linux central deploy guide；Gateway runbook；compat CLI docs；known issues；完成后更新 `HANDOFF.md` conformance entry |
| 测试 | SDK install/import; Windows full smoke; Linux central no-agent smoke; Linux+Agent build-to-cluster smoke; upload resume; event reconnect; full pytest |
| 退出标准 | 发布包有明确安装入口、回滚方式、版本兼容提示和端到端证据 |
| 停止项 | 文档声称已完成但无 smoke 证据；安装需要用户手工编辑内部环境路径才可开始 |

## 5. 发布验收矩阵

| 场景 | P0 必须证明 |
|---|---|
| Web + SDK 同合同 | 同一 YAML 经 Web 和 SDK 得到相同 Resolved Plan、Job、events、Manifest |
| Windows full | 本机 Web/API/scheduler/worker 离线完成 dirty workspace build + local sim |
| Branch auto build | detached worktree 编译，不改变用户当前 branch、tracked/untracked 文件 |
| Existing Selena + Cluster | Linux 中央服务无 Agent 完成 Cluster dry/smoke，不编译 Selena |
| Linux + light Agent | Agent 执行授权本地编译、产物登记/校验/上传和数据上传；不执行本地仿真或 Cluster 运行期，Cluster 提交后用户电脑可离线 |
| light Agent data E2E | 授权本地路径 -> Agent 检索/校验/上传 -> `DatasetRef` -> 中央 Cluster 使用 -> Agent 离线仍完成 |
| Platform Gateway | Linux 不兼容旧 Cluster client 时，任务路由到平台 Gateway/Worker |
| Data upload | shared direct、Agent local、SDK local、browser folder upload 都生成 DatasetRef |
| Environment/preflight | 缺依赖前置失败并给 action；Preflight mismatch 硬拦截 |
| Progress/manifest | 阶段、日志、进度、失败 action、最终 Manifest 可从 Web/SDK 查询 |

## 6. 全局停止项

出现以下情况应停止发布推进，先修正架构或实现：

- 任何 P0 路径要求 Linux 编译 Selena。
- 无 Windows 用户的 Cluster 路径依赖其本机环境或在线 Agent。
- Web、SDK、CLI 各自维护不同调度规则。
- 用户 YAML 包含 Agent ID、工具链路径、Cluster 密钥、共享盘映射、内部队列或上传分片。
- 自动分支编译会 checkout/stash/reset/force 用户主工作区。
- 当前 dirty workspace 构建没有 fingerprint 和产物追溯。
- Cluster 任务提交后仍依赖用户电脑轮询才能完成。
- 环境检查、进度或 Manifest 只存在原型模块，未接入真实 Stage。

## 7. 历史阶段索引（冻结）

### v3：分析与平台原型

MF4 reader、日志解析、早期 builder、TUI 等能力已被后续模块复用；scanner/regression/preset/orchestrator 等旧 pipeline 思路不再作为 v5 backlog。

### v4：单机 CLI 与多项目配置

多项目 config、build/analyze/ask/diff/history/init/open-vs、analysis plugins 等保留为兼容和底层能力。v5 不再把 CLI 作为用户产品入口，也不把 full config 当作用户业务合同。

### v4 扩展：本地/Cluster 双通路

`rsim run`、`rsim cluster`、profiles、data adaptivity、environment check 是可复用底层能力。它们必须迁移到 `SimulationSpec -> /api/v1 -> Scheduler -> Stage adapter`，不得继续形成独立产品流程。

### v5 原型：Linux control plane + Windows agent

现有 control server、polling agent、remote web、server-side cluster executor 是 v5 的重要基础，但当前仍是 legacy `/api/*` 和 task_type 模型。下一步不是继续扩展旧 task payload，而是按 WP1-WP9 收敛到 v1 API、DAG 和能力路由。
