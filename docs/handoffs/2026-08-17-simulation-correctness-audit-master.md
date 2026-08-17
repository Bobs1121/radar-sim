# radar-sim 全服务场景链路审计：汇总 handoff

日期：2026-08-17  
任务书：`docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md`（执行合同）  
方式：14 个并行审计子代理（与主 AI 同模型）+ 主 AI 汇总。审计只读，未修改任何源码、未提交代码。  
范围：Web/SDK 提交 → Windows Connector / Selena 编译 → 本地/Cluster 执行 → 批量结果 → 结果下载 → 故障恢复的完整链路，覆盖 brief 第 11 节 Task 0/0.1/A/B/C/D/E/F/G/H/I/J/K/L/M/N/O。

## 1. 结论

**有条件上线（受信内网单用户 / 测试部署）。正式多租户被 P0 认证缺陷阻断。**

- 核心链路（提交 → 状态机 → 传输 → 构建 → 运行 → 收集 → 下载）在代码与自动化测试层面齐备；无已确认的重复执行、结果完整性、分支污染、数据丢失类 P0 缺陷。旧分支污染问题（`selena_branch_changed`）已修复并有真实 Job attempt=4 证据。
- 不能判定「可上线」：P0 认证缺失（`X-Rsim-User` 在 `authentication_required=false` 下可伪造、同 owner 设备可互相冒充）+ 13 类故障注入场景的真实部署验收全部未完成。
- 因此当前只能称为「受信内网试用」，不满足 brief 7.4 的正式多租户门禁。

## 2. 交付物：14 份审计文档（docs/audits/）

| 任务 | 交付物 | 核心结论 |
|---|---|---|
| Task 0 | `2026-08-17-simulation-correctness-gap-matrix.md`（入口文档，358 行） | 三层状态模型 + 状态转移表 + 故障注入证据表 + P0/P1/P2 清单 + 逐链路结论 |
| Task A | `2026-08-17-product-scenario-matrix.md` | 场景矩阵；批量重试机制为 per-Stage（无 per-input 失败重试） |
| Task B | `2026-08-17-control-plane-state-machine-audit.md` | 状态机健全：commit→bind 重启窗口由 handoff 重放覆盖（3 处触发）；旧回调 fencing；cancel/success 竞态正确 |
| Task C | `2026-08-17-multi-user-security-audit.md` | `X-Rsim-User` 可伪造（P0）；跨 owner 查询/下载/传输在 owner 正确时被强制（404/403）；Bearer 基建已实现未启用 |
| Task D | `2026-08-17-selena-build-provenance-audit.md` | 5.3 决策矩阵大部分实现+测试；`max_candidates=512` 深层目录漏检已修复；若干矩阵行缺测试/字段 |
| Task E | `2026-08-17-connector-install-upgrade-audit.md` | 安装/升级/watchdog 静态检查通过；2 个 P2 缺口（配置损坏恢复仅覆盖缺失、无杀毒诊断） |
| Task F | `2026-08-17-web-sdk-parity-audit.md` | 单一合同成立（spec_hash/DAG/error envelope 一致）；base URL 约定正确；无固定仿真总时长；无 per-input 失败重试 API（P1） |
| Task G | `2026-08-17-data-transfer-batch-audit.md` | 断点续传/源变化/幂等实现+测试；Linux 不接收 MF4 正文成立；250+/磁盘满需真实验收 |
| Task H | `2026-08-17-partial-result-audit.md` | partial 只由 `selena_failed` 产生（6.3 边界全成立）；checkpoint/重启恢复正确；Cluster collect 不重复 submit；缺「只重试失败输入」能力（P1） |
| Task I | `2026-08-17-result-delivery-audit.md` | result.path 不可写不丢 server ZIP（先发布 ZIP 再 best-effort 本地交付）；无结果 GC/磁盘水位/告警（P1） |
| Task J | `2026-08-17-cluster-long-run-audit.md` | submission receipt + Config.cfg 反查防重复 submit；长队列不设总时长上限；批量 result.ini 不截断 |
| Task M | `2026-08-17-source-to-source-routing-audit.md` | 源×目标组合已实现+测试；`source_to_local` 稳定 503/`needs_input` 无静默绕路 |
| Task N | `2026-08-17-project-free-build-matrix.md` | 项目无关成立（V2 generic 流无硬编码项目依赖）；用户脚本为执行入口 |
| Task O | `2026-08-17-agent-user-journey-audit.md` | 单实例/身份保留/Web-SDK 复用同一 Agent 成立；真实 Windows 首装/升级/重启需验收 |

