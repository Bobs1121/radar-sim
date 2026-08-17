# Cluster 提交、长队列与结果收集审计（Task J）

> 日期：2026-08-17
>
> 任务来源：`docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md` 第 11 节 Task J、第 4A.3 节、第 10 节风险 6/10/11、第 12 节验收矩阵。
>
> 审计对象：`core/cluster.py`、`core/cluster_runs.py`、`core/cluster_stage_executor.py`、`core/api_v1.py`（Cluster 相关 DAG/能力绑定）。
>
> 约束：本次为纯代码级审计 + 自动化测试，**未修改任何源码**，未提交任何 commit。真实长队列 Cluster 运行无法从本机触达，相关验收项统一标注为"需要真实部署验收"。

## 1. 结论

Cluster 提交去重、结果收集无固定总超时、大批量逐输入结果不截断这三条关键不变量，当前代码实现 + 自动化测试均成立：

| 审计项 | 结论 | 证据类型 |
|---|---|---|
| submit 成功后控制面重启不重复外部提交 | 已实现 + 已测试 | `ClusterRunStore` 收据 + `execute_cluster_submit` 恢复路径 |
| 外部 manager 返回 task count（非 durable job id）不丢失追踪 | 已实现 + 已测试 | Config.cfg 目录唯一查询键 |
| 状态页短暂不可达不误判失败 | 已实现 + 已测试 | collector 观测循环 + 退避 |
| 长队列/长运行不被固定总超时杀掉 | 已实现 + 已测试 | 无控制面 wall-clock deadline |
| 大批量 result.ini 不截断（fixed-50 修复） | 已实现 + 已测试 | `task_results` 全量保留 + 动态扫描上限 |
| 大批量输出匹配非 O(n²) | 已实现 + 已测试 | `output_by_parent` 路径索引 |
| 结果目录晚到 / 可重试 collect | 已实现 + 已测试 | 页面成功但 result.ini 未到齐 -> 继续观察；collect 复用 run_ref |
| 真实长队列 / 真实重启窗口 | **需要真实部署验收** | 本机无法触达 live Cluster |

测试基线：`.venv/Scripts/python.exe -m pytest tests/test_cluster.py tests/test_cluster_runs.py tests/test_cluster_stage_executor.py tests/test_cluster_direct_refs.py -q`
结果：`79 passed, 1 failed in 53.42s`。唯一失败 `test_cluster_check_allows_xmlrpc_without_python2` 是**环境依赖问题**（本地 venv 未安装 `asammdf`，`MF4 acquisition source reader` 检查项无法通过），与 Cluster 提交/收集逻辑无关，需要部署机补齐 `asammdf` 后复测，不算代码回归。

---

## 2. 提交去重：submission receipt + Config.cfg 唯一查询路径 + recovery

### 2.1 收据先于本地状态落库（防 submit 与 mark_submitted 之间的重启窗口）

`core/cluster_runs.py`：

- `cluster_runs` 表含 `submission_receipt_json` 列，且 `UNIQUE(owner, control_job_id)`（`core/cluster_runs.py:110-114`），同一控制 Job 只能有一条 Cluster run 记录。
- `create_run()` 幂等：同一 `owner+control_job_id` 已存在时，若已解析输入完全一致则直接返回既有 run，不新建（`core/cluster_runs.py:176-189`）；若输入不一致则抛 `ClusterRunStoreError`（fail-closed）。
- `record_submission_receipt()` 在 `mark_submitted()` 之前把外部副作用持久化（`core/cluster_runs.py:240-276`），docstring 明确"Cluster submission is not transactional with SQLite...A restarted executor can adopt it without issuing a second external submission"（`core/cluster_runs.py:248-254`）。
- `get_submission_receipt()` 返回一条私有收据，含 `control_job_id` 身份一致性校验（`core/cluster_runs.py:278-290`）。

### 2.2 submit 阶段的恢复顺序（receipt -> Config.cfg 反查 -> 才真正 submit）

`core/cluster_stage_executor.py` 的 `execute_cluster_submit()`：

