# radar-sim 产品需求文档（PRD）

> 版本：v5.0
> 状态：发布基线
> 更新日期：2026-07-15
> 产品定位：Selena 编译与雷达数据仿真的统一调度平台

## 1. 文档定位

本文档描述 radar-sim v5 的目标产品合同和发布边界，不等同于当前仓库已完成能力。凡涉及现状、迁移顺序和代码落点，以 `docs/DETAILED_DESIGN.md` 和 `DEVELOPMENT_PLAN.md` 的“Current implementation / Target architecture”标注为准。

> **最高优先级产品基线：** 用户于 2026-07-15 最终确认的字段、四种组合、Linux/Windows/Cluster 职责和发布门禁已固化在 `docs/PRODUCT_CONTRACT.md`。本文残留的 Runtime Bundle、project、结果名、超时等旧用户入口描述均被该文件覆盖，必须在实现收敛中移除，不能反向要求用户填写。

- 详细技术方案见 `docs/DETAILED_DESIGN.md`。
- 可执行发布计划见 `DEVELOPMENT_PLAN.md`。
- CLI 是管理员、调试和兼容工具，不是第三种产品入口。
- “Mode A / Mode B”和“T1 / T2 / T3”不再作为面向用户的产品模式，只可用于描述历史实现或用户能力画像。

### 1.0 2026-07-15 用户合同收敛（覆盖下文旧 `SimulationSpec` 入口表述）

当前唯一面向新用户的业务配置是 `UserRunConfig 2.0`。旧 `SimulationSpec v1` 继续作为兼容 API/内部项目适配输入，但 Web 新建任务、YAML 导入导出和 SDK `submit_run()` 不再要求用户填写 `project`、profile、recipe 或 Cluster 拓扑。

`UserRunConfig 2.0` 只包含以下用户决策：

| 区域 | 用户填写 | 产品约束 |
|---|---|---|
| Selena | `source`、代码仓路径、分支、Selena 编译脚本、软件包编译脚本，或已有 Selena 文件夹 | Linux 永不编译；软件包脚本用于内部适配识别和依赖检查/修复；填写分支时使用隔离 worktree；留空分支时编译当前工作区修改；已有目录必须整体使用 exe 和依赖 DLL |
| Runtime | 两种来源都填写与 Selena 分支/产物匹配的 Runtime XML | Runtime XML 与 Selena 分支/产物强绑定；内部可生成 Bundle，但用户不接触 Bundle 概念 |
| 数据 | 一个数据路径，或 Web/SDK 上传后形成的 `dataset://` 引用 | 软件递归发现 MF4；Cluster 只消费已授权共享路径或 DatasetRef，不依赖用户电脑 |
| 仿真配置 | Adapter、MatFilter | MatFilter 必填；Adapter 由识别出的 recipe 决定是否必填（ovrs25 可留空）；两者均不进入 Runtime Bundle，可使用共享路径或 `config-asset://` 引用 |
| 执行 | `target`（auto/local/cluster） | Web 只展示和提交配置，底层调用与 SDK 相同的 `/api/v1` 调度合同；超时、结果名和保留策略不是首版用户配置 |

三种用户能力必须形成以下可理解行为：

| 用户环境 | 允许路径 |
|---|---|
| Windows 完全部署 | 本地编译；本地或 Cluster 仿真 |
| Windows 轻量部署 | 本地编译、完整 Selena 目录所需内容/必要数据上传；只由 Cluster 仿真，不支持本地仿真 |
| 完全不部署/无 Windows | 填写共享可达的已有 Selena 文件夹，或从 Web/SDK 选择并上传；由 Linux 服务直接触发 Cluster |

内部登记后的 Selena 资产可以多用户可见和复用，但用户也可以始终以文件夹路径定位；Adapter/MatFilter 中央上传引用首版按用户隔离。所有对外 Job、Stage、Manifest 只返回逻辑引用，不返回物理路径、提交命令、密码或平台凭据。

### 1.1 当前实现边界

当前仓库已经形成 v5 对外合同：`UserRunConfig 2.0`、`/api/v1`、Python SDK、统一 Job DAG、隔离 worktree、Runtime Bundle、数据/配置上传、Windows full 本地仿真、Linux Cluster Stage、Bearer 鉴权和 full/light 安装脚本均已实现。

发布限制只有三项：真实企业 Cluster 共享盘/manager 需在目标环境验收；真实 Selena build 被目标源码 `runtime.cpp(20) error C2382` 阻塞；当前版本只复用已登记 Runtime Bundle，不接受裸 `Selena.exe` 路径自动导入。Windows `full + linux` 使用同一中央入口选择 local/Cluster；`full + local` 是不带 Cluster executor 的离线本地模式。

