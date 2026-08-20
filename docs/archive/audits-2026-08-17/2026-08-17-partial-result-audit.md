# radar-sim 本地/Cluster 仿真、partial 与 retry 语义审计（Task H）

日期：2026-08-17
范围：Windows 本地批量仿真（`core/agent_local_run.py`、`cli/agent.py`）、Cluster 提交与收集（`core/cluster_stage_executor.py`、`core/cluster_runs.py`）、结果归档与交付（`core/local_results.py`、`core/result_delivery.py`）、本地 result outbox（`core/agent_result_outbox.py`）、Stage 绑定与 finalizer（`core/stage_binder.py`）、控制面 finalize/retry（`core/control_service.py`）、API 状态映射（`core/api_v1.py`）。
审计方式：AUDIT ONLY，未修改任何源代码，未提交任何内容。
复测命令与结果见第 8 节：定向回归 `77 passed + 2 skipped`（`test_agent_local_run.py` / `test_local_selena_runner.py` / `test_result_delivery.py` / `test_local_results.py` / `test_agent_result_outbox.py` / `test_cluster_stage_executor.py`）。

## 1. 结论先行

本地与 Cluster 两条仿真链路的 **partial / retry / checkpoint / 框架-引擎边界** 核心不变式大部分成立且有自动化测试：

1. **本地逐输入状态模型完整**：每个输入有 `index / input_relative_path / checksum(size/mtime) / output_relative_path / status / returncode / error_code`，并在 Agent 本地 SQLite lease 中逐输入 checkpoint（`agent_local_run.py` 建表 `agent_local_runs` 与 `execute_local_run` 的逐输入循环）。
2. **partial 只能由真实 Selena 混合结果产生**：`_is_partial_local_result()`（`cli/agent.py:2698`）要求“status=failed 且 succeeded>0 且 failed>0 且**所有失败条目 error_code 均为 `selena_failed`**”。paramconfig、依赖缺失、launcher、contract 等框架错误码一律不产生 partial。
3. **partial 的“控制面成功 + 业务结果 partial”分层成立**：partial 的 `run_simulation` Stage 以 returncode 0 / status `succeeded` 提交（`cli/agent.py:2200-2201`，`_local_stage_result` 返回 0），下游 collect/finalize 继续；manifest 携带 `status=partial`，public API 再从 manifest 派生 `partial`（`api_v1.py:3481-3495`）。Cluster 侧 manifest 从结构化 per-input 计数派生 `partial`（`cluster_stage_executor.py:1657`）。
4. **Connector 重启/断电只恢复未完成输入**：`execute_local_run` 重启后重建 `completed_indices`，对已成功且输出 checksum 有效的输入跳过不重跑（`agent_local_run.py:603-629,647-649`），输出被删/被改则重跑该条（`agent_local_run.py:616-623`）。
5. **Cluster collect retry 不重新 submit**：collect 全程不调用 submit；submit 幂等由 submission receipt + 唯一 Config.cfg 路径反查保证（`cluster_stage_executor.py:1056-1153`），collect 归档失败保持 run 非终态可重试（`test_cluster_stage_executor.py:837`）。
6. **两个必须如实声明的缺口**：
   - **GAP-A（P1）没有“只重试失败输入”的 API/SDK/Web 行为**（任务书 6.3 明确要求交付）。当前只有 Stage 级 `retry_stage`（`control_service.py:2885`、`api_v1_fastapi.py:682`、SDK `retry_stage`）；`partial` 任务下 `run_simulation` Stage 在 DB 里是 `succeeded`，`retry_stage` 只接受 `failed/cancelled` Stage，**无法重新跑失败输入**。唯一的“按失败输入重跑”逻辑在**旧版** `cli/run.py:132-182`（`retry_failed_at_end` 单批次进程内重试），不属于 V2 Windows-full Agent 路径。
   - **GAP-B（P2）control-plane 内部把 partial manifest 归一化为 `failed`**：`submit_task_result` 对 finalize_manifest 且 manifest 非成功时把 task 置 `failed`（`control_service.py:2453-2462`），`_reconcile_failed_manifest_jobs_locked` 把 job 置 `failed`（`control_service.py:501-570`，`_FAILED_MANIFEST_STATUSES` 含 `partial`）。public API 靠 `_v1_status` 重新派生 `partial` 才对外正确；DB 层 Job/Stage 是 `failed`，会导致“partial 业务结果在 Web/SDK 正确显示、但内部诊断/重试入口把它们当 failed”，且 `retry_stage` 可以重试 finalize 但不会重跑失败输入。