1. 先读收据：`get_submission_receipt()`（`core/cluster_stage_executor.py:1062`）。若有，直接 `mark_submitted(..., submit_mode="recovered-receipt")`，返回 `recovered_existing_submission=True`，**不再调用外部 submit**（`core/cluster_stage_executor.py:1063-1076`）。
2. 无收据时，先按唯一 Config.cfg 目录向 manager 状态页反查：`get_cluster_web_status(config, _cluster_status_query(lease))`（`core/cluster_stage_executor.py:1084-1092`）。若页面找到既有外部 job id，则 `record_submission_receipt(...submit_mode="recovered-existing-submission")` + `mark_submitted(...)`（`core/cluster_stage_executor.py:1094-1112`），同样不重复提交。
3. 只有"既无收据、又未反查到既有外部 job"时才真正 `submit_cluster_job(lease.config_path, config, dry_run=False)`（`core/cluster_stage_executor.py:1114`）。
4. 提交成功后立即 `record_submission_receipt` -> `mark_submitted`（`core/cluster_stage_executor.py:1144-1152`），把"外部副作用已发生"的窗口收口。

### 2.3 Config.cfg 唯一查询路径

- `_cluster_status_query()` 返回 `PureWindowsPath(config_path).parent`，即生成的唯一 Config.cfg 目录（`core/cluster_stage_executor.py:1417-1426`）。docstring 说明这是"portable lookup key"，即使外部提交返回值只是 task count（例如 `12`）也能用它解析真实 job（例如 `10357`）。
- `core/cluster.py` 的 `_find_web_job_id_by_path()` 在 jobs 页面按 Config.cfg 路径反查 durable job id（`core/cluster.py:407-421`）。
- `_external_job_id()` 解析 stdout 末行，剥离 `value=` 前缀（`core/cluster_stage_executor.py:2141-2147`）。

### 2.4 提交失败是"可重试 Stage"而不是丢 Job

- 外部 submit 返回非零 / 超时 / 拒绝时抛 `ClusterStageExecutionError`，code 为 `CLUSTER_GATEWAY_UNREACHABLE`（网络类）或 `CLUSTER_SUBMISSION_REJECTED`（manager 明确拒绝），actions 为 `retry_stage`（`core/cluster_stage_executor.py:1115-1142`）。此时 run 保持 `prepared`，重试该 Stage 不会重建 Selena、不会重新打包、不会重复传输。
- 提交握手只有独立可配置的 transport timeout：`_cluster_submission_timeout_seconds()`（默认 120s，`core/cluster.py:1124-1141`）+ `_TimeoutTransport`（`core/cluster.py:1144-1152`）。它只限制提交请求本身（brief 第 0.3 条允许的安全边界），不限制仿真运行。

### 2.5 测试证据（提交去重）

- `tests/test_cluster_runs.py:43` `test_run_is_owner_isolated_and_idempotent_per_control_job`：同 `owner+control_job_id` 重复 `create_run` 返回同一 ref；跨 owner 不可见；输入不一致抛错。
- `tests/test_cluster_runs.py:69` `test_submission_receipt_survives_before_run_state_commit`：收据在 run 仍为 `prepared` 时即可读取，随后 `mark_submitted(submit_mode="recovered-receipt")` 成功。
- `tests/test_cluster_stage_executor.py:198` `test_submit_adopts_durable_receipt_without_second_external_submission`：有收据时，`submit_cluster_job` 被 monkeypatch 成"调用即抛 AssertionError"（绝不允许第二次外部提交），`execute_cluster_submit` 返回 `recovered_existing_submission=True` 且 `external_job_id="10321"`。
- `tests/test_cluster_stage_executor.py:163` `test_submit_transport_failure_is_retryable_without_rebuilding`：`<urlopen error timed out>` -> `CLUSTER_GATEWAY_UNREACHABLE`，run 保持 `prepared`。
- `tests/test_cluster_stage_executor.py:749` `test_collect_queries_by_generated_job_directory_and_waits_for_every_dataset_file`：部署返回 task count `"2"`，collector 用 `_cluster_status_query` 生成目录反查，最终等待 2 个输入全部落齐。

