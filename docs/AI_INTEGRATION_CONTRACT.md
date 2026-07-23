# Web、SDK 与 AI 集成契约

## 结论

Web、Python SDK、未来 Skill/MCP 只使用同一套 `/api/v1`，不得复制任务编排、路径转换、Cluster 提交或结果判断逻辑。Skill/MCP 是 Python SDK 的薄封装，不是第二个仿真后端。

首版公共调用闭环：

1. `RadarSimClient.submit_yaml(path, idempotency_key=...)` 提交与 Web 相同的用户 YAML。
2. `get_job()` / `list_jobs()` 查询任务。
3. `watch()` / `wait()` 通过可续传事件游标等待任务。
4. `diagnosis()` 获取稳定、脱敏、AI 可理解的业务结论。
5. `manifest()` 获取运行清单。
6. `list_results()` / `get_result()` / `download_result()` 获取结果。

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
| `get_simulation` | `get_job()` |
| `wait_simulation` | `wait()` |
| `diagnose_simulation` | `diagnosis()` |
| `get_simulation_manifest` | `manifest()` |
| `download_simulation_result` | `download_result()` |

AI 调用顺序固定为：提交后保存 `job_id`；等待终态；读取 diagnosis；仅当 `artifacts_available=true` 时下载结果。重试提交必须复用同一 `idempotency_key`，Stage 重试必须使用 diagnosis 返回的 `retry_stage` 动作。

暂不实现独立 MCP Server。等 Linux Web/SDK 全流程稳定后，MCP 仅做参数描述、权限适配和 SDK 调用转发。
