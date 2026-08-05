# radar-sim 控制面与数据面分离实施计划

> 状态：已确认产品方向，待按阶段实施
> 日期：2026-08-05
> 权威上位合同：`docs/PRODUCT_CONTRACT.md`

## 1. 目标

Linux 服务是自动化脚手架，不是文件服务器。Web、SDK 和未来 MCP/Skill 只通过 Linux 完成配置提交、任务编排、进度查询、取消/重试和结果引用查询。MF4、Selena.exe/DLL、Runtime、MatFilter、Adapter 等任务文件在需要迁移时，由文件所在设备直接进入 Cluster 可访问的数据面；不得经过 Linux Web/API 端口或先落入 Linux 本地暂存目录。

本计划优先复用现有成熟能力：Cluster UNC 共享工作区、`prepare_cluster_job()`、Cluster Gateway、Windows one-click Connector、SDK 路径发现与内容指纹。首版不引入对象存储、Kafka、MLflow 或另一套调度系统。

## 2. 三类流量

| 类型 | 内容 | 允许经过 Linux API |
|---|---|---|
| 控制面 | YAML/JSON、Job/Stage/Event、心跳、传输计划、文件 Manifest、校验值、外部 Cluster Job ID、结果摘要 | 是 |
| 数据面 | MF4、完整 Selena 目录、DLL、Runtime、MatFilter、Adapter、大型结果归档 | 否；必须直达/直出 Cluster 可访问存储 |
| 小型诊断 | 截断日志、错误码、环境摘要 | 是；必须限大小，不能借日志接口传文件 |

## 3. 用户行为与服务响应矩阵

| Selena | 目标 | 数据/文件位置 | 所需客户端能力 | 服务的预期响应 |
|---|---|---|---|---|
| build | local | Windows 本地或该电脑可读共享盘 | Windows full | 编译并本机仿真；本机可达输入的传输 Stage 为 `skipped` |
| build | local | 数据只在远端 | Windows full + 远端读取/直传能力 | Windows 编译；数据原地读取或源端直传 Windows，Linux 不中转 |
| existing | local | Selena、数据均为 Windows 本机可达 | Windows full | 原地校验目录后本机仿真；不搬运输入 |
| existing | local | 本地数据 + 远端 Selena | Windows full + 远端读取/直传能力 | 数据不动；Selena 原地读取或源端直传 Windows 受控缓存 |
| existing | local | 远端数据 + 本地 Selena | Windows full + 远端读取/直传能力 | Selena 不动；数据原地读取或源端直传 Windows 受控缓存 |
| build | cluster | Windows 本地 | Windows light/full | 等待同 owner 的持久 Connector；编译成功后同时/随后直传 Selena、数据和配置；完成后 Agent 可离线，Linux 提交 Cluster |
| existing | cluster | Windows 本地 | Windows light/full 或本机 SDK direct-transfer | 不检查 VS；直传成功后登记引用并提交 Cluster |
| existing | cluster | Cluster 可读共享路径 | 无用户 Agent | 零复制登记，Linux 直接提交 Cluster |
| existing | cluster | Windows/Linux SDK 调用机本地 | SDK direct-transfer adapter | SDK 获取传输计划后直传；Linux 只收进度和完成 Manifest |
| existing | cluster | 纯浏览器所在电脑本地 | 本机 Connector | 未连接时进入等待并显示一次连接入口；不能把浏览器文件上传回退到 Linux 中转 |

`target=auto` 必须先选择执行目标，再决定是否需要传输。无论目标本地还是 Cluster，都遵循“执行端可读则原地使用、否则源端直传执行端”；Cluster 目标优先原地共享引用，其次复用已登记引用，最后才签发客户端直传计划。本地目标不得上传本机已有输入，但允许远端输入直接进入 Windows full。

### 3.1 通用资源图，而不是项目特例

内部把一次任务表示为资源图：`source workspace`、`Selena runtime directory`、`runtime_xml`、`mat_filter`、`adapter`、一个或多个 `MF4` 是资源节点，Windows full 或 Cluster 是执行节点。Resolver 为每项资源选择一个能够读它的源节点，再计算到执行节点的零复制引用或直传边。项目识别只帮助推导编译命令、环境依赖和产物目录，不参与数据传输与仿真参数的固定分支。

