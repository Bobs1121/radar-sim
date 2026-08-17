# radar-sim 仿真正确性缺口矩阵（Task 0：完整性/正确性总入口）

> 日期：2026-08-17
> 任务：执行任务书（`docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md`）第 11 节 **Task 0 / Task 0.1** 的汇总交付物。
> 性质：**纯汇总（SYNTHESIS ONLY）**——本文件不做任何代码修改、不提交任何 commit；全部结论来自 13 份并行审计文档（见文末索引），审计文档是证据的权威来源，本文件不重新推导代码结论。
> 用途：**给新 AI / 新审计者的第一入口文档**。阅读本文件即可获得「三层状态分层、状态转移表、故障注入场景覆盖、P0/P1/P2 风险、逐链路结论」的整体视图，再按需进入对应审计文档取 file:line 证据。
> 约定：证据一律引用审计文档（`docs/audits/<date>-xxx.md`）及其记录的 `file:line`；若审计文档之间结论/定级不一致，本文件在相应小节显式标注。

---

## 0. 阅读入口与方法说明

### 0.1 十三份审计文档索引（证据源）

| # | 审计文档 | 任务 | 结论一句 | 定向回归 |
|---|---|---|---|---|
| 1 | `2026-08-17-product-scenario-matrix.md` | Task A | 合同/字段/DAG/状态/错误信封与文档高度一致；**无「只重试失败输入」能力**；真实部署验收未做 | 267 passed |
| 2 | `2026-08-17-control-plane-state-machine-audit.md` | Task B | 状态机核心正确；**commit→bind 重启窗口、cancel→success 落盘两条竞态缺直接回归测试** | 120 passed |
| 3 | `2026-08-17-multi-user-security-audit.md` | Task C | **受信内网试用，不满足正式多租户**（no-auth `X-Rsim-User` 可伪造） | 98 passed |
| 4 | `2026-08-17-selena-build-provenance-audit.md` | Task D | 决策按真实构建边界；5.3 矩阵 9 行中 3 行实现+测试，其余为命名/记录/测试缺口（P1 非阻断） | 236 passed |
| 5 | `2026-08-17-connector-install-upgrade-audit.md` | Task E | 安装/升级/单实例代码完备；**config 损坏恢复仅覆盖「缺失」**、杀毒诊断缺失（P2） | 81+140+12 passed |
| 6 | `2026-08-17-web-sdk-parity-audit.md` | Task F | Web/SDK 同一 `/api/v1` 合同、同一 spec_hash/DAG；**无 `wait_job()` 方法名、无指数退避**；失败输入重试粒度不同 | 151+72 passed |
| 7 | `2026-08-17-data-transfer-batch-audit.md` | Task G | 「Linux 不接收 MF4 正文」不变量成立；断点/源变化/重复请求均实现+测试；磁盘满/250+ 需真实部署验收 | 174 passed |
| 8 | `2026-08-17-partial-result-audit.md` | Task H | partial/框架-引擎边界/checkpoint 实现+测试；**无「只重试失败输入」API（P1）**；partial 在 DB 被记 failed（P2） | 77 passed |
| 9 | `2026-08-17-result-delivery-audit.md` | Task I | result.path 不可写不丢 server ZIP 成立；**结果归档无 GC/磁盘水位/告警（P1）**；Cluster 结果永不过期（P1） | 73+59+7 passed |
| 10 | `2026-08-17-cluster-long-run-audit.md` | Task J | 提交去重/无固定总超时/结果不截断/O(n) 匹配成立；真实长队列需部署验收 | 79 passed,1 env-fail |
| 11 | `2026-08-17-source-to-source-routing-audit.md` | Task M | 四组合正文不经 Linux API 成立；`source_to_local` 稳定 fail-closed；端到端需部署验收 | 109 passed |
| 12 | `2026-08-17-project-free-build-matrix.md` | Task N | 项目无关编译成立；无影响 V2 的硬编码；legacy `core/config.py` 硬编码仅 legacy 路径使用（P2） | 236 passed（同 D） |
| 13 | `2026-08-17-agent-user-journey-audit.md` | Task O | 用户旅程代码级完备、无重复注册/双 Agent/身份漂移；config 损坏恢复缺口 + 多会话单实例边界（P2） | 81+140+12 passed |

### 0.2 本文件的判据约定

- **已实现 + 已测**：审计文档给出 `file:line` + 具体自动化测试名/结果。
- **文档声明**：文档有描述，但无代码/无测试对应，或代码缺失（如「只重试失败输入」）。
- **需真实部署验收**：代码/单测已有，但真实 Windows 首装、真实 Selena 编译、真实 Cluster、250+ 批量、双 owner 认证隔离、真实服务重启窗口等必须在目标部署环境实机验收，本机无法替代，**一律不得据此宣称已通过**。
- **Fail-closed**：任何一处证据缺失即标「未证实 / 需真实部署验收」，不假设成功。

### 0.3 审计文档之间的一致性说明（发现的不一致点）

1. **partial 的 DB/API 分层定级不一致**：Task A §2.2 与 §5 将「partial 在 DB 为 failed、API 投影为 partial」列为 **P1**；Task H GAP-B 将其列为 **P2**。对外 API 均正确显示 partial，分歧在「DB 直接消费者（诊断/重试/stale）是否会误读为全失败」的风险权重。本文件按**最高级 P1** 汇总并标注两处来源。
2. **「只重试失败输入」能力**：Task A / Task F / Task H 三处一致判 **P1**（任务书 §6.3 明确交付项缺失）；Task H 结论另称其为该任务范围内「阻断项」。三处定级一致，本文件统一为 P1-高优先。
3. **结果归档 GC/磁盘水位/告警**：Task I 判 P1 并明确「阻断正式长期上线，不阻断单次结果交付」。
4. **Task A 的 P0 表为「无」**：Task A §5 明确「本次审计未发现已确认的 P0 缺陷；但真实多租户认证未验收，若直接对外宣称支持多用户即构成 P0 风险」，把 P0 判定责任委托给 Task C；Task C 确认 P0（身份来源未认证）。两处不矛盾，本文件按 Task C 定级。

---

## 1. 完整状态分层（任务书 4A：控制面 / 执行 / 业务结果三层）

任务书 4A 明确「不能用一个 `status` 字段互相覆盖」三层状态。十三份审计文档共同确认：**radar-sim 在代码上确实实现了三层分离**，但存在 2 处「分层边界需要复核」的已知点（见 1.4）。

### 1.1 控制面状态（Job/Stage 是否被创建、queued、claimed、running、terminal、合法 attempt）

来源：Task B（`core/control_service.py` 为唯一真相，SQLite `_control.db`）。

**Stage 合法状态**（`control_service.py:19-24`，Task B §2）：
`queued / running / cancel_requested / cancelling(legacy) / succeeded / failed / cancelled / skipped / blocked`