以下场景**需要真实部署验收**（本机不可用）：真实混合成功/失败批量、真实全部失败、真实“框架失败 + 一个旧成功输出”、真实取消中断、真实 Connector 重启。详见第 7 节。

## 2. 逐输入状态模型分析（任务书 6.1）

### 2.1 本地路径（`core/agent_local_run.py`）

数据源与持久化：

- `create_from_authorized_inputs()`（`agent_local_run.py:176`）把 `prepare_data` 的 Data Lease 输入固化到 lease：
  - 每个输入记录 `relative_path / path / size / mtime_ns / checksum`（`_verify_data_lease`，`agent_local_run.py:961-987`）。
  - 每个输入计算稳定 `output_relative_path = outputs/<index:04d>-<stem>-<checksum12>-out.MF4`（`_output_relative_path`，`agent_local_run.py:1071-1074`），并在写入前 `_safe_output_relative` 校验（必须 `outputs/*.mf4`、非绝对、无 `..`、无设备名，`agent_local_run.py:1058-1068`）。
  - DB 表 `agent_local_runs`（`agent_local_run.py:124-145`）持久化：`lease_id / job_id / status / outputs_json / error_count / error_code / diagnostics_json / execution_token / execution_pid / running_since`。
- 输入再校验：执行前 `_verify_stored_input` 重新校验 size/mtime/checksum（`agent_local_run.py:990-1007`），失败记 `input_changed_after_preflight`（`agent_local_run.py:685-702`），该条不启动 Selena、不影响其他条。

状态机（lease 层）：`ready -> running -> succeeded | failed | cancelled`（`_TERMINAL`，`agent_local_run.py:50`；`mark_running` `:312`；`finish` `:379`；`checkpoint` 保持 `running` `:402`）。

逐输入状态（`execute_local_run`，`agent_local_run.py:533`）：

| 字段 | 含义 | 赋值位置 |
|---|---|---|
| `index` | 输入序号（1-based） | `agent_local_run.py:647` |
| `input_relative_path` | 脱敏逻辑输入名（无物理路径） | `_relative_input_path` `:855-860` |
| `output_relative_path` | 稳定输出相对路径 | `item["output_relative_path"]` |
| `status` | `succeeded/failed`（运行期逐条记录） | `:750-774` |
| `returncode` | Selena 退出码 | `outcome.exit_code` `:741,767` |
| `error_code` | 稳定错误码（小写 snake_case） | `_safe_error_code` `:1117-1121`；`_selena_error_code` `local_selena_runner.py:132-142` |
| `engine_log_tail` | 受限日志尾（脱敏、限 200 行/16000 字符） | `_redact_runner_diagnostics` `:877-907`、`_bounded_lines` `:863-874` |

每条执行分支（`agent_local_run.py:647-820`）：

- 已在 `completed_indices`（重启恢复）→ 跳过不重跑（`:648-649`）。
- 用户取消 → `finish(status="cancelled")`，返回 130（`:650-657`，及运行中 `:733-740`）。
- `_verify_stored_input` 失败 → `input_changed_after_preflight`（`:685-702`）。
- `outcome.exit_code == 0` → 写输出并校验 checksum，记 `succeeded`（`:741-758`）。
- 非零 → 记 `failed` + `error_code`（`:759-774`）。
- `LocalRunnerUnavailable` → `runner_unavailable` 并 `break`（整批停止，`:775-789`）。
- `AgentLocalRunError` → `runner_contract_failed`（`:790-802`）。
- 其他异常 → `runner_contract_failed`（不泄露消息，`:804-818`）。

批次终态：`status = "succeeded" if failures == 0 and len(outputs) == len(inputs) else "failed"`（`agent_local_run.py:822`）。`result()` 汇总 `file_count / error_count / error_code`，且仅当 `failed` 时统计 `failed_input_count / succeeded_input_count / total_input_count`（`agent_local_run.py:430-463`）——这正是 partial 判定所需的结构化计数。

### 2.2 本地 partial 判定与业务映射（`cli/agent.py`）

