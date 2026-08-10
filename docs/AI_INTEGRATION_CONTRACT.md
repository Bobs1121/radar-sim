# Web、SDK 与 AI 集成契约

## 结论

Web、Python SDK、未来 Skill/MCP 只使用同一套 `/api/v1`，不得复制任务编排、路径转换、Cluster 提交或结果判断逻辑。Skill/MCP 是 Python SDK 的薄封装，不是第二个仿真后端。

首版公共调用闭环：

1. `RadarSimClient.submit_yaml(path, idempotency_key=...)` 提交与 Web 相同的用户 YAML。
2. `capabilities()` 查询与 Web 右上角相同的 owner-scoped Windows/Cluster 能力。
3. `prepare_direct_transfers()` / `resume_direct_transfers()` 让 SDK 调用机把可读的 Selena 完整目录、Runtime、MatFilter、Adapter 和 MF4 按同一 `TransferPlan` 直接送到 Cluster 数据面；`get_job_transfer_status()` 查询传输汇总。
4. `get_job()` / `list_jobs()` 查询任务，`cancel()` / `retry_stage()` 执行与 Web 相同的任务动作。
5. `watch()` / `wait()` 通过可续传事件游标等待任务。
6. `diagnosis()` 获取稳定、脱敏、AI 可理解的业务结论。
7. `manifest()` 获取运行清单。
8. `list_results()` / `get_result()` / `download_result()` 获取并校验结果。

当任务需要读取 Windows 本地路径或执行 Windows 编译/本地仿真时，SDK 集成方可先调用
`RadarSimClient.download_windows_connector(destination)` 下载同源的一键连接入口（内部兼容参数固定为 `unified`），
并由实际的 Windows 用户执行一次。该方法不在 Linux 上执行 PowerShell，也不把 Agent Token 写入 YAML；
安装后的连接由 Windows 登录自启和监督进程持久复用。若电脑关机、睡眠或尚未登录，SDK/Web 只能等待连接恢复，
不能远程唤醒电源。`existing + cluster` 且输入已在 Cluster 可访问位置时不需要调用此方法。

用户 YAML 中的文件路径统一由 `/api/v1` 规范化；Skill/MCP 不应自行替换斜杠或解析 Windows/UNC，直接转发用户输入。

## 控制面与数据面边界

Web、SDK、Skill/MCP 通过 Linux `/api/v1` 传递的只能是 YAML/JSON、控制命令、状态、进度、Manifest 和逻辑引用。MF4、Selena.exe/DLL、Runtime、MatFilter、Adapter 与大型结果文件不得编码进 MCP 消息、模型上下文或 Linux API 请求体。

当执行端不能读取用户路径时，Linux 返回内部 TransferPlan 或稳定等待/动作状态：Windows/Linux 本机 SDK、一次安装的 Connector 或 Cluster 上传网关负责把文件从源端直接送到本地 Windows full/Cluster 数据面。Skill/MCP 只解释 `waiting_for_local_connector`、`waiting_for_cluster_access`、`transferring_direct_to_cluster`、`cluster_direct_transfer_unavailable` 等状态并调用 SDK 的继续/重试动作，不读取文件正文，也不自行实现 SMB/UNC 复制。

Web 与 SDK 的差别只在源端执行者：浏览器不能从路径文本读取任意本地文件，所以 Web 把本地路径交给同 owner 的持久 Connector；SDK 进程能够读取该路径时直接执行同一传输计划。Web 不再用 `/run-data-uploads` 或 `/config-assets` 把 MF4/Runtime/MatFilter/Adapter 正文上传到 Linux。SDK 的 `submit_run()` / `submit_yaml()` 同样禁止回退这些 legacy body-upload 接口；旧 upload 方法只保留兼容维护，不属于 UserRunConfig 主链路，也不得被 Skill/MCP 调用。

远程 Linux SDK 调用机通过请求内、非 YAML 的 `client_transfer_roles` 告知控制面哪些输入由该进程可读；该提示只决定签发哪些 owner/Job-bound TransferPlan，不携带路径正文、目标路径或凭据。独立挂载的共享/Cluster 文件系统仍按零复制处理。

本地仿真中，本机可达输入原地使用；远端输入不可原地读取时可由源端直达统一 Windows Connector。Cluster 仿真中，共享输入原地引用，本地输入直达 Cluster。两类数据流都不经过 Linux 控制面。完整产品合同见 `docs/PRODUCT_CONTRACT.md`，实施合同见 `docs/CONTROL_DATA_PLANE_PLAN.md`。

