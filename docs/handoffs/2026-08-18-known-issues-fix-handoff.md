# 2026-08-18 已知问题修复交接

## 结论

本轮修复覆盖 Web/SDK、多用户任务调度、源到源数据传输、长任务观察、批量 partial 恢复、Cluster 就绪检查、结果归档容量管理和 Connector 相关外围错误。Selena 内部仿真结果内容本身（包括点云内容正确性）不在本轮范围；认证也按受信内网约束不纳入本轮。

本地全仓回归：`1669 passed, 12 skipped, 1 warning`。唯一 warning 是 Starlette/httpx 弃用提示，不是业务失败。

## 已修复的问题

### 1. 源路径与传输

- Windows SDK 可读的盘符路径和 UNC 路径都被识别为源端物理输入；UNC 不再因为语法分类为 `shared` 就被 SDK 错误地当成 Cluster 零复制路径。
- Web 对盘符、`file://`、UNC 和 `//` 形式生成同一组 `client_transfer_roles`，浏览器仍由持久 Connector 执行正文传输，Linux 只保存计划、进度和 Manifest。
- SDK 直传遇到 HTTP 5xx、408、429、网络传输异常、TransferPlan 暂不可用时才返回可恢复等待；永久 4xx 合同错误、非法 role、输入不可用和证据错误直接抛出，不能伪装成“等待连接”。
- 数据目录发现支持取消回调，在遍历目录、文件和 checksum 阶段及时退出；长批量扫描不会靠固定总时长强行失败。

### 2. Cluster 就绪门

- 能力心跳只说明 Linux executor/Gateway 在线；正式服务额外调用部署拥有的 Cluster readiness probe，检查共享路径、`client.py`/Manager/Worker 依赖、提交路径、工作区写入和提交凭据。
- readiness 失败时：`validate` 返回稳定 blocker；正式提交会把 Cluster 的第一道 gate 置为 `needs_input/blocked`，不会先传输大文件、编译或提交外部 Cluster 任务。
- 共享工作区写 probe 使用唯一临时文件并在检查后清理，不会随着 Web/SDK 并发校验持续污染 `_rsim_probe`。
- readiness 只在正式提交运行；dry-run 不执行网络探测、写入或其他外部副作用。

### 3. 批量 partial 与失败输入重跑

- 新增 `POST /api/v1/jobs/{job_id}/retry-failed-inputs`，Web 提供“只重试失败数据”，SDK 提供 `client.retry_failed_inputs(job_id, input_paths=())`。
- 不传 `input_paths` 重跑全部失败输入；传入子集时服务端校验每个路径确实是失败输入。
- Windows 本地 lease 保留已成功输入的 checkpoint，只清除选中的失败 checkpoint。
- Cluster 重跑建立新 Config/新目录，只复制选中的失败 MF4；原成功输出复制到新结果根并在收集时与新结果合并。
- ClusterRun lease 在 terminal 状态下安全重置为 `prepared`，清除旧外部提交 receipt 和旧内部结果行，不重复提交旧任务。
- 本地和 Cluster 每次 partial 重跑都使用新的结果归档 run reference，避免 immutable ResultCatalog 把旧 partial 归档误当成新结果；旧归档仍按 retention 保留。
- 失败再次出现时仍为 `partial`，可继续对剩余失败输入调用同一动作；全部成功后才转为 `succeeded`。

### 4. SDK 长任务、幂等和大文件恢复

- `wait()`/`wait_job()` 默认 `timeout=None`，timeout 只是 SDK 观察窗口，不是仿真总时长，也不会因为观察窗口结束取消服务端 Job。
- 非 dry-run `submit_run()` 未传 key 时自动生成 `sdk-<uuid>` 幂等键；`client.last_submission_key` 暴露最近一次 key，响应丢失后可以安全重放，不创建重复 Job。
- Artifact 和 Runtime Bundle 分块上传在连接中断或 offset 冲突后先查询服务端 offset，再继续精确字节；永久错误直接返回。
- Result ZIP 下载发生断流时使用临时文件重新开始有限重试，最终始终校验 catalog SHA-256；checksum 不匹配不会重试伪装成网络问题。
- `download_windows_connector_for_run()` 明确保持“SDK 下载、实际 Windows 用户执行一次”的边界；readiness 返回结构化连接动作，SDK 不在 Linux 进程中隐式执行 PowerShell。