- `_is_partial_local_result()`（`cli/agent.py:2698-2730`）：**只有** `status=failed` 且 `succeeded_input_count>0` 且 `failed_input_count>0` 且 `files` 非空 且 **所有失败 item 的 error_code 均为 `selena_failed`** 才返回 True。文件注释明确：`runner-unavailable or all-input failure remains a hard Stage failure; only a real mixed *Selena-engine* outcome is allowed`。
- `_local_stage_result()`（`cli/agent.py:2733-2736`）：partial → `{"status":"partial"}, returncode=0`；否则按 `result["status"]` 映射 0/1。
- `_execute_v5_local_simulation`（`cli/agent.py:2676-2695`）：执行 lease，`returncode==0 or _is_partial_local_result` → 返回 Stage 结果（partial 走 0）；否则硬失败。
- `_local_result_input_results`（`cli/agent.py:2739-2756`）：把逐输入 `index/input_relative_path/output_relative_path/status/returncode/error_code` 提取进 manifest 的 `input_results`。
- `_execute_v5_local_collect`（`cli/agent.py:2829-2940`）：`local_result["status"] != "succeeded" and not _is_partial_local_result` → 抛 `ValueError("local run did not succeed")`（`:2847-2848`）——框架失败不会进入收集。
- `_execute_v5_local_finalize`（`cli/agent.py:2943-2997`）：manifest `status = "partial" if _is_partial_local_result else local["status"]`（`:2957,2976`）。

### 2.3 Cluster 路径（`core/cluster_stage_executor.py`）

- 逐输入状态来自 Cluster 结果目录 `result.ini`：`_cluster_input_results`（`cluster_stage_executor.py:1501-1555`）逐条产出 `index / input_relative_path / result_relative_path / output_relative_path / status(succeeded/failed/unknown) / returncode / error_code`；失败且无 error_code 默认 `cluster_simulation_failed`（`:1542-1543`）。250+ 输入不被截断（`test_cluster_batch_input_results_are_not_truncated`，`test_cluster_stage_executor.py:659`）。
- 汇总进 `finalize_result` 的 `summary`：`succeeded_input_count / failed_input_count / total_input_count / input_results`（`cluster_stage_executor.py:1379-1397`）。
- partial 派生：`build_public_run_manifest`（`:1606-1642`）→ `_summary_is_partial`（`:1657-1665`）：`succeeded>0 and failed>0 and files and input_results` → `manifest.status="partial"`；若 `succeeded` 但结构化失败计数 >0 → 强制 `failed`（`:1624-1628`，`_summary_reports_failure` `:1645-1654`）。

### 2.4 服务端 manifest 归一化与 API 状态

- `_normalize_manifest_outcome`（`control_service.py:159-182`）：仅以结构化失败计数为准；`failed_input_count/failed_count/fail_count > 0` 且 status 不是 `partial/failed` → 强制 `failed`。
- `_v1_status`（`api_v1.py:3481-3501`）：job 为 `succeeded/failed` 但 manifest status 为 `partial` → 对外返回 `partial`。
- diagnosis：`manifest_partial` → `outcome="partial", code="simulation_partial", category="simulation"`，并给出 `succeeded/total` 摘要（`api_v1.py:2100-2140`）。

## 3. 最终状态矩阵（任务书 6.2）逐行核验