## 2. 产品定义

### 2.1 一句话定义

radar-sim 通过统一的 Web 和 Python SDK/API，让用户使用一份可复用 YAML 完成 Selena 选择或编译、数据准备、本地或 Cluster 仿真、过程监控和结果归档。

### 2.2 用户问题

自动驾驶数据仿真同时受以下条件约束：

- 数据通常对应特定项目、Selena 分支或兼容产物；
- Selena 只能在满足工具链要求的 Windows 环境编译；
- 本地仿真依赖用户 Windows 电脑上的 Selena 和仿真环境；
- Cluster 仿真需要共享存储、Cluster 客户端或兼容网关；
- 不同用户拥有的源码、编译环境、仿真环境和共享存储权限不同；
- 现有流程把机器路径、工具链参数和 Cluster 细节暴露给用户，配置难以复用。

产品必须把这些基础设施差异收敛为平台能力，用户只描述要仿真的业务内容。

### 2.3 产品原则

1. **两个入口**：Web 和 Python SDK/API。
2. **一套业务合同**：Web 与 SDK 最终都提交同一种 `UserRunConfig 2.0`；`SimulationSpec` 只作兼容层。
3. **一个调度核心**：入口不实现调度，统一由后端解析、校验、拆解和路由任务。
4. **编译来源与仿真目标解耦**：Selena 从哪里来，不决定仿真在哪里执行。
5. **Cluster 不依赖用户电脑**：数据和 Selena 就绪后，Cluster 任务由 Linux 服务及平台自有 Gateway/Worker 完成。
6. **Linux 不编译 Selena**：所有 Selena 编译只在具备编译能力的 Windows 节点执行。
7. **轻量 Agent 首版不是仿真节点**：Windows 轻量 Agent 只执行授权工作区 Selena 本地编译、产物登记/校验/上传、必要数据检索/校验/上传，并把任务交还中央调度；它不支持本地仿真，也不得成为 Cluster 仿真运行期间依赖。
8. **本地仿真只属于 Windows 完整部署**：需要本地仿真的用户必须安装 Windows full deployment；无部署用户只能使用 existing Selena + shared/uploaded data 进行 Cluster 仿真。
9. **环境参数不进入用户配置**：工具路径、共享盘映射、Agent ID、队列、端口和密钥由平台管理。
10. **全过程可见且可追溯**：每一步有状态、进度、日志、责任节点、失败原因和恢复动作。
11. **不破坏用户工作区**：不得用强制 checkout、reset 或自动 stash 覆盖用户修改。
12. **复用优先且可回退**：具体实现优先改造现有代码，或采用成熟独立模块和标准协议；新增依赖必须先完成小型 spike、记录风险和回退方案，不能演变成全栈重写。

## 3. 用户与场景

用户不需要先选择“T1/T2/T3”。系统根据当前部署、已注册节点和环境检查结果自动得到可用能力。

| 用户情况 | 可用 Selena | 可用仿真目标 | 产品行为 |
|---|---|---|---|
| Windows full deployment，具备源码、编译和本地仿真环境 | 当前工作区、指定分支编译、已有产物 | 本地、Cluster | 可本地仿真；Cluster 仍需登记/上传产物和数据后由平台执行 |
| Linux central + Windows 轻量 Agent，具备授权源码和编译环境 | 当前工作区、指定分支编译、已有产物 | Cluster | Agent 只做编译、产物登记/校验/上传和数据准备；后续 Cluster 仿真由中央执行 |
| Windows full deployment，无编译能力但有本地仿真环境 | 已有产物 | 本地、Cluster | 禁止安排编译任务；本地仿真仅在 full deployment 内执行 |
| 无部署用户，或没有 Windows 源码/本地环境 | 已有产物 | Cluster | 只能使用 existing Selena + shared/uploaded data，无需安装客户端 |

### 3.1 核心场景 A：当前工作区修改后编译并仿真

用户正在 Selena 仓库的当前分支开发，可能存在未提交修改。用户在 Web 导入 YAML 或选择项目，确认当前工作区、数据路径和仿真目标后发起任务。

系统必须：

1. 检查工作区、分支、修改状态和编译环境；
2. 编译当前真实工作区，包含未提交修改；
3. 记录 branch、commit、dirty fingerprint、构建参数和产物校验值；
4. Windows full 可将 Selena 产物交给本地仿真；Cluster 目标必须登记并上传至 Cluster 可访问位置后交由中央执行；
5. 持续展示编译、数据准备、仿真和归档进度。

