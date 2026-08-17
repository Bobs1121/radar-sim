# radar-sim 控制面与状态机审计（Task B）

日期：2026-08-17
范围：`core/control_service.py`、`core/api_v1.py`、`cli/server.py`、`core/control_http.py`、`core/stage_binder.py`、`core/stages.py`、`core/task_store.py`、`core/agent_result_outbox.py` 的 claim / heartbeat / stale / cancel / retry / restart / handoff / outbox 逻辑。
审计方式：AUDIT ONLY，未修改任何源代码，未提交任何内容。
复测命令与结果见第 6 节：定向回归 `120 passed`。

## 1. 结论先行

控制面的 Job/Stage 状态机以 SQLite（`_control.db`）为唯一真相，所有 claim、attempt、handoff、cancel、retry、stale 恢复都在单个事务内（`BEGIN IMMEDIATE`）完成，并附带结构化 `job_events` 审计。针对执行任务书（Task B 行动清单）逐项核实：

1. claim / heartbeat / stale / cancel / retry / restart / handoff / outbox 均已实现并有 SQLite 证据（见第 3、4 节）。
2. “Stage 结果提交（commit）与后继绑定（bind）之间发生重启”不会丢阶段：后继 Stage 保持 `queued + assigned_agent_id=__v1_scheduler__`，由三处持久化的 handoff 重放兜底（Agent poll / 服务端 maintenance / control_http）。但该恢复路径 **缺少直接回归测试**（见第 5 节 GAP-1）。
3. 旧 attempt 回调 fencing、新 attempt 抢占、cancel 与 success 竞态、partial finalize 均有对应测试与代码路径（见第 4、5 节）。

结论：状态机核心正确，但“commit→bind 重启窗口”和“cancel 后 success 落盘”两条竞态缺少直接回归测试，属于**测试缺口（非代码缺口）**，建议补齐。真实 Linux 服务重启恢复需在部署环境验收（本机无法复现）。

## 2. 状态机：Stage / Task 状态转移表

Stage 合法状态（来自 `core/control_service.py:19-24` 与代码路径）：

```text
queued  running  cancel_requested  cancelling(legacy)  succeeded  failed  cancelled  skipped  blocked
```

### 2.1 转移表

| 当前状态 | 动作/触发 | 下一状态 | 代码位置 | 说明 |
|---|---|---|---|---|
| queued | `claim_next_task` | running | `control_service.py:1839-2045` | 原子 CAS，创建 `stage_attempts` 行，`attempt_count+1` |
| queued | `cancel_job`（queued/blocked） | cancelled | `control_service.py:2611-2628` | 未开始的任务直接终态 |
| queued | 上游失败 `_cancel_remaining_tasks_locked` | cancelled | `control_service.py:3528-3548` | error code=UPSTREAM_FAILED |
| queued | `reclaim_stale_tasks`（stale 且未超 max_attempts） | queued（重排） | `control_service.py:2214-2247` | 清 `assigned_agent_id/claimed_at/started_at`；attempt 记 failed(AGENT_STALE) |
| running | `submit_task_result`(success) | succeeded | `control_service.py:2509-2527` | 写 result_json/output_ref |
| running | `submit_task_result`(fail) | failed | 同上 | 并 `_cancel_remaining_tasks_locked` |
| running | cancel 语义（`_resolve_task_result_status`） | cancelled | `control_service.py:3409-3415` | cancel_requested 且 returncode!=0 / failed → cancelled |
| running | cancel 语义（success 落盘） | succeeded | `control_service.py:3412-3413` | cancel_requested 但 status=succeeded → 保留 succeeded |
| running | `reclaim_stale_tasks` 且 cancel_requested/cancelling | cancelled | `control_service.py:2134-2170` | 死 Agent 无法回执时由 stale 兜底终态 |
| running | `reclaim_stale_tasks` 且 attempts >= max_attempts | failed | `control_service.py:2171-2213` | 并取消下游 |
| running | 旧 attempt 结果在 reclaim 后被采纳（`can_adopt_reclaimed_attempt`） | running（同一 attempt） | `control_service.py:2355-2413` | 不新增 attempt，复用旧终态 |
| cancel_requested/cancelling | `reclaim_stale_tasks` | cancelled | `control_service.py:2134-2170` | legacy Connector 中间态兜底 |
| failed | `retry_stage` | queued | `control_service.py:2990-3007` | 只允许 failed/cancelled |
| cancelled | `retry_stage` | queued | 同上 | 重置依赖闭包下游 |
| blocked | 初始创建 | blocked | `create_job` `control_service.py:1727-1764` | `needs_input` 是 Job 级投影 |

