# Web、SDK 与 AI 集成契约

> **V2 单轨收敛声明（2026-08-18）**：本文件是 Web/SDK/AI 集成合同，继续有效。V2 单轨收敛索引见 `docs/V2_ARCHITECTURE.md`，该文件在其之上收敛 Web/SDK 同能力、唯一 `user-run-config/2.0` 与明确删除清单（用户可见 project/profile/recipe/light/full/legacy 概念全部移除）。仓库现在提供可分发的 radar-sim MCP Server 和 Skill 包；它们只能薄封装 SDK，不得复制调度、传输或结果判断。Cluster 结果归档、Manifest、Web/SDK/MCP 下载已经由统一结果接口提供。

## 结论

Web、Python SDK、未来 Skill/MCP 只使用同一套 `/api/v1`，不得复制任务编排、路径转换、Cluster 提交或结果判断逻辑。Skill/MCP 是 Python SDK 的薄封装，不是第二个仿真后端。

首版公共调用闭环：

1. `import_yaml()` / `export_yaml()` 读写完整或不完整的 YAML 草稿；返回 `complete=false` 时只表示草稿尚未达到提交条件，不会创建 Job。
2. `RadarSimClient.submit_yaml(source, idempotency_key=...)` 提交与 Web 相同的完整用户 YAML；`source` 可以是 YAML 文本或本地 YAML 文件路径，MCP 不需要为了提交而创建临时文件。
3. `user_run_config_schema()` / `cluster_readiness()` / `capabilities()` 查询 Web 使用的公开配置合同、Cluster 提交门禁和 owner-scoped Windows/Cluster 能力。
4. `prepare_direct_transfers()` / `resume_direct_transfers()` 让 SDK 调用机把可读的 Selena 完整目录、Runtime、MatFilter、Adapter 和 MF4 按同一 `TransferPlan` 直接送到 Cluster 数据面；`get_job_transfer_status()` 查询传输汇总。
5. `get_job()` / `list_jobs()` 查询任务，`cancel()` / `retry_stage()` 执行与 Web 相同的任务动作。
6. `watch()` / `wait_job()` 通过可续传事件游标等待任务；默认 `timeout=None` 表示只观察、不设置仿真总时长，业务方需要观察窗口时再显式传入 `timeout`。Agent 若不应在 Connector/path 等待上无限阻塞，使用 `wait_until_actionable()`；它会在终态或 `needs_input`/可恢复等待状态返回。
7. `diagnosis()` 获取稳定、脱敏、AI 可理解的业务结论。
8. `manifest()` 获取运行清单。
9. `list_results()` / `get_result()` / `download_result()` 获取并校验结果。
10. `retry_failed_inputs()` 在批量任务为 `partial` 时只重跑失败输入；已成功输入不会重复执行，重跑后的结果会生成新的不可变归档引用。

`Job`、`Event`、`EventsPage`、`RunConfigValidationResult`、`JobDiagnosis` 和 `ManifestResponse` 均提供 `to_dict()`；返回值只包含控制面 JSON、用户配置路径和逻辑引用，不含 MF4/Selena/结果文件正文，适合直接作为 MCP/Skill 的 JSON 结果。`Job.terminal`、`Job.needs_input` 和 `Job.progress_percent` 可用于统一终态、动作等待和进度展示判断。

YAML 草稿和可提交配置是两个明确阶段：导入/导出接口允许只填写 `selena`、只填写数据路径或只填写部分仿真选项，并返回 `missing_fields`/`validation_errors`；`validate_run()`、`submit_run()` 和 Web 的“检查配置/提交任务”仍严格要求完整 `UserRunConfig 2.0`。Skill 不应为了让草稿通过而补猜路径、项目或运行参数。

### Owner identity

在可信内网 `--insecure-no-auth` 试用中，Web 首次打开会要求用户输入
NTID；浏览器只保存规范化后的 `user-<小写标识>`，SDK 未显式传入 `user`
时使用相同格式的本机登录名。要跨电脑复用同一个 Connector，Web 与 SDK
应使用同一个稳定标识。清理浏览器缓存或更换浏览器后重新输入该标识即可；
旧 `web-*`/`sdk-*` 身份会保留到升级动作完成，服务不会把它们静默合并为另一个
owner。`X-Rsim-User` 在该模式下只是可伪造的隔离标签，不是认证，不能把服务
暴露到不受信网络。正式认证模式忽略此头，owner 只能来自用户 Bearer token，
Connector 配对仍需部署方提供受控的短期配对流程。