## 3. P0/P1/P2 风险分级（去重后，来源见各审计文档）

### P0 — 阻断正式多租户上线
- **P0-1 认证缺失（Task C，A/F/O 印证）**：`authentication_required=false` 时 `X-Rsim-User` 可伪造（`api_v1_fastapi.py:298-315`），`agent_id` 可冒充（`:327-337`）；7.4 门禁 0/6 达标 → 只能「受信内网试用」。启用 Bearer 为部署期变更（`serve-v1 --auth-file` 挂 `http-auth.json`），回滚=去掉 flag。

### P1 — 高优先，生产前必修（不阻断受信内网单用户）
- **P1-1 无「只重试失败输入」API/SDK/Web**（brief §6.3 明确交付物；Task A/F/H 一致定为 P1，Task H 视为其范围阻断项）：仅 Stage 级 `retry_stage`；`partial` Job 的 `run_simulation` 在 DB 为 succeeded，无法只重跑失败 MF4。per-failed-input 重试只存在于 legacy `cli/run.py`，不在 V2 Windows-full Agent。
- **P1-2 partial 在控制面 DB 被归一化为 failed**（`control_service.py:2453-2462`）：公开 API 通过 `_v1_status` 再导出 partial，但内部 DB/诊断/retry 入口按 failed 处理。（Task A 定 P1，Task H 定 P2，已记录分级不一致。）
- **P1-3 commit→bind 重启窗口缺直接回归测试**（Task B GAP-1/GAP-3）：无测试直接调用 `reconcile_stage_handoffs`；真实服务重启恢复需部署验收。
- **P1-4 结果归档无 GC/磁盘水位/告警**（Task I）：`retain_until` 只隐藏不回收；结果 catalog 无 `min_free_bytes` 检查；无过期/磁盘告警。
- **P1-5 保留期不对称**（Task I）：Cluster 结果默认永不过期（`cluster_stage_executor.py:1372-1378`）vs Windows-local 默认 30 天。
- **P1-6 真实端到端验收缺失**：跨全部场景（双 owner、250+ 批量、断网、重启、长 Cluster 队列、真实下载断流）需在部署环境完成。

### P2 — 建议改进（22 项摘要）
SDK 等待命名/退避（无 `wait_job()` 字面方法、`watch()` 固定 poll_interval）；下载 checksum mismatch 抛裸 `ValueError` 无稳定 code；result.path 不可写缺端到端故障注入测试；cancel→success 竞态缺测试；config 损坏恢复仅覆盖缺失未覆盖损坏；杀毒/Defender 诊断缺失；单实例 mutex 为 session 级；Web 重试按钮口径；250+/磁盘满/断网/UNC 未验收；Cluster 无幂等 request ID；blocked collector；retry payload 仅 local 路由；legacy-only 硬编码路径（`core/config.py`，V2 已绕过）；审计日志/认证 pairing；遗留 `web/` 目录；过时测试文件名（`test_stage_routing.py`、`test_agent_store_paths.py` 不存在）；build provenance 记录缺口（Task D G1-G7：fresh 误标 incremental、commit/源码指纹/工具链/脚本 checksum 未进决策、`clean_applied/clean_proof/incremental_reused` 字段缺失）。

## 4. 状态转移与正确性核心结论（Task 0 摘要）

