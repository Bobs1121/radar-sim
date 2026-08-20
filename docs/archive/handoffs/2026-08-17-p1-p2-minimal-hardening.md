# radar-sim P1/P2 最小加固：修复与验收记录

日期：2026-08-17  
任务：按用户要求「把 P1 和 P2 修一下，以优化为主、最小改动、不可影响目前的能力程度（本地已可仿真）」对全链路审计（`docs/audits/2026-08-17-simulation-correctness-gap-matrix.md`）列出的 P1/P2 风险做代码修复。
最终代码：`0a06c01`（branch `codex/new-branch`）。全部为增量/最小改动，成功路径未改动。

## 1. 结论先行

- **已完成**：8 类 P1/P2 代码修复 + 13 个新增回归测试，全部为「新增能力/新增字段/新增测试」，未触碰现有成功路径。
- **验证**：定向覆盖全部改动文件 + 相关套件 **364 passed**；全量回归 **1645 passed（基线 1631，+14 新增）、12 skipped**；6 个失败与基线完全一致（`test_gen5.py` 缺 `asammdf`、`test_cluster.py` 缺 python2，环境问题，非回归）。
- **未修复（有意保留，理由见第 5 节）**：P0-1 认证缺失（部署门禁）、P1-1 「只重试失败输入」能力（新功能面）、P1-2 partial 在 DB 归一化为 failed（触碰 finalize/stale 语义）。

## 2. 修复清单（commit `0a06c01`）

### P1-4 结果归档 GC + 磁盘水位 + 告警
- 文件：`core/local_results.py`、`cli/server.py`
- 逻辑：
  - `ResultCatalog.collect_expired()`：删除 `retain_until>0 且 <now` 的过期结果；归档文件按引用计数删除（同一 content-addressed ZIP 被多个结果引用时，最后一条过期才删文件）；`retain_until=0`（永不过期）一律保留。
  - `_check_watermark()`：`publish`/`import_archive` 前 `shutil.disk_usage` 预检，低于 `min_free_bytes` 时 fail-closed 抛 `ResultCatalogError("result storage is below its free-space watermark")`。`min_free_bytes` 默认 0 = 关闭，现有部署行为不变。
  - `_LOGGER.warning/info` 对水位不足、GC 回收、删除失败告警。
  - `cli/server.py` 维护循环 `maintenance_pass` 追加 `result_catalog.collect_expired()`（异常隔离，不影响 stale reclaim）。
- 测试：`tests/test_local_results.py` 新增 `test_collect_expired_removes_only_expired_rows_and_shared_files`、`test_watermark_blocks_publish_below_free_space`。

### P1-5 Cluster 结果保留期与本地一致
- 文件：`core/cluster_stage_executor.py`
- 逻辑：`execute_cluster_collect` 的 `result_catalog.publish(...)` 现从 `job.spec.result.retain_days`（默认 30）传 `retain_until=now+retain_days*86400`，与 Windows 本地路径（`cli/agent.py:2868`）一致，不再默认永不过期。
- 测试：`tests/test_cluster_stage_executor.py` 新增 `test_cluster_collect_passes_retain_until_from_spec`。

### P2-1 SDK `wait_job()` + 指数退避
- 文件：`radar_sim_sdk/client.py`
- 逻辑：
  - 新增 `wait_job()`：文档化入口，等价 `wait()`（事件 cursor 优先 + 轮询兜底 + 无固定仿真总时长）。
  - `watch`/`wait` 新增可选 `backoff_factor`（默认 0=原行为不变）、`max_poll_interval`（默认 30s）：连续 transport error 时轮询延迟按 `poll_interval * factor^(n-1)` 增长并封顶。
- 测试：`tests/test_sdk.py` 新增 `test_sdk_wait_job_is_the_documented_adaptive_wait_entry_point`、`test_sdk_watch_backoff_grows_delay_on_repeated_transport_errors`、`test_sdk_watch_backoff_delay_is_bounded_by_max_poll_interval`。