### 3.2 核心场景 B：指定分支自动编译

用户填写分支并选择自动编译。系统自动获取分支、准备隔离 worktree、触发编译、登记产物并继续仿真。

- 不得切换或清理用户当前工作区；
- 分支不存在、权限不足或环境不满足时，应在编译前失败；
- 如果没有可用 Windows 编译节点，应提供已有 Selena 建议，而不是把任务交给 Linux 编译。

### 3.3 核心场景 C：已有 Selena + Cluster 仿真

无 Windows 用户从 Linux Web 或 SDK 发起任务。系统根据项目和数据自动推荐兼容的已有 Selena，用户确认后直接执行 Cluster 仿真。

- 用户电脑不参与任务；
- 若旧 Cluster 接入依赖 Windows、Python 2 或共享盘，由平台自有 Cluster Gateway/Worker 处理；
- Linux 是唯一对外服务入口，不承担 Selena 编译。

### 3.4 核心场景 D：本地数据或共享数据

用户只填写或选择数据路径，不选择“本地/共享/上传”等技术类别。

- 平台可直接访问的共享路径：检索后直接使用；
- Windows Agent 可访问的本地路径：由 Agent 检索、打包、校验并按需上传；
- SDK 调用机器可访问的本地路径：由 SDK 协助上传；
- 中央 Web 无 Agent：仍只看到“数据路径”，也可用同字段旁的文件夹选择器确认本机目录；提交时自动上传并转换为可复用数据引用；
- 系统内部把原始路径解析为稳定的数据引用，用户无需填写共享盘目标路径。

## 4. 产品入口

### 4.1 Web

Web 是统一服务能力的可视化入口，不拥有独立业务或调度逻辑。Windows 完整部署和 Linux 中央服务使用同一套页面与 API。

一级导航：

1. **新建任务**：导入 YAML、填写业务参数、选择路径、查看自动推荐并提交；
2. **任务中心**：查看阶段进度、实时日志、失败原因、重试/取消和结果；
3. **资源**：查看 Selena 产物、数据解析结果和可用项目；
4. **环境**：查看当前节点能力、依赖检查、自动修复和 Agent 状态；
5. **配置**：导入、修改、校验和导出业务 YAML。

Web 必须满足：

- 所有任务操作调用版本化 REST API；
- 配置表单和 YAML 双向同步；
- 路径字段优先使用文件夹选择器确认；
- 中央 Web 不能读取本机路径时，明确引导 Agent 选择或浏览器上传；
- 任务过程用阶段时间线展示，不以原始终端日志代替状态；
- 高级日志可展开，但默认只展示用户可行动的信息。

### 4.2 Python SDK 与 REST API

第一版正式 SDK 使用 Python，并保留稳定、版本化的 REST API 供其他语言直接集成。

SDK 最小能力：

```python
from radar_sim_sdk import RadarSimClient, UserRunConfig

client = RadarSimClient(base_url="https://radar-sim.example")
config = UserRunConfig.from_yaml("simulation.yaml")

validation = client.validate_run(config)
job = client.submit_run(config)
for event in client.watch(job.id):
    print(event.stage, event.status, event.progress, event.message)
result = client.wait(job.id)
```

要求：

- SDK 不重新实现调度规则；
- SDK 与 Web 使用相同 REST schema、错误码和状态机；
- SDK 可在调用机器上解析用户确认的本地路径并执行断点续传；
- REST API 以 `/api/v1` 作为首个正式版本；
- 当前 `/api/*` 端点在迁移期作为兼容接口，不作为新集成合同。

## 5. 部署形态

部署形态是运行位置选择，不是另一套产品。

### 5.1 Windows 完整部署

面向经常进行本地编译和仿真的 Windows 用户。一键安装以下组件：

- 本机 Web；
- 本机 REST API 和统一调度器；
- 本地执行节点；
- 环境检查与修复组件；
- 可选的 Cluster 接入组件。

能力：

- 断开 Linux 服务时仍可完成本地编译和本地仿真；
- 配置 Cluster 后可提交 Cluster 任务；
- SDK 只需把 `base_url` 指向本机服务；
- 可选择连接中央 Linux 服务以共享产物和统一查看任务。

### 5.2 Linux 中央服务

Linux 提供中央 Web、REST API、调度器、元数据、日志和结果索引。