---

## 3. 长队列 / 状态页短暂不可达：observing，不是 failed

`core/cluster_stage_executor.py` 的 `execute_cluster_collect()`（`core/cluster_stage_executor.py:1156-1405`）：

1. 无固定总 deadline。docstring 明确："Collection is intentionally open-ended...this control-plane observer must not impose a second wall-clock deadline. Long batches can finish after hours, and a temporary status-page outage must not turn a still-running external job into a terminal control failure"（`core/cluster_stage_executor.py:1181-1188`）。主循环 `while True:`（`core/cluster_stage_executor.py:1189`）。
2. 证据优先级：先检查受控共享结果目录（`_inspect_cluster_job_for_collection`，`core/cluster_stage_executor.py:1204`），再访问状态页（`get_cluster_web_status`，`core/cluster_stage_executor.py:1228`）。目录证据完成即终态，不依赖页面。
3. 状态页短暂不可达 -> 观察降级，不退化为失败。`_is_transient_cluster_gateway_error()`（`core/cluster_stage_executor.py:2165-2197`）识别 `timed out / connection refused / connection reset / urlopen error / http 502-504 / bad gateway / service unavailable / 连接超时 / 无法访问` 等网络类错误；对这些错误 `gateway_error_streak += 1`，指数退避 `min(15.0 * 2 ** min(streak-1, 3), 120.0)` 后 `continue`（`core/cluster_stage_executor.py:1230-1247`）。退避只影响下一次观测时间，不是仿真超时。
   - 注意：普通路径查不到（`job id not found`）不被当作 transient 错误（`core/cluster_stage_executor.py:2174-2176`），而是正常继续轮询——这正是"提交后页面暂时看不到任务"的处理。
4. 页面显示 succeeded 但 result.ini/MF4 未到齐 -> 保持 `running`。`terminal_status_without_result_streak` 递增并告警（`core/cluster_stage_executor.py:1282-1298`），继续等待共享目录证据，不把复制延迟判为失败。
5. 长队列 / 长仿真由 Cluster 自身的 Config.cfg `timeout` 管理（写入 `timeout = <timeout_min>;`，`core/cluster.py:1628`，默认 120，`core/cluster.py:146`），控制面不再叠加第二个 deadline。这是执行级保护，不是控制面"结果未可见就猜失败"。
6. 状态页是观测源：`_terminal_state()`（`core/cluster_stage_executor.py:2150-2162`）从页面 tasks 推导状态，但只有 `result.ini`-based 目录证据（`success_count/fail_count`）才真正收口（`core/cluster_stage_executor.py:1314-1397`）。页面 `succeeded` 但 `result.ini` 报失败时，collect 以 result.ini 为准覆盖为 failed（`core/cluster_stage_executor.py:1352-1364`）。

### 测试证据（无固定超时 / 状态页不可达）

- `tests/test_cluster_stage_executor.py:234` `test_collect_gateway_outage_keeps_observing_until_shared_result`：状态页一直 `Connection refused`，`sleep_fn` 把虚拟时钟一次 +7200s（跨过旧的一分钟观察窗口），共享目录完成后才 `succeeded`——证明收集不被固定 wall-clock deadline 杀掉。
- `tests/test_cluster_stage_executor.py:308` `test_collect_uses_complete_shared_results_when_status_gateway_is_unreachable`：共享结果已完成时，`get_cluster_web_status` 被 monkeypatch 成"调用即 pytest.fail"，证明目录证据优先级高于页面。
- `tests/test_cluster_stage_executor.py:359` `test_collect_waits_for_result_ini_after_official_completion`：页面已 `finished`，但结果目录先只有 MF4、后 result.ini 才到齐；虚拟时钟 +7200s 后成功——结果复制晚到不误判失败。
- `tests/test_cluster_stage_executor.py:488` `test_collect_uses_result_ini_when_official_page_has_no_tasks`：页面无任务行（V2 部署常见），仅靠目录 result.ini 收口并正确判定 `failed`（`result.ini` 报告失败）。
- `tests/test_cluster_stage_executor.py:531` `test_collect_overrides_web_succeeded_when_result_ini_reports_failure`：32 个任务页面全 `finished`，但 32 个 result.ini 全失败（Selena return -1），collect 覆盖为 `failed`——这是 2026-08-14 线上真实故障的回归测试。
- `tests/test_cluster.py:767` `test_get_cluster_web_status_preserves_readable_state`：状态页解析出 job id、`simulating`、worker_hosts。
- `tests/test_cluster.py:699` `test_cluster_client_submission_timeout_is_retryable`：client 提交超时返回 returncode 124，可重试。

