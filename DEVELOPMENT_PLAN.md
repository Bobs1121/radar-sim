# radar-sim V2 实施与发布计划

> 状态：当前唯一开发计划（V2 单轨）
> 更新：2026-08-13
> 适用：Web、Python SDK/REST API、Linux 控制面、统一 Windows Connector、Cluster 调度
> 权威合同：[`docs/PRODUCT_CONTRACT.md`](docs/PRODUCT_CONTRACT.md)
> 架构基线：[`docs/V2_ARCHITECTURE.md`](docs/V2_ARCHITECTURE.md)

## 1. 目标和边界

radar-sim 是 Selena 编译与仿真工具的轻量外围脚手架，不实现 Selena 内部算法，也不替用户安装 Visual Studio 或本地仿真环境。V2 只保留一条用户主链：

```text
UserRunConfig 2.0
  -> Web 或 Python SDK/REST API
  -> Linux 控制面
  -> 统一 Windows Connector（需要 Windows 文件、编译或本地仿真时）
  -> Cluster 执行面或 Windows 本地执行面
  -> 每条输入的 Manifest、结果目录和 ZIP
```

以下内容不是用户概念，也不得重新出现在 YAML、Web 表单、SDK 业务参数或安装选项中：业务项目名、固定产品白名单、profile/recipe、运行档位、内部产物目录、Runtime Bundle 引用、Agent ID、Cluster 内部拓扑和共享盘类型。数据库中可以暂时保留旧字段，但只能承载匿名 `execution_identity` 或迁移兼容数据，不能参与业务路由。

## 2. 当前代码基线

已进入 V2 主链的能力（以代码和测试为准）：

- `core.user_config.UserRunConfig`：唯一 2.0 YAML/JSON 合同，Web 与 SDK 共用；未知业务字段拒绝。
- `core/api_v1.py`、`core/api_v1_fastapi.py`、`radar_sim_sdk/`：统一校验、提交、任务状态、事件、传输、Manifest、诊断和结果下载入口。
- `core/stage_binder.py` 与 Stage 执行器：固定阶段按需 `skipped`，每次任务使用 owner/设备绑定恢复物理路径。
- `WorkspaceRecognizer`、已有 Selena 导入和通用脚本执行：不读取项目配置；编译只执行用户选择的 Selena 脚本，并从脚本/受控输出根确认产物及 DLL。
- 统一 Windows Connector：一次安装、持久连接、重连、单实例、无周期黑色终端窗口；按任务执行本地路径访问、编译、本地仿真和 Cluster 直传。
- Cluster `shared_zero_copy`/`shared_copy`：共享路径零复制或源设备直写 Cluster 数据面，Linux 不接收大型文件正文。
- 结果 Manifest、ZIP、SDK 下载和接收端结果目录；批量输入逐条记录成功/失败，不因单条内部仿真失败取消其它输入。
- owner、Job、Stage、Connector、Transfer 和 Result 的隔离与幂等；SDK 显式用户标识统一为稳定 `user-<name>` 命名空间。

旧模块可以继续作为内部实现或迁移材料存在，但不能被新 Web/SDK 流程直接暴露。任何与本节冲突的旧说明均视为过时，不得据此新增代码。

## 3. 待完成的发布门禁

### P0-A：合同和文档收口

- 所有面向用户的说明只引用 `UserRunConfig 2.0`、统一 Connector、local/cluster 和源到源传输。
- 每次代码变更同时更新 `HANDOFF.md` 或当次日期的 handoff，记录代码证据、测试证据、外部环境限制和未验证项。
- 旧用户入口、旧 YAML、旧业务项目适配器不再作为兼容承诺；如代码尚存，只能在内部迁移/维护边界使用。

### P0-B：自动化回归

必须持续通过以下合同测试：

- YAML 导入/导出、路径正反斜杠和 Web/SDK canonical hash/DAG 一致；
- `existing`/`build` × `local`/`cluster` 四组合的阶段路由；Linux 不领取编译或本地仿真；
- 用户显式 MatFilter/source 优先，空值从受控路径/元数据稳定推导；多源选择有记录；
- 本地目标无上传副作用；Cluster 目标无 Linux 大文件正文，直传具备校验、续传、取消和幂等；
- 统一 Connector 首装、重启自启、断线重连、单实例和版本契约；
- 两个 owner 并发时任务、路径绑定、日志、Manifest 和结果互不可见；
- 批量输入的部分成功、真实 Manifest 和结果 ZIP/本地物化；错误按控制面、Connector、路由、编译、仿真内部、结果六类表达。