| 情况（任务书） | 目标状态 | 已成功结果 | 可重试方式 | 实现（file:line） | 测试（exact test） | 真实验收 |
|---|---|---|---|---|---|---|
| 所有输入成功且归档完整 | Manifest `succeeded` | 全部可下载 | 按需重跑，不默认重复 | `_execute_v5_local_finalize` `agent.py:2976`；`build_public_run_manifest` `cluster_stage_executor.py:1619`；全部 Stage `succeeded` → job `succeeded`（`control_service.py:3329-3331`） | `test_windows_full_local_preflight_run_collect_finalize_path_free`（`test_windows_full_local_e2e.py:142`）；`test_agent_local_run.py` 全成功用例 `test_injected_runner_writes_only_controlled_deterministic_outputs`（`test_agent_local_run.py:209`）、`test_zero_timeout_means_unlimited_local_batch_runtime`（`test_agent_local_run.py:157`）；Cluster `test_collect_waits_for_result_ini_after_official_completion`（`test_cluster_stage_executor.py:359`） | **需要真实部署验收**（本机无 live server 批量混合任务） |
| 至少一条成功、至少一条 Selena 内部失败 | `partial`，归因 `simulation` | 保留并可下载 | 只重试失败输入（**当前无该 API，见 GAP-A**） | 本地：`_is_partial_local_result` + `_local_stage_result` + manifest `partial`（`agent.py:2698,2733,2976`）；Cluster：`_summary_is_partial`（`cluster_stage_executor.py:1657`） | 本地：`test_partial_local_result_is_collectible_stage_outcome`（`test_windows_full_local_e2e.py:15`）、`test_partial_local_result_collects_and_finalizes_input_results`（`test_windows_full_local_e2e.py:58`）、`test_mixed_batch_continues_after_one_input_failure`（`test_agent_local_run.py:428`）；Cluster：`test_partial_cluster_result_keeps_each_input_outcome_in_manifest`（`test_cluster_stage_executor.py:590`）；API：`test_partial_manifest_is_terminal_downloadable_and_not_reported_as_total_failure`（`test_api_v1_service.py:477`） | **需要真实部署验收**（真实混合批量；并需交付失败输入重试 API） |
| 全部输入 Selena 内部失败 | `failed`，归因 `simulation` | 无成功输出，可留诊断包 | 重试失败输入/整批，用户决定 | 本地：`succeeded_input_count=0` → `_is_partial_local_result=False` → Stage 硬失败（`agent.py:2693-2695,2733-2736`）；Cluster：`fail_count>0 且 success=0` → `state="failed"`，manifest `failed`（`cluster_stage_executor.py:1214-1220,1354-1364`） | 本地：`test_failed_runner_persists_per_input_engine_diagnostics_without_paths`（`test_agent_local_run.py:405`，双输入全 `selena_failed`，`failed_input_count==2`、status `failed`）；Cluster：`test_failed_cluster_result_publishes_downloadable_diagnostics`（`test_cluster_stage_executor.py:680`） | **需要真实部署验收** |
| Connector/工具链/Runtime/Transfer/Manifest 框架失败 | `failed` 或 `needs_input`，**不得伪装成 partial** | 不能伪装 partial | 修复外部条件后从最近安全 Stage 重试 | 本地：`_is_partial_local_result` 拒绝非 `selena_failed` 错误码（`agent.py:2727-2730`）；`_execute_v5_local_collect` 拒绝非 succeed/partial（`agent.py:2847-2848`）；`local_selena_runner.py` 区分 `paramconfig_failed`/`selena_dependency_missing`/`selena_launch_failed`/`runtime_timeout`/`unsafe_runtime_argument`（`local_selena_runner.py:67-126,132-142`）；Cluster：`ClusterStageExecutionError` 稳定 code（`cluster_stage_executor.py:72-84`） | 本地：`test_connector_failure_is_not_disguised_as_partial_engine_result`（`test_windows_full_local_e2e.py:38`）；`test_missing_native_runner_fails_with_stable_path_free_code`（`test_agent_local_run.py:368`）；`test_runner_classifies_windows_missing_dll_before_engine_start`（`test_local_selena_runner.py:204`）；`test_runner_reports_actionable_paramconfig_failure...`（`test_local_selena_runner.py:146`）；Cluster：`test_submit_transport_failure_is_retryable_without_rebuilding`（`test_cluster_stage_executor.py:163`） | **需要真实部署验收**（真实断连/缺依赖/磁盘满等） |
| 用户取消 | `cancelled` | 已固化结果保留 | 明确新 attempt，不重复已完成输入 | 本地：`execute_local_run` 取消 → `finish(cancelled)` 返回 130（`agent_local_run.py:650-657,733-740`）；collect 上传取消 → 稳定 `cancelled` 不重试（`agent.py:2898-2902`）；Cluster：`execute_cluster_collect` 取消 → `finalize_result(state="cancelled")`（`cluster_stage_executor.py:1190-1196`）；服务端 job 取消派生 `cancelled`（`control_service.py:3332-3345`） | `test_cancellation_is_terminal_and_does_not_call_runner`（`test_agent_local_run.py:352`）；`test_collect_cancellation_creates_path_free_terminal_result`（`test_cluster_stage_executor.py:139`）；`test_materialize_honors_cancellation_before_copy`（`test_result_delivery.py:120 附近`） | **需要真实部署验收**（真实用户取消与成功回调竞态） |
| 仿真成功但 result.path 写失败 | 业务结果仍从 ZIP 获取，ZIP 必须保留 | ZIP 保留 | 只重试 delivery，不重跑仿真 | `_materialize_local_result` 捕获 `ResultDeliveryError` 返回稳定 `failed` 不抛出（`agent.py:2769-2826`）；catalog `publish` 先于本地交付（`agent.py:2863->2871`） | `test_materialize_is_atomic_idempotent_and_preserves_manifest`（`test_result_delivery.py:57`）；partial 收集落盘（`test_windows_full_local_e2e.py:118-133`） | **需要真实部署验收**（真实 result.path 不可写 + ZIP 仍可下载；Task I 同款缺口） |
| Manifest/Checksum/归档不一致 | `failed`，不发布不可信结果 | 不发布 | 重跑收集/归档，不能标 succeeded | `_normalize_manifest_outcome` 强制 `failed`（`control_service.py:159-182`）；`build_public_run_manifest` 归一化历史矛盾（`cluster_stage_executor.py:1622-1628`）；`ResultCatalog._register` 同 run 不同内容拒绝（`local_results.py:379-385`）；`finalize_result` 终态不可变（`cluster_runs.py:323-341,365-366`） | `test_public_manifest_normalizes_structured_historical_failure`（`test_cluster_stage_executor.py:909`）；`test_same_run_cannot_be_replaced_with_different_content`（`test_local_results.py:136`）；`test_diagnosis_normalizes_historical_manifest_mismatch_and_infrastructure_failure`（`test_api_v1_service.py:548`） | 历史矛盾有测试；真实归档源变化需部署验收 |