当任务需要读取 Windows 本地路径或执行 Windows 编译/本地仿真时，SDK 集成方可先调用
`RadarSimClient.download_windows_connector(destination)` 下载同源的一键连接入口（内部兼容参数固定为 `unified`），
并由实际的 Windows 用户执行一次。该方法不在 Linux 上执行 PowerShell，也不把 Agent Token 写入 YAML；
安装后的连接由 Windows 登录自启和监督进程持久复用。若电脑关机、睡眠或尚未登录，SDK/Web 只能等待连接恢复，
不能远程唤醒电源。`existing + cluster` 且输入已在 Cluster 可访问位置时不需要调用此方法。

若 SDK 提交后返回 `windows_connection_required` 或 `windows_path_access_required`，集成方应按以下顺序处理：下载入口、由实际 Windows 用户执行一次、等待 `capabilities()` 显示连接、再调用 `resume_direct_transfers()`；SDK 不会在 Linux 进程中隐式执行 PowerShell。`download_windows_connector_for_run()` 返回的脚本只下载/安装一次，之后同一 Windows 用户登录会自动启动并复用连接。

用户 YAML 中的文件路径统一由 `/api/v1` 规范化；Skill/MCP 不应自行替换斜杠或解析 Windows/UNC，直接转发用户输入。

## 控制面与数据面边界

Web、SDK、Skill/MCP 通过 Linux `/api/v1` 传递的只能是 YAML/JSON、控制命令、状态、进度、Manifest 和逻辑引用。MF4、Selena.exe/DLL、Runtime、MatFilter、Adapter 与大型结果文件不得编码进 MCP 消息、模型上下文或 Linux API 请求体。

当执行端不能读取用户路径时，Linux 返回内部 TransferPlan 或稳定等待/动作状态：Windows/Linux 本机 SDK、一次安装的统一 Connector 或 Cluster 上传网关负责把文件从源端直接送到目标 Windows/Cluster 数据面。Skill/MCP 只解释 `waiting_for_local_connector`、`waiting_for_cluster_access`、`transferring_direct_to_cluster`、`cluster_direct_transfer_unavailable` 等状态并调用 SDK 的继续/重试动作，不读取文件正文，也不自行实现 SMB/UNC 复制。

Web 与 SDK 的差别只在源端执行者：浏览器不能从路径文本读取任意本地文件，所以 Web 把本地路径交给同 owner 的持久 Connector；SDK 进程能够读取该路径时直接执行同一传输计划。Web 和 SDK 都不提供 `/run-data-uploads` 或项目化 `/dataset-uploads` 创建入口，MF4/Runtime/MatFilter/Adapter 正文不得经过 Linux HTTP 服务；`submit_run()` / `submit_yaml()` 只使用源到目标传输计划。

远程 Linux SDK 调用机通过请求内、非 YAML 的 `client_transfer_roles` 告知控制面哪些输入由该进程可读；该提示只决定签发哪些 owner/Job-bound TransferPlan，不携带路径正文、目标路径或凭据。独立挂载的共享/Cluster 文件系统仍按零复制处理。

本地仿真中，本机可达输入原地使用；远端输入不可原地读取时可由源端直达统一 Windows Connector。Cluster 仿真中，共享输入原地引用，本地输入直达 Cluster。两类数据流都不经过 Linux 控制面。完整产品合同见 `docs/PRODUCT_CONTRACT.md`，实施合同见 `docs/CONTROL_DATA_PLANE_PLAN.md`。

## SDK 的配置一致性与本地发现

SDK/Connector 可以在源端读取 `simulation.mat_filter` 为空时的高置信候选，并把该文件作为 `mat_filter` 直传资源；SDK 不会把这个源端发现结果写回提交的 `UserRunConfig`。因此同一份 YAML 经过 Web 与 SDK 时，用户配置、`spec_hash`、幂等请求和 Stage DAG 保持一致；源端传输计划仍可携带实际需要的 MatFilter 文件。

## Diagnosis 契约

HTTP：`GET /api/v1/jobs/{job_id}/diagnosis`

SDK：`RadarSimClient.diagnosis(job_id)`

响应版本：`radar-sim.job-diagnosis/1.0`

稳定字段：