---

## 4. 大批量结果数量 vs Manifest：动态扫描上限、O(n) 输出索引、fixed-50 修复

### 4.1 逐输入 result.ini 不截断（fixed-50 修复）

- `core/cluster.py` 的 `inspect_cluster_job()`：`task_results` 全量保留，docstring 明示"Keep every per-input result for the V2 collector. The collector may publish a large batch manifest; silently retaining only the first 50 made a successful batch look complete while losing its truth data."（`core/cluster.py:595-598`）。`success_count/fail_count` 由全部 `task_results` 统计（`core/cluster.py:563-564`）。代码中不存在 `task_results[:50]` / `result_files[:...]` 截断。
- 默认首轮扫描上限 `max_files=500`（`core/cluster.py:522`），`truncated` 标记真实返回（`core/cluster.py:538-539, 602`）。

### 4.2 动态扫描上限（不再固定 10,000 文件）

`_inspect_cluster_job_for_collection()`（`core/cluster_stage_executor.py:1429-1459`）：

- 首轮 `inspect_cluster_job(job_dir)` 若 `truncated=True` 且尚无 result.ini，做一次有界复扫。
- 复扫上限按预期输入数动态放大：`retry_limit = max(1000, count * 8 + 512)`（`core/cluster_stage_executor.py:1452`），其中 `count` 来自 `_expected_cluster_task_count(job)`（数据集 `file_count`，`core/cluster_stage_executor.py:1408-1414`）。数据集允许最多 20,000 个输入文件，复扫按每个输入的 result.ini + MF4 + 日志预留空间，不再给 Cluster 路径叠加第二个隐藏的 10,000 文件上限。
- `_collection_probe_is_complete()`：`expected_count` 下成功+失败数不足时不判完成（`core/cluster_stage_executor.py:1462-1484`）；页面/目录任一证据达到终态才收口。

### 4.3 O(n) 输出索引（大批量不 O(n²)）

`_cluster_input_results()`（`core/cluster_stage_executor.py:1501-1555`）：

- 先把所有输出 MF4 的 parent 建字典 `output_by_parent`（`core/cluster_stage_executor.py:1517-1524`），再对每个 result.ini 用 `task_relative` 直接查 `output_by_parent.get(task_relative.casefold(), "")`（`core/cluster_stage_executor.py:1535`）得到对应输出，避免逐对线性扫描。构建成本 O(输出数)，查询 O(1)。
- 每个输入生成 path-safe 的 `input_results` 行：`index/input_relative_path/result_relative_path/output_relative_path/status/returncode/error_code`（`core/cluster_stage_executor.py:1544-1554`），全部写入 summary（`core/cluster_stage_executor.py:1393`）。

### 4.4 Manifest 数量对比

- `finalize_result` 的 summary 含 `file_count / success_count / fail_count / succeeded_input_count / failed_input_count / total_input_count / input_results`（`core/cluster_stage_executor.py:1384-1395`）。
- `build_public_run_manifest()` 把 `input_results` 放入公开 manifest，并按逐输入状态推导 `partial`（`core/cluster_stage_executor.py:1606-1642, 1657-1665`）：有成功 + 有失败且文件/输入存在 -> `partial`。
- `result_ref` 对 `(run_ref, state, files, summary)` 做确定性 SHA-256，同一内容幂等（`core/cluster_runs.py:317-342`）。