**Job 级状态**（`_refresh_job_status_locked`，`control_service.py:3303-3378`，Task B §2.2）：
`queued / running / needs_input / succeeded / cancelled / failed`（`cancel_requested` 为中间态，公共 API 投影为 `cancelling`，`api_v1.py:3483-3484`，Task A §1.3）

**证据字段（SQLite）**：
- `tasks`：`status / assigned_agent_id / claimed_at / started_at / cancel_requested / attempt_count / dependencies_json / payload_json`（Task B §3.1/§3.4）
- `stage_attempts`：`(stage_id, attempt)` 唯一键 + `agent_id / status / error_json`（Task B §3.2）
- `agents`：`last_heartbeat / current_task_id`（Task B §3.5/§3.6）
- `jobs`：`status / result_json(manifest/summary) / idempotency_key`（Task B §3.3/§3.4）
- `job_events`：结构化审计（`stage.succeeded / stage.cancelled / stage.retry / stage.resumed` 等，Task B §3 多处）

**关键属性**：所有 claim/attempt/handoff/cancel/retry/stale 均在单个 `BEGIN IMMEDIATE` 事务内完成（Task B §1）。

### 1.2 执行状态（Agent 进程/Connector、Windows 子进程、Transfer、Cluster 外部 Job、结果复制是否仍有真实活动）

来源：任务书 4A.1/4A.2/4A.3 + Task B §7 + Task H §5 + Task G + Task J。

任务书 4A.1 的三档语义：
1. **`running/observing`**：Agent 心跳正常但暂时没有日志 → 继续等待，**不得判失败**。
2. **`reconnecting/unknown`**：Agent 心跳断开但 execution lease/PID/外部 Job 仍能证明执行 → 继续恢复观察。
3. **stale recovery**：只有 Agent 进程确认退出、子进程树已结束、没有可接管 lease 时才进入；必须先 fence 旧 attempt 再决定重试。

**各类执行活动的证据字段**：

| 执行活动 | 存活/进度证据 | 代码位置 | 来源审计 |
|---|---|---|---|
| Agent 在线 | `agents.last_heartbeat` ≤120s + exact `agent_id` + owner 匹配 + contract≥15 + status≠offline | `api_v1.py:2335-2380` | Task E/O |
| Windows 子进程 | `agent_local_runs` 的 `execution_token / execution_pid / running_since`；`_pid_alive` 判定；旧 PID 存活则 `LocalRunAlreadyExecuting`（观察既有执行者，不重复启动） | `agent_local_run.py:350-356,576-586` | Task H/B |
| 本地批量进度 | 逐输入 checkpoint（`outputs / error_count / error_code / diagnostics.items`），`checkpoint()` 保持 `running`，`execution_token` 写所有权校验 | `agent_local_run.py:402-428,493-496` | Task H |
| Transfer 进度 | 已发送 offset、最近成功 chunk 时间、最近心跳、`expires_at` 空闲租约（`TRANSFER_IDLE_LEASE_SECONDS=86400`）、重试次数；`.partial` 与最终文件分离 | `transfer_service.py:52,576-581`；`direct_transfer.py:816-935` | Task G |
| Cluster 外部 Job | submission receipt（`submission_receipt_json`）+ Config.cfg 目录唯一查询键 + `get_cluster_web_status`；collector 目录证据优先于状态页 | `cluster_runs.py:240-276`；`cluster_stage_executor.py:1189-1247,1417-1426` | Task J |
| 结果复制 | 结果目录 `result.ini`/MF4 存在性 + size probe；页面 finished 不能替代目录证据 | `cluster_stage_executor.py:1262-1364` | Task J |

### 1.3 业务结果状态（输入是否成功/失败/部分成功，Manifest/Checksum 是否完整，结果是否可下载）

来源：任务书 6.1/6.2/6.3 + Task A §1.3 + Task H §2 + Task I §3.4。

**Job/Manifest 终态**：`succeeded / partial / failed（归因 simulation 或 framework）/ cancelled / needs_input`

**逐输入状态**（任务书 6.1，`input_results`）：`queued/running/succeeded/failed/skipped/cancelled`，每项含 `index / input_relative_path / output_relative_path / checksum / returncode / error_code / retry count / 最后一次 attempt`（`agent_local_run.py:647-820`；`cluster_stage_executor.py:1501-1555`）。

**证据字段**：
- Manifest：`status` + `input_results` + `summary`（`succeeded_input_count / failed_input_count / total_input_count / file_count`）`cluster_stage_executor.py:1379-1397`
- `result_ref = result:sha256:<64hex>`（内容寻址、owner 绑定）`local_results.py:227-228,323-324`
- `archive_checksum`、`retain_until`（`local_results.py:75,123,344`）
- `delivery.status ∈ {delivered, already_present, failed, not_reported}`（`agent.py:2960-2998`）

**partial 的唯一合法来源**：只有真实 Selena per-input 混合成功/失败才产生 partial（`_is_partial_local_result` 要求失败项 error_code 全为 `selena_failed`，`cli/agent.py:2698-2730`；Cluster `_summary_is_partial` 需 `succeeded>0 and failed>0`，`cluster_stage_executor.py:1657-1665`）。

### 1.4 分层边界与已知不一致点

| # | 不一致点 | 分层含义 | 证据 | 风险 |
|---|---|---|---|---|
| 1 | partial 任务在 **DB Job/Stage = failed**，公共 API 投影为 **partial** | 控制面终态 vs 业务结果已分层，但 DB 直接消费者（诊断/重试/stale）看到 `failed` | `control_service.py:2453-2462,501-570`（finalize→failed + 启动期归一化）；`api_v1.py:3481-3501`（投影）；`test_api_v1_service.py:531-532` | **P1/P2（定级不一致见 0.3-1）**；需复核 stale/恢复逻辑不会因 DB `failed` 清理成功结果 |
| 2 | partial 的 `run_simulation` Stage 以 returncode 0 / status `succeeded` 提交 | 业务 partial 与控制面 Stage 成功分层（允许下游 collect/finalize 继续） | `cli/agent.py:2200-2201,2733-2736`；`cluster_stage_executor.py:1657` | 正确实现；副作用是 `retry_stage` 只接受 failed/cancelled，无法重跑 partial 的失败输入（见 P1-2） |
| 3 | 阶段 `blocked` → Job `needs_input` | 内部态映射到公共态，属实现细节 | `api_v1.py:3497-3500` | 无风险，Task A §1.3 |
| 4 | `cancel_requested` → 公共 `cancelling`（中间态） | 文档未列中间态，属实现细节 | `api_v1.py:3483-3484` | 无风险，Task A §1.3 |

---

## 2. 状态转移表（卡住 / 未知 / 恢复 / 失败 / 成功）