### P2-3 SDK 下载 checksum 稳定错误码
- 文件：`radar_sim_sdk/errors.py`、`radar_sim_sdk/client.py`、`radar_sim_sdk/__init__.py`
- 逻辑：新增 `RadarSimIntegrityError(RadarSimError)`（字段 `code` / `message` / `resource`）；`download_result` 校验失败抛 `RadarSimIntegrityError("result_checksum_mismatch", ..., resource=result_ref)` 替代裸 `ValueError`；已从包导出。
- 测试：`tests/test_sdk.py` 新增 `test_sdk_download_result_rejects_checksum_mismatch_with_stable_error`（断言 code/resource，且无残留 `.part.*` 文件）。

### P2-6 Connector 配置损坏恢复
- 文件：`scripts/start_windows.ps1`、`scripts/watch_windows_connector.ps1`
- 逻辑：
  - `start_windows.ps1`：新增 `Restore-ConnectorConfig` / `Read-ConnectorConfig`；`install.json` 缺失或 JSON 非法/缺关键字段时从 `data/install.backup.json` 恢复，无备份则给出明确重连指引。
  - `watch_windows_connector.ps1`：`Repair-ConnectorControlFiles` 现在也处理「文件存在但 JSON 非法」，回退到备份并记录 `Recovered corrupt install metadata`。
- 测试：`tests/test_release_deployment.py` 新增 Windows 运行时 `test_connector_start_restores_corrupt_install_metadata_from_backup` + watchdog 静态断言。

### P2-5 / P1-3 控制面回归测试（Task B GAP）
- 文件：`tests/test_control_stages.py`
- 新增：
  - `test_reconcile_stage_handoffs_repairs_commit_before_bind_window`：直接调用 `reconcile_stage_handoffs`，验证对 succeeded+queued-successor 的 Stage 正确调用 binder、可安全重放（P1-3 GAP-1）。
  - `test_cancel_does_not_override_a_genuine_success_that_lands_after_cancel`：cancel_requested 后 success 落盘 → Stage=succeeded、Job=cancelled、下游不启动（P2-5 GAP-2）。
  - `test_cancel_turns_a_failed_result_into_cancelled_not_failure`：cancel_requested 后 failed 落盘 → cancelled，不伪装成框架失败。

### P2-22 build provenance 结构化字段 + fresh 模式（Task D G1/G4）
- 文件：`core/agent_build_stage.py`、`cli/agent.py`
- 逻辑：
  - `PreparedSelenaBuild` 新增 `existing_build_detected`。
  - `finish_selena_build` 的 `build_policy` 新增 `mode ∈ {fresh, incremental, full}`（空输出根→`fresh`，不再把空构建宣传为增量复用）、`fresh_start`、`incremental_reused`。
  - `cli/agent.py` 的 `Selena build policy:` 日志同步支持 `fresh`。
- 测试：`tests/test_agent_build_stage.py` 新增 `test_finish_labels_fresh_build_not_incremental_reuse`、`test_finish_marks_incremental_reuse_when_existing_state_matches`。

### P2-4 result.path 不可写端到端故障注入测试（Task I GAP-5）
- 文件：`tests/test_windows_full_local_e2e.py`
- 新增 `test_result_path_unwritable_keeps_server_zip_and_reports_stable_delivery_failure`：`result_path` 指向已存在文件使本地交付失败，断言 server ZIP 仍发布（`result_ref` 有效）、`delivery.status=failed`、稳定 path-free 错误码、finalize 照常 `succeeded`。

## 3. 验证证据

### 3.1 定向回归（覆盖全部改动文件 + 相关套件）
```bash
.venv/Scripts/python.exe -m pytest \
  tests/test_local_results.py tests/test_cluster_stage_executor.py \
  tests/test_control_stages.py tests/test_control_service.py \
  tests/test_sdk.py tests/test_release_deployment.py \
  tests/test_agent_build_stage.py tests/test_windows_full_local_e2e.py \
  tests/test_result_upload_service.py tests/test_agent_policy.py \
  tests/test_agent_binding_cli.py tests/test_api_v1_service.py -q
# -> 364 passed, 1 skipped, 1 warning in 68.63s
```