### 2.2 Job 级状态转移（`_refresh_job_status_locked`，`control_service.py:3303-3378`）

| 条件 | 结果 |
|---|---|
| 任一 Stage running | `running`（或 cancel_requested） |
| 任一 Stage queued | `queued`（或 cancel_requested） |
| 任一 Stage blocked | `needs_input`（或 cancel_requested） |
| 全部 Stage ∈ {succeeded, skipped} | `succeeded` |
| cancel_requested 且无 failed 且全部终态 | `cancelled`（用户取消优先于已成功上游） |
| 全部 Stage ∈ {cancelled, skipped} | `cancelled` |
| 其余（含任一 failed） | `failed` |

### 2.3 非法转移列表

以下转移必须被拒绝（代码中均有对应检查）：

| 非法转移 | 拒绝点 |
|---|---|
| queued → succeeded/failed（未 claim 直接提交结果） | `submit_task_result`：`task is no longer running` → `TaskResultRejected("stale_task_result")`（`control_service.py:2346-2418`） |
| 终态 → 任意状态（重复回调） | `submit_task_result`：`task already completed`（`control_service.py:2346-2347`）；transfer 重复 manifest 幂等忽略（`complete_transfer_stage` `control_service.py:1486-1493`） |
| 旧 attempt 结果覆盖新 attempt | attempt 数不匹配 → `TaskResultRejected("stale_task_result")`（`control_service.py:2439-2444`）；已在新 claim 后被拒（测试 `test_late_result_from_reclaimed_attempt_cannot_complete_new_attempt`） |
| 其他 Agent 提交结果 | agent 不匹配 → `TaskResultRejected("agent_task_mismatch")`（`control_service.py:2419-2427`） |
| heartbeat 认领不属于自己的任务 | `TaskResultRejected("agent_heartbeat_task_mismatch")`（`control_service.py:799-810`） |
| 成功 Stage 重复绑定/换 Agent | `bind_stage_to_agent` 仅接受 `queued` 且 CAS 校验（`control_service.py:873-901`） |
| 非 failed/cancelled Stage 直接 retry | `retry_stage`：`only failed/cancelled stages can be retried`（`control_service.py:2895-2896`） |
| cancel 掩盖真实 failed Stage | `_refresh_job_status_locked`：有 failed 则 Job=failed（`control_service.py:3346-3348`） |
| 依赖未就绪即 claim | `_task_is_ready_to_claim_locked`：依赖必须全 ∈ {succeeded, skipped}（`control_service.py:3380-3397`） |

## 3. 关键恢复路径与 SQLite 证据

每个恢复点对应 SQLite 表/列与事件，均为可查询证据。

### 3.1 Stage 结果 commit 与后继 bind 之间的重启（任务书第 2 行动项）

- 正常路径：`api_v1.py:2558` / `control_http.py:319` 在 `submit_task_result` 成功后**同步**调用 `advance_after_stage_result`（`stage_binder.py:1096`）完成后继绑定。
- 重启窗口：若进程在 Stage 提交成功后、绑定前崩溃，后继 Stage 停留在 `status='queued'`、`assigned_agent_id='__v1_scheduler__'`。
- 兜底重放（三处，幂等）：
  1. `api_v1.py:2440-2448` `poll_agent` 每次 Agent 轮询先 `reconcile_stage_handoffs`；
  2. `cli/server.py:631` maintenance_pass 每轮对非终态 Job 重放；
  3. `control_http.py:266` poll 路由。
- `reconcile_stage_handoffs`（`control_service.py:3066-3122`）只对“succeeded 且后继 queued”的 Stage 调用 binder，失败按 `StageBindingError` 静默跳过等下一轮。
- SQLite 证据：`tasks` 表后继行 `status='queued'`、`assigned_agent_id='__v1_scheduler__'`、`dependencies_json` 含已完成 Stage id；`job_events` 有 `stage.succeeded`。`_task_is_ready_to_claim_locked`（`control_service.py:3380`）保证依赖为 succeeded/skipped 时才可被 claim。
- 测试覆盖：**间接**（`test_control_http_environment_result_automatically_hands_off_build`、`test_run_config_resolution_flow.py` 覆盖同步 handoff），**无直接调用 `reconcile_stage_handoffs` 的回归测试** → GAP-1（见第 5 节）。