### 2.1 控制面 Stage / Job 转移表

来源：Task B §2.1/§2.2/§7。列：当前状态 → 触发/条件 → 下一状态 → 证据要求 → 代码位置 → 恢复类别。

| 当前状态 | 触发 / 条件 | 下一状态 | 证据要求 | 代码位置 | 类别 |
|---|---|---|---|---|---|
| queued | `claim_next_task` 原子 CAS | running | 创建 `stage_attempts` 行、`attempt_count+1` | `control_service.py:1839-2045` | 正常 |
| queued | `cancel_job`（queued/blocked） | cancelled | 未开始直接终态 | `control_service.py:2611-2628` | 用户动作 |
| queued | 上游失败 `_cancel_remaining_tasks_locked` | cancelled | `error.code=UPSTREAM_FAILED` | `control_service.py:3528-3548` | 级联 |
| queued | `reclaim_stale_tasks`（stale 且 attempt<max） | queued（重排） | 清 `assigned_agent_id/claimed_at/started_at`，attempt 记 `failed(AGENT_STALE)` | `control_service.py:2214-2247` | **自动恢复** |
| running | `submit_task_result`(success) | succeeded | 写 `result_json/output_ref` | `control_service.py:2509-2527` | 正常 |
| running | `submit_task_result`(fail) | failed | 并 `_cancel_remaining_tasks_locked` | 同上 | 失败 |
| running | cancel_requested + `status∈{"",cancelled}` / returncode!=0 / failed | cancelled | `tasks.cancel_requested` + 事件 | `control_service.py:3409-3420` | 用户动作 |
| running | cancel_requested + `status=succeeded` | **succeeded** | 真实成功不被取消抹掉，证据保留 | `control_service.py:3412-3413` | **自动恢复（竞态 GAP-2）** |
| running | `reclaim_stale_tasks` + cancel_requested/cancelling | cancelled | 死 Agent 无法回执时 stale 兜底终态 | `control_service.py:2134-2170` | 自动恢复 |
| running | `reclaim_stale_tasks` + attempt≥max_attempts | failed | 并取消下游 | `control_service.py:2171-2213` | **需用户 retry** |
| running（reclaim 后未新 claim） | 旧 attempt 结果到达 `can_adopt_reclaimed_attempt` | running（**同一 attempt**，不新增） | 回调 attempt==当前 attempt_count、agent 一致、原 error 为 AGENT_STALE | `control_service.py:2355-2413` | **自动恢复，不重复执行** |
| running（已新 claim） | 旧 attempt 结果到达 | 拒绝 `stale_task_result` | attempt 数不匹配 | `control_service.py:2439-2444` | **自动拒绝（fencing）** |
| cancel_requested/cancelling | `reclaim_stale_tasks` | cancelled | legacy 中间态兜底 | `control_service.py:2134-2170` | 自动恢复 |
| failed / cancelled | `retry_stage` | queued | 仅允许 failed/cancelled；重置依赖闭包下游 | `control_service.py:2990-3007,3711-3756` | **需用户 retry** |
| — | 创建时 | blocked | `needs_input` 是 Job 级投影 | `control_service.py:1727-1764`；`api_v1.py:3497-3500` | 等待输入 |

**Job 级汇总（`_refresh_job_status_locked`，`control_service.py:3303-3378`）**：任一 running→running；任一 queued→queued；任一 blocked→needs_input；全部∈{succeeded,skipped}→succeeded；cancel_requested 且无 failed 且全终态→cancelled；全部∈{cancelled,skipped}→cancelled；其余（含任一 failed）→failed。

### 2.2 「卡住 / 未知 / 恢复」语义表（任务书 4A.1，非状态机转移而是观察语义）

| 观察状态 | 触发条件 | 处置 | 不得做的动作 | 证据要求 | 来源 |
|---|---|---|---|---|---|
| `running/observing` | Agent 心跳正常但暂时无日志 | 继续等待 | **不能**用「超过 N 分钟没有日志」判失败 | 心跳 + execution lease/PID | 任务书 4A.1；Task B §7-1 |
| `reconnecting/unknown` | 心跳断开但 lease/PID/外部 Job 仍证明执行 | 继续恢复观察 | 不能把控制面不可达、Agent 离线、Selena 非零混成同一个 failed | execution_token/PID；Cluster 外部 job | 任务书 4A.1；Task B §7-5/9 |
| stale recovery | 进程确认退出 + 子进程树结束 + 无可接管 lease | 先 fence 旧 attempt 再决定重试 | 旧 callback 晚到不能重复启动 | `_pid_alive=False` + reclaim | 任务书 4A.1；Task H §5.2 |
| Transfer stalled/observing | 长时间无进度 | 先进入 observing/告警；**需 deployment policy 才转 retryable failure** | 不能硬编码仿真/传输总时长 | 空闲租约 `expires_at`、chunk 时间 | 任务书 4A.2；Task G §3.5 |
| Cluster 状态页不可达 | `_is_transient_cluster_gateway_error` | 指数退避后 continue，观察降级 | 不退化为 failed；不设控制面 wall-clock deadline | 目录证据优先于页面 | 任务书 4A.3；Task J §3 |
| 结果目录晚到 | 页面 finished 但 result.ini 未齐 | 保持 running，`terminal_status_without_result_streak` 告警继续等 | 不把复制延迟判为失败 | 受控共享结果目录 | 任务书 4A.3；Task J §3-4 |
| cancel | 用户动作 | 终止当前 attempt，保留已固化结果 | 不得被后台 stale 逻辑误转成普通 failure | `cancel_requested` + 终态事件 | 任务书 4A.3；Task B §3.3 |

### 2.3 非法转移清单（必须被拒绝）

来源：Task B §2.3。

| 非法转移 | 拒绝点 |
|---|---|
| queued → succeeded/failed（未 claim 直接提交结果） | `TaskResultRejected("stale_task_result")`，`control_service.py:2346-2418` |
| 终态 → 任意状态（重复回调） | `task already completed`；transfer 重复 manifest 幂等忽略（`control_service.py:1486-1493`） |
| 旧 attempt 结果覆盖新 attempt | attempt 数不匹配 → `stale_task_result`（`control_service.py:2439-2444`） |
| 其他 Agent 提交结果 | `agent_task_mismatch`（`control_service.py:2419-2427`） |
| heartbeat 认领不属于自己的任务 | `agent_heartbeat_task_mismatch`（`control_service.py:799-810`） |
| 成功 Stage 重复绑定 / 换 Agent | `bind_stage_to_agent` 仅接受 queued + CAS（`control_service.py:873-901`） |
| 非 failed/cancelled Stage 直接 retry | `only failed/cancelled stages can be retried`（`control_service.py:2895-2896`） |
| cancel 掩盖真实 failed Stage | `_refresh_job_status_locked`：有 failed 则 Job=failed（`control_service.py:3346-3348`） |
| 依赖未就绪即 claim | `_task_is_ready_to_claim_locked`：依赖必须全∈{succeeded,skipped}（`control_service.py:3380-3397`） |