### 5. 结果存储

- `default_result_catalog()` 默认启用 `RSIM_RESULT_MIN_FREE_BYTES=1 GiB` 水位；部署可通过同名环境变量显式调整。
- 归档前按源文件总大小和 ZIP 开销预留磁盘空间；若低于水位，会先尝试一次过期结果 GC，再 fail-closed 返回稳定错误。
- 维护线程周期性执行 `collect_expired()`；content-addressed ZIP 只有最后一个引用过期时才删除。
- Windows 本地和 Cluster 结果均按用户 `retain_days` 计算 `retain_until`，不再出现 Cluster 默认永久保留而本地默认 30 天的不一致。

## Web/SDK 合同

两种入口仍只使用同一 `UserRunConfig 2.0` 和同一 `/api/v1`：

1. `validate_run()` 看 execution/readiness；Cluster readiness 不是 capability 心跳的替代品。
2. `submit_run()`/`submit_yaml()` 只提交配置和元数据；可读源端由 SDK 或 Connector 执行 TransferPlan。
3. 用 `wait_job()` 等待长任务；超时后可继续 `get_job()`，不能把 SDK `TimeoutError` 当仿真失败。
4. terminal 后先 `diagnosis()`/`manifest()`；`partial` 必须按 `input_results[]` 展示。
5. `partial` 使用 `retry_failed_inputs()`；阶段级 `retry_stage()` 只用于外围 Stage 恢复。
6. `artifacts_available=true` 时用 `download_job_result()` 或 `download_result()`，SDK 做临时文件和 checksum 校验。

## 关键设计取舍

| 决策 | 取舍 | 原因 |
|---|---|---|
| readiness 在提交时再次检查 | 额外增加一次短探测延迟 | 失败尽早暴露，避免先传输/编译后才发现 Cluster 不可用 |
| SDK 自动生成幂等键 | 调用方需保存 `last_submission_key` 以处理极端响应丢失 | 兼顾简单调用与不会重复建 Job |
| partial 重跑使用新的结果引用 | 旧 partial 归档会暂时保留 | ResultCatalog 内容不可变，不能原地覆盖；保留旧证据更利于审计 |
| Cluster 重跑只复制失败输入 | 需要在新结果根合并旧成功输出 | 避免批量任务因一条失败重新仿真全部数据，同时保证最终 Manifest 完整 |
| SDK 大 ZIP 断流从头重试 | 可能重复读取已传输字节 | 当前统一 FileResponse 不依赖 Range；先保证不会留下半成品和错误结果，后续可在 API/SDK 同时加入 Range resume |
| readiness provider 可选 | framework-only 单元嵌入不强制依赖真实 Cluster | 生产 `serve-v1` 必须注入 provider；测试 double 保持可组合 |

## 验收交付物

- 代码：本文件所在提交中的 SDK、API、Cluster executor、ResultCatalog、Connector 数据路径恢复相关修改。
- 自动化：全仓 `1669 passed, 12 skipped, 1 warning`；专项覆盖 UNC、传输错误分类、长任务、上传/下载断流、Cluster readiness、local/Cluster partial retry、结果水位和 GC。
- 部署前必须再次确认：工作区干净、候选 release 通过平台无关门禁、无运行中/排队任务、服务切换后 health、Cluster executor/Gateway、`NRestarts=0` 和 readiness 均正常。

## 明确不在本轮范围

- Selena 内部仿真算法、点云/信号内容是否正确；这些属于仿真内容验收，不应由外围框架把结果改写成“成功”。
- 受信内网无认证模式的恶意冒充防护；`X-Rsim-User` 仅作 owner 路由标签。
- source-to-local 跨电脑反向传输、独立 MCP/Skill 包和关机设备远程唤醒。