### 3.2 旧 attempt 回调与新 attempt fencing（任务书第 3 行动项）

- 代码：`submit_task_result` 的 `can_adopt_reclaimed_attempt`（`control_service.py:2355-2413`）。
  - 采纳条件：Stage 已被 stale 置回 `queued`、`cancel_requested=0`、回调 attempt == 当前 `attempt_count`、`stage_attempts` 中该 attempt 的 agent 一致且 `error.code == 'AGENT_STALE'`。
  - 满足时把任务重新置回 running（**不新增 attempt**），再正常消费终态——避免重复编译/传输/仿真。
  - 不满足（已有新 attempt）则 `TaskResultRejected("stale_task_result")` 拒绝，实现 fencing。
- SQLite 证据：`stage_attempts` 表 `(stage_id, attempt)` 唯一键 + `agent_id/status/error_json`；`tasks.attempt_count`。
- 测试：`test_result_from_reclaimed_attempt_is_adopted_before_new_claim`（test_control_service.py:557）；`test_late_result_from_reclaimed_attempt_cannot_complete_new_attempt`（test_control_service.py:521）；`test_submit_task_result_rejects_different_agent`（test_control_service.py:500）；`test_result_callback_must_claim_task_before_submission`（test_control_stages.py:432）。

### 3.3 cancel 与 success 竞态

- 代码：`_resolve_task_result_status`（`control_service.py:3399-3420`）：
  - `cancel_requested` + `status in ("", "cancelled")` → cancelled；
  - `cancel_requested` + `status == "succeeded"` → **succeeded**（真实成功不被取消抹掉）；
  - `cancel_requested` + `status == "failed"` 或 returncode!=0 → cancelled（取消优先于失败）；
- Job 级：`_refresh_job_status_locked`（`control_service.py:3338-3348`）保证用户取消不会把已成功的上游报告成 failed；真实 failed Stage 也不会被取消掩盖。
- SQLite 证据：`jobs.cancel_requested`、`tasks.cancel_requested`、`tasks.status`、`job_events.stage.cancelled`。
- 测试：`test_cancel_preserves_skipped_stage_and_finishes_running_cancelled`（test_control_stages.py:155）；`test_cancelled_job_with_succeeded_upstream_is_not_reported_failed`（test_control_stages.py:363）；`test_cancel_request_does_not_hide_a_real_failed_stage`（test_control_stages.py:393）；`test_cancel_running_job_sets_cancel_requested_and_final_cancelled`（test_control_service.py:139）。
- 缺口：**无测试直接断言“cancel_requested 后 success 落盘 → Stage=succeeded”（即 `_resolve_task_result_status` 第 3412-3413 行分支）** → GAP-2（见第 5 节）。

### 3.4 partial finalize 与 Manifest 业务结果

- 代码：`submit_task_result` 对 `finalize_manifest` 且 `manifest_failed` 时把最终状态强制 `failed`（`control_service.py:2453-2466`），并发布 `simulation_failed` code；`_normalize_manifest_outcome`（`control_service.py:159-182`）把失败计数矛盾的成功 manifest 归一化为 failed；启动期 `_reconcile_failed_manifest_jobs_locked`（`control_service.py:501-570`）修正历史记录。
- partial：`test_partial_manifest_continues_to_finalize_and_keeps_successful_outputs` 证明批量中部分失败仍保留成功输出。
- SQLite 证据：`jobs.result_json` 的 manifest/summary、`jobs.status`、finalize Stage `result_json`。
- 测试：`test_finalize_manifest_stage_publishes_job_result`（test_control_stages.py:531）；`test_failed_manifest_marks_job_failed_but_remains_available`（:550）；`test_partial_manifest_continues_to_finalize_and_keeps_successful_outputs`（:585）；`test_structured_failure_count_overrides_succeeded_manifest_status`（:624）；`test_diagnostic_errors_without_failure_count_do_not_override_success`（:658）；`test_startup_reconciles_historical_success_with_failed_manifest`（:688）；`test_startup_normalizes_failed_job_with_contradictory_historical_summary`（:724）。

### 3.5 stale reclaim（dead-agent 恢复）