## 4. partial 的绝对边界（任务书 6.3）—— 每个框架条件都被代码阻止

| 不得产生 partial 的框架条件 | 代码阻止点（file:line） | 说明 / 证据 |
|---|---|---|
| Connector 依赖缺失 | `local_selena_runner.py:132-142`（`selena_dependency_missing` 0xC0000135 等）；`cli/agent.py:2727-2730`（非 `selena_failed` 一律不 partial）；`agent.py:2220-2233`（`connector_dependency_missing` → Stage failed） | 依赖错误码 ≠ `selena_failed` → `_is_partial_local_result=False`。测试 `test_runner_classifies_windows_missing_dll_before_engine_start`（`test_local_selena_runner.py:204`）、`test_connector_failure_is_not_disguised_as_partial_engine_result`（`test_windows_full_local_e2e.py:38`） |
| paramconfig 生成失败 | `local_selena_runner.py:67`（`paramconfig_outside_lease`）、`:70`（`unsafe_runtime_argument`）、`:86`（`paramconfig_failed`）；`agent.py:2727-2730` | 均非 `selena_failed`。测试 `test_runner_reports_actionable_paramconfig_failure...`（`test_local_selena_runner.py:146`） |
| Runtime Bundle 解压/校验失败 | 本地 preflight 阶段：`_verify_runtime_locations`（`agent_local_run.py:935-958`）；`create_from_authorized_inputs` 失败即拒绝（`agent_local_run.py:176-229`）；Cluster：`execute_cluster_preflight`/`CLUSTER_RUNTIME_BUNDLE_REF_UNAVAILABLE`（`cluster_stage_executor.py:800-866`） | 发生在 run 之前，Stage 失败，无逐输入结果，不产生 partial |
| 输入传输不完整 | `prepare_data` 必须 resolved 后才进入 preflight/run（Stage 绑定 `stage_binder.py` 数据角色门禁）；本地 `_verify_stored_input`（`agent_local_run.py:990-1007`）；Cluster `_expected_cluster_task_count` + `_collection_probe_is_complete`（`cluster_stage_executor.py:1408-1413,1462-1484`） | 未完整不允许进入 run；Cluster collect 缺 `expected_count` 时抛“结果不完整，可重试收集不重跑”（`cluster_stage_executor.py:1320-1323`） |
| Agent 与控制面失联且无 execution lease 证据 | 控制面把失联视为 `running/observing/reclaim` 而非结果；`execute_local_run` 只回收**进程已死**的 lease（`agent_local_run.py:576-586`）；lease 状态不是结果状态 | 不会把失联写成 partial；partial 只来自固化 `diagnostics.items` |
| 结果归档或 Manifest 校验失败 | `ResultCatalog.publish/import_archive` 校验失败抛错（`local_results.py:188-336`）；Cluster collect 归档失败 → 抛错且 run 保持非终态（`cluster_stage_executor.py:1372-1378` → 测试 `test_collect_archive_failure_does_not_make_cluster_run_terminal`，`test_cluster_stage_executor.py:837`）；`_execute_v5_local_collect` 归档/上传失败抛错（`agent.py:2921-2933`） | 归档失败不产 partial，而是可重试的 collect 失败 |
| 所有输入都失败 | `succeeded_input_count=0` → `_is_partial_local_result=False`（`agent.py:2711-2716`）；Cluster `_summary_is_partial` 需 `succeeded>0`（`cluster_stage_executor.py:1661-1665`） | 全失败 → manifest `failed`，不是 `partial` |

