# 2026-08-18 需求确认与继续审查交接

## 当前目标

把 radar-sim 做成一个围绕 Selena 编译与仿真的轻量外围框架：Web 和 Python SDK 使用同一套 API/Job/Stage/Manifest，支持多用户、单条和批量数据、本地编译、本地仿真、Cluster 仿真、源端到 Cluster 的直传、长任务、结果获取和失败恢复。

本项目的正确性边界是外围流程与状态合同，不负责 Selena 内部算法、仿真服务器内部排队策略或点云/信号内容正确性。

## 用户已确认的产品边界

必须支持：

- Web 与 SDK 同合同、同 YAML、同状态和同结果接口；
- 多用户逻辑隔离、并发提交、单条/批量 MF4；
- `existing` 和 `build` 两种 Selena 来源；编译使用用户填写的 Selena 脚本、构建目录、输出目录；不运行 clean，不切换/清理用户工作区；
- `local`、`cluster`、`auto` 目标；Cluster 由现有 Cluster 排队；
- Windows 本地源或 Linux SDK 本地源到 Cluster 的源到源直传；共享/Cluster 可见输入零复制；正文不经过 Linux HTTP；
- Connector 一次安装、稳定 owner、登录自启、断线重连、状态恢复；
- 长任务不依赖固定仿真总时长；
- batch partial 保留成功输入，只重跑失败输入，结果链路完整；
- 结果 ZIP/Manifest/SDK 下载、retention、GC、磁盘水位。

明确不扩大：

- Selena 内部仿真失败只作为业务结果，不由框架修复或伪装；
- 点云/信号内容一致性不在本框架外围修复范围；
- 认证安全本轮按受信内网约束；
- 远端资源反向传到本地 Windows（`source_to_local`）当前只允许稳定 fail-closed，不得伪装支持；
- 不为了单点问题增加项目专用分支、项目注册表、隐式路径猜测或第二套调度流程。

## 已完成并已部署的基线

- 已提交并推送：`674366f970ac61e25253189a78aa7c4d657c53c0`，分支 `codex/new-branch`。
- 当前线上 release：`/home/hoz2wx/radar-sim-674366f`。
- `radar-sim-v1.service`：`active`，`NRestarts=0`，`ExecMainStatus=0`。
- 线上环境显式配置：`RSIM_RESULT_MIN_FREE_BYTES=1073741824`。
- 线上 SDK health/capabilities/Cluster readiness/Connector 下载 smoke 已通过；Cluster readiness 返回 `ready=true`、`blockers=[]`。
- 线上当前共享控制库无 queued/running Job。
- Connector 包线上校验：`8401694` bytes，SHA-256 `b73a6e184c19c2307a6e18462a48f05bf7267d171460e960f2c7eb7ab33d0cbb`。
- 上一阶段全仓回归：`1669 passed, 12 skipped, 1 warning`；本次 readiness/取消语义继续审查后的最新全仓回归：`1673 passed, 12 skipped, 1 warning`。warning 为 Starlette/httpx 弃用提示。

## 已完成的主要能力

1. UNC/盘符本地源路由、Web `client_transfer_roles`、SDK 直传错误分类；永久 4xx 不再伪装 waiting。
2. `wait_job()`/`wait()` 默认无固定观察总时长；SDK 非 dry-run 自动幂等键，暴露 `last_submission_key`。
3. Artifact/Runtime Bundle 分块上传 offset 恢复；结果 ZIP 断流有限重试和 SHA-256 校验。
4. Cluster readiness 提交前探测；结果水位、过期 GC、retention 和 Cluster/local 统一保留期。
5. `POST /api/v1/jobs/{job_id}/retry-failed-inputs`、Web“只重试失败数据”、SDK `retry_failed_inputs()`。
6. Windows 本地 checkpoint 恢复；Cluster partial 新包只包含失败输入，并合并旧成功输出；每次重试使用新结果归档引用。

## 本轮继续改动（已提交并部署）

这些修改已完成针对性测试、全仓回归并部署到 `30ad4a6`：