- 无 Windows 用户：无需安装任何组件，只能使用已有 Selena + shared/uploaded data 进行 Cluster 仿真；
- 有 Windows 编译或本地数据能力的用户：一键安装轻量 Agent，提供授权工作区 Selena 本地编译、产物登记/校验/上传、路径选择和数据检索/校验/上传能力；
- Linux 不执行 Selena 编译；
- Cluster 仿真由 Linux 可直接执行的适配器或平台自有 Gateway/Worker 执行，不依赖用户 Agent 保持在线。

### 5.3 Windows 轻量 Agent

轻量 Agent 必须支持：

- 一键安装、一次注册、开机启动和原位升级；
- 自动发现本机能力并上报健康状态；
- 提供安全的本机文件夹选择；
- 执行被授权的工作区 Selena 本地编译；
- 登记、校验并上传或同步编译产物到 Cluster 可访问位置；
- 执行必要的数据路径检索、校验和上传；
- 将编译/数据准备结果交还中央调度；
- 只允许访问用户确认或管理员绑定的目录；
- 网络中断后可恢复日志和任务状态；
- 卸载后不删除用户源码、数据和仿真结果。

轻量 Agent 首版明确不支持本地仿真，不声明 `simulation.local` capability，也不得执行或维持 Cluster 仿真运行期。需要本地仿真的用户必须使用 Windows full deployment。

## 6. 统一业务配置：SimulationSpec v1

### 6.1 设计目标

`SimulationSpec` 是 Web 导入/导出、Python SDK 和 REST API 共用的唯一用户配置模型。

- YAML 可跨任务、入口和部署复用；
- 只包含业务意图；
- 缺省项由项目目录和平台策略补全；
- 环境相关的解析结果只进入内部 `ResolvedSimulationSpec`，不写回用户 YAML；
- schema 必须有版本，升级时提供迁移器。

### 6.2 建议结构

用户创建任务时的**最小配置只有两个字段**：

```yaml
project: bydod25
data:
  path: "D:/measurement/CBNA_0117"
```

导入时系统补齐 schema version、Selena 自动策略、执行目标、默认 profile 和结果保留期；导出时生成下面的完整规范形式，便于复用和审计。

```yaml
schema_version: "1.0"

project: bydod25

selena:
  mode: auto               # auto | current_workspace | branch | existing
  branch: ""               # mode=branch 时必填；auto 时可作为推荐约束
  artifact: ""             # mode=existing 时可填；为空则由系统推荐
  publish_path: ""         # 自动编译产物的项目内复用路径；可空，由系统生成
  auto_build: true
  build_mode: Release

data:
  path: "D:/measurement/CBNA_0117"
  limit: 0
  required_signals: []

simulation:
  target: auto             # auto | local | cluster
  profile: default
  timeout_minutes: 0

result:
  name: ""
  retain_days: 30
```

### 6.3 用户可见字段约束

允许进入 YAML 的字段：

- 项目；
- Selena 使用意图、分支、已有产物和业务相关构建模式；
- 数据路径及业务相关筛选条件；
- 本地/Cluster/自动目标；
- 仿真 profile 和业务级运行参数；
- 结果命名和保留策略。

禁止进入 YAML 的字段：

- Windows Agent ID、hostname、服务端口；
- Visual Studio、TCC、Python、Git 可执行文件路径；
- 用户代码仓绝对路径；
- Cluster manager 地址、Python 2 路径、共享盘映射和凭证；
- 暂存目录、上传分片、内部队列、重试和锁参数；
- 服务端数据库和日志目录。

这些参数分别存放在项目目录、部署策略、节点绑定、环境快照和密钥存储中。

### 6.4 Selena 自动推荐

当 `selena.mode=auto` 时，系统根据以下信息生成有序候选：

1. 项目和数据元信息；
2. 用户填写的分支约束；
3. 产物的 branch、commit、接口清单和构建模式；
4. 数据要求的信号或接口兼容性；
5. 目标仿真环境可访问性；
6. 产物健康状态、时间和校验值。

系统默认选取最高可信候选并要求用户确认。具备 Windows 编译能力时，可同时建议“从当前工作区/分支重新编译”；无 Windows 编译能力时不得显示不可执行的自动编译选项。

## 7. 统一任务模型

### 7.1 任务阶段

一次仿真请求被调度器解析为可观测阶段：