| 字段 | 含义 |
|---|---|
| `status` | 调度任务状态 |
| `outcome` | 归一化业务结果：`pending`、`needs_input`、`succeeded`、`partial`、`failed`、`cancelled` |
| `code` | 稳定结论码，例如 `simulation_partial`、`simulation_failed`、`infrastructure_failed` |
| `category` | `none`、`configuration`、`infrastructure`、`simulation`、`system` |
| `summary` | 不含路径、密钥和堆栈的稳定说明 |
| `action` | 下一项可执行动作；可直接映射到 SDK 方法 |
| `artifacts_available` | 当前结果是否可通过公共结果接口下载 |
| `result_ref` | 路径无关的 `result:sha256:*` 引用 |
| `evidence` | 只含状态、Stage 类型、稳定错误码等安全证据 |
| `consistency` | 历史 Job 与 Manifest 不一致时返回 warning |

### MCP/Skill 异常处理

调用方只需按稳定类型处理，不解析日志或自然语言：

| 异常 | 含义 | 建议动作 |
|---|---|---|
| `RadarSimApiError` | 服务端合同、权限、能力或任务动作失败；保留 `code`、`status_code`、`detail`、`actions`、`request_id` | 根据 `code/actions` 补输入、连接 Connector、重试 Stage 或报告失败 |
| `RadarSimTransportError` | HTTP 连接/读取失败，服务端是否已提交可能未知 | 保留同一个 `idempotency_key` 重放提交；读取 `get_job()`/`list_jobs()` 恢复状态 |
| `RadarSimTransferCancelledError` | 调用方明确取消了源到目标传输 | 不自动重试；由用户重新选择继续或取消任务 |
| `RadarSimIntegrityError` | 结果 ZIP 校验值不一致 | 丢弃临时文件并重新下载；持续失败时报告结果完整性异常 |
| `TimeoutError` | 仅表示本次 SDK 观察窗口结束，不会取消服务器 Job | 继续 `get_job()`/`diagnosis()`，不要把它当作仿真失败 |
| 本地 `ValidationError`/`ValueError` | MCP 输入不是完整的 `UserRunConfig 2.0` 或 YAML/结果参数非法 | 先使用 `import_yaml()` 的 `complete`、`missing_fields`、`validation_errors`，不要猜路径或项目 |

Stage 失败码也遵循同一归类，不需要 Skill/MCP 识别项目或解析堆栈：

| Stage 错误码 | `diagnosis.category` | 含义 |
|---|---|---|
| `selena_failed`、`simulation_engine_failed`、`engine_failed`、`runtime_timeout` | `simulation` | 外围已经完成调度，Selena/仿真引擎返回失败或超时；查看该 Stage 日志，不把它误报成 Linux 调度故障 |
| `selena_launch_failed`、`runner_unavailable`、`runner_contract_failed` | `configuration` | Windows Agent 无法启动或执行安全的 Selena 调用 |
| `connector_dependency_missing` | `configuration` | 连接器脚手架缺少可选 Python 依赖；安装器/Agent 给出可执行补齐提示 |

Windows 本地运行的 Stage 结果会携带有限的 `diagnostics`（失败输入序号、稳定错误码和 Selena 日志尾部）；完整日志只保留在 Agent lease/结果归档中。Agent 在提交前会移除本机盘符、UNC 和工作目录，Linux、Web、SDK 只看到逻辑输入名和脱敏日志。日志上报是辅助通道，若控制面短暂不可达，终态结果仍以 Stage 回调为准，不会因为“日志接口失败”把成功任务改成失败。

`outcome=failed` 与 `artifacts_available=true` 可以同时成立。它表示仿真业务失败，但失败现场、`result.ini`、日志或部分输出已经归档；调用方应下载结果诊断，不能因为有产物而把任务判断为成功。

### 批量输入的部分成功

一次任务的 `data.path` 可以解析为多个输入文件。Windows 本地执行和 Cluster 执行都不因单个输入返回非零而截断整个批次：只要至少有一个输入成功并产生输出，控制面会继续执行结果收集与 Manifest 归档。最终 Job 与 Diagnosis 均使用 `partial` 表示“部分成功”，稳定码为 `simulation_partial`；`artifacts_available=true`，成功输出和失败诊断都可读取。`partial` 不是全成功，也不是基础设施失败。

Manifest 在这种情况下使用 `status=partial`，并提供：