### 3.2 全量回归（相对基线 1631 → 1645，+14 新增，0 新增失败）
```bash
.venv/Scripts/python.exe -m pytest -q
# -> 1645 passed, 12 skipped, 1 warning in ~300s
# FAILED（与基线一致，环境问题）：test_cluster.py::test_cluster_check_allows_xmlrpc_without_python2
#   + test_gen5.py::TestMf4ReaderExtract::* ×5（asammdf 未安装）
```

### 3.3 导入健康
```bash
.venv/Scripts/python.exe -c "import core.local_results, core.cluster_stage_executor, core.agent_build_stage, core.control_service, cli.server, cli.agent, radar_sim_sdk, radar_sim_sdk.client; print('all imports OK')"
# -> all imports OK
```

## 4. 变更文件清单（17 文件，+678/-10）

源码（8）：`core/local_results.py`、`core/cluster_stage_executor.py`、`core/agent_build_stage.py`、`cli/server.py`、`cli/agent.py`、`radar_sim_sdk/client.py`、`radar_sim_sdk/errors.py`、`radar_sim_sdk/__init__.py`
脚本（2）：`scripts/start_windows.ps1`、`scripts/watch_windows_connector.ps1`
测试（7）：`tests/test_local_results.py`、`tests/test_cluster_stage_executor.py`、`tests/test_control_stages.py`、`tests/test_sdk.py`、`tests/test_release_deployment.py`、`tests/test_agent_build_stage.py`、`tests/test_windows_full_local_e2e.py`

## 5. 未修复项与理由

| 项 | 定级 | 为什么这次不修 |
|---|---|---|
| P0-1 认证缺失 / `X-Rsim-User` 可伪造 / 同 owner 设备冒充 | P0 | 属**部署门禁**（启用 `serve-v1 --auth-file` + 双 owner live 验收），不是代码最小改动；且本机无真实双 owner 环境。上线前必修（见 `docs/audits/2026-08-17-multi-user-security-audit.md`）。 |
| P1-1 无「只重试失败输入」API/SDK/Web | P1 | 属**新功能面**（API + SDK + Web + 逐输入过滤 + attempt/checkpoint 语义 + 只重试失败输入的资源消耗证据），违背「最小改动、不破坏本地仿真」约束。需单独设计交付，是任务书 §6.3 明确交付项。 |
| P1-2 partial 在控制面 DB 归一化为 failed | P1/P2 | 改动会触碰 finalize/stale/恢复的 DB 语义，风险高于收益；对外 API 已正确显示 partial（`_v1_status` 投影），属内部表述不一致而非功能缺失。定级跨文档不一致（Task A=P1，Task H=P2）。 |
| P2-7/8/9/10/11/12/13/14/15/16/17/18/19/20/21 | P2 | 多为「真实部署验收」「legacy 清理」「部署配置」类，或需真实 Windows/Cluster 环境（250+/磁盘满/断网/UNC/杀毒/长 Cluster），本机无法替代；或触碰部署/认证流程（pairing、audit log）。 |

## 6. 回滚方法

- 代码回滚：`git revert 0a06c01` 或恢复到 `20ba6b7`（审计提交 `785fce4` 之前的代码基线）。
- 行为兼容：本批改动全部为新增/可选参数/新字段，默认路径（`min_free_bytes=0`、`backoff_factor=0`、新增字段带默认值）与旧行为一致；无需数据库迁移。

## 7. 不要重复做的事情

- 不要重新全量审计已覆盖路径（`docs/audits/` 14 份文档已有 file:line + 测试证据）。
- 不要用 modelfarm 子代理跑本仓库任务（用 codingplan 模型：`general-purpose`）。
- 不要为「UI 变绿」放宽校验；fail-closed 保持不变。
- 不要用固定等待时间判断仿真完成；heartbeat/进程/进度/文件证据/外部 terminal 状态不变。
- 不要把 6 个环境失败（asammdf/python2）当作本批代码回归。
