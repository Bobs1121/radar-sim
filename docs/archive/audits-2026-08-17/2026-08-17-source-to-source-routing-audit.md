# radar-sim 源到源传输与 local/Cluster 组合验收审计（Task M）

日期：2026-08-17
范围：`Windows 本地源 -> Cluster 目标`、`SDK/Linux 源 -> Cluster 目标`、共享路径原地读取、`Windows local 目标` 四种组合；`existing/build × local/Cluster` 组合测试；当前不支持的 `source_to_local`（远端 -> Windows 本地）必须返回稳定 `unavailable/needs_input`，不能静默绕路。
明确排除：Selena 引擎内部算法错误本身。

## 结论

数据正文在受控直传路径上确实不经过 Linux API：Linux 只下发/接收 `TransferPlan`（仅元数据）、进度和完成 manifest；文件字节由 Connector/SDK 通过 `core.direct_transfer.execute_transfer` 直接写入 Cluster 数据面（`client_target_root`）。Linux Cluster executor 只对目标文件做存在性/size probe，不解析、不哈希、不归档 MF4 正文。四个 source×target 组合都有自动化测试覆盖（本次定向回归 `109 passed, 2 skipped`）；真实 Windows 源->Cluster 目标、真实 Linux SDK 直传、真实共享路径读取和真实 Windows local 目标在开发机上不可实测，标记为“需要真实部署验收”。

不支持组合 `source_to_local`（远端 -> Windows 本地）在 API、TransferService、SDK 三层都以稳定错误码 `source_to_local_unavailable`（Stage 级 `needs_input` + `blocked`，计划签发接口 503）失败关闭，无任何静默回退到 Cluster-root 伪装或 Linux body 上传。

## 1. source×target 组合的 route/Stage/TransferPlan 矩阵

### 1.1 组合总览

| 组合 | 源 | 目标 | 路由证据（file:line） | prepare_data dispatch_scope | 是否下发 TransferPlan | 目标文件校验证据 |
|---|---|---|---|---|---|---|
| A. Windows 本地源 -> Cluster | 用户 Windows 路径（`agent` 分类） | Cluster | `core/api_v1.py:553-561` `direct_transfer_existing_cluster`；`core/api_v1.py:722-729` 选中 cluster 时 `_apply_direct_transfer_stage` | `direct_transfer`（`core/api_v1.py:868`） | 是，`shared_copy` 模式 | manifest entry.sha256 == 源哈希；Cluster probe 校验 size |
| B. SDK/Linux 源 -> Cluster | Linux SDK `/home/...` 路径 | Cluster | `radar_sim_sdk/client.py:315-433` `_auto_prepare_direct_transfers`；`core/api_v1.py:719-721` | `direct_transfer` | 是，SDK `issue_transfer_plan` + `execute_transfer_plan` | 同上 |
| C. 共享路径原地读取（zero-copy） | `dataset://` / `shared://` / central 逻辑路径 | Cluster | `core/api_v1.py:912-944`；`core/cluster_stage_executor.py:562-570` `_cluster_visible_reference` | `shared_reference`（`core/api_v1.py:916`），`transfer_required=False` | 否（zero-copy，`core/api_v1.py:913-919`） | DatasetRef/元数据只记录，不搬运 |
| D. Windows local 目标 | 与执行节点共置的 Windows 路径 | Windows local | `core/api_v1.py:730-735` 选中 local 时 `_apply_source_to_local_stage`；`core/stage_routing.py:27-42` `register_artifact_dispatch_scope` local 走 `local_runtime_registration` | `local_data`（`core/api_v1.py:653`） | 否（原地读取，`test_web_and_sdk_share_local_zero_transfer_scheduling` 断言 `transfers.calls == []`） | 本机已有文件原地使用 |

### 1.2 existing/build × local/Cluster 组合