- 三层状态（控制面/执行/业务结果）在代码中已拆分；所有恢复点有 SQLite 证据（WAL + `BEGIN IMMEDIATE`，`control_service.py`）。
- 判定原则成立：无固定 wall-clock 总时长；观察降级（`observing/reconnecting`）与终态（`failed`）分离；只有明确证明进程/外部 Job 终止才 final fail。
- partial 绝对边界（brief 6.3）逐条在代码中被阻止（`cli/agent.py:2698` `_is_partial_local_result` 要求所有失败项 error_code==`selena_failed`）。
- 详见入口文档 `docs/audits/2026-08-17-simulation-correctness-gap-matrix.md` 的状态转移表与故障注入证据表。

## 5. 已完成修改

无源码修改、无 commit。本次为纯审计，交付 14 份审计文档（见第 2 节）。此前的代码修复基线为 `20ba6b7`（branch: `codex/new-branch`），详见前序 handoffs（`2026-08-17-comprehensive-runtime-hardening.md`、`2026-08-17-non-engine-failure-audit.md`）。

## 6. 真实证据（当前机器可复现部分）

各审计文档均记录定向测试结果，总计约 **1000+ passed, 0 failed**（本机 Python 3.12 venv）：
- 控制面/状态机：120 passed（Task B）
- 构建/workspace/provenance：236 passed（Task D+N）
- Connector 安装/身份：81+140+12 passed（Task E+O）
- Web/SDK 合同：151+72 passed（Task F）
- 数据/传输：174 passed（Task G）
- partial/重试：77 passed（Task H）
- 结果交付：73+59+7 passed（Task I）
- Cluster：见 Task J 文档
- 源×目标：109 passed（Task M）
- 认证/owner：98 passed（Task C）

生产环境（Linux `10.190.171.44:8877`，release `/home/hoz2wx/radar-sim-d3de370`）的真实 Job `job_26028465ebeb` 已于前序 handoff 记录为最终 `succeeded`（10 Stage、批量 3/3、Manifest `succeeded`、ZIP SHA-256 校验一致）；本次审计未在部署环境新增 Job，部署级场景均标记「需要真实部署验收」。

## 7. 未完成事项（下一步动作）

1. **P0 认证（阻断）**：启用 Bearer（`serve-v1 --auth-file`），双 owner + 双设备 live 验收，确认伪造 `X-Rsim-User` 无效。责任：部署 owner。
2. **P1-1 失败输入重试**：为 V2 API/SDK/Web 增加「只重试失败输入」能力（brief 6.3 deliverable），并实测成功输入不重复消耗编译/仿真资源。
3. **P1-4/5 结果 GC/水位/告警/保留期对齐**：实现归档 GC、磁盘水位、告警，统一 Cluster/local 保留期。
4. **P1-3 重启窗口回归测试**：新增直接调用 `reconcile_stage_handoffs` 的测试；部署环境做真实服务重启恢复。
5. **Task K/L 真实闭环**：按 `docs/release-deployment.md` 做 release/系统/Connector 回滚验收；Web + SDK 同一 YAML 的真实 `spec_hash`/DAG/Manifest 对比。
6. **部署级故障注入**：250+ 批量、断网续传、磁盘满、UNC、长 Cluster 队列、结果目录晚到、真实下载断流、杀毒/权限阻断。

## 8. 回滚方法

- 本次为审计，无代码/部署变更，回滚不适用。若后续按 P1 实施代码修复：代码 release 回滚到 `20ba6b7`；systemd 恢复 `radar-sim-v1.service.bak-d3de370`；Connector 用官方 installer 降级；数据库无迁移（SQLite 向后兼容）。
- 若启用 Bearer 后需回滚：去掉 `--auth-file` 并 `systemctl --user restart radar-sim-v1.service`，回到受信内网模式。

## 9. 不要重复做的事情

- 不要重新解释 `job_26028465ebeb` 为仿真内部失败；它是框架链路证据，最终已 succeeded。
- 不要用固定等待时间判断仿真完成；用 heartbeat/进程存活/进度/文件证据/外部 terminal 状态。
- 不要为「UI 变绿」放宽校验；fail-closed。
- 不要重新审计已覆盖的代码路径（14 份文档已含 file:line 与测试证据）；新工作应聚焦第 7 节未完成事项。
- 不要用 modelfarm 子代理跑本类任务（`agents-state.json` 已将 general-purpose/Explore 指向 codingplan 模型）。
