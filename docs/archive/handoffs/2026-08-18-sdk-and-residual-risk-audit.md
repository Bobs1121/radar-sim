# 2026-08-18 SDK 与剩余流程风险审查

## 结论

当前 SDK 的核心接口已经可用，但还不能称为“覆盖所有用户故事、所有本地路径和所有恢复场景的完整正式 SDK”。

当前已验证可用的范围：

- `RadarSimClient` base URL 不带 `/api/v1`，客户端统一追加版本前缀；
- Web/SDK 共用 `UserRunConfig 2.0`、canonical fingerprint 和十阶段 DAG；
- `validate_run()`、`submit_run()`、`submit_yaml()`、幂等提交；
- SDK/Linux 或 Windows 本地可读路径的 TransferPlan 直传，不经过 Linux HTTP 正文上传；
- `watch()`/`wait()`/`wait_job()`、事件 cursor、取消、Stage 级 retry、diagnosis；
- Job Manifest、结果 ZIP 下载、临时文件、SHA-256 校验和原子替换；
- 配置资产、Runtime Bundle、结果归档上传接口；
- owner、API 错误、Transport 错误和完整性错误的结构化模型。

本次证据：

- SDK/传输/结果专项：`87 passed, 1 skipped`；
- 线上 SDK 只读 smoke：health 正常、Windows 1 个、Cluster 2 个、当前 Job 列表为空；
- 之前全量 Python 回归：`1654 passed, 12 skipped, 1 warning`；
- 线上最终 release：`/home/hoz2wx/radar-sim-95e7e32`。

## P1：会导致用户任务无法顺利完成或长期卡住

### P1-1 SDK/Server 对 UNC 本地源的自动路由仍不完整

`radar_sim_sdk.client._sdk_local_transfer_sources()` 明确把 `classify_data_path() == "shared"` 的 UNC 路径排除，即使 SDK 进程在 Windows 上真实能够读取该 UNC 路径，也不会把 `dataset`/`runtime_xml` 等角色加入 `client_transfer_roles`。

结果可能是：

1. 控制面把 UNC 路径当作 Cluster zero-copy shared path；
2. Cluster 实际没有对应挂载时，任务进入 `shared_dataset_unavailable` 或 Cluster preflight 失败；
3. SDK 没有执行用户期望的“本地源 → Cluster 目标”直传。

本次已修复的是 Agent 本地 Selena 执行时保留原始 UNC 别名；这不等于 SDK/Web 已经把“本机可读 UNC”自动路由到 source-to-Cluster transfer。需要增加明确的源端能力判断或用户可控的 `source_scope=local` 语义，并覆盖 Web/SDK/Agent 三端。

### P1-2 SDK 直传把永久 API 错误包装成 waiting

`_auto_prepare_direct_transfers()` 对直传过程使用宽泛 `except Exception`。只有少数已知传输错误会保留原 code，其余错误会被转换为：

```text
cluster_direct_transfer_unavailable
Local input transfer is waiting for a connected Agent or an accessible Cluster target.
```

因此以下永久错误可能表现为“等待”：

- `invalid_transfer_item`；
- role 不匹配；
- Job/Stage owner 不匹配；
- TransferPlan 合同错误；
- 服务端配置错误；
- 过期或非法的 Stage 状态。

用户会看到任务一直等，而不是马上得到可修复的配置错误。应按 HTTP 状态和稳定 `ApiError.code` 分类：传输/503/408/429 可等待，其余 4xx 合同错误必须直接抛出并保留 `request_id`。

### P1-3 没有“只重试失败输入”的 SDK/API/Web 能力

当前 SDK 只有：

```python
client.retry_stage(job_id, stage_id)
```

这只能重试失败或取消的 Stage。批量任务产生 `partial` 后，成功输入已经完成，失败输入没有公开的逐输入 retry API，用户无法指定失败 MF4 重跑并避免成功输入重复执行。

这对批量仿真是高风险缺口：失败一条可能迫使用户重新提交整个批次，或者根本无法恢复。

### P1-4 长期结果存储的磁盘水位没有真正配置到生产

`ResultCatalog` 有 `min_free_bytes` 和回收代码，但 `default_result_catalog()` 没有从部署配置传入有效水位，默认值仍为 `0`，相当于关闭磁盘水位保护。

结果归档长期增长时，可能出现：

- 磁盘写满；
- 新结果归档失败；
- Web/SDK 只能看到仿真完成但结果不可下载；
- 维护线程无法恢复已经产生的结果。

需要把结果 retention、GC、最小剩余空间和告警配置统一纳入部署配置，并做真实低磁盘验证。