这样可以自然覆盖：远端数据+本地仿真、本地数据+远端 Selena+本地仿真、不同电脑分别持有 Selena 与数据、共享数据+本地编译+Cluster 等组合，而无需增加用户配置字段。

## 4. 最小内部合同

### 4.1 TransferPlan

由 Linux 控制面签发，属于内部对象，不进入用户 YAML：

```json
{
  "transfer_id": "transfer:sha256:<opaque>",
  "owner_scope": "<opaque>",
  "job_id": "job_xxx",
  "stage_id": "task_xxx",
  "mode": "shared_copy|source_to_local|gateway_upload",
  "source_role": "dataset|runtime_bundle|runtime_xml|mat_filter|adapter",
  "target_root": "<deployment managed cluster UNC or gateway target>",
  "relative_root": "<opaque isolated prefix>",
  "resume": true,
  "expires_at": "<timestamp>"
}
```

`target_root` 只下发给被 owner/Stage 绑定的 Connector 或 SDK，不出现在公共 Job、Web 日志、MCP 响应和 YAML 导出中。客户端不能自报目标路径；服务端只接受部署白名单中的共享根或上传网关。

### 4.2 TransferManifest

客户端完成直传后仅向 Linux 回传元数据：文件相对路径、大小、SHA-256、目标逻辑引用、开始/完成时间和传输结果。Linux 使用 Cluster 挂载点做有界存在性/Manifest 校验，不读取完整 MF4，也不重新计算多 GB 文件哈希。

### 4.3 逻辑引用

- `dataset://...` 必须解析到 Cluster 可访问位置；不得只指向 Linux 私有磁盘。
- Runtime Bundle/Asset 引用同理，物理文件必须在 Cluster 数据面。
- 引用按 owner 隔离；团队复用必须通过显式可见性策略，不能猜测其他用户路径。

## 5. 客户端适配器

P0 提供 `shared_copy`：Windows Connector 或 SDK 将文件直接复制到现有 Cluster UNC 工作区，保留目录结构并支持 `.partial`、续传和完成后的原子重命名。复用用户现有域账号/共享盘访问能力，不在 Linux 保存用户 SMB 凭据。本地仿真需要远端输入时使用同一轻量传输内核的 `source_to_local` 模式，目标是 Windows full 的 owner/Job 隔离缓存，不经过 Linux。

Linux SDK 调用机满足以下任一条件即可直传：

1. 已挂载 Cluster 共享目录；
2. 部署方提供非 Linux 中转的 Cluster 上传网关；
3. 运行跨平台本机 Connector。

若均不具备，服务必须在传输开始前返回 `cluster_direct_transfer_unavailable` 和明确动作，不能静默降级为 Linux HTTP 上传。P1 可增加 `gateway_upload` 适配器，但必须保持同一 TransferPlan/Manifest 合同。

## 6. Stage 与状态语义

保留现有 DAG 名称以减少改造面，但重新定义 `prepare_data`/`register_artifact` 的数据面行为：

- `waiting_for_local_connector`：本地文件存在，但对应 owner 的客户端尚未连接；Job 保持可恢复等待，不失败、不要求重提。
- `waiting_for_cluster_access`：客户端在线但不能访问签发的 Cluster 数据面；展示网络/权限检查动作。
- `transferring_direct_to_cluster`：客户端正在直传；Linux Web/API 仍需保持低延迟响应。
- `transfer_completed`：Linux 已接收 Manifest 并登记逻辑引用。
- `transfer_skipped_shared`：输入原本已被 Cluster 访问。
- `transfer_skipped_local_execution`：本地仿真不需要传输。
- `cluster_direct_transfer_unavailable`：提交前明确阻断；不能改走 Linux 中转。

Web 和 SDK 展示相同状态、进度、重试/取消动作。传输失败只重试对应 Transfer Stage；不得重新编译已经成功且指纹未变化的 Selena，也不得重复提交 Cluster Job。

## 7. 多用户、鲁棒性和安全