- `summary.total_input_count`、`summary.succeeded_input_count`、`summary.failed_input_count`；
- `input_results[]`，每条包含逻辑 `input_relative_path`（Cluster 若无法从 `result.ini` 得到原始 MF4 名称，则使用稳定的任务结果目录标识）、`output_relative_path`、`status`、`returncode` 和稳定 `error_code`；
- `diagnostics.engine_log_tail`（若 Selena 产生了可安全归档的日志尾部）。

`input_results[].status=succeeded` 的条目可以继续被下载或交给后续处理；`failed` 条目只作为业务诊断，不会被伪装为成功。若所有输入都失败、Runner 不可用或控制面无法建立执行租约，仍按普通 Stage 失败处理，不进入部分成功分支。Web、SDK、Skill/MCP 都应展示逐条结果，而不是只显示一个批次级布尔值。

当 Job 为 `partial` 时，Web 的“只重试失败数据”和 SDK 的
`client.retry_failed_inputs(job_id)` 使用同一接口。省略 `input_paths` 表示重试全部失败输入，
也可以传入失败条目的 `input_relative_path` 子集。Windows 本地租约保留已成功输入的 checkpoint；
Cluster 会建立只包含待重试 MF4 的新包，并在最终 Manifest 中合并旧成功结果与本次新结果。
重试失败再次得到 `partial` 时仍可重复调用该接口；重试成功或失败都不会覆盖旧的不可变结果归档。

结果判断优先级：

1. Manifest 明确为 `partial` 时，业务结果为 `partial`，稳定码为 `simulation_partial`。
2. Manifest 明确为 failed/failure/cancelled 时，分别归一化为失败或取消；业务失败使用 `simulation_failed`。
3. 否则 Job 为 failed 时，按 Stage 稳定错误码区分 configuration、infrastructure、system。
4. 其余结果跟随 Job 调度状态。
5. Job 与 Manifest 结论冲突时，返回 `job_manifest_outcome_mismatch`，同时使用上述归一化结论。

Diagnosis 不返回用户本地绝对路径、共享盘路径、Agent 标识、服务端物理位置、密钥、任意原始错误消息或堆栈。

## Skill/MCP 正式薄封装

建议只暴露以下工具，并逐项调用 SDK：

| Skill/MCP 工具 | SDK |
|---|---|
| `get_simulation_schema` | `user_run_config_schema()` |
| `submit_simulation` | `submit_yaml()` |
| `get_simulation_readiness` | `cluster_readiness()` |
| `get_simulation_capabilities` | `capabilities()` |
| `check_agent_tools` | Agent Tools Manifest + local install state |
| `update_agent_tools` | versioned local Agent Tools bootstrap |
| `check_windows_connector` | local MCP Connector check + `capabilities()`/`windows_connector_status()` |
| `install_or_update_windows_connector` | local MCP installer + exact-device verification |
| `resume_simulation_transfer` | `resume_direct_transfers()` |
| `get_simulation_transfer` | `get_job_transfer_status()` |
| `get_simulation` | `get_job()` |
| `wait_simulation` | `wait_job()` |
| `diagnose_simulation` | `diagnosis()` |
| `get_simulation_manifest` | `manifest()` |
| `download_simulation_result` | `download_result()` |
| `retry_failed_inputs` | `retry_failed_inputs()` |

AI 调用顺序固定为：提交后保存 `job_id`；等待终态；读取 diagnosis；仅当 `artifacts_available=true` 时下载结果。重试提交必须复用同一 `idempotency_key`，Stage 重试必须使用 diagnosis 返回的 `retry_stage` 动作，批量 `partial` 则使用 `retry_failed_inputs` 动作。

`submit_run()` 未显式传入 `idempotency_key` 时 SDK 会自动生成幂等键，并通过
`client.last_submission_key` 暴露最近一次提交键，便于响应丢失后安全重放。Artifact/Runtime Bundle
分块上传会在连接中断或 offset 冲突后先读取服务端 offset 再继续；结果 ZIP 下载会在断流后有限重启并始终做 SHA-256 校验。永久 4xx、证据不匹配和 checksum 错误直接返回，不转换成等待状态。

MCP Server 和 Skill 已作为仓库内正式集成资产提供：`radar_sim_mcp/` 与 `skills/radar-sim-simulation/`。它们只做参数描述、权限适配、Connector 本机安装策略和 SDK 调用转发，不创建第二套调度器。认证开启时 Connector 安装仍需部署方提供短期 pairing。