补充边界（任务书 4A.4 / 6.3 “有一个文件成功不能变成 partial”）：本地 `_is_partial_local_result` 只认 `selena_failed` 错误码，因此“框架失败 + 一个旧成功输出”场景下，即使有成功文件，只要失败项错误码是 `paramconfig_*`/`selena_dependency_*`/`selena_launch_failed`/`runner_*` 等，就仍是 Stage 硬失败而非 partial。**当前缺少该组合（framework failure + one old success）的专属测试**（见第 7 节未验收项）。

## 5. Checkpoint / Connector 重启恢复分析

### 5.1 逐输入 checkpoint（`agent_local_run.py:402-428`）

- `checkpoint()` 在批次运行中持续写 `outputs`（已完成文件的 relative/size/checksum）+ `error_count` + `error_code` + `diagnostics.items`（逐条状态）到 Agent 本地 SQLite，**状态保持 `running`**，不提前承诺成功。
- 通过 `execution_token` 校验写所有权（`_update`，`agent_local_run.py:493-496`）：旧进程/旧 attempt 的晚到写被拒。

### 5.2 重启后只恢复未完成输入（`execute_local_run`，`agent_local_run.py:533-832`）

- 重启时 `mark_running` 先检查：若旧 PID 仍存活且 token 不同 → `LocalRunAlreadyExecuting`（观察既有执行者，不重复启动，`agent_local_run.py:350-356`；`test_duplicate_connector_process_observes_one_local_execution` `test_agent_local_run.py:238`）。
- 旧 PID 已死（`_pid_alive=False`）→ 接管 lease（`agent_local_run.py:576-586`；`test_dead_connector_execution_lock_is_recoverable` `test_agent_local_run.py:265`）。
- 接管后重建 `completed_indices`：只接受 `diagnostics.items` 中 `status=succeeded` **且** `_checkpoint_output_is_valid`（输出文件存在、checksum/size 一致，`agent_local_run.py:1009-1021`）的输入；输出缺失/被改 → 丢弃旧成功标记重跑（`agent_local_run.py:616-623`）。
- 主循环 `for index ...: if index in completed_indices: continue`（`agent_local_run.py:647-649`）——**已成功输入绝不重跑**。

测试证据：`test_recovery_resumes_after_durable_batch_checkpoint`（`test_agent_local_run.py:281`）：模拟输入 1 已 checkpoint 成功、执行器死亡，重启后 `executed == [2]`（只跑未完成输入），最终 summary `file_count=2, error_count=0`。

### 5.3 结果先落 outbox 再投递（`core/agent_result_outbox.py`）

- `AgentResultOutbox.put` 在 HTTP 回调前持久化终态 payload（`agent_result_outbox.py:89-140`），控制面不可达时排队，恢复后冲刷（`test_control_client_queues_result_when_control_plane_is_down_then_flushes` `test_agent_result_outbox.py:32`；`test_result_outbox_survives_store_reopen_and_tracks_attempts` `test_agent_result_outbox.py:10`）。
- 同一 `(task_id, attempt)` 终态不同 payload 被拒绝（`agent_result_outbox.py:131-139`）。

### 5.4 Cluster 收集的恢复语义

- `execute_cluster_collect` 循环无总时长上限，状态页短暂不可达只降观察不失败（`cluster_stage_executor.py:1189-1247`；`test_collect_gateway_outage_keeps_observing_until_shared_result` `test_cluster_stage_executor.py:234`、`test_collect_uses_complete_shared_results_when_status_gateway_is_unreachable` `:308`）。
- Web 显示 finished 但 `result.ini` 未完整 → 继续观察不判失败（`:1262-1299`；`test_collect_waits_for_result_ini_after_official_completion` `:359`）；`result.ini` 报告失败覆盖 Web `succeeded`（`:1354-1364`；`test_collect_overrides_web_succeeded_when_result_ini_reports_failure` `:531`）。