---

## 3. 故障注入场景证据表（任务书 Task 0 要求制造的场景）

列：场景 → 当前代码是否覆盖（file:line）→ 自动化测试名/结果 → 是否需真实部署验收。

| # | 故障注入场景 | 代码覆盖（file:line） | 自动化测试（结果） | 需真实部署验收 |
|---|---|---|---|---|
| 1 | **Agent 心跳停止** | `reclaim_stale_tasks`（`control_service.py:2060-2273`）；`agents.last_heartbeat`；心跳归属校验（`control_service.py:799-810`）；本地 PID 存活判定（`agent_local_run.py:576-586`） | `test_reclaim.py` 全 10 条 + `test_control_stages.py` 4 条：`test_reclaim_requeues_task_when_agent_silent` / `test_reclaim_fails_task_after_max_attempts` / `test_reclaim_unlimited_attempts`（120 passed，Task B §7） | **是**（真实服务/Agent 重启窗口，Task B GAP-3） |
| 2 | **日志停止但进程存活** | 心跳正常→`running/observing` 继续等待（任务书 4A.1）；本地旧 PID 存活→`LocalRunAlreadyExecuting` 观察不重复启动（`agent_local_run.py:350-356`） | `test_duplicate_connector_process_observes_one_local_execution`（`test_agent_local_run.py:238`，Task H） | **是**（真实进程存活无日志） |
| 3 | **Transfer 无进度** | 空闲租约 86400s + `report_progress` 续租（`transfer_service.py:52,576-581`）；.partial 分离 + 校验 offset 续传（`direct_transfer.py:816-935`）；过期带 partial 会话续租（`dataset_store.py:320-342`、`artifact_store.py:554-575`） | `test_active_transfer_renews_idle_lease_instead_of_using_wall_clock_deadline`、`test_expired_active_session_with_partial_file_is_renewed`（Task G §3.5） | **是**（真实 TCP 中断；转 retryable failure 需 deployment policy，任务书 4A.2） |
| 4 | **Cluster 状态页不可达** | `_is_transient_cluster_gateway_error` 指数退避 continue（`cluster_stage_executor.py:2165-2197,1230-1247`）；目录证据优先（`cluster_stage_executor.py:1204`） | `test_collect_gateway_outage_keeps_observing_until_shared_result`（虚拟时钟 +7200s，`test_cluster_stage_executor.py:234`）；`test_collect_uses_complete_shared_results_when_status_gateway_is_unreachable`（:308） | **是**（真实持续宕机数小时，Task J §6） |
| 5 | **结果目录晚到** | `terminal_status_without_result_streak` 告警继续等（`cluster_stage_executor.py:1282-1298`）；`result.ini` 覆盖页面（`cluster_stage_executor.py:1352-1364`） | `test_collect_waits_for_result_ini_after_official_completion`（+7200s，`test_cluster_stage_executor.py:359`）；`test_collect_uses_result_ini_when_official_page_has_no_tasks`（:488）；`test_collect_overrides_web_succeeded_when_result_ini_reports_failure`（:531） | **是** |
| 6 | **服务重启** | SQLite 唯一真相；commit→bind 窗口由三处幂等重放兜底：`reconcile_stage_handoffs`（`control_service.py:3066-3122`），触发点 `api_v1.py:2440-2448` / `cli/server.py:631` / `control_http.py:266`；Cluster receipt 先于状态落库；TransferPlan/结果/上传会话 SQLite 持久；outbox at-least-once | `test_plan_metadata_round_trips_through_sqlite_restart`；`test_durable_idempotency_survives_new_api_service_instance`（`test_api_v1_service.py:1173`）；`test_v1_idempotency_replay_does_not_call_source_provider_again`（:1523）；`test_result_outbox_survives_store_reopen`（`test_agent_result_outbox.py:10`） | **是**；**GAP-1（P1 测试缺口）**：`reconcile_stage_handoffs` 无直接回归测试，仅同步 handoff 被覆盖（Task B §5） |
| 7 | **旧 callback 晚到** | `can_adopt_reclaimed_attempt` 采纳同一 attempt（不新增 attempt，不重复执行）；新 claim 后则 fencing 拒绝（`control_service.py:2355-2413,2439-2444`） | `test_result_from_reclaimed_attempt_is_adopted_before_new_claim`（`test_control_service.py:557`）；`test_late_result_from_reclaimed_attempt_cannot_complete_new_attempt`（:521）；`test_submit_task_result_rejects_different_agent`（:500） | **是**（真实网络延迟/长任务） |
| 8 | **result.path 不可写** | `catalog.publish` 先于本地交付（`cli/agent.py:2863→2871`）；`_materialize_local_result` 捕获 `ResultDeliveryError` 返回稳定状态不抛出（`cli/agent.py:2769-2826`）；server ZIP 保留 | `test_materialize_is_atomic_idempotent_and_preserves_manifest`（`test_result_delivery.py:57`）——**机制性覆盖；缺端到端故障注入回归**（Task I GAP-5，P2） | **是**（真实不可写目录 + ZIP 仍可下载，Task I 未验收项 4） |
| 9 | **cancel 与 success 竞态** | `_resolve_task_result_status`：cancel_requested + succeeded→succeeded（证据保留）；+ failed/returncode!=0→cancelled（`control_service.py:3399-3420`）；Job 级不掩盖真实失败（`control_service.py:3338-3348`） | `test_cancel_preserves_skipped_stage_and_finishes_running_cancelled`（`test_control_stages.py:155`）；`test_cancelled_job_with_succeeded_upstream_is_not_reported_failed`（:363）；`test_cancel_running_job_sets_cancel_requested_and_final_cancelled`（`test_control_service.py:139`）；`test_cancellation_is_terminal_and_does_not_call_runner`（`test_agent_local_run.py:352`） | **是**；**GAP-2（P2 测试缺口）**：无直接断言「cancel_requested 后 success 落盘→Stage=succeeded」的测试（Task B §5） |
| 10 | **断网** | SDK 状态变更请求不自动重试（POST attempts==1，`client.py:1221`）；Agent poll 退避重连（`cli/agent.py:313-354`）；outbox 缓冲冲刷（`agent_result_outbox.py`）；Cluster gateway 退避；Transfer 校验 offset 续传 | `test_sdk_does_not_retry_transport_errors_for_state_changing_requests`（`test_sdk.py:1392`）；`test_control_client_queues_result_when_control_plane_is_down_then_flushes`（`test_agent_result_outbox.py:32`）；`test_sdk.py:902,926,954` | **是**（真实断网 + 服务/Connector 重启组合窗口，Task G §3.7） |
| 11 | **重启（Connector/Agent）** | 逐输入 checkpoint 只恢复未完成输入（`agent_local_run.py:603-629,647-649`）；死 PID lease 接管（`agent_local_run.py:576-586`）；输出被删/改则重跑该条（`agent_local_run.py:616-623`） | `test_recovery_resumes_after_durable_batch_checkpoint`（`test_agent_local_run.py:281`，重启后 `executed==[2]`）；`test_dead_connector_execution_lock_is_recoverable`（:265）；`test_same_connector_reregistration_preserves_running_assignment`（`test_control_service.py:173`） | **是**（真实批次中途重启） |
| 12 | **重复提交** | 幂等三要素 `(owner, idempotency_key, request_hash)` 唯一索引（`control_service.py:452-454`）；`_raise_idempotency_conflict` 409（`api_v1.py:3592-3599`）；Cluster 外部提交去重：receipt→Config.cfg 反查→才 submit（`cluster_stage_executor.py:1056-1153`） | `test_sdk_validate_and_submit_run_share_v2_hash_with_web_json`（`test_sdk.py:57`）；`test_idempotency_is_scoped_by_owner`（`test_api_v1_service.py:1238`）；`test_durable_idempotency_survives_new_api_service_instance`（:1173）；`test_sdk.py:1392`（attempts==1）；`test_submit_adopts_durable_receipt_without_second_external_submission`（`test_cluster_stage_executor.py:198`，外部 submit 被禁止二次调用） | 代码+测试通过；真实 live 幂等需部署验收 |
| 13 | **多用户并发** | owner 限定全链路：Job `_get_owned_job` 404（`api_v1.py:2933-2952`）；Transfer `_owned_plan` 403（`transfer_service.py:679-689`）；Result `_row` 404（`local_results.py:412-426`）；Agent 注册强制 owner（`api_v1.py:2267-2327`）；build lock 按 workspace（`build_lock.py`） | `test_bearer_auth_derives_owner_and_ignores_spoofed_user_header`（`test_api_v1_fastapi.py:51`）；`test_agent_bearer_auth_derives_identity_and_rejects_body_spoof`（:72）；`test_idempotency_is_scoped_by_owner`；`test_connector_agent_id_cannot_be_silently_rebound_to_another_owner`（:394）；Task C 合计 98 passed | **是**；且 **no-auth 下身份本身可伪造（P0）**，双 owner 真实集成必须认证启用后验收 |