1. `resolve_spec`：解析项目、Selena、数据和目标；
2. `environment_check`：检查所需节点与依赖；
3. `prepare_source`：绑定当前工作区或创建分支 worktree；
4. `build_selena`：按需编译；
5. `register_artifact`：登记、校验并按需上传 Selena；
6. `prepare_data`：检索、校验、去重并按需上传数据；
7. `preflight`：执行产物、数据、接口和目标环境强校验；
8. `run_simulation`：本地运行或提交 Cluster；
9. `collect_results`：监控、获取和索引结果；
10. `finalize_manifest`：生成可追溯清单。

不需要的阶段应标记为 `skipped`，不能从 UI 消失。

### 7.2 状态

任务状态：

- `validating`
- `needs_input`
- `queued`
- `running`
- `cancelling`
- `succeeded`
- `failed`
- `cancelled`

阶段状态：

- `pending`
- `ready`
- `running`
- `blocked`
- `succeeded`
- `failed`
- `skipped`
- `cancelled`

每次状态变化必须生成事件，至少包含时间、阶段、执行节点、进度、用户消息、技术详情、错误码和建议动作。

任务中心 P0 必须展示：任务状态、整体进度、当前阶段、阶段列表、失败摘要和当前可执行动作。运行中至少提供取消；失败时只允许从可重试阶段重试；`needs_input` 必须直接展示补充配置或改用已有资源的动作。对于已经提交到外部 Cluster 的任务，取消不能只修改平台数据库状态，必须记录外部 job ID、取消/补偿结果及“外部任务可能仍在运行”的明确状态。

### 7.3 能力路由

调度器按任务要求匹配执行能力，不按“用户模式”写死流程：

- `source.workspace.read`
- `source.git.worktree`
- `build.selena`
- `data.local.read`
- `data.upload`
- `artifact.register`
- `artifact.validate`
- `artifact.upload`
- `simulation.local`
- `simulation.cluster`
- `cluster.gateway`
- `result.collect`

Cluster 任务的数据和 Selena 就绪后，必须可从平台执行节点独立完成。用户 Agent 离线不能导致已提交的 Cluster 运行中断。

能力归属规则：

| capability | Windows full | Windows 轻量 Agent 首版 | Linux central / platform Gateway |
|---|---|---|---|
| `build.selena` | 支持 | 仅限用户授权工作区/绑定 workspace | 不支持 |
| `data.local.read` / `data.upload` | 支持 | 仅限用户授权目录并用于 Cluster staging | shared/browser/SDK upload 或平台 staging |
| `artifact.register` / `artifact.validate` / `artifact.upload` | 支持；绑定同一构建节点和授权输出目录 | 支持；仅限同一 light Agent 构建节点和授权输出目录 | 只消费已形成的平台 `SelenaArtifact`，不替用户节点补登记 |
| `simulation.local` | 支持 | 不支持 | 不支持 |
| `simulation.cluster` / `cluster.gateway` | 可通过适配器或 Gateway 提交 | 不支持运行期；只交还中央调度 | 支持并承担 Cluster 运行期 |

light build-to-cluster 的 Stage -> capability -> node-kind 矩阵：

| Stage | capability / 责任 | Cluster build-to-cluster node kind | local target node kind |
|---|---|---|---|
| `resolve_spec` | central scheduler 解析 `SimulationSpec`、策略和候选 | central scheduler | Windows full 内置 scheduler |
| `environment_check` | 按阶段目标拆分检查；light Agent 只检查其 build/data staging 所需本机环境，Cluster 环境检查在 central/Gateway | central scheduler + `windows_agent` light build/data checks + `linux_executor`/`platform_gateway` Cluster checks | `windows_full` |
| `prepare_source` | `source.workspace.read` / `source.git.worktree` | `windows_full` 或 `windows_agent` light | `windows_full` |
| `build_selena` | `build.selena` | `windows_full` 或 `windows_agent` light | `windows_full` |
| `register_artifact` | `artifact.register` + `artifact.validate` + `artifact.upload`，必须在同一构建节点和授权目录内完成；平台形成 `SelenaArtifact` 后解除对 Agent 在线依赖 | 构建节点为 `windows_full` 或 `windows_agent` light；完成后由 central catalog 持有 `SelenaArtifact` | `windows_full` |
| `prepare_data` | `data.local.read` / `data.upload`；本地路径由 Windows 节点处理，shared/browser/SDK 路径由 central 处理 | 本地路径：`windows_full` 或 `windows_agent` light；shared/browser/SDK：central scheduler/upload service | `windows_full` |
| `preflight` | 产物、数据和 Cluster 目标强校验 | `linux_executor` 或 `platform_gateway`；light Agent 不领取 | `windows_full` |
| `run_simulation` | `simulation.cluster` / `cluster.gateway` 或 `simulation.local` | `linux_executor` 或 `platform_gateway`；light Agent 不领取 | `windows_full` only |
| `collect_results` | `result.collect` | `linux_executor` 或 `platform_gateway`；light Agent 不领取 | `windows_full` |
| `finalize_manifest` | central manifest finalization | central scheduler / `linux_executor` / `platform_gateway`；light Agent 不领取 | `windows_full` |