## 6. Cluster collect-retry 不重新 submit 的证明

### 6.1 submit 幂等（不会重复外部提交）

`execute_cluster_submit`（`cluster_stage_executor.py:1056-1153`）顺序：

1. 先查 submission receipt：存在 → 直接 `mark_submitted` 接管，**不再 submit**（`:1062-1076`）。
2. 无 receipt → 用唯一 Config.cfg 目录路径向 manager 反查既有外部 job：找到 → `record_submission_receipt` + `mark_submitted`，`recovered_existing_submission=True`（`:1084-1112`）。
3. 都无 → 才 `submit_cluster_job`（`:1114`）；成功后先 `record_submission_receipt`（`:1144-1149`）再 `mark_submitted`（`:1150-1152`），缩小“已提交但未落库”窗口。

`record_submission_receipt`（`cluster_runs.py:240-276`）在推进 run 状态前持久化 `external_job_id`；`mark_submitted`（`cluster_runs.py:221-238`）无外部 id 直接拒绝。`finalize_result` 对同一 run 幂等且终态不可变（`cluster_runs.py:323-341,365-366`）。

测试证明：`test_submit_adopts_durable_receipt_without_second_external_submission`（`test_cluster_stage_executor.py:198`）——pre-写入 receipt 后 `submit_cluster_job` 被 monkeypatch 成“若被调用即抛 AssertionError”，断言 `recovered_existing_submission is True`、`external_job_id=="10321"`。

### 6.2 collect 全程不 submit

`execute_cluster_collect`（`cluster_stage_executor.py:1156-1405`）只调用 `run_store.update_state / finalize_result / result_catalog.publish` 与外部只读观察 `get_cluster_web_status / inspect_cluster_job`，**没有任何 `submit_cluster_job` 调用**。因此 collect Stage 被重试时，只会重新观察结果目录与归档，不会产生第二个外部 Cluster Job。

- 结果不完整（`finished_count < expected_count`）→ 抛 `ClusterStageExecutionError("Cluster results are incomplete; collection can be retried without rerunning simulation")`（`cluster_stage_executor.py:1320-1323`）。
- 归档失败（源文件变化）→ 抛错但 run 保持 `running` 非终态，可安全重试 collect（`test_collect_archive_failure_does_not_make_cluster_run_terminal` `test_cluster_stage_executor.py:837`，断言 `store.get(...).state == "running"`）。
- 取消 → `finalize_result(state="cancelled")`（`cluster_stage_executor.py:1190-1196`）。

### 6.3 本地 collect 重试同样不重跑仿真

`_execute_v5_local_collect`（`cli/agent.py:2829-2940`）：`catalog.list(owner=owner)` 中已存在同 `run_ref` 记录 → 复用 immutable 归档，不再 publish/conflict（`agent.py:2858-2869`）；上传对 5xx/408/409/429 重试最多 3 次且幂等（`agent.py:2885-2934`）。测试：`test_windows_full_local_preflight_run_collect_finalize_path_free`（`test_windows_full_local_e2e.py:142`）断言 `collected_again["result_ref"] == collected["result_ref"]`；transport 首次失败后重试成功且结果一致（`test_windows_full_local_e2e.py:253-270`）。

## 7. 缺口、未验收项与风险分级

### 7.1 代码缺口

- **GAP-A（P1）没有“只重试失败输入”的 API/SDK/Web 行为**（任务书 6.3 明确要求）：`retry_stage` 只接受 `failed/cancelled` Stage（`control_service.py:2895-2896`），而 partial 任务的 `run_simulation` Stage 在 DB 为 `succeeded`，无法重跑失败输入；SDK 只有 `retry_stage`（`radar_sim_sdk/client.py:806`）；API 只有 `POST /jobs/{job_id}/stages/{stage_id}/retry`（`api_v1_fastapi.py:682-684`）。唯一按失败输入重试的代码在旧版 `cli/run.py:132-182`（`retry_failed_at_end`，单次进程内），不属 V2 Windows-full Agent。**现状只能重试 collect/finalize 等 Stage，不能只重跑失败 MF4。**
- **GAP-B（P2）control-plane 内部把 partial 归一化为 `failed`**：`submit_task_result`（`control_service.py:2453-2462`）与 `_reconcile_failed_manifest_jobs_locked`（`control_service.py:501-570`）把 finalize_manifest task 和 job 置 `failed`；public API 靠 `_v1_status` 重派生 `partial` 才对用户正确（`api_v1.py:3481-3495`）。这使“partial”在 DB 诊断/事件/重试入口表现为 `failed`，是分层状态语义的表述不一致（控制面终态 vs 业务结果），**不改代码、仅记录**。
- **GAP-C（P2）`retry_stage` 重试 finalize 的 payload 重建只在 local 路由覆盖**（`control_service.py:2920-2989`）；对 partial 业务没有“重试只重跑失败输入”的等价物（见 GAP-A）。