### 4.5 测试证据（大批量）

- `tests/test_cluster_stage_executor.py:659` `test_cluster_batch_input_results_are_not_truncated`：250 个 result.ini 全部生成 `input_results`，`len(rows)==250`，最后一条 `index==250`。
- `tests/test_cluster_stage_executor.py:424` `test_collect_rechecks_truncated_directory_before_declaring_result_ini_missing`：首轮 500 文件截断 -> 复扫 `max_files=1000` -> 拿到 result.ini；断言调用序列 `[("inspect", None), ("inspect", 1000), ("inspect", None)]`。
- `tests/test_cluster_stage_executor.py:590` `test_partial_cluster_result_keeps_each_input_outcome_in_manifest`：1 成功 + 1 失败 -> run state `failed`，但 manifest 为 `partial`，`input_results` 两条状态为 `["succeeded","failed"]`。
- `tests/test_cluster.py:477` `test_inspect_and_fetch_cluster_job_outputs`、`tests/test_cluster.py:501` `test_inspect_cluster_job_rejects_worker_success_without_output`：`finished-success` 判定要求非空输出 MF4 + result.ini。

---

## 5. 诊断样例：Cluster 不可达 / 结果目录晚到 / 可重试 collect

### 5.1 稳定错误码（不泄露私有路径，给用户动作）

| 场景 | code | actions | 位置 |
|---|---|---|---|
| 提交握手网络类失败（timeout/refused/unreachable） | `CLUSTER_GATEWAY_UNREACHABLE` | `retry_stage` | `cluster_stage_executor.py:1140` |
| manager 明确拒绝提交 | `CLUSTER_SUBMISSION_REJECTED` | `retry_stage` | `cluster_stage_executor.py:1140` |
| 环境依赖缺失（manager/凭证/worker 路径） | `CLUSTER_ENVIRONMENT_UNAVAILABLE` | `retry_stage` | `cluster_stage_executor.py:530-534` |
| 共享数据不可达/未挂载/权限 | `CLUSTER_SHARED_DATA_UNAVAILABLE` | `check_shared_path` | `cluster_stage_executor.py:421-429, 464-477` |
| 结果不完整（成功+失败 < 预期） | 可重试异常（无固定 code，message 明示"collection can be retried without rerunning simulation"） | 由 Stage 重试驱动 | `cluster_stage_executor.py:1320-1323` |
| 结果目录源文件在归档时变化 | 抛异常，run 保持 `running` 可重试 | 重试 collect | 见测试 `test_collect_archive_failure_does_not_make_cluster_run_terminal` |

### 5.2 观测期日志样例（真实诊断痕迹）

```text
Cluster status observation unavailable; continuing output polling (consecutive_failures=1, error=<urlopen error [Errno 111] Connection refused>)
Cluster reports success but shared results are not complete; continuing collection (observations=1)
Cluster results are incomplete; collection can be retried without rerunning simulation
```

分别对应 `cluster_stage_executor.py:1239-1246`（状态页不可达）、`1290-1298`（结果目录晚到）、`1321-1323`（结果不完整可重试）。

### 5.3 可重试 collect 不重新 submit

- `collect_results` 通过 `run_simulation` 已固化的 `cluster_run_ref` 复用同一个 ClusterRun（`core/cluster_stage_executor.py:295-305`），`finalize_manifest` 只消费 `result_ref`（`core/cluster_stage_executor.py:306-322`），不重跑仿真、不重新提交。
- 取消：`cancelled()` 回调置 run 为 `cancelled` 并固化空文件结果（`core/cluster_stage_executor.py:1190-1196`），保留已固化的成功结果，不自动重跑。
- 测试：`tests/test_cluster_stage_executor.py:837` `test_collect_archive_failure_does_not_make_cluster_run_terminal`：归档抛 `RuntimeError("source changed")` 时 run 仍为 `running`（可重试，不终态）；`tests/test_cluster_stage_executor.py:139` `test_collect_cancellation_creates_path_free_terminal_result`：取消产生 path-free `cancelled` 结果。