| Selena 来源 | 目标 | 现状 | 证据 |
|---|---|---|---|
| existing + cluster（调用方本地输入） | Cluster | 直接传输：`resolve_spec`/`register_artifact` 跳过，`prepare_data` 为源端 barrier | `core/api_v1.py:589-594`（resolve_spec skip `runtime_bundle_direct_transfer`）、`617-622`（register_artifact skip）；`tests/test_control_data_plane_contract.py:402` `test_existing_cluster_direct_transfer_uses_prepare_data_barrier_without_agent_wait` |
| existing + cluster（共享可见） | Cluster | 完全 Cluster 可见时 Linux-only，跳过 Windows 解析/注册 | `core/api_v1.py:595-601` skip `existing_selena_is_cluster_visible`；`core/cluster_stage_executor.py:573-606` `_shared_existing_execution_expected`；`tests/...:382` `test_shared_existing_cluster_skips_windows_resolution_and_registration` |
| build + cluster | Cluster | 编译由 Windows Agent 完成，产物经 `register_artifact` 直传；`prepare_data` 元数据只声明，源码目录绝不当 runtime bundle 搬运 | `core/api_v1.py:819-824` 注释；`tests/test_control_plane_transfer_api.py:233` `test_build_direct_stage_never_treats_source_workspace_as_runtime_bundle` |
| existing + local | Windows local | 已注册 Bundle 或共置 Selena 保留本机，不建 TransferPlan | `core/api_v1.py:602-616` skip `existing_selena_kept_on_local_full_agent`；`core/stage_routing.py:38-41` |
| build + local | Windows local | 本机编译后本机运行，`register_artifact` 走本地 lease 复用 | `core/stage_routing.py:38-41`；`tests/test_stage_binder.py:192` `test_local_register_artifact_is_bound_as_local_lease_reuse` |

### 1.3 目标文件 checksum 证据（可证明处）

- 复制端：`core/direct_transfer.py:816-935` `_copy_file_with_resume` 在复制同时流式计算 SHA-256，`os.replace` 原子发布前校验 `partial` 与源一致（`core/direct_transfer.py:911-917`）；manifest entry 携带 `sha256`（`core/direct_transfer.py:918-927`）。
- 测试证据：`tests/test_direct_transfer.py:191` `test_nested_copy_streams_hash_and_publishes_atomically`（断言 `destination.read_bytes() == content` 且 `entry.sha256 == hashlib.sha256(content)`）；`tests/test_direct_transfer.py:222` `test_partial_resume_hashes_prefix_and_remainder`；`tests/test_direct_transfer.py:270` `test_idempotent_retry_reuses_matching_published_file`（二次重试 `status == "skipped"` 且 checksum 一致）。
- Linux 侧校验：Cluster executor 对目标只做存在性 + size probe（`core/cluster_stage_executor.py:1947-1956`），不重哈希正文；真实完整校验发生在复制端 manifest 生成时。因此“目标文件 checksum 证据”以复制端 manifest sha256 为准，Linux probe 只验证 size。

## 2. Linux 不接收大文件正文的证明（file:line）

1. **计划签发只收元数据**：`core/api_v1.py:1849-1876` `issue_transfer_plan` 仅接受 `{source_role, relative_path, size, checksum/sha256, mtime_ns}` 元数据，未知字段直接 422；注释 `core/api_v1.py:719-721`“Connector/SDK 在 Job/Stage 持久化后只把 `TransferPlanItem` 元数据发回”。
2. **manifest 只收元数据**：`core/api_v1.py:1935-2023` `receive_transfer_manifest` 逐条构造 `TransferManifestEntry`（relative_path/size/checksum/storage_ref），无 body 字段；`core/transfer_service.py:584`“Validate and persist metadata only; never read a transferred file”。
3. **TransferStore 明确无文件内容**：`core/transfer_service.py:249-250`“Small SQLite metadata store; no file content or physical target data”；`core/transfer_service.py:1-8` docstring“never opens source or destination files”。
4. **SDK 直写数据面**：`radar_sim_sdk/client.py:536-605` `execute_transfer_plan` 调用 `core.direct_transfer.execute_transfer` 直接写 `signed.client_target_root`，从不经过 `_request`；`radar_sim_sdk/client.py:456-462`“Linux receives only plan/progress/manifest metadata；execute_transfer_plan writes bytes directly to the signed target root and never sends a file body through `_request`”。
5. **复制内核**：`core/direct_transfer.py:938-1003` `execute_transfer` 在调用方本地打开源文件并复制到目标；Linux API 全程不接触源文件句柄。
6. **Cluster probe 边界**：`core/cluster_stage_executor.py:446-448` Linux Cluster 解析 `metadata_only=True`；`core/cluster_stage_executor.py:1947-1956`“Probe is deliberately bounded to existence/size metadata. Do not hash, parse or archive the object on Linux”。
7. **测试证明**：
   - `tests/test_control_data_plane_contract.py:123` `test_web_user_run_never_uploads_task_file_bodies_to_linux`：断言 `radar_sim_web/static/app.js` 不含 `api("/run-data-uploads"`、`api("/config-assets"`、`body: file`、`body: blob`，且页面文案含“文件正文不经过本 Linux Web 服务”。
   - `tests/test_control_data_plane_contract.py:144` `test_sdk_existing_cluster_local_paths_never_use_linux_body_uploads`：SDK `submit_run` 对 `_upload_existing_selena`/`import_existing_runtime_bundle`/`upload_runtime_bundle`/`upload_artifact`/`upload_config_asset` 全部 monkeypatch 为 forbidden，只允许一次 `POST /api/v1/run-jobs`。
   - `tests/test_control_data_plane_contract.py:226` `test_linux_control_plane_does_not_import_server_visible_existing_cluster_bodies`：断言 Linux `import_existing` 从未被调用（`calls == []`）。
   - `tests/test_control_data_plane_contract.py:255` `test_existing_cluster_agent_resolution_never_calls_linux_body_upload_helpers`：断言 Connector 不调用 body 上传 helper。
   - `tests/test_direct_transfer_clients.py:230` `test_agent_execute_plan_writes_bytes_and_only_reports_metadata`：复制字节 + 只上报元数据。