- 代码：`reclaim_stale_tasks`（`control_service.py:2060-2273`），参数来自 `cli/server.py:46-80` 部署控制（默认 interval=30s、stale_after=300s、max_attempts=0=无限、assignment_grace=30s）。
- 三条分支：cancel_requested → cancelled；attempts>=max_attempts → failed+取消下游；否则 → queued 重排。
- SQLite 证据：`agents.last_heartbeat`、`tasks.status/claimed_at/started_at`、`tasks.error_json.code='AGENT_STALE'`、`stage_attempts` 终态。
- 测试：test_reclaim.py 全 10 条 + test_control_stages.py 的 4 条 stale 测试（见第 6 节）。

### 3.6 Connector 重启 / 注册后恢复

- 代码：`_register_agent_record` UPSERT 保留 `current_task_id`（`control_service.py:743-777`）；`claim_next_task` 的 orphan 修复（`control_service.py:1899-1936`）把 running/assigned 但 Agent 当前任务被清空的任务恢复给同一 Agent（不新增 attempt）；心跳任务归属校验（`control_service.py:799-810`）。
- SQLite 证据：`agents.current_task_id`、`tasks.assigned_agent_id`、`job_events.stage.resumed`。
- 测试：`test_same_connector_reregistration_preserves_running_assignment`（test_control_service.py:173）；`test_heartbeat_without_current_task_keeps_assignment`（:162）；`test_claim_repairs_legacy_orphan_before_claiming_new_work`（:256）；`test_heartbeat_cannot_claim_another_agent_task_identity`（:604）。

### 3.7 retry（含历史 payload 修复）

- 代码：`retry_stage`（`control_service.py:2885-3064`）：
  - 只接受 failed/cancelled；
  - `register_artifact` 无 `dispatch_scope` 时按 target 修复 payload（`control_service.py:2902-2918`）；
  - local `finalize_manifest` 用可信前序 Stage 结果重建 `runtime_bundle_id/result_ref/delivery`（`control_service.py:2920-2989`）；
  - 重置依赖闭包下游 `_retry_reset_candidates_locked`（`control_service.py:3711-3756`），把 cancelled/failed 下游复位为 queued/skipped。
- SQLite 证据：`tasks.payload_json` 修复前后、`tasks.status`、`job_events.stage.retry/retry_reset`、`stage_attempts` 保留 attempt 1 审计。
- 测试：`test_retry_repairs_legacy_register_artifact_route`（test_control_service.py:410）；`test_retry_repairs_local_finalizer_bundle_and_result_handoff`（:441）；`test_retry_after_user_cancel_resets_cancelled_dependency_descendants`（:578）；`test_retry_source_restores_upstream_cancelled_parallel_branch_and_downstream`（test_control_stages.py:481）；`test_attempt_fail_retry_attempt_two_success_preserves_attempt_one`（:110）；`test_retry_rejects_invalid_stage_state`（:148）。

### 3.8 Connector 终态 outbox

- 代码：`core/agent_result_outbox.py`（SQLite `agent_result_outbox`，主键 `(task_id, attempt)`，at-least-once）；`cli/agent.py:3748-3828` `submit_result` 先 `outbox.put` 再 HTTP；`cli/agent.py:3477-3504` `flush_result_outbox` 投递并 `remove`；永久性错误（stale/wrong agent/unknown task）直接 `remove` 防止死循环。
- SQLite 证据：Agent 本地 `result-outbox.db` 的 `agent_result_outbox` 行；`attempts/last_error/updated_at`。
- 测试：`test_result_outbox_survives_store_reopen_and_tracks_attempts`（test_agent_result_outbox.py:10）；`test_control_client_queues_result_when_control_plane_is_down_then_flushes`（:32）；`test_result_outbox_rejects_different_payload_for_same_attempt`（:65）。

### 3.9 前端/控制面持久化（`core/task_store.py`）

- 该模块是 legacy 前端任务日志的 SQLite 存储（`tasks`/`task_logs`），与控制面 `_control.db` 分离；与控制面状态机无耦合。审计确认其只做 upsert/append，不影响 Job/Stage 状态机正确性。

## 4. 回归测试清单（tests）

以下文件与用例在本机复测全部通过（详见第 6 节）：