入口状态必须区分“用户缺配置”和“等待机器完成检查”。中央已有 logical workspace binding、但 Linux 无法读取 Windows fingerprint 时，Job 进入 `pending_node` 并等待匹配 Agent；只有没有 binding、或多个 binding 需要用户选择时才进入 `needs_input`。Agent 只向中央广告 binding ID/project/health，不上报绝对路径。build 成功后的绝对产物路径保存在 Agent-local lease 中，`register_artifact` 以 `build_stage:attempt + lease_id` 回到同一 Agent执行可续传上传；上传完成形成共享 `storage_ref` 后，Cluster 后续不再依赖用户电脑在线。

## 8. 环境检查与处理

### 8.1 检查原则

环境检查按用户选择的任务动态生成，不要求用户一次配置所有工具。

| 任务能力 | 主要检查 |
|---|---|
| Selena 编译 | Windows、代码仓、Git、分支、VS/TCC/工具集、磁盘、写权限 |
| 本地仿真 | Selena runtime、仿真依赖、profile、数据访问、输出空间 |
| 数据上传 | 路径权限、文件检索、网络、暂存空间、校验与断点续传 |
| Cluster 仿真 | 已有 Selena、数据可达、共享存储/Gateway、Cluster 在线状态 |

### 8.2 处理等级

- **自动处理**：目录创建、项目绑定验证、可逆配置生成、TCC 已支持的自动修复、断点恢复；
- **确认后处理**：需要管理员权限的软件安装、较大上传、代码 fetch、工具集安装；
- **仅指导**：账号权限、许可证、无法自动获取的专有工具和网络策略；
- **禁止处理**：强制清理用户工作区、覆盖源码、静默修改用户分支历史。

检查结果统一为 `ready / degraded / unavailable`，并提供机器可读错误码和用户可执行的修复建议。

## 9. 过程可视化

Web 任务详情必须同时提供：

- 顶部总体状态、开始时间、持续时间和当前阶段；
- 完整阶段时间线，包括跳过的阶段；
- 编译和仿真进度、当前文件/用例、成功/失败计数；
- 当前执行节点的友好名称和能力，不展示内部地址或密钥；
- 可流式查看并下载的日志；
- 失败摘要、技术详情、修复动作和可重试范围；
- 输入 YAML、Resolved Plan 摘要、Selena 指纹、数据引用和结果 Manifest；
- 取消、从失败阶段重试、复制任务和导出 YAML。

## 10. 产物与数据管理

### 10.1 SelenaArtifact

每个 Selena 产物至少记录：

- project、branch、commit；
- clean/dirty 状态和 dirty fingerprint；
- 构建模式、工具链摘要；
- 二进制校验值和接口/信号 manifest；
- 创建人、创建节点、创建时间；
- **逻辑 `storage_ref`（如 `shared://selena/<project>/<user-path>/selena.exe`），永不暴露物理服务器路径**；
- 健康状态和保留策略。

#### 产物发布合同（path-first）

1. **用户选择相对发布路径**：用户在上传/登记时选择一个项目作用域内的相对可复用路径（如 `bydod25/rel/selena.exe`）。系统不接受绝对服务器路径、UNC 路径或驱动器路径作为发布目标。
2. **中央托管命名空间**：管理员配置一个文件系统根目录（默认在 `RSIM_HOME` 下仅供开发/测试）。所有产物通过归一化的逻辑引用访问，例如 `shared://selena/<project>/<user-path>/selena.exe`。
3. **同路径 checksum 冲突规则**：
   - 若同一逻辑路径已存在产物，且新产物 checksum 与旧产物 **相同**，视为幂等完成，可复用已有记录；
   - 若 checksum **不同**，必须拒绝并返回稳定冲突错误，要求用户选择不同的版本化路径；
   - 不允许静默覆盖不同 checksum 的内容。
4. **可见性规则**：
   - **clean build**（无未提交修改、构建期间源码未变化）的产物默认 **shared**，可被多用户复用；
   - **dirty build** 或 **source-changed build** 的产物强制 **private**，仅对创建者可见，除非产品文档明确决定 otherwise；
   - 该例外必须在文档中清晰说明。