**结论**：13 个故障注入场景中，**12 个已有代码路径 + 自动化测试背书**，其中「服务重启」「result.path 不可写」「cancel→success 竞态」存在**测试缺口（GAP-1 P1 / GAP-5 P2 / GAP-2 P2）**，详见 Task B/I；**全部 13 个场景的“真实部署验收”均未完成**，不能据此宣称已通过（fail-closed）。

---

## 4. P0 / P1 / P2 风险清单（跨 13 文档去重汇总）

> 定级口径：P0 = 阻断正式多用户上线（owner/auth、结果完整性、重复执行、分支污染、数据丢失）；P1 = 高优先、非阻断但必须在生产前修复；P2 = 建议改进。每项标注来源审计文档；同一风险多处出现时取最高级并列出全部出处。

### 4.1 P0（阻断正式多用户上线）

| ID | 风险 | 说明 | 来源 |
|---|---|---|---|
| P0-1 | **认证缺失 / owner 可伪造 / 同 owner 多设备可互相冒充** | 生产部署 `authentication_required=false`（`cli/server.py:346-360,643` loopback no-auth），owner 来自客户端可填写的 `X-Rsim-User`（`api_v1_fastapi.py:306-315`），无签名/会话；`user-<id>` 低熵可猜测；no-auth 下 `agent_identity` 放行任意声明的 agent_id（`api_v1_fastapi.py:327-330`）。资源授权层健全（跨 owner 404/403），但**身份来源可伪造**。启用 Bearer（`--auth-file`）后伪造头被忽略（单测 `test_api_v1_fastapi.py:51-70,72-128` 通过），但**未在真实双 owner 部署启用/验收**。门禁 7.4：0 项完全满足正式多租户。 | Task C（§2/§4/§8）；Task A §4.2；Task F P1；Task O §4 |

> 说明：按任务书 §13 结论三选一，P0-1 使「正式多用户上线」被阻断；但对「受信内网单用户/测试部署」不阻断，因此总结论为**有条件上线**（详见第 6 节）。其余文档标注为 P0 的项均为「必须已实现且已测」的核心要求（正文不经 Linux、source_to_local 不静默绕路、partial 边界），当前均成立，不构成开放风险。

### 4.2 P1（高优先、非阻断，但必须在生产前修复 / 交付）

| ID | 风险 | 说明 | 来源 |
|---|---|---|---|
| P1-1 | **无「只重试失败输入」API/SDK/Web 能力** | 任务书 §6.3 明确交付项。当前仅 Stage 级 `retry_stage`（`control_service.py:2885`、`api_v1_fastapi.py:682`、SDK `retry_stage`）；partial 任务的 `run_simulation` Stage 在 DB 为 `succeeded`，`retry_stage` 只接受 failed/cancelled，**无法重跑失败输入**；Web 逐条结果区无重试按钮（`app.js:1054-1072`）。「成功输入不重复执行」仅靠 Connector 重启 checkpoint 保证（故障恢复语义），非用户主动能力。 | Task A §2.1；Task F GAP-2；Task H GAP-A（Task H 称其为范围内阻断项） |
| P1-2 | **partial 在控制面 DB 被归一化为 failed** | `submit_task_result`（`control_service.py:2453-2462`）与 `_reconcile_failed_manifest_jobs_locked`（`control_service.py:501-570`）把 finalize task/job 置 `failed`；public API 靠 `_v1_status` 重派生 `partial`。DB 直接消费者（诊断/重试/stale）看到 failed，需复核不会被误清理成功结果。定级不一致：Task A=P1，Task H=P2（见 0.3-1）。 | Task A §2.2；Task H GAP-B；Task B §3.4 |
| P1-3 | **commit→bind 重启窗口无直接回归测试** | `reconcile_stage_handoffs`（`control_service.py:3066-3122`）真实存在且三处幂等重放，但 `tests/` 无直接调用；真实 Linux 服务重启本机无法复现。 | Task B GAP-1/GAP-3 |
| P1-4 | **结果归档无 GC、无磁盘水位、无告警** | `retain_until` 只在读取层「隐藏」过期结果，磁盘 ZIP 与 DB 行只增不减（`local_results.py:344-361`）；结果存储无 `dataset_store` 那种 `min_free_bytes` 水位（`dataset_store.py:285-287`）；`cli/server.py` 维护线程只做 stale reclaim、无告警。长期运行会导致磁盘写满→归档失败→数据丢失风险。Task I 明确「阻断正式长期上线，不阻断单次交付」。 | Task I GAP-1/2/3（§4.2/§8） |
| P1-5 | **Cluster 结果默认永不过期，与 Windows 本地默认 30 天不一致** | `cluster_stage_executor.py:1372-1378` 的 `publish(...)` 未传 `retain_until`（默认 0=永不过期）；本地 `retain_days` 默认 30（`core/spec/model.py:150`）。retention 策略不统一、无集中配置入口。 | Task I GAP-4（§4.2/§8） |
| P1-6 | **真实端到端验收缺失（跨场景）** | 真实 Windows 首装/升级/断网/重启、真实 Selena 编译（含 clean 真实执行）、真实 Cluster 提交/排队/收集、250+ 批量、真实长任务取消/partial、真实结果大文件下载、真实断网续传、真实服务重启窗口——全部「需真实部署验收」，本机无法替代。此为贯穿性 P1（非代码缺陷，但阻断「宣称已通过」）。 | Task A §4.3；Task D §8；Task E §6；Task G §7；Task H §7.2；Task I §7；Task J §6；Task M §4.3 |