- `tests/test_control_service.py`：claim/heartbeat/cancel/retry/stale 采纳与 fencing、owner 隔离、capability gating。
- `tests/test_control_stages.py`：依赖 claim、attempt 审计、retry 闭包重置、cancel 竞态、finalize/partial/manifest、stale reclaim、历史 DB 迁移。
- `tests/test_reclaim.py`：dead-agent 恢复全场景（静默、max_attempts、幂等、无限重试、assignment grace、busy/orphan/legacy 契约）。
- `tests/test_server_maintenance.py`：maintenance loop 生命周期、异常存活、部署参数、serve-v1 启动。
- `tests/test_control_agent.py`：Agent 侧构建超时策略与 v5 build 阶段。
- `tests/test_stages.py`：固定十阶段 DAG 与 skip 语义。
- `tests/test_stage_binder.py`：绑定 CAS、snapshot 过期、build→register 同 Agent 绑定。
- `tests/test_agent_result_outbox.py`：outbox 持久化/恢复/投递。
- `tests/test_control_http.py` / `tests/test_run_config_resolution_flow.py`：HTTP 层 handoff 与 run-config 绑定。

## 5. 缺口（GAP）与建议回归测试（审计不写码）

### GAP-1（P1，测试缺口，对应任务书行动项 2）
**场景**：Stage 结果 commit 与后继 bind 之间进程重启。
**精确事件序列**：
1. 创建 `simulation.v1` Job（resolve_spec → environment_check → build_selena → register_artifact …），Agent 完成并提交 resolve_spec/environment_check 结果（结果 commit 为 succeeded）。
2. 模拟“进程在 bind 前崩溃”：不调用同步 `advance_after_stage_result`，直接用新 `ControlService` 实例重开同一 DB（等价重启），此时后继 Stage 应仍为 `queued`、`assigned_agent_id='__v1_scheduler__'`。
3. 调用 `service.reconcile_stage_handoffs(job_id)`（等价 maintenance/poll 触发）。
4. 断言：后继 Stage 被绑定到真实 required Agent（`assigned_agent_id` 变为 Windows Agent id）、`claim_next_task` 可领取、无 Stage 丢失。
**现状**：`reconcile_stage_handoffs` 在 `tests/` 中无任何直接调用；只有同步 handoff 被覆盖。代码路径真实存在（三处兜底），但无回归保护。

### GAP-2（P2，测试缺口，对应任务书行动项 3 cancel-vs-success）
**场景**：用户取消后，running Stage 的 success 回调落盘。
**精确事件序列**：
1. Agent claim 任务 → `cancel_job`（任务 `cancel_requested=1`）。
2. Agent 提交 `status="succeeded", returncode=0`。
3. 断言：Stage 保持 `succeeded`（证据保留），Job 最终为 `cancelled`（用户意图优先）；或按产品语义断言 Stage=succeeded + Job=cancelled 的组合。
**现状**：`_resolve_task_result_status` 第 3412-3413 行有该分支，但无测试断言该组合；现有 cancel 测试用的是 `returncode=-15`（失败 → cancelled）。

### GAP-3（P2，需要真实部署验收）
**场景**：真实 Linux 服务重启恢复。本机无法停止/重启线上 `radar-sim-v1.service`，无法制造真实“commit→bind 窗口”。建议在部署环境：重启服务后对处于中间态（succeeded Stage + queued 后继）的 Job 执行 `reconcile_stage_handoffs` 并核对 `job_events`。此项标记为**需要真实部署验收**。

## 6. 复测结果（本机，Windows，venv Python 3.12）