---

## 6. 已实现 + 已测试 vs 仅文档声明

严格区分"实现+测试"与"仅代码级、需真实部署验收"：

| 项 | 状态 |
|---|---|
| 提交去重（收据/Config.cfg 反查/幂等 create_run） | 实现 + 单元/集成测试通过 |
| 无固定总超时（collector open-ended） | 实现 + 测试通过（虚拟时钟 +7200s） |
| 状态页不可达 -> 观察降级 | 实现 + 测试通过 |
| 结果目录晚到 -> 继续观察 | 实现 + 测试通过 |
| 大批量 result.ini 不截断 + 动态扫描上限 | 实现 + 测试通过（250 输入 / 复扫 1000） |
| O(n) 输出匹配 | 实现 + 测试通过 |
| collect 可重试不重新 submit | 实现 + 测试通过 |
| 端到端 Cluster 管线（preflight->submit->collect->finalize） | 实现 + 测试通过（`test_existing_bundle_cluster_pipeline_finishes_without_windows_or_adapter`，`test_cluster_stage_executor.py:1123`） |
| 真实长队列 Cluster 运行（live server） | **需要真实部署验收** |
| 真实"submit 成功 -> 控制面重启 -> 恢复"窗口 | **需要真实部署验收**（本机无法制造生产重启窗口；代码路径由收据+反查覆盖） |
| 真实 250+ 输入生产批量 | **需要真实部署验收** |
| 真实状态页持续宕机数小时 + 结果晚到数小时 | **需要真实部署验收** |

---

## 7. 风险等级与未解决项

| 风险 | 等级 | 说明 |
|---|---|---|
| 提交接口无幂等 request ID，收据窗口仍有残余分布式边界 | P2 | `execute_cluster_submit` 通过收据 + Config.cfg 反查把窗口收口到"manager 接受请求后、任何收据/状态可见前同时故障"这一最小集合；彻底消除需 Cluster 提交接口提供幂等 request ID（见 `2026-08-17-non-engine-failure-audit.md` 第 7 节）。 |
| 共享结果目录永久不可达 | P2 | collector 保持观察（非终态），需要部署方增加"外部任务已终态但结果目录无活动"的 inactivity 告警；是否自动转 retryable failure 由部署策略决定，不能硬编码仿真总时长。 |
| 阻塞式 collector 长期占住一个 worker | P2 | 当前为阻塞 collector + 独立 heartbeat（低风险最小变更）；完整演进为异步 reconciler（DB lease + 短轮次）为后续 Sprint 项（见 `2026-08-14-cluster-collection-resilience.md` 第 5 节）。 |
| 本地 venv 缺 `asammdf` 导致 1 个环境类测试失败 | P3 | 非代码回归；部署/CI 机需 `pip install .[v5-server]` 后复测 `test_cluster_check_allows_xmlrpc_without_python2`。 |

未解决/需补做：
1. 真实部署验收：长队列、提交后控制面重启、250+ 输入生产批量、状态页长宕机（第 6 节清单）。
2. 异步 reconciler 的 schema、lease claim 与并发接管测试（独立 Sprint）。
3. 结果共享目录永久不可达的运维告警与可配置的部署级回收策略。

## 8. 复测命令

```bash
cd /d/RamboStar/idea/radar-sim
.venv/Scripts/python.exe -m pytest tests/test_cluster.py tests/test_cluster_runs.py tests/test_cluster_stage_executor.py tests/test_cluster_direct_refs.py -q
# 预期：79 passed, 1 failed（asammdf 环境依赖，需部署机补齐后复测）
```

## 9. 关联文档

- `docs/handoffs/2026-08-14-cluster-collection-resilience.md`（本审计的线上修复背景：`CLUSTER_GATEWAY_UNREACHABLE` 误判与 open-ended collector）
- `docs/handoffs/2026-08-17-non-engine-failure-audit.md` 第 7、8 节（Cluster 提交/结果收集故障树与当前保护）
- `docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md` 第 4A.3、10（风险 6/10/11）、11（Task J）、12 节