### 4.3 P2（建议改进 / 待真实部署补证）

| ID | 风险 | 说明 | 来源 |
|---|---|---|---|
| P2-1 | SDK 等待命名与退避：无 `wait_job()` 方法名（仅 diagnosis `action.type`）；`watch()` 固定 `poll_interval` 无指数退避 | 功能上「cursor 优先 + 轮询兜底 + 无固定仿真总时长」成立；建议对齐文档命名、评估加退避 | Task F GAP-1 |
| P2-2 | SDK `wait()`/`watch()` 默认 600s 本地轮询边界易被误读为「固定总时长」 | 是调用方观察窗口、非服务端；需文档/示例澄清 | Task A §2.4 |
| P2-3 | 下载 checksum mismatch 无稳定错误码 + 结果下载无专属测试 | `client.py:923` 抛裸 `ValueError`；config-asset 有测试（`test_sdk.py:640`）但结果下载没有 | Task I GAP-5 |
| P2-4 | result.path 不可写缺端到端故障注入回归测试 | 机制由「publish 先于 delivery + delivery 失败被捕获」保证（`agent.py:2863→2871,2769-2826`），单点测试覆盖，缺端到端证明 | Task I GAP-5 |
| P2-5 | cancel→success 落盘分支无直接回归测试（`_resolve_task_result_status` 3412-3413） | 现有 cancel 测试用 returncode=-15（→cancelled），无「success 落盘→Stage=succeeded」断言 | Task B GAP-2 |
| P2-6 | Connector 配置损坏（存在但 JSON 非法）不自动从 backup 恢复 | `start_windows.ps1:19-26` / `watch_windows_connector.ps1:44-53` 只处理 `Test-Path` 为假（文件缺失）；`ConvertFrom-Json` 失败不回退 | Task E §4.4；Task O §1 |
| P2-7 | 缺企业杀毒/Defender 拦截诊断 | 全脚本无杀毒诊断；下载/解压/pip 被拦只有通用网络错误 | Task E §4.5 |
| P2-8 | 单实例互斥体为 session 级（`Local\RadarSimConnector-<SID>`），多 RDP/快速用户切换理论上可并存双 supervisor | watchdog `Find-ConnectorSupervisor` 全机扫描缓解但不根除；常规单登录用户不受影响 | Task E §4.1；Task O §1 |
| P2-9 | Web 重试按钮条件与 API `available_actions` 口径不一 | `app.js:1232-1234`（failed/cancelled 都显示）vs `api_v1.py:3469-3478`（仅 failed 下发 action） | Task A §2.3 |
| P2-10 | 目标磁盘满路径未真实验证 / 250+ 文件直接传输 + Manifest 对账未验收 / 真实断网/重启窗口 / 真实 UNC+Linux probe 双命名空间 | 代码有配额/空闲预检（`dataset_store.py:285-287`）、`max_files=20000`；全部需真实部署验收 | Task G §3.8/§3.3/§3.9/§8 |
| P2-11 | Cluster 提交接口无幂等 request ID，收据窗口仍有残余分布式边界 | receipt + Config.cfg 反查已收口到最小集合；彻底消除需 Cluster 接口提供幂等 request ID | Task J §7 |
| P2-12 | 共享结果目录永久不可达缺 inactivity 告警；是否自动转 retryable failure 需部署策略 | collector 保持观察（非终态）；不能硬编码仿真总时长 | Task J §7 |
| P2-13 | 阻塞式 collector 长期占住 worker | 演进为异步 reconciler（DB lease + 短轮次）为后续 Sprint 项 | Task J §7 |
| P2-14 | retry payload 重建仅覆盖 local 路由（`control_service.py:2920-2989`） | Cluster 侧 finalizer/collect retry 不重建 partial 输入列表 | Task H GAP-C |
| P2-15 | legacy-only 硬编码残留（`core/config.py:400,919-928,953,1058,1065`：`/apl/byd/bindings`、`ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR`、`R2D2.py`） | 仅 legacy CLI/非 generic 路径使用，V2 generic 用 `generic_only=True` 绕开且有测试证明；建议清理或标注 legacy-only | Task N §1/§3 |
| P2-16 | 下载/取消/重试审计日志缺专用验收 | 有结构化 Job 事件（`api_v1.py:2227+`），但无「下载审计」专用记录与验收 | Task C §4/§8 |
| P2-17 | `create_http_auth_config` 无 CLI/文档化生成入口 | 仅 Python API 与测试使用 | Task C §8 |
| P2-18 | 认证启用的 pairing 流程未部署 | 启用 Bearer 后一键安装端点返回 `connector_pairing_required` 409，需先部署短时效 pairing | Task C §6.1 |
| P2-19 | `web/` 目录为 7 月遗留 V1 前端（测试指向 `radar_sim_web/static/app.js`） | 建议清理避免误读；不影响测试 | Task M §5 |
| P2-20 | 文档过时文件名 `tests/test_stage_routing.py`（brief Task M 命令）不存在 | 已用 `test_control_plane_transfer_api.py`/`test_transfer_service.py` 覆盖同一职责 | Task M §5 |
| P2-21 | 本地 venv 缺 `asammdf` 导致 1 个环境类测试失败 | `test_cluster_check_allows_xmlrpc_without_python2`，非代码回归；部署机需 `pip install .[v5-server]` | Task J §7 |
| P2-22 | fresh 构建被标为 `incremental`、commit/toolchain/build script checksum 不参与决策、`clean_applied`/`incremental_reused` 未记录、部分 5.2 字段未持久化、矩阵多行缺直接测试 | Task D G1-G7（构建 provenance 记录的合规性缺口，非安全漏洞；旧分支污染已知故障不会复发） | Task D §7；Task N §2 |