### P0-C：部署与真实验收

在部署参数外置的 Linux 服务（当前验收地址 `10.190.171.44`）完成：

1. 服务启动、重启恢复、健康检查、任务中心刷新和 SDK 连接；
2. 已有 Selena + Cluster：共享路径零复制与 Windows 本地资源直传两条路径；
3. 已有 Selena + 本地仿真：统一 Connector 原地执行并物化结果；
4. 本地编译 + Cluster/本地仿真：至少一条真实编译脚本和 dirty workspace；
5. 同一 YAML 分别经 Web 与 SDK 提交，获得一致阶段、Manifest 和结果；
6. 新 Windows 用户从一次安装到任务完成的黑盒流程，以及两用户并发隔离。

真实验收必须把 Job ID、Stage 状态、外部 Cluster Job ID、Manifest 摘要、结果路径/ZIP 校验值写入 handoff。自动化测试不能替代真实验收。

## 4. 明确不在本轮发布的能力

下列能力在实现前必须单独更新合同和验收，不得在 README、Web 或 SDK 中暗示已经完成：

- 远端资源到本地 Windows 的通用 `source_to_local`；当前不可达时返回稳定的 `source_to_local_unavailable`，不经 Linux 绕传。
- Cluster 结果自动反向直传并解压到任意用户设备；当前保留 owner 隔离的 ZIP/引用与 SDK 下载。
- MCP Server 或 Skill 的独立调度器；未来只允许薄封装现有 SDK/API，不复制 DAG 或传输逻辑。
- 关机、未登录或网络隔离状态下远程唤醒 Windows Connector。
- Linux 编译 Selena、执行 Windows 本地仿真或承载用户大文件正文。
- 安装 Visual Studio、修复 Selena 内部 runnable/算法错误。

## 5. 设计和运维红线

1. Web、SDK、REST API 只调用同一调度核心；不得恢复第二套业务流程。
2. 用户的分支默认已切好，编译当前工作区及未提交修改；默认不 checkout、reset、clean 或 stash。分支字段只用于差异提醒。
3. Linux 只签发控制信息和传输计划；文件所在设备直接写执行目标，Cluster 输入优先共享零复制。
4. 同一资源的传输或阶段失败只影响该资源/阶段；不得重复编译成功产物或重复提交外部 Cluster Job。
5. 不确定时宁可返回可操作的 `needs-input`/`unavailable`，不得根据历史项目名、路径盘符或默认值伪造成功。
6. 每个用户的 owner、设备绑定和物理目标根由服务端隔离；无认证部署仅适用于受信内网，不能被描述为强认证多租户。
7. Connector 保持轻薄：不运行第二个 Scheduler/Web/数据库，不保存用户业务项目规则，不周期弹出终端窗口。

## 6. 开发启动检查表

下一次开发开始前按以下顺序检查，避免上下文漂移：

1. 阅读本文件、`docs/PRODUCT_CONTRACT.md`、`docs/V2_ARCHITECTURE.md`、`docs/DETAILED_DESIGN.md` 和最新 handoff；
2. `git status` 与当前部署 commit 对照，确认没有覆盖用户未提交修改；
3. 先运行与修改范围对应的 V2 合同测试，再修改代码；
4. 变更后补测试和 handoff 证据；
5. 完成 Linux immutable deploy、健康检查和真实任务验收后，才把对应门禁标记为完成；
6. 未完成或外部依赖阻断必须保留为明确的 `未验收/阻断`，不能用旧文档中的“已完成”替代。

## 7. 文档权威顺序

1. `docs/PRODUCT_CONTRACT.md`：用户侧不可偏移合同；
2. `PRD.md`：产品需求与发布范围；
3. `docs/V2_ARCHITECTURE.md`：拓扑、删除清单、验收矩阵；
4. `docs/DETAILED_DESIGN.md`：代码级设计；
5. 本文件：当前实施顺序与发布门禁；
6. `HANDOFF.md` 和 `docs/handoffs/*.md`：带日期的实时证据与未决事项。