### P1-5 真实 SDK 端到端覆盖仍不完整

当前 SDK 专项测试主要是 TestClient/MockTransport 加文件系统传输测试；线上只做了 SDK health/capabilities/list jobs 的只读 smoke，历史上做过已有 Job 的 wait/download。

仍缺少同一个最终 release 上的完整真实证据：

- SDK + existing + Windows 本地数据 → Cluster；
- SDK + existing + UNC/DFS 数据；
- SDK + build + Cluster；
- SDK 进程在 submit 响应丢失后用同一 idempotency key 恢复；
- 直传过程中断网、进程重启和大批量恢复；
- SDK 下载大结果 ZIP 的断流后重试；
- SDK 看到 partial 后的后续动作。

## P2：不会立即破坏任务，但会造成误解或恢复不便

### P2-1 `wait_job()` 的 timeout 是观察窗口，不是仿真总时长，但默认值容易误解

当前 `wait_job()` 默认 `timeout=600`。超时只会让 SDK 调用抛 `TimeoutError`，不会取消服务器 Job，但用户如果没有捕获异常，容易误以为仿真失败。

建议提供：

- `timeout=None` 表示无限观察；或
- `wait_forever()`；或
- SDK 示例明确要求长任务传入业务观察窗口，并在 TimeoutError 后继续 `get_job()`。

### P2-2 SDK 下载/上传大文件的恢复接口不完全对称

结果归档上传已有 chunk offset 恢复；但 `upload_artifact()` 和 `upload_runtime_bundle()` 的公开便捷方法没有内部重试循环，网络中断后用户需要自己保存 session 并调用底层 append 接口恢复。

结果下载也没有进度回调，断流后不自动重试，需要调用方重新发起下载。

### P2-3 SDK 不自动安装/启动 Windows Connector

`download_windows_connector_for_run()` 只下载 `connect.cmd`，不会执行安装。这个边界出于安全和操作系统权限是合理的，但 SDK 用户如果只调用 `submit_run()`，遇到本地 Windows 路径时可能只得到 waiting Job，不知道还需要人工执行 Connector。

SDK 应在 readiness 返回 `windows_connection_required` 时提供结构化动作和明确文档示例，至少包括：

```python
launcher = client.download_windows_connector_for_run(config, destination)
# 用户/企业安装器在 Windows 上执行 launcher
job = client.resume_direct_transfers(job, config)
```

### P2-4 submit 网络响应丢失时，自动幂等恢复仍依赖调用方保存 key

状态变更 POST 不自动重试是正确的，避免重复创建 Job；但 `idempotency_key` 由调用方可选传入。调用方不传 key 时，如果服务器已经创建 Job、响应在网络中丢失，SDK 无法自动找回原 Job，用户重新调用可能产生两个 Job。

建议 SDK 在 `submit_run()` 未提供 key 时自动生成并返回/记录可恢复的 submission token，或者强制要求非 dry-run 提交必须显式传入稳定 key。

## 已确认没有问题的 SDK 基础能力

| 能力 | 当前判断 |
|---|---|
| base URL 处理 | 正确：调用方不带 `/api/v1` |
| owner 默认值 | 正确：默认 `user-<OS login>`，与 Web/Connector 约定一致 |
| Web/SDK 同 YAML | 正确：共用 `UserRunConfig 2.0` 和 fingerprint |
| 幂等提交 | 服务端有 durable idempotency；SDK支持传 key |
| Linux 不接收直传正文 | 正确：SDK 仅发送 plan/progress/manifest 元数据 |
| 传输校验 | 有 size/mtime/SHA-256、`.partial`、原子 rename |
| 结果下载完整性 | 有 archive checksum、临时文件和原子替换 |
| API 错误模型 | 有 `RadarSimApiError`、code/status/detail/actions/request_id |
| 长任务服务端执行 | 不受 SDK `wait_job()` 观察超时影响，Job 仍在服务端运行 |

## 最终 SDK 放行结论

SDK 不是“没有实现”，而是已经具备可用主干；但按用户之前要求的“多用户、单条/批量、本地/Cluster、源到源、长任务、失败恢复”正式交付标准，目前结论为：

> SDK 核心可用于受控内网和已验证路径；还不能宣称对所有 Windows UNC、本地直传失败分类、partial 失败输入重试和长期结果容量场景全部完善。

正式放行 SDK 前，优先级顺序应为：

1. UNC 本地源的明确 source-scope/直传路由；
2. 直传永久错误不再伪装 waiting；
3. 失败输入独立 retry；
4. 生产结果水位、GC、retention 和告警；
5. 最终 release 上完成 SDK existing/build × local/Cluster × single/batch 的真实验收。