---

## 5. 每条链路的最终结论（逐阶段）

> 每阶段回答四个问题：**状态从哪里来 / 什么证据证明 / 断线重启后如何恢复 / 什么条件才允许 terminal**。标记：✅=已实现+已测；📄=文档声明；⚠=需真实部署验收（或存在缺口）。

### 5.1 Web/SDK 提交（submit/创建）
- **状态来源**：`POST /api/v1/run-jobs`，幂等三要素 `(owner, idempotency_key, request_hash)` 唯一索引；返回 Job id + spec_hash + 首状态。
- **证据**：`spec_hash == config.fingerprint()`（Web 与 SDK 同一 `UserRunConfig 2.0`，同一 10 阶段 DAG）；owner 来自 `X-Rsim-User`（no-auth）或 Bearer。
- **恢复**：SDK 状态变更请求不自动重试（POST attempts==1，网络错误不重复提交）；服务重启后幂等键仍有效（durable idempotency 测试）。
- **Terminal 条件**：创建即固化 SQLite（`create_job`，`control_service.py:1727-1764`）；冲突 409 `idempotency_conflict`。
- **标记**：✅ 已实现+已测；⚠ owner 认证为 no-auth（P0-1）；⚠ 真实 live server 端到端需部署验收。

### 5.2 resolve_spec
- **状态来源**：Job 创建后 scheduler claim→Agent 执行；规范化 YAML、选 route、识别 workspace/data/assets。
- **证据**：canonical spec、spec_hash、binding/路由证据（`selected_execution_target`，`stage_routing.py:8-24`）。
- **恢复**：Stage 结果 commit 后 `advance_after_stage_result`（同步）+ `reconcile_stage_handoffs`（三处幂等重放）兜底 commit→bind 重启窗口。
- **Terminal 条件**：succeeded；配置问题→`needs_input`（不启动编译）。
- **标记**：✅ 已实现+已测；⚠ commit→bind 窗口直接测试缺失（P1-3）；⚠ 真实服务重启需部署验收。

### 5.3 environment_check
- **状态来源**：Agent 执行；Connector/VS/Python/Perl/CMake/脚本/输出根/Runtime readiness + `incremental_build_policy` 结构化检查（`environment_snapshot.py:436-487`）。
- **证据**：path-free readiness checks、script checksum、build policy code（`selena_full_rebuild_required` / `selena_clean_commands_suppressed` / `selena_clean_explicitly_allowed`）。
- **恢复**：环境缺失可修复后重试；不搬大文件。
- **Terminal 条件**：succeeded / needs_input / blocked（需 full 但无 clean 命令时阻断，`environment_snapshot.py:446-449`）。
- **标记**：✅ 已实现+已测；⚠ 真实 Windows 环境（无 Python/VS/代理/杀毒）需部署验收（Task E/O）。

### 5.4 prepare_data
- **状态来源**：Connector/SDK 直传或共享零拷贝；Linux 只接收 plan/progress/manifest 元数据（`_apply_direct_transfer_stage` 注释，`api_v1.py:775-791`；`transfer_service.py:249-250`「no file content」）。
- **证据**：完整文件集合 + size/checksum + transfer manifest；`prepare_data` 必须所有 required roles resolved 才成功（`test_manifest_roles_complete_stage_only_after_all_resources`）。
- **恢复**：断点续传（校验 offset、`.partial` 分离、原子 rename）；源变化丢弃 partial；plan/manifest/chunk 三级幂等；SQLite 持久；空闲租约 86400s。
- **Terminal 条件**：全部 role resolved；**未完整不得进入 preflight/run**（任务书 4A.2）。
- **标记**：✅ 已实现+已测（Task G 174 passed，含「Linux 不接收正文」契约测试）；⚠ 磁盘满/250+/真实断网/真实 UNC 需部署验收（P2-10）。

### 5.5 build_selena
- **状态来源**：`prepare_selena_build`→`_branch_rebuild_policy`（比较 branch/build_mode/entrypoint checksum，从不读项目名，`agent_build_stage.py:266-336`）→ full/incremental → `adapt_build_script_for_incremental` 恢复 clean 命令 → 执行 → `finish_selena_build` → `stage_runtime_bundle_from_build`。
- **证据**：`build_policy.mode`（`full`/`incremental`）+ `reason`（如 `selena_branch_changed`）；执行前重算脚本 checksum（`verify_prepared_build`，`agent_build_stage.py:968-977`）；产物 checksum；Bundle manifest（branch/commit/toolchain）；`runtime_bundle_leases` 按 `(project, workspace_binding_id)` 隔离。
- **恢复**：`WorkspaceBuildLock` 按 workspace_root 串行（`build_lock.py`，崩溃 OS 自动释放锁）；build 失败→`retry_stage(build)` 不重跑无关；full 但无 clean 命令→blocked（fail-closed）。
- **Terminal 条件**：产物确认 succeeded / failed / blocked（脚本被改或 clean 语义无法识别）。
- **标记**：✅ 已实现+已测（Task D/N 236 passed，含 `selena_branch_changed` full 回归与深层 `selena.exe` 漏检修复 `max_candidates=512`）；⚠ 真实 Windows 脚本编译、`clean_applied` 结构化字段缺失、fresh/incremental 命名、commit/toolchain 不参与决策（P2-22/P1 见 0.3）需部署验收。

### 5.6 register_artifact
- **状态来源**：本机 Bundle 注册（`local_runtime_registration`）或 Cluster 直传（`direct_transfer` 域），`stage_routing.py:27-42`。
- **证据**：stable Bundle ID / transfer ref；不能重复提交已成功 Bundle（幂等）。
- **恢复**：旧 register_artifact 无 `dispatch_scope` → retry 时自动修复 payload（`control_service.py:2902-2918`）。
- **Terminal 条件**：succeeded / failed。
- **标记**：✅ 已实现+已测；⚠ 真实 Bundle 上传/下载与跨 Job 复用需部署验收。

### 5.7 preflight
- **状态来源**：检查完整数据、Runtime、MatFilter、Adapter、执行权限；`_verify_runtime_locations` / `execute_cluster_preflight`。
- **证据**：all roles resolved、execution plan；Runtime Bundle 校验失败即拒绝，不产生 partial。
- **恢复**：不通过不能启动 Selena；修复后从最近安全 Stage 重试。
- **Terminal 条件**：succeeded / failed / needs_input（数据不完整）。
- **标记**：✅ 已实现+已测。