边界说明：`core/dataset_store.py`、`core/artifact_store.py` 存在历史 body-upload（`append_dataset_upload`/`append_artifact_upload` 等）用于浏览器 central 上传与本地结果归档，不属于“Windows/SDK 源 -> Cluster 数据面”这一受控直传路径；`docs/handoffs/resource-routing.md` 明确这些 legacy 方法“不被 Web、submit_run、submit_yaml、Skill 或 MCP 路径使用”。

## 3. 不支持组合 `source_to_local` 的稳定错误样例

### 3.1 代码路径与稳定错误码

- **计划签发接口（API）**：`core/api_v1.py:1799-1810`——`transfer_mode == "source_to_local"` 时抛 `ApiV1Error(code="source_to_local_unavailable", status_code=503)`，actions 指向 `use_co_located_inputs`。
- **TransferService**：`core/transfer_service.py:498-509`——`issue_plan(mode="source_to_local")` 抛 `TransferError("source_to_local_unavailable", status_code=503)`，actions `use_co_located_inputs`。模式只能由服务端 Stage 路由决定，请求体不能覆盖（`core/api_v1.py:1882-1885`）。
- **Stage 路由层**：`core/api_v1.py:1188-1271` `_apply_source_to_local_stage` 检测到 local 目标 + agent 分类本地源 + 无共置可读能力时，设置 `dispatch_scope=local_execution_unavailable`、`transfer_status=source_to_local_unavailable`，随后 `_block_source_to_local_tasks`（`core/api_v1.py:1274-1295`）把非 skipped Stage 置为 `blocked`、error code=`source_to_local_unavailable`、Job `status=needs_input`。
- **内核模式集**：`core/direct_transfer.py:23` `TRANSFER_MODES` 声明 `source_to_local`，但任何该模式的计划都无法签发（见上），因此 `execute_transfer` 永不收到 `source_to_local` 计划——不存在“伪装成 source_to_local”的复制。
- **SDK**：不实现第二套路由；等待/阻塞时把服务器 `waiting`/`blocked` 透传（`radar_sim_sdk/client.py:436-454` `_direct_transfer_waiting`），`needs_input`/`blocked` 在结果判断中保留（`radar_sim_sdk/client.py:857`）。
- **UI**：`radar_sim_web/static/app.js:1286-1288` `needs_input -> "需要处理"`、`blocked -> "已阻塞"`；`friendlyStageDetail`（`app.js:1312-1333`）对未知 code 回退到 `stage.error.message + action`，因此 `source_to_local_unavailable` 的 API error envelope（code + message + actions）会在 Web 稳定呈现，与 SDK/API 一致，且不隐藏绝对路径（API 错误信息为“The connected simulation computer cannot read one or more configured inputs”，无本地路径）。

### 3.2 无静默绕路的测试证明

- `tests/test_transfer_service.py:353` `test_source_to_local_is_rejected_without_target_specific_windows_cache`：断言 code=`source_to_local_unavailable`、status_code=503、且 `get_job_transfer_status` 无任何 plan（不落计划、不建隔离根）。
- `tests/test_control_plane_transfer_api.py` / `test_control_data_plane_contract.py:558` `test_missing_direct_transfer_capability_blocks_with_stable_status_not_http_upload`：同类 fail-closed 语义——缺数据面根时 Job `needs_input`、`prepare_data` `blocked`、code=`cluster_direct_transfer_unavailable`、`dispatch_scope != "data_upload"`。
- `tests/test_api_v1_service.py:962` `test_explicit_local_submission_waits_for_first_connector_instead_of_failing`：区分“首次无 Connector”的可恢复等待（`windows_connection_required`，不阻塞）与“有 Connector 但读不到远端输入”的 `source_to_local_unavailable` 阻断——前者断言任何 Stage error code 都不等于 `source_to_local_unavailable`，即不会被误判成永久阻断。