- 每个目标目录由 owner scope、Job 和 Transfer 随机标识共同隔离；禁止使用用户名或用户原始路径拼接物理目标。
- Connector 只能领取同 owner、匹配本机路径绑定的任务；SDK token 只能更新自己签发的 TransferPlan。
- 目标必须位于部署白名单根目录内，拒绝 `..`、设备路径、符号链接/重解析点逃逸和非白名单 UNC。
- 传输状态持久化；电脑重启、网络抖动和 Agent 重连后从已完成分片/文件继续。
- 内容指纹相同且可见性允许时复用，不重复复制；源文件在传输期间改变则该次失败并要求重试。
- Linux 在直传期间只处理小型控制请求；健康检查、Agent 轮询和 Web/SDK 查询不得被文件 I/O 阻塞。
- 取消任务时停止客户端传输并清理 `.partial`；已完成共享对象按部署保留期回收。

## 7.1 轻量 Agent 边界

Windows light/full 的公共 Connector 必须保持轻薄：

- 不保存项目目录、Cluster manager 或业务 profile；任务所需的编译脚本、路径和内部 TransferPlan 均由控制面按次下发；
- 不运行第二套 Scheduler、Catalog、Web 或数据库服务；只保留连接身份、受控路径绑定、极小的续传状态和最近诊断日志；
- 编译能力只是调用用户给定脚本、发现输出并验证 Selena.exe/DLL，不复制项目规则；
- 传输能力只是从授权源流式复制到授权目标、计算/校验摘要和上报进度，不解析完整 MF4；
- full 仅在 light 基础上增加本地仿真执行器和本地结果收集；不能因此改变 Web/SDK 合同；
- 一次安装、登录自启、断线重连、自动升级和任务续跑属于基础能力，用户不理解 Agent ID、服务 URL 或内部模式。

## 8. 分阶段实施

### P0-A：冻结合同与禁止新中转

- 文档和错误码统一为控制面/数据面分离。
- 新建 Cluster 任务禁止创建 Linux dataset/artifact HTTP upload session。
- 本地仿真合同测试证明零上传。

### P0-B：Windows Connector 直传

- Linux 签发 `shared_copy` TransferPlan。
- light/full Connector 将 MF4、Selena 目录和必要配置直接复制到 Cluster UNC staging。
- 完成后回传 Manifest；Linux 登记 Dataset/Bundle/Asset 引用。
- 支持续传、取消、进度、同一任务幂等和 Agent 重连。

### P0-C：SDK 直传与 Web 一次连接

- Windows/Linux SDK 实现相同 TransferPlan 客户端；可访问共享目录时无需 Agent。
- Web 检测本地路径后只提供一次连接入口，不提供经 Linux 上传大文件的回退。
- 本机已持久连接时自动领取，用户不重复配置。

### P0-D：部署与端到端门禁

- 在 `10.190.171.44` 部署一次。
- 用至少一个大 MF4 验证客户端到 Cluster 共享目录的字节计数，Linux API 入站字节不随 MF4 大小增长。
- 传输期间并发验证新用户 Web、SDK、Agent 心跳和任务列表。
- 验证 build+cluster、existing+cluster、build+local、existing+local 四条路径。

### P1：非 SMB 上传网关、MCP 与 Skill

- 在不改用户 YAML的前提下增加 `gateway_upload`。
- MCP/Skill 只调用控制面 API、解释状态和执行允许的重试；不得把本地文件内容编码进 MCP 消息或模型上下文。
- 为 AI 暴露稳定错误码、动作、Stage 证据和诊断包，保持仿真内核与外围调度问题分层。

## 9. 发布验收

只有以下证据齐全才可标记完成：

- 网络/服务指标证明 1 GB 级 MF4 未进入 Linux API 请求体和 Linux 私有数据目录；
- Cluster 共享目标出现正确文件、大小和 Manifest，真实 Cluster 仿真成功；
- 直传中断后续传成功，未重复编译、未重复提交 Cluster；
- 本地仿真没有 TransferPlan、上传会话或 Cluster staging 副作用；
- 两个 owner 并发传输互不可见且不串任务；
- Web 与 SDK 对相同 YAML 给出相同路由和状态；
- Linux 服务在直传期间健康检查、任务查询和 Agent polling 保持稳定。