## Diagnosis 契约

HTTP：`GET /api/v1/jobs/{job_id}/diagnosis`

SDK：`RadarSimClient.diagnosis(job_id)`

响应版本：`radar-sim.job-diagnosis/1.0`

稳定字段：

| 字段 | 含义 |
|---|---|
| `status` | 调度任务状态 |
| `outcome` | 归一化业务结果：`pending`、`needs_input`、`succeeded`、`failed`、`cancelled` |
| `code` | 稳定结论码，例如 `simulation_failed`、`infrastructure_failed` |
| `category` | `none`、`configuration`、`infrastructure`、`simulation`、`system` |
| `summary` | 不含路径、密钥和堆栈的稳定说明 |
| `action` | 下一项可执行动作；可直接映射到 SDK 方法 |
| `artifacts_available` | 当前结果是否可通过公共结果接口下载 |
| `result_ref` | 路径无关的 `result:sha256:*` 引用 |
| `evidence` | 只含状态、Stage 类型、稳定错误码等安全证据 |
| `consistency` | 历史 Job 与 Manifest 不一致时返回 warning |

Stage 失败码也遵循同一归类，不需要 Skill/MCP 识别项目或解析堆栈：

| Stage 错误码 | `diagnosis.category` | 含义 |
|---|---|---|
| `selena_failed`、`simulation_engine_failed`、`engine_failed`、`runtime_timeout` | `simulation` | 外围已经完成调度，Selena/仿真引擎返回失败或超时；查看该 Stage 日志，不把它误报成 Linux 调度故障 |
| `selena_launch_failed`、`runner_unavailable`、`runner_contract_failed` | `configuration` | Windows Agent 无法启动或执行安全的 Selena 调用 |
| `connector_dependency_missing` | `configuration` | 连接器脚手架缺少可选 Python 依赖；安装器/Agent 给出可执行补齐提示 |

Windows 本地运行的 Stage 结果会携带有限的 `diagnostics`（失败输入序号、稳定错误码和 Selena 日志尾部）；完整日志只保留在 Agent lease/结果归档中。Agent 在提交前会移除本机盘符、UNC 和工作目录，Linux、Web、SDK 只看到逻辑输入名和脱敏日志。日志上报是辅助通道，若控制面短暂不可达，终态结果仍以 Stage 回调为准，不会因为“日志接口失败”把成功任务改成失败。

`outcome=failed` 与 `artifacts_available=true` 可以同时成立。它表示仿真业务失败，但失败现场、`result.ini`、日志或部分输出已经归档；调用方应下载结果诊断，不能因为有产物而把任务判断为成功。

结果判断优先级：

1. Manifest 明确为 failed/failure/cancelled/partial 时，业务结果为 `simulation_failed`。
2. 否则 Job 为 failed 时，按 Stage 稳定错误码区分 configuration、infrastructure、system。
3. 其余结果跟随 Job 调度状态。
4. Job 与 Manifest 结论冲突时，返回 `job_manifest_outcome_mismatch`，同时使用上述归一化结论。

Diagnosis 不返回用户本地绝对路径、共享盘路径、Agent 标识、服务端物理位置、密钥、任意原始错误消息或堆栈。

## 未来 Skill/MCP 的最薄封装

建议只暴露以下工具，并逐项调用 SDK：

| Skill/MCP 工具 | SDK |
|---|---|
| `submit_simulation` | `submit_yaml()` |
| `get_simulation_capabilities` | `capabilities()` |
| `resume_simulation_transfer` | `resume_direct_transfers()` |
| `get_simulation_transfer` | `get_job_transfer_status()` |
| `get_simulation` | `get_job()` |
| `wait_simulation` | `wait()` |
| `diagnose_simulation` | `diagnosis()` |
| `get_simulation_manifest` | `manifest()` |
| `download_simulation_result` | `download_result()` |

AI 调用顺序固定为：提交后保存 `job_id`；等待终态；读取 diagnosis；仅当 `artifacts_available=true` 时下载结果。重试提交必须复用同一 `idempotency_key`，Stage 重试必须使用 diagnosis 返回的 `retry_stage` 动作。

暂不实现独立 MCP Server。等 Linux Web/SDK 全流程稳定后，MCP 仅做参数描述、权限适配和 SDK 调用转发。