## 4. 自动化测试 vs 需要真实部署验收

### 4.1 本次定向回归结果

命令（实际执行的测试文件，`test_stage_routing.py` 不存在，brief 中该项为过期条目，已用 `test_control_plane_transfer_api.py` 与 `test_transfer_service.py` 替代）：

```text
.venv/Scripts/python.exe -m pytest tests/test_direct_transfer.py tests/test_direct_transfer_clients.py tests/test_control_data_plane_contract.py tests/test_control_plane_transfer_api.py tests/test_cluster_direct_refs.py tests/test_stage_binder.py tests/test_data_stage_binding.py tests/test_transfer_service.py -q
109 passed, 2 skipped, 1 warning in 7.87s
```

（2 skipped 为平台相关用例；1 warning 为已知 Starlette/httpx 弃用告警。）

### 4.2 组合 -> 自动化测试映射

| 组合 | 自动化测试（精确名称，全部通过） |
|---|---|
| A. Windows 本地源 -> Cluster | `test_control_data_plane_contract.py::test_existing_cluster_direct_transfer_uses_prepare_data_barrier_without_agent_wait`（Windows 语法 `D:/...` 路径）、`::test_web_user_run_never_uploads_task_file_bodies_to_linux`、`::test_existing_cluster_agent_resolution_never_calls_linux_body_upload_helpers` |
| B. SDK/Linux 源 -> Cluster | `test_control_data_plane_contract.py::test_sdk_existing_cluster_local_paths_never_use_linux_body_uploads`、`::test_linux_sdk_posix_sources_use_direct_transfer_hint_not_linux_body_route`；`test_direct_transfer_clients.py::test_agent_execute_plan_writes_bytes_and_only_reports_metadata`、`::test_agent_transfers_selena_and_each_config_asset_as_independent_role`、`::test_sdk_dataset_fingerprint_explicit_source_skips_mf4_reads` |
| C. 共享路径原地读取 | `test_control_data_plane_contract.py::test_shared_cluster_inputs_are_zero_copy_and_need_no_windows_connector`、`::test_shared_existing_cluster_skips_windows_resolution_and_registration`；`test_direct_transfer_clients.py::test_agent_mixed_shared_dataset_skips_lease_and_transfers_only_local_assets` |
| D. Windows local 目标 | `test_control_data_plane_contract.py::test_web_and_sdk_share_local_zero_transfer_scheduling`（断言 `transfers.calls == []`）；`test_api_v1_service.py::test_explicit_local_submission_waits_for_first_connector_instead_of_failing`、`::test_explicit_local_rejects_control_plane_only_data_reference`；`test_stage_binder.py::test_local_register_artifact_is_bound_as_local_lease_reuse` |
| 传输内核（partial/resume/checksum/隔离） | `test_direct_transfer.py::test_partial_resume_hashes_prefix_and_remainder`、`::test_corrupt_partial_is_restarted_instead_of_appended`、`::test_nested_copy_streams_hash_and_publishes_atomically`、`::test_idempotent_retry_reuses_matching_published_file`、`::test_cancellation_removes_partial_but_not_published_file`、`::test_source_size_change_during_copy_is_detected`、`::test_source_mtime_only_change_during_copy_is_detected`、`::test_planned_size_digest_and_mtime_are_enforced`、`::test_owner_job_and_transfer_each_change_destination`、`::test_isolation_path_contains_no_raw_owner_job_or_transfer`、`::test_manifest_never_contains_absolute_source_or_target_paths`、`::test_gateway_upload_is_explicitly_unavailable`、`::test_expired_plan_is_rejected_before_copy` |
| TransferPlan 完整性与 owner 隔离 | `test_control_plane_transfer_api.py::test_transfer_plan_requires_direct_stage_and_owner`、`::test_manifest_roles_complete_stage_only_after_all_resources`、`::test_build_direct_stage_never_treats_source_workspace_as_runtime_bundle`；`test_control_data_plane_contract.py::test_transfer_progress_and_plan_access_are_owner_isolated`；`test_transfer_service.py::test_owner_job_transfer_isolation_is_opaque_and_unique`、`::test_plan_metadata_round_trips_through_sqlite_restart` |
| 不支持组合 source_to_local | `test_transfer_service.py::test_source_to_local_is_rejected_without_target_specific_windows_cache`；`test_control_data_plane_contract.py::test_missing_direct_transfer_capability_blocks_with_stable_status_not_http_upload`；`test_api_v1_service.py::test_explicit_local_submission_waits_for_first_connector_instead_of_failing` |
| Cluster 直接引用（目标文件 probe） | `test_cluster_direct_refs.py::test_direct_resource_probe_checks_size_without_file_body`、`::test_probe_accepts_transfer_service_style_owner_resolver`、`::test_cluster_stage_context_derives_transfer_service_callbacks`、`::test_cluster_preflight_direct_refs_skips_linux_archive_and_copy`、`::test_dataset_worker_root_preserves_all_manifest_entries`、`::test_direct_dataset_can_use_shared_selena_and_independent_runtime_xml`、`::test_direct_selena_and_runtime_xml_can_use_shared_dataset` |
| 数据绑定/代理（不泄漏/不抢单） | `test_data_stage_binding.py::test_pending_data_stage_does_not_leak_to_unmatched_agent`、`::test_one_click_agent_binds_first_windows_data_path_without_linux_fallback`、`::test_one_click_data_bootstrap_never_claims_shared_or_central_paths`、`::test_successful_agent_direct_transfer_updates_path_free_resolved_spec` |