5. **Agent 上传边界**：light Agent 只能登记、校验、上传自己刚在授权 workspace/output 目录中产生的产物。上传完成后，平台 catalog 形成 `SelenaArtifact`，后续 Cluster 阶段只消费 `storage_ref`，不再依赖该 Agent 在线。

当前工作区构建必须包含未提交修改。目标分支构建必须使用隔离 worktree，不得 checkout、stash、reset 或 force 用户主工作区。

`artifact.register`、`artifact.validate`、`artifact.upload` 是正式 Stage capability。它们必须绑定同一构建节点、该节点的授权 workspace/output 目录和本次构建 attempt。

### 10.2 数据处理

平台内部负责：

- 路径规范化与可达性探测；
- 文件或数据集检索；
- 大文件分片、断点续传、校验和去重；
- 上传到 Cluster 可访问的暂存区域；
- 从用户原始路径映射为内部稳定引用；
- 生命周期清理和任务引用保护。

YAML 保持业务路径。内部位置、上传会话和共享映射只存在于 Resolved Plan 与任务 Manifest。

## 11. 权限与安全

- Linux 中央服务必须有真实身份认证；现有仅信任用户 header 的方式只能用于开发环境；
- 用户只能查看自己的任务、日志、上传和产物，管理员授权的共享资源除外；
- Agent 注册使用一次性 token，后续使用可吊销的节点凭据；
- 本机文件选择由用户主动确认，Agent 只允许访问绑定工作区和已授权路径；
- 凭据、Cluster 密码和共享存储密钥不得写入 YAML、日志或 Manifest；
- 上传内容执行路径、类型、大小和恶意文件校验；
- 安装包和升级包必须签名。

## 12. 非功能需求

### 12.1 可用性

- 新用户完成一次 Agent 安装和项目绑定后，后续任务不再重复填写环境路径；
- 共享数据 + 已有 Selena 的 Cluster 任务可在无客户端条件下提交；
- 网络短暂中断后，Agent、上传和日志可恢复；
- 所有失败均提供稳定错误码和可执行建议。

### 12.2 性能

- 配置校验在不扫描大数据内容时应在 3 秒内返回；
- 任务提交在 2 秒内返回 job ID；
- 状态事件从执行端到 Web 的可见延迟不超过 3 秒；
- 大文件上传支持分片、断点续传和内容去重；
- Cluster 任务不得因等待用户电脑轮询而降低调度效率。

### 12.3 可靠性与追溯

- Job、Stage 和事件持久化；
- Agent 重启后不会重复执行已完成的非幂等阶段；
- 每次运行生成不可变 Manifest；
- 任务可以从失败阶段安全重试；
- 同一工作区的冲突构建必须串行或隔离。

### 12.4 兼容性

- Windows 完整部署与轻量 Agent 支持项目实际使用的 Windows 版本；
- Linux 中央服务不得引入 Selena 编译依赖；
- 首版 SDK 支持 Python 3.10+；
- 用户 YAML 使用正斜杠或反斜杠均可，路径归一化由解析器处理。

## 13. 发布范围与优先级

### 13.1 P0：首版发布必须完成

P0 是可发布的最小一致产品，不要求重写所有历史模块，但必须消除会破坏产品边界的旧路径。

1. `SimulationSpec v1` schema、校验器、YAML 导入/导出和 legacy config adapter；
2. `/api/v1` 最小 REST API、事件订阅/轮询和 Python SDK；
3. Web 新建任务、任务中心、环境页和配置导入导出只调用统一 API，不再直接实现编译/仿真调度；
4. Job/Stage/Event 状态模型和全过程展示，跳过阶段也必须可见；
5. 编译来源与仿真目标的统一解析和 capability 路由；
6. 当前 dirty 工作区构建必须进入构建，记录 branch、commit、dirty fingerprint 和前后 fingerprint；
7. 指定分支自动编译必须使用隔离 worktree，移除 Web/API 自动流程中的 checkout、stash、reset、force；
8. Windows 完整部署可离线完成本机 Web/API/scheduler/worker、当前工作区或分支编译、本地仿真；
9. Linux 中央服务可在无 Windows 用户场景下使用已有 Selena + 共享/上传数据完成 Cluster 仿真；
10. 旧 Cluster 接入若不能在 Linux 直接运行，必须路由到平台自有 Windows Gateway/Worker，不能退回依赖用户 Agent；
11. 轻量 Windows Agent 一键安装/注册/启动，提供授权工作区 Selena 本地编译、产物登记/校验/上传、文件夹确认、数据检索/校验/上传能力，并在准备完成后交还中央调度；
12. 数据解析只要求用户提供路径；共享路径、Agent 本地路径、SDK 本地路径和浏览器文件夹上传都解析为内部 `DatasetRef`；
13. 环境检查、自动处理、Preflight、结构化进度和最终 Manifest 接入真实执行链；
14. 兼容现有 profile/config、CLI 和 legacy `/api/*`，但新能力只能进入 `SimulationSpec`、`/api/v1`、调度器和 Stage adapter。