### A. Cluster readiness 最早 gate

文件：`core/api_v1.py`、`core/control_service.py`、`radar_sim_web/static/app.js`、对应测试。

问题：build+Cluster readiness 失败时，如果只阻塞 `preflight`，上游 `resolve_spec/build_selena/prepare_data` 仍可能编译或传输。

当前方案：

- `existing+cluster` 阻塞 `environment_check`；
- `build+cluster` 阻塞最早的 `resolve_spec`；
- readiness 专属 blocked Stage 可通过 `retry_stage` 重新排队；
- Web 对 readiness blocked Stage 显示“重新检查 Cluster”。

审查重点：不能破坏 `resolve_spec -> environment_check -> build` 正常绑定；不能让普通配置 blocked Stage 被误允许 retry；retry 后下游依赖必须仍按 DAG 正确推进。

### B. Cluster 执行前二次 readiness

文件：`core/cluster_stage_executor.py`、`cli/server.py`。

生产 `ClusterStageContext` 注入 `environment_probe`，在 preflight 创建 Config.cfg 前再次检查实际 Cluster 依赖，覆盖提交后 Cluster 状态变化窗口。该字段放在 dataclass 最后，保持既有 positional embedding 兼容。

审查重点：测试 double 未注入 probe 时行为不变；生产实际 `serve-v1` 必须注入；检查失败必须是可重试的 Cluster Stage 错误，不能被识别为 Selena 内部失败。

### C. SDK 传输取消语义

文件：`radar_sim_sdk/errors.py`、`radar_sim_sdk/client.py`、`radar_sim_sdk/__init__.py`、`tests/test_sdk.py`。

问题：用户主动取消直传时，底层 `transfer_cancelled` 被 `_auto_prepare_direct_transfers()` 当成 waiting，可能在 resume 时再次传输。

当前方案：新增公开 `RadarSimTransferCancelledError(code="transfer_cancelled")`；SDK 执行 transfer 时将用户取消作为终态异常，不再转换为 needs-agent，也不加入 retryable transfer code。

审查重点：网络断开仍可恢复；用户取消不自动重试；服务端 TransferPlan 状态仍可通过 `cancel_transfer()` 查询/取消；不把异常类引入第二套传输协议。

### D. SDK upload-session 创建重试

当前 `_request()` 对以下服务端幂等创建接口允许有限 transport retry：

- `/api/v1/artifact-uploads`
- `/api/v1/runtime-bundle-uploads`
- `/api/v1/result-uploads`

只对服务端自身按 owner/evidence/run 查重的创建接口开放，不扩大到普通状态变更 POST。

## 已执行的收口动作

1. 新增针对性测试通过：readiness earliest gate、blocked readiness retry、preflight 二次 probe、SDK transfer cancellation、upload-session retry。
2. 受影响专项回归通过：`208 passed, 1 warning`。
3. 全仓回归通过：`1673 passed, 12 skipped, 1 warning`。
4. 切换前确认线上无 Job；服务切换到 `/home/hoz2wx/radar-sim-30ad4a6` 后保持 `active`、`NRestarts=0`、`ExecMainStatus=0`。
5. 线上 health、capabilities、Cluster readiness、Connector poll、Connector ZIP checksum 和结果水位环境均复验通过。
6. 两个 owner 的并发 Cluster-visible `validate_run` 均返回 `ready=true`、无 blocker，未创建测试 Job。
7. 线上验收只证明外围框架与控制链路；不把 Selena 内部结果内容正确性宣称为本框架已验证。

## 禁止事项

- 不要直接部署当前未提交修改；
- 不要为了验证而启动真实 Selena/Cluster 大任务，除非测试数据、工程和清理方案已明确；
- 不要删除用户未明确授权的数据、Job、Agent 或旧 release；
- 不要把 `X-Rsim-User` 当成认证；
- 不要把 Selena 内部失败归因成框架失败，也不要把框架失败改写成仿真成功；
- 不要用固定时间替代状态/心跳/结果证据。