### 7.2 未验收项（本机无 live server / 无真实 Windows/Cluster，标记“需要真实部署验收”）

1. 真实混合成功/失败批量（8 成功 2 失败 → `partial`，成功结果可下载，失败输入有日志尾）——任务书 3.1 用户故事 6。
2. 真实全部输入失败 → `failed`（归因 simulation）且无成功输出。
3. 真实“框架失败 + 一个旧成功输出” → 必须是 `failed` 而非 `partial`（当前**无专属回归测试**，逻辑由 `_is_partial_local_result` 错误码白名单保证）。
4. 真实用户取消中断 → `cancelled`，已完成结果保留，重试是新 attempt 不重复已完成输入。
5. 真实 Connector 重启（批次中途）→ 只恢复未完成输入（自动化单测 `test_recovery_resumes_after_durable_batch_checkpoint` 已覆盖，真实重启仍需部署验收）。
6. 真实 Cluster collect retry（结果目录晚到/归档失败）→ 不产生第二个外部 Job（自动化测试已覆盖 receipt 接管，真实 manager 验收需部署）。
7. “只重试失败输入” API/SDK/Web 行为与实测证据（**当前未实现，见 GAP-A**）。

### 7.3 风险分级

| 级别 | 项 | 说明 |
|---|---|---|
| P1 | GAP-A：无“只重试失败输入” API/SDK/Web | 任务书 6.3 明确要求交付；当前只能 Stage 级重试，partial 任务无法只重跑失败输入 |
| P2 | GAP-B：control-plane 内部把 partial 记成 failed | 对外 API 已正确显示 partial，内部 DB/诊断/重试语义是 failed，表述不一致 |
| P2 | 真实批量/取消/Connector 重启/框架失败+旧成功输出 场景未实测 | 需真实部署验收（自动化单测已覆盖代码路径） |
| P2 | GAP-C：retry payload 重建仅覆盖 local 路由 | Cluster 侧 finalizer/collect retry 不重建 partial 输入列表 |

## 8. 复测命令与结果

```bash
# 本地/Cluster 仿真、partial、retry、checkpoint 定向回归
.venv/Scripts/python.exe -m pytest tests/test_agent_local_run.py tests/test_local_selena_runner.py tests/test_result_delivery.py tests/test_local_results.py tests/test_agent_result_outbox.py -q
# -> 50 passed, 2 skipped in 5.83s

.venv/Scripts/python.exe -m pytest tests/test_cluster_stage_executor.py -q
# -> 27 passed in 7.05s

# 合计 77 passed, 2 skipped（Python 3.12，.venv）
```

（`test_api_v1_service.py` 与 `test_windows_full_local_e2e.py` 中与 partial 相关的用例单独摘录引用，见第 3 节；未在本文件命令内整跑以控制时长。）

## 9. 结论

本地与 Cluster 链路的 **partial 语义、框架-引擎错误边界、逐输入 checkpoint/恢复、Cluster collect-retry 不重 submit、以及“成功输入不重复执行”的代码层保证均已实现并有自动化测试**；partial 的绝对边界（Connector 依赖、paramconfig、Runtime Bundle、传输不完整、失联、归档/Manifest 校验失败、全失败）在代码层都被阻止。

**阻断项（P1）**：任务书 6.3 要求的“只重试失败输入” API/SDK/Web 行为尚未实现，当前只能做 Stage 级重试，partial 任务无法只重跑失败 MF4。在补齐该能力并通过真实部署验收之前，Task H 的结论为“partial/retry/checkpoint 核心机制可受信内网使用，但失败输入重试能力未交付”。