命令（分批，均 <2min）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_reclaim.py tests/test_server_maintenance.py tests/test_control_agent.py -q
# 27 passed in 12.84s
.venv/Scripts/python.exe -m pytest tests/test_control_service.py tests/test_control_stages.py tests/test_stages.py tests/test_stage_binder.py tests/test_agent_result_outbox.py -q
# 73 passed in 18.15s
.venv/Scripts/python.exe -m pytest tests/test_control_http.py tests/test_run_config_resolution_flow.py -q
# 20 passed in 15.28s
```

**合计：120 passed，0 failed，0 skipped。** 与 handoff 记录的定向 build/control 回归量级一致。未跳过任何文件。

## 7. 失败模式分类

| # | 失败模式 | 证据/测试 | 分类 |
|---|---|---|---|
| 1 | Agent 心跳静默（running，未超 max_attempts） | `test_reclaim_requeues_task_when_agent_silent`、`test_reclaim_stale_requeues_with_terminal_attempt_and_event` | **自动恢复**（requeue→新 attempt） |
| 2 | Agent 反复崩溃超过 `max_attempts` | `test_reclaim_fails_task_after_max_attempts`、`test_reclaim_stale_max_attempts_fails_stage_attempt_and_downstream` | **需用户 retry**（Stage failed + 下游 UPSTREAM_FAILED；`retry_stage`） |
| 3 | `max_attempts=0`（部署默认无限） | `test_reclaim_unlimited_attempts` | **自动恢复**（无限 requeue） |
| 4 | 用户取消（running） | `test_cancel_running_job_sets_cancel_requested_and_final_cancelled`、`test_reclaim_stale_cancel_requested_finishes_cancelled_without_requeue` | **自动恢复**（终态 cancelled，证据保留） |
| 5 | 旧 attempt 回调在 reclaim 后、新 claim 前到达 | `test_result_from_reclaimed_attempt_is_adopted_before_new_claim` | **自动恢复**（采纳同 attempt，不重复执行） |
| 6 | 旧 attempt 回调在新 claim 后到达 | `test_late_result_from_reclaimed_attempt_cannot_complete_new_attempt` | **自动拒绝**（fencing；由新 attempt 负责） |
| 7 | cancel 后 success 落盘 | 代码 `_resolve_task_result_status`（GAP-2 无测试） | **自动恢复**（Stage 证据保留；Job cancelled） |
| 8 | Stage commit 与后继 bind 间重启 | `reconcile_stage_handoffs`（GAP-1 无测试） | **自动恢复**（三处幂等重放）；真实验收待部署 |
| 9 | Connector 重启丢当前任务指针 | `test_same_connector_reregistration_preserves_running_assignment`、`test_claim_repairs_legacy_orphan_before_claiming_new_work` | **自动恢复**（同 attempt 接管） |
| 10 | 上游真实失败 | `test_cancel_request_does_not_hide_a_real_failed_stage` | **需用户 retry**（Job=failed；retry 上游） |
| 11 | finalizer/partial/Manifest 业务失败 | `test_failed_manifest_marks_job_failed_but_remains_available`、`test_partial_manifest_continues_to_finalize_and_keeps_successful_outputs` | **需用户 retry**（只重试 finalize/失败输入；成功输出保留） |
| 12 | 旧 register_artifact 无 dispatch_scope / 旧 finalizer 缺 runtime_bundle_id/result_ref | `test_retry_repairs_legacy_register_artifact_route`、`test_retry_repairs_local_finalizer_bundle_and_result_handoff` | **需用户 retry**（retry 时自动修复 payload，不重编译/重仿真） |
| 13 | 控制面不可达时 Connector 终态结果投递 | `test_control_client_queues_result_when_control_plane_is_down_then_flushes`、`test_result_outbox_survives_store_reopen_and_tracks_attempts` | **自动恢复**（outbox at-least-once） |
| 14 | 真实 Linux 服务重启恢复 | 无本机证据 | **需要真实部署验收** |

## 8. 代码位置索引（关键文件:行）

- `core/control_service.py`: claim `1839` / reclaim `2060` / submit_result `2326`（采纳旧 attempt `2355-2413`）/ cancel `2588` / retry `2885` / reconcile_handoffs `3066` / job 状态刷新 `3303` / 依赖 claim 门 `3380` / 状态解析 `3399` / attempt 保障 `3422` / 完成 attempt `3469` / 取消下游 `3505` / retry 闭包 `3711`。
- `core/stage_binder.py`: `advance_after_stage_result` `1096` / `bind_local_stage_after_result` `965`（runtime_bundle_id 回退 `1001-1014`）。
- `core/api_v1.py`: `submit_agent_result` `2518`（同步 handoff `2558`）/ `poll_agent` reconcile `2440-2448` / `cancel_job` `1707` / `retry_stage` `1715`。
- `cli/server.py`: `_maintenance_settings` `46-80` / maintenance_pass `613-636`（reconcile `631`）。
- `core/control_http.py`: reconcile `266` / advance `319`。
- `core/agent_result_outbox.py`: 全文件。
- `cli/agent.py`: `submit_result` `3748-3828` / `flush_result_outbox` `3477-3504`。

## 9. 审计边界（诚实声明）

- 本审计为**代码与测试审计**，未修改/提交任何代码。
- 真实部署恢复（重启线上 Linux 服务、真实 Windows/Cluster 重启窗口）本机无法复现，相关项标注为**需要真实部署验收**（GAP-3 / 分类 #14）。
- `core/api_v1.py` 全量（4070 行）仅审计与本任务相关的 claim/heartbeat/cancel/retry/handoff/outbox 调用点；数据面（transfer/dataset/artifact）与 Cluster 执行器不属于 Task B 范围。
- 状态机“自动恢复”均指控制面自动推进，不代表外部 Windows 进程/Cluster 作业自动复活；外部执行器边界不在本任务范围。