### 4.3 需要真实部署验收（本机无法证明）

以下必须在真实 Windows Connector + 真实 Cluster 共享/Linux 部署上复测（对应执行任务书 3.2/4A.2/9.1/第 12 节）：

- 真实 Windows 本地源 -> Cluster 目标：数百 MB MF4 直传、断网续传、服务/Connector 重启后从校验过 offset 续传、源变化丢弃 partial、目标磁盘满。
- 真实 Linux SDK 进程 -> Cluster 目标：`/home/...` 路径在无 Windows Connector 的 Linux 主机上直传共享目录。
- 真实共享路径原地读取：UNC/挂载共享存在性 + size probe、共享挂载延迟/权限丢失时 `CLUSTER_SHARED_DATA_UNAVAILABLE`。
- 真实 Windows local 目标：本机编译 + 本机批量运行 + 结果落盘 + ZIP 下载 checksum。
- 目标端文件 checksum 的端到端核对：复制端 manifest sha256 与最终 Cluster worker 读取的文件一致性，当前只在复制端单侧证明，Linux 侧仅 size probe。

## 5. 风险分级与未解决项

| 项 | 等级 | 状态 |
|---|---|---|
| 数据正文不经过 Linux API（受控直传路径） | P0 核心要求 | 已实现 + 已测（见第 2 节） |
| 不支持组合不静默绕路（source_to_local） | P0 | 已实现 + 已测（见第 3 节） |
| TransferPlan role/relative path/checksum/partial/resume/manifest 完整性 | P0 | 已实现 + 已测（内核层） |
| 真实 Windows->Cluster、Linux SDK->Cluster、共享读取、local 目标端到端 | P1 | 需要真实部署验收（本机无 Windows/Cluster 现场） |
| 共享路径在 Linux 上存在性/size probe 的部署挂载映射 | P1 | 代码路径已实现；真实 UNC mount map 需部署验证 |
| `web/` 目录为 7 月遗留旧版，`radar_sim_web/static/` 为被测试的现行 Web | P2 | 建议清理避免误读；不影响测试（测试指向 radar_sim_web/static/app.js） |
| `docs/audits` 无 `test_stage_routing.py`（brief 第 11 节 Task M 命令里的过期文件名） | P2 | 已用 `test_control_plane_transfer_api.py`/`test_transfer_service.py` 覆盖同一职责 |

## 6. 复测命令

```text
cd /d/RamboStar/idea/radar-sim
.venv/Scripts/python.exe -m pytest tests/test_direct_transfer.py tests/test_direct_transfer_clients.py tests/test_control_data_plane_contract.py tests/test_control_plane_transfer_api.py tests/test_cluster_direct_refs.py tests/test_stage_binder.py tests/test_data_stage_binding.py tests/test_transfer_service.py -q
```

本次结果：`109 passed, 2 skipped, 1 warning in 7.87s`。

## 7. 结论

Task M 四个 source×target 组合在代码与自动化测试层面均已实现：正文不经过 Linux API、TransferPlan 元数据完整、`source_to_local` 稳定 fail-closed。真实 Windows/Cluster/Linux SDK 端到端验收标记为“需要真实部署验收”，不宣称已通过线上验证。