### 5.8 run_simulation
- **状态来源**：本地 `execute_local_run`（逐输入 checkpoint）或 Cluster `execute_cluster_submit`（receipt）→ `execute_cluster_collect`（观察）。
- **证据**：execution token/PID/start time/heartbeat/log/子进程树（本地）；submission receipt + external job id + Config.cfg 唯一查询键（Cluster）；逐输入证据。
- **恢复**：本地重启→只恢复未完成输入（`completed_indices`）；Cluster submit 重启→receipt/Config.cfg 反查不重复提交；Cluster collect 无固定总超时，状态页不可达降级观察、结果晚到继续等；cancel→终止当前 attempt 保留已固化结果。
- **Terminal 条件**：进程退出码 0 **不是**唯一证据（需完整 Manifest/输出/checksum/逐输入结果，任务书 4A.4）；Cluster 页面 finished 不能替代完整 `result.ini`；partial 只由真实 Selena 混合结果产生。
- **标记**：✅ 已实现+已测（Task H 77 passed / Task J 79 passed）；⚠ 真实长任务/批量/取消/Connector 重启需部署验收；⚠ 「只重试失败输入」缺失（P1-1）。

### 5.9 collect_results
- **状态来源**：本地 `_execute_v5_local_collect`（catalog 归档先于本地交付）或 Cluster `execute_cluster_collect`（result.ini 优先于页面）。
- **证据**：完整 result manifest、result_ref、checksum；结果不完整→可重试 collect。
- **恢复**：collect retry **不重新 submit**（Cluster）/ **不重跑仿真**（本地，复用 immutable 归档）；归档失败保持 run 非终态可重试。
- **Terminal 条件**：succeeded（归档完整）/ failed（归档失败可重试）/ cancelled。
- **标记**：✅ 已实现+已测；⚠ 真实结果目录晚到/归档失败需部署验收。

### 5.10 finalize_manifest
- **状态来源**：只消费已固化 result_ref/summary，不重跑仿真（`agent.py:2960-2998`；`cluster_stage_executor.py:306-322`）。
- **证据**：immutable manifest、状态一致；`_normalize_manifest_outcome` 把失败计数矛盾的成功 manifest 归一化为 failed（`control_service.py:159-182`）；`ResultCatalog._register` 同 run 不同内容拒绝（`local_results.py:379-385`）。
- **恢复**：finalizer retry 不重新编译/仿真（retry 时修复 payload 重建 `runtime_bundle_id/result_ref`，`control_service.py:2920-2989`）。
- **Terminal 条件**：finalize 成功→Job 终态；manifest 不一致→failed（不发布不可信结果）；用户取消→cancelled。
- **标记**：✅ 已实现+已测；⚠ partial 在 DB=failed vs API=partial 分层不一致（P1-2）；⚠ partial 失败输入无法重跑（P1-1）。

### 5.11 下载（download）
- **状态来源**：`GET /api/v1/results/{ref}/download`；owner 校验 + 过期检查 + archive 重校验（`api_v1_fastapi.py:1001-1010`；`local_results.py:363-370`）；SDK 临时文件 + 流式 SHA-256 + 原子 `replace`（`client.py:897-927`）。
- **证据**：`archive_checksum` 比对、`result_ref` 内容寻址、不可变归档；manifest/result_ref/catalog-ZIP 在同一 catalog 事务内一致。
- **恢复**：断流→显式重试新临时文件（服务端只读流式，断流不损坏归档）；服务重启不丢结果；outbox 缓冲冲刷。
- **Terminal 条件**：checksum 一致即交付成功；`result_unavailable` 404 / checksum mismatch 裸 `ValueError`（P2-3）。
- **标记**：✅ 已实现+已测（Task I 73+59+7 passed）；⚠ 真实大 ZIP/断流/重启/双 owner 并发需部署验收；⚠ GC/磁盘水位/告警缺失（P1-4）、Cluster retention 不一致（P1-5）。

---

## 6. 总体就绪结论

**有条件上线：受信内网单用户 / 测试部署可用；正式多租户被 P0 阻断。**

按任务书 §13 三选一：

- **「可上线」不成立**：P0-1（认证缺失/owner 可伪造/同 owner 多设备冒充）使正式多用户门禁 7.4 未通过；且 13 个故障注入场景的真实部署验收（Windows 首装/升级/断网/重启、真实 Selena 编译、真实 Cluster 长队列与重启窗口、250+ 批量、双 owner 认证隔离、结果大文件下载、真实服务重启恢复）全部未完成。
- **「有条件上线」成立**：在「受信内网、单用户/指定场景、认证未启用但接受 `X-Rsim-User` 为分组标签」的前提下，核心链路（提交/状态机/传输/构建/运行/结果/下载）代码实现 + 自动化测试齐备，且未发现已确认的「重复执行 / 结果完整性 / 分支污染 / 数据丢失」P0 缺陷（旧分支污染 `selena_branch_changed` 已知故障已修复并有真实 attempt=4 证据）。
- **「阻断」不成立（对受信内网场景）**：不存在无法恢复的问题；但**若要宣称「支持正式多用户」，则处于被阻断状态**。

**上线前必须完成的（P0/P1 门禁）**：
1. 启用 Bearer（`--auth-file`）并从认证主体派生 owner；两 owner live 全链路验收（P0-1）。
2. 交付「只重试失败输入」API/SDK/Web 行为与实测证据（P1-1，任务书 §6.3）。
3. 补 commit→bind 重启窗口直接回归测试（P1-3）；复核 partial 的 DB= failed 不会被 stale/恢复误清理（P1-2）。
4. 补结果归档 GC / 磁盘水位 / 告警，统一 Cluster 与本地 retention（P1-4/P1-5）。
5. 按任务书 §12 完成真实部署验收矩阵并留存 Job ID / Manifest / checksum（P1-6）。

**本文件为入口文档**：后续任何 AI 声称「某条链路已修复」前，必须先在本矩阵对应行核对「已实现+已测 / 文档声明 / 需真实部署验收」标记与证据出处，再进入对应审计文档取 file:line。

---

## 7. 审计边界与后续动作

- 本文件为纯汇总：未修改任何源码、未提交任何 commit；证据均来自 13 份审计文档（文末索引）。
- 若后续 Task H 交付「只重试失败输入」、Task C 完成认证启用验收、Task B 补齐 GAP-1/GAP-2、Task I 实现 GC/水位/告警，需回填本矩阵相应行并更新标记。
- 真实部署验收的执行机：目标 Linux `10.190.171.44`（`radar-sim-v1.service`，候选 release `/home/hoz2wx/radar-sim-d3de370`）+ 真实 Windows + 真实 Cluster；验收证据格式按任务书 §12。
- 已知需注意的代码-文档命名差异（非功能问题，汇总时确认）：`build_policy.mode` 代码用 `full`/`incremental`，brief 5.2 用 `fresh`/`incremental`/`full_clean`；fresh 被归入 `incremental`（Task D §3/§10）。