### 13.2 P1：首版稳定后

- Agent 静默自动更新和灰度发布；
- 内容寻址的数据与 Selena 全局去重；
- 产物审批、共享和保留策略；
- 更完整的 SSO、组织和角色管理；
- Webhook、通知和外部流水线集成；
- 更细粒度的阶段并行与资源配额。

### 13.3 明确不做

- 在 Linux 编译 Selena；
- 为无 Windows 用户提供平台托管 Selena 编译农场；
- 让浏览器绕过用户确认读取任意本地路径；
- 为 Web、SDK、CLI 各维护一套调度规则；
- 把 Agent、共享盘和工具链参数暴露为日常用户配置；
- 让 Windows 轻量 Agent 在首版执行本地仿真，或成为 Cluster 仿真运行期间依赖；
- 首版同时发布多语言 SDK；
- 为迁就历史文档继续扩展 Mode A/B 或 T1/T2/T3 分支流程。

## 14. 首版验收标准

### 14.1 产品合同

- 同一 YAML 可被 Web 导入、导出，并被 Python SDK提交；
- Web 和 SDK 提交后得到相同的 Resolved Plan、状态和结果；
- YAML 不包含机器、工具链、Cluster 和调度内部参数。

### 14.2 Windows 完整部署

- 一次安装后可离线打开本机 Web；
- 可选择当前 dirty 工作区完成 Selena 编译和本地仿真；
- 可指定其他分支在隔离 worktree 编译；
- 不修改用户当前分支和未提交文件；
- 环境问题在执行前展示并提供处理动作。

### 14.3 Linux 中央服务

- 无 Windows 客户端用户可以选择共享数据或上传数据；
- 可以使用推荐的已有 Selena 完成 Cluster 仿真；
- 任务提交和运行不依赖用户电脑在线；
- Linux 进程不会触发 Selena 编译。

### 14.4 轻量 Agent

- 用户通过一次操作完成安装、注册和启动；
- 中央 Web 可请求用户确认本机文件夹；
- Agent 能上报授权工作区编译、产物登记/校验/上传、数据读取/校验/上传能力；
- Agent 不上报 `simulation.local`、`simulation.cluster`、`cluster.gateway` 或 Cluster run/collect/finalize 阶段能力，本地仿真和 Cluster 运行期请求不会路由到轻量 Agent；
- 编译产物和数据上传完成后，Cluster 仿真可在 Agent 离线情况下继续；
- 本地数据 E2E 必须证明：授权本地路径 -> Agent 检索/校验/上传 -> 生成 `DatasetRef` -> 中央 Cluster 使用该 `DatasetRef` -> Agent 离线仍能完成；
- Agent 断线重连后其负责的准备阶段状态和日志不丢失。

### 14.5 可观测性

- 用户能看到从解析配置到 Manifest 的完整阶段；
- 编译和仿真有实时进度，不只有“运行中”；
- 失败包含稳定错误码、失败阶段和建议动作；
- 每次成功运行可以导出 YAML、日志、Selena 指纹和 Manifest。

## 15. 产品决策记录

| 决策 | 结论 |
|---|---|
| 用户入口 | Web + Python SDK/API |
| 调度位置 | 当前部署中的统一后端调度器 |
| 部署 | Windows 完整部署；Linux 中央服务 + 可选轻量 Agent |
| Linux 编译 Selena | 不支持 |
| 无 Windows 用户 | 已有 Selena + Cluster |
| Cluster 依赖旧 Windows 工具 | 使用平台自有 Gateway/Worker |
| 轻量 Agent 首版边界 | 只做授权编译、产物/数据校验上传和交还中央调度；不做本地仿真，不承担 Cluster 运行期 |
| 数据来源选择 | 用户只提供路径，系统自动解析和准备 |
| 配置合同 | SimulationSpec YAML，Web/SDK 共用 |
| 分支自动编译 | 隔离 worktree，不修改用户工作区 |
| 未提交修改 | 支持编译并记录 dirty fingerprint |
| SDK 首发语言 | Python，其他语言使用 REST |
