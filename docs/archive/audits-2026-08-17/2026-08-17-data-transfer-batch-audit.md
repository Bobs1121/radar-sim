# 数据、TransferPlan、断点续传与批量输入审计（Task G）

> 日期：2026-08-17
>
> 任务来源：`docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md` 第 11 节 Task G、第 4A.2 节、第 9 节（9.1/9.2）、第 10 节风险 9/16、第 12 节验收矩阵。
>
> 审计对象：`core/direct_transfer.py`、`core/transfer_service.py`、`core/dataset_store.py`、`core/artifact_store.py`、`core/dataset_upload_service.py`、`core/artifact_upload_service.py`、`core/cluster_stage_executor.py`、`core/api_v1.py`（transfer 相关）、`radar_sim_sdk/client.py`。
>
> 约束：本次为纯代码级审计 + 自动化测试，**未修改任何源码**，未提交任何 commit。真实中断网络 / 活体服务器直传 / 250+ 文件生产批量无法从本机触达，相关验收项统一标注为"需要真实部署验收"。

## 1. 结论

"Linux 控制面不接收 MF4 大文件正文，Connector/SDK 直接写入目标数据面"这一核心不变量，以及单文件/批量/断点/源变化/重复 chunk/服务重启/共享路径/UNC/逻辑路径等断点续传能力，当前代码实现 + 自动化测试均成立。目标磁盘满与真实 250+ 文件生产批量仅有代码级保护（配额/空闲空间预检），缺少真实磁盘满与大规模传输的实测。

| 审计项 | 结论 | 证据类型 |
|---|---|---|
| 单文件直接传输（chunk + checksum + 原子 rename） | 已实现 + 已测试 | `_copy_file_with_resume` + `test_nested_copy_streams_hash_and_publishes_atomically` |
| 批量多文件（数据集/多种 role） | 已实现 + 已测试 | `execute_transfer` 顺序复制 + `test_agent_transfers_selena_and_each_config_asset_as_independent_role` |
| 250+ 文件 / 大批量 Manifest 完整性 | 代码级 + 部分测试；真实规模**需要真实部署验收** | `DatasetStoreQuota.max_files=20000`；`test_multifile_upload_resumes_and_finalizes_without_public_physical_path` |
| 源变化检测（size/mtime/digest） | 已实现 + 已测试 | `SourceChangedError` + `test_source_size_change_during_copy_is_detected` |
| 断点续传（.partial + 校验 offset + 续租） | 已实现 + 已测试 | `_copy_file_with_resume` + `test_partial_resume_hashes_prefix_and_remainder` |
| 重复 chunk / 重复请求幂等 | 已实现 + 已测试 | `save_or_reuse_plan` / `artifact_chunks` + `test_issue_plan_reuses_non_expired_active_and_completed_request` |
| 服务/Connector 重启恢复 | 已实现 + 已测试 | SQLite 持久 + `test_plan_metadata_round_trips_through_sqlite_restart` |
| 目标磁盘满 | 仅代码级预检，**需要真实部署验收** | `DatasetStoreQuota.min_free_bytes` 磁盘预留 |
| 共享路径 / UNC / dataset/shared 逻辑路径 | 已实现 + 已测试 | `validate_transfer_root` / `shared_reference` / `shared://dataset://` |
| Linux 不接收大文件正文 | 已实现 + 已测试 | `_metadata_dict(code="file_body_rejected")` + `test_control_data_plane_contract` 全套 |

测试基线：`.venv/Scripts/python.exe -m pytest tests/test_direct_transfer.py tests/test_direct_transfer_clients.py tests/test_transfer_service.py tests/test_dataset_store.py tests/test_dataset_upload_service.py tests/test_artifact_store.py tests/test_artifact_upload_service.py tests/test_control_plane_transfer_api.py tests/test_control_data_plane_contract.py -q`
结果：**`174 passed, 3 skipped, 1 warning in 30.46s`**（3 个 skip 为 Windows-only/环境类用例，非回归）。

---

## 2. TransferPlan 生命周期与资源 role 清单

### 2.1 TransferPlan 生命周期（Mermaid）

```mermaid
flowchart TD
    A[Connector/SDK 解析用户 YAML<br/>本地扫描源文件] --> B[POST /jobs/{id}/stages/{id}/transfers<br/>仅元数据: role + relative_path + size]
    B --> C{Linux 控制面 issue_transfer_plan<br/>api_v1.py:1746 / transfer_service.issue_plan}
    C -->|无部署数据面| C1[cluster_direct_transfer_unavailable<br/>status=needs_input]
    C -->|ok| D[TransferPlan 持久化 pending<br/>owner/job/stage 绑定 + 隔离 relative_root]
    D --> E[Connector/SDK execute_transfer_plan<br/>SDK client.py:536 -> direct_transfer.execute_transfer]
    E --> F[逐文件: 源快照 size/mtime<br/>-> .partial 校验 offset 续传 -> chunk 流式复制<br/>-> SHA-256 -> os.replace 原子发布]
    F --> G[报 progress: POST /transfers/{id}/progress<br/>节流上报 + idle lease 续租]
    G --> H[报 manifest: POST /transfers/{id}/manifest<br/>逐条目 size/checksum/storage_ref 校验]
    H --> I{receive_transfer_manifest<br/>api_v1.py:1935 校验与计划完全一致}
    I -->|完全匹配| J[complete_transfer_stage<br/>该 role 标记 resolved]
    I -->|不一致| I1[manifest_conflict / *_mismatch<br/>status 409/422, 不发布]
    J --> K[全部 required roles resolved<br/>prepare_data 才可成功]
    K --> L[Cluster executor resolve_storage_ref<br/>server_probe_root 定位物理文件<br/>cluster_stage_executor.py:1900]
    L --> M[preflight / run_simulation / collect_results]
    M --> N[result_ref + canonical ZIP<br/>SDK download_result 校验 checksum]
```

关键：**Linux 控制面全程只接触 plan/progress/manifest 元数据**。`direct_transfer.py` 模块 docstring（`direct_transfer.py:1-7`）声明 "The control plane deals in plans, progress and manifests only; this module never performs HTTP, YAML or project discovery."；`transfer_service.py` docstring（`transfer_service.py:1-4`）声明 "It never opens source or destination files."

### 2.2 资源 role 清单

`SOURCE_ROLES = {"dataset", "runtime_bundle", "runtime_xml", "mat_filter", "adapter"}`（`direct_transfer.py:24-26`）。各 role 的语义与数据面：

| Role | 含义 | 来源 | 传输/登记路径 | 代码位置 |
|---|---|---|---|---|
| `dataset` | 输入 MF4 数据集（单文件/多文件） | 用户 `data.path`、中央上传、Agent 上传 | 本地源 -> TransferPlan shared_copy；或 dataset:// 逻辑引用零拷贝 | `_apply_direct_transfer_stage` `api_v1.py:811`；`dataset_upload_service.py:66-199` |
| `runtime_bundle` | Selena.exe + 同目录 DLL | Windows 构建产物 / 已注册 Bundle | 本地源 -> TransferPlan（仅 selena.exe/DLL）；已注册则逻辑 Bundle ID 零拷贝 | `_apply_direct_transfer_stage` `api_v1.py:815-824`；`_scan_sdk_transfer_items` `client.py:1438-1443` |
| `runtime_xml` | Runtime 配置文件 | 用户 `selena.runtime_xml` | 本地源 -> TransferPlan | `api_v1.py:818,824`；`_sdk_local_transfer_sources` `client.py:1338` |
| `mat_filter` | 滤波配置文件 | 用户 `simulation.mat_filter`（缺失时可推断） | 本地源 -> TransferPlan；推断角色在计划签发前声明 | `api_v1.py:826,836-849`；`client.py:1339` |
| `adapter` | 项目适配器 | 用户 `simulation.adapter_file` | 本地源 -> TransferPlan | `api_v1.py:825`；`client.py:1340` |
| Result archive（结果归档） | 仿真结果 ZIP | Windows 本地结果 / Cluster 结果 | 结果上传 -> catalog 原子发布；SDK 下载校验 checksum | `client.py:897-927`（download_result）；`client.py:929-948`（create_result_upload） |

其余 `TRANSFER_MODES = {"shared_copy", "source_to_local", "gateway_upload"}`（`direct_transfer.py:23`）；P0 内核仅支持 `shared_copy`，`source_to_local` 与 `gateway_upload` 明确返回稳定 503/不可用（`transfer_service.py:496-511`）。

---

## 3. 逐数据路径需求：实现 / 测试 / 待实测

### 3.1 单文件

- **实现**：`execute_transfer`（`direct_transfer.py:938-1003`）对单文件构造一个 `TransferSource`，`_copy_file_with_resume`（`direct_transfer.py:816-935`）完成"源快照 -> .partial 写入 -> SHA-256 -> os.replace 原子发布"。
- **测试**：`test_direct_transfer.py` `test_nested_copy_streams_hash_and_publishes_atomically`、`test_planned_size_digest_and_mtime_are_enforced`、`test_progress_callback_reports_relative_path_and_completion`；`test_transfer_service.py` `test_client_copy_manifest_and_trusted_resolve_end_to_end`。
- **结果**：通过。

### 3.2 批量（多文件 / 多 role）

- **实现**：`execute_transfer` 顺序复制 `files` 集合（`direct_transfer.py:983-994`），每项一个独立 `ManifestEntry`；`TransferManifest` 校验 `total_bytes = sum(entries.size)`（`direct_transfer.py:626-631`）。Agent/SDK 按 role 分别签发独立计划（`_auto_prepare_direct_transfers`，`client.py:315-433`，逐个 role 幂等）。
- **测试**：`test_direct_transfer_clients.py` `test_agent_scan_preserves_complete_directory_and_normalizes_checksums`、`test_agent_transfers_selena_and_each_config_asset_as_independent_role`、`test_agent_dataset_transfer_plan_carries_radar_fingerprints`；`test_control_plane_transfer_api.py` `test_manifest_roles_complete_stage_only_after_all_resources`。
- **结果**：通过。

### 3.3 250+ 文件

- **实现（代码级）**：`DatasetStoreQuota.max_files = 20_000`（`dataset_store.py:63`）、`max_total_size = 20 TB`（`dataset_store.py:65`），manifest 校验 `len(items) > quota.max_files` 即拒绝（`dataset_store.py:628-629`）。直接传输内核无固定文件数上限，逐文件顺序复制 + 逐文件 checksum。
- **测试**：未发现专门构造 250+ 文件的**直接传输**测试。批量 Manifest 完整性由 `test_multifile_upload_resumes_and_finalizes_without_public_physical_path`（多文件，非 250+）与 `test_manifest_roles_complete_stage_only_after_all_resources`（多 role）覆盖。250+ 输入的真实批量验证在 Cluster 侧有 `tests/test_cluster_stage_executor.py` 的 `test_cluster_batch_input_results_are_not_truncated`（250 个 result.ini），但那是结果收集侧，不是本次数据面传输侧。
- **结论**：**需要真实部署验收**。建议发布门禁增加 250+ 文件 / 大目录（含子目录）MF4 批量从 Windows 源直接传输到 Cluster 数据面，核对目标端文件数 + Manifest 条目数与 checksum。

### 3.4 源变化（传输中源文件被改）

- **实现**：`_source_snapshot` 捕获 size + mtime_ns（`direct_transfer.py:787-791`）；复制前校验 `transfer_source.size/mtime_ns`（`direct_transfer.py:831-834`），复制后 `_check_source_snapshot` 复查（`direct_transfer.py:911`），size/mtime 变化抛 `SourceChangedError`（`direct_transfer.py:794-800`）。digest 不符（计划中带 sha256 时）同样抛 `SourceChangedError`（`direct_transfer.py:846-847, 915-916`）。`SourceChangedError` 在 service 层映射为稳定错误 `source_changed_during_transfer` / 409 + `retry_transfer` action（`transfer_service.py:718-719`）。
- **测试**：`test_source_size_change_during_copy_is_detected`、`test_source_mtime_only_change_during_copy_is_detected`、`test_source_change_is_returned_as_stable_service_error`（`test_transfer_service.py`）。
- **结果**：通过。

### 3.5 断点续传（resume / .partial）

- **实现**：`.partial` 与最终文件分离——`partial = destination.with_name(destination.name + ".partial")`（`direct_transfer.py:837`）；最终文件只在校验通过后 `os.replace` 原子发布（`direct_transfer.py:917`），崩溃不会留下"看似完整"的目标文件。续传逻辑（`direct_transfer.py:860-894`）：
  1. 若 `plan.resume and partial.exists()`，以 `partial.stat().st_size` 为候选 offset（`direct_transfer.py:862-866`），offset 不得超过源 size，否则归零。
  2. 以 `r+b` 打开 partial，**从 offset 起逐 chunk 比对源与 partial 前缀**（`direct_transfer.py:870-882`）；前缀不一致则 `valid=False`，截断归零从头复制（`direct_transfer.py:883-888`）——**绝不在未校验的 offset 上继续拼接**。
  3. 校验通过后从 offset 续写（`direct_transfer.py:890-905`），`fsync` 后复查源快照、对比全量 digest，才 `os.replace`（`direct_transfer.py:906-917`）。
  - 取消：抛 `TransferCancelled` 时删除 partial 但保留已发布文件（`direct_transfer.py:928-933`）。
  - 会话级续租：`TRANSFER_IDLE_LEASE_SECONDS = 86400`（`transfer_service.py:52`），`report_progress` 每次续租 `expires_at`（`transfer_service.py:576-581`）；dataset/artifact 上传会话过期但 partial 仍在时自动续租恢复（`dataset_store.py:320-342`、`artifact_store.py:554-575`）。
- **测试**：`test_partial_resume_hashes_prefix_and_remainder`、`test_corrupt_partial_is_restarted_instead_of_appended`、`test_cancellation_removes_partial_but_not_published_file`、`test_active_transfer_renews_idle_lease_instead_of_using_wall_clock_deadline`、`test_expired_active_session_with_partial_file_is_renewed`（artifact_store）。
- **结果**：通过。

### 3.6 重复 chunk / 重复请求（幂等）

- **实现**：TransferPlan 幂等——`_transfer_request_key` 由 owner/job/stage/mode/role/target/排序后的 items/source_fingerprints 哈希（`transfer_service.py:212-246`）；`save_or_reuse_plan` 用 `BEGIN IMMEDIATE` 跨进程串行化，命中未过期的 pending/in_progress/completed 即复用同一 plan（`transfer_service.py:315-360`）；failed/cancelled/过期不复用，重试获得新隔离根（`transfer_service.py:319-321`）。Dataset 上传 chunk 幂等——`dataset_upload_chunks` 以 `(file_id, offset)` 为主键，重发同 offset 同 checksum 视为幂等重试（`dataset_store.py:374-386`），不同数据拒绝（`dataset_store.py:379-380`）；artifact 上传同 offset 同 size/checksum 幂等（`artifact_store.py:613-629`）。
- **测试**：`test_issue_plan_reuses_non_expired_active_and_completed_request`、`test_issue_plan_reissues_for_failed_cancelled_expired_or_changed_metadata`、`test_issue_plan_is_sqlite_serialized_across_concurrent_retries`、`test_chunk_retry_is_idempotent_but_different_data_is_rejected`（dataset_store）、`test_overwrite_offset_idempotent_exact_match`、`test_non_contiguous_offset_rejected`（artifact_store）。
- **结果**：通过。

### 3.7 服务/Connector 重启恢复

- **实现**：TransferPlan 存 SQLite `transfer_plans` 表（`transfer_service.py:249-298`），`plan_json/progress_json/manifest_json` 持久化，`test_plan_metadata_round_trips_through_sqlite_restart` 证明跨 DB 重建可读。dataset/artifact 上传 session 全量 SQLite 持久（`dataset_store.py:185-239`、`artifact_store.py:340-409`），进程重启后 `get_session` 按 id 恢复，过期但有 partial 时续租。传输内核恢复点：进程在 `.partial` 写入后崩溃 -> 重启后以校验过的 offset 续传（见 3.5）。
- **测试**：`test_plan_metadata_round_trips_through_sqlite_restart`、`test_session_survives_recreate_store`（artifact_store）、`test_finalize_recovers_after_atomic_move_before_catalog_insert`（dataset_store）。
- **结果**：通过（SQLite 层）。**真实服务重启窗口（重启恰在 chunk 落盘与 manifest 上报之间）需要真实部署验收**。

### 3.8 目标磁盘满

- **实现（代码级）**：Dataset 上传在创建 session 时做磁盘预留检查：`free - globally_reserved - total_size < min_free_bytes(=1GiB)` 则抛 `DatasetUploadQuotaError`（`dataset_store.py:285-287`）；配额含 `max_total_size / max_owner_reserved_bytes`（`dataset_store.py:65-66, 276-277`）。
- **测试**：未发现专门模拟"目标磁盘写满"的真实测试（无法在测试中可靠制造 ENOSPC 而不污染共享磁盘）。`test_manifest_and_chunk_quota_are_enforced` 只验证配额/大小校验，非真实 ENOSPC。
- **结论**：**需要真实部署验收**。建议用一次性小容量临时目录/伪满盘（或 quota 注入极小 `min_free_bytes`）实测直接传输与 dataset/artifact 上传在磁盘满时的稳定错误码与 partial 保留语义。

### 3.9 共享路径 / UNC / dataset/shared 逻辑路径

- **实现**：
  - UNC：`validate_transfer_root` 接受 `\\host\share` 形式生产根，拒绝 `\\?\` 等设备路径（`direct_transfer.py:674-709`）；`_root_identity` 对 UNC 用 casefold 归一（`direct_transfer.py:712-716`）。`test_unc_root_is_supported_and_device_roots_are_rejected` 通过。
  - 共享/逻辑路径零拷贝：`dataset://`/`shared://` 引用走 `shared_reference` dispatch（`api_v1.py:912-920`，`transfer_status=transfer_skipped_shared`，不签发 TransferPlan）；SDK `_sdk_local_transfer_sources` 明确跳过 `shared://`/`dataset://`/UNC 源（`client.py:1344-1352`）。
  - 双命名空间：`client_target_root`（UNC/客户端写入）+ `server_probe_root`（Linux 挂载探测），`resolve_storage_ref` 用同一 `relative_root` 在两命名空间解析（`transfer_service.py:655-677`、`direct_transfer.py:1032-1077`）。
- **测试**：`test_unc_root_is_supported_and_device_roots_are_rejected`、`test_local_root_requires_explicit_test_opt_in`、`test_dual_namespace_plan_dispatches_client_root_and_resolves_probe_root`、`test_production_writer_root_without_linux_probe_is_not_deployment_ready`、`test_shared_cluster_inputs_are_zero_copy_and_need_no_windows_connector`、`test_shared_existing_cluster_skips_windows_resolution_and_registration`（control_data_plane_contract）。
- **结果**：通过。真实 UNC 挂载与 Linux probe 挂载的一致性与断线需**真实部署验收**。

### 3.10 dataset/shared 逻辑路径（catalog 登记）

- **实现**：`dataset_upload_service.finalize` 把完成的上传登记进 `DatasetCatalog`，返回 `dataset://sha256/<digest>` 逻辑路径（`dataset_upload_service.py:168-199`）；`_storage_ref` 为 `shared://datasets/<project>/<opaque>`（`dataset_store.py:701-703`）。artifact 为 `shared://selena/<project>/<path>`（`artifact_store.py:239-241`）。
- **测试**：`test_public_upload_source_kind_is_server_owned_and_returns_reusable_data_path`、`test_same_manifest_has_owner_scoped_storage_reference`、`test_storage_ref_cannot_be_resolved_by_another_owner`、`test_resolves_finalized_content_without_public_path_leak`。
- **结果**：通过。

---

## 4. 断点续传 / 源变化 / 重复请求：详细证据

### 4.1 `.partial` 分离与原子 rename

- `.partial` 是唯一"进行中"文件，最终名只在验证后出现（`direct_transfer.py:837-838, 916-917`）。
- `os.replace(str(partial), str(destination))`（`direct_transfer.py:917`）为同盘原子操作，进程崩溃不可能留下半截目标文件被后续误用。
- artifact 上传同样：temp 文件 `.store/temp/<session_id>.tmp`（`artifact_store.py:1042-1045`）→ `os.replace` 到目标 + 父目录 fsync（`artifact_store.py:829-836`）；dataset 上传 staging 目录 → `os.replace(staging, target)` 原子发布（`dataset_store.py:450`）。
- 取消时删除 partial 但保留已发布文件（`direct_transfer.py:928-933`）——满足 brief "已完成 role 幂等重放，不重复创建目标根"。

### 4.2 从校验过的 offset 续传

`direct_transfer.py:860-894`：
- 候选 offset = partial 当前大小（`direct_transfer.py:865-866`）；
- 逐 chunk 将 partial 前缀与源对比（`direct_transfer.py:872-881`），任一字节不一致即 `valid=False`；
- 不一致则 `offset=0`、`truncate(0)` 重来（`direct_transfer.py:883-888`）；
- 一致才 `seek(offset)` 续写（`direct_transfer.py:890-891`）。

结论：**绝不基于未经校验的字节数盲目续传**，与 brief 4A.2"从校验过的 offset 继续"一致。

### 4.3 源变化时丢弃旧 partial，不继续拼接

- 复制前源快照（size/mtime）不符计划即 `SourceChangedError`（`direct_transfer.py:831-834`）；
- 复制后复查源快照（`direct_transfer.py:911`）与全量 digest（`direct_transfer.py:914-916`）；
- `SourceChangedError`/`TransferCancelled` 分支主动 `partial.unlink(missing_ok=True)`（`direct_transfer.py:928-933`）——**旧 partial 被丢弃**，不会被拼进新源。

### 4.4 重复请求幂等

- plan 级：`request_key` + `BEGIN IMMEDIATE` 复用（`transfer_service.py:315-360`），`test_issue_plan_is_sqlite_serialized_across_concurrent_retries` 证明并发 SDK 重试只得到一个 plan。
- manifest 级：`receive_manifest` 已存在相同 manifest 返回 `already_completed`（`transfer_service.py:592-596`），不同内容抛 `manifest_conflict`/409（`transfer_service.py:594-595`）；`test_manifest_resubmission_is_idempotent_but_conflicts_are_rejected`。
- chunk 级：`(file_id, offset)` 主键幂等（3.6 节）。

### 4.5 已完成 role 幂等重放，不重复创建目标根

SDK `_auto_prepare_direct_transfers` 先读 `resolved_roles`（已有 durable manifest 的 role），**跳过已解析 role 不重发计划**（`client.py:362-371`），避免盲目重试把 MF4 重复复制进 Cluster worker 目录导致同一输入跑两次。对应测试 `test_manifest_roles_complete_stage_only_after_all_resources`。

---

## 5. 大批量 Manifest 完整性

### 5.1 直接传输侧

- `TransferManifest` 要求 `entries` 非空、`total_bytes` 与条目和一致（`direct_transfer.py:620-631`）；`execute_transfer` 对每个源文件生成一个 `ManifestEntry`（`direct_transfer.py:983-994`），计划路径唯一性校验 `len(paths) != len(set(paths))` 拒绝重复（`direct_transfer.py:979-981`）。
- service 层 `receive_manifest` 校验：条目集合与计划**完全一致**（`set(received) == set(planned)`，`transfer_service.py:601-604`），逐条目 size/mtime/checksum 与计划匹配（`transfer_service.py:605-612`），storage_ref 绑定到 plan+entry（`transfer_service.py:613-615`），不一致即 422/409 拒绝，**fail-closed 不发布**。

### 5.2 多文件 dataset 上传侧

- session 记录每文件 `ordinal/relative_path/expected_size/expected_checksum/received_bytes/status`（`dataset_store.py:205-217`）；finalize 时 `_verify_content_root` 对每个文件校验 size + SHA-256（`dataset_store.py:593-599`），并在 staging 内写私有 `.dataset-manifest.json`（`dataset_store.py:601-618`）。
- manifest 指纹 `_manifest_fingerprint` 对排序后的文件列表（relative,size,checksum）哈希（`dataset_store.py:696-698`），同 owner/project/fingerprint 幂等复用。

### 5.3 250+ 规模现状

- 代码支持：`max_files=20000`，无固定直接传输文件数上限；批量结果侧有 250 输入测试（`test_cluster_batch_input_results_are_not_truncated`）。
- **缺口**：数据面直接传输侧没有 250+ 文件/250+ 条目的 Manifest 完整性专项测试（本轮 `174 passed` 中无该规模用例）。真实 250+ MF4 目录（含子目录）传输 + 目标端文件数与 checksum 对账需**真实部署验收**。

---

## 6. 证明：Linux 控制面不接收 MF4 大文件正文

### 6.1 控制面 API 只接受元数据

`core/api_v1.py`：
- `issue_transfer_plan`（`api_v1.py:1746-1899`）：请求仅 `source_role + items(relative_path,size,checksum,mtime_ns) + source_fingerprints`；`allowed_item_fields` 白名单**不含任何文件正文**（`api_v1.py:1850`），未知字段抛 `invalid_transfer_item`/422（`api_v1.py:1870-1876`）。
- `_apply_direct_transfer_stage`（`api_v1.py:775-791`）docstring："This method is deliberately metadata-only. It does not inspect any user path, enumerate a directory, open a file, or create a transfer plan. A Connector/SDK performs that work ... and sends only TransferPlanItem metadata back"。
- `receive_transfer_manifest`（`api_v1.py:1935-2023`）：只解析 `TransferManifestEntry`（relative_path/size/checksum/storage_ref/mtime_ns...），异常统一 `"Transfer manifest must contain metadata only"`/422（`api_v1.py:2023`）。
- 缺数据面根时**提交前阻断**（`needs_input`）而不是先收文件（`api_v1.py:887-908`）。

`core/direct_transfer.py`：
- `_metadata_dict` 仅接受 str/int/float/bool 值，任何文件类内容抛 `DirectTransferError(code="file_body_rejected")`（`direct_transfer.py:451-459`）。
- 内核 docstring（`direct_transfer.py:1-7`）："The control plane deals in plans, progress and manifests only; this module never performs HTTP, YAML or project discovery."

`core/transfer_service.py`：
- `TransferStore` "no file content or physical target data"（`transfer_service.py:249-250`）；`receive_manifest` "Validate and persist metadata only; never read a transferred file"（`transfer_service.py:584`）。

### 6.2 SDK/Connector 直接写目标数据面

`radar_sim_sdk/client.py`：
- 数据面适配器注释（`client.py:456-462`）："Linux receives only plan/progress/manifest metadata; `execute_transfer_plan` writes bytes directly to the signed target root and never sends a file body through `_request`."
- `issue_transfer_plan` 请求里 `owner` 与两个物理根**刻意缺席**（`client.py:476-480`）——目标根由部署配置选择，请求体不能指定。
- `execute_transfer_plan`（`client.py:536-605`）是对 `core.direct_transfer.execute_transfer` 的薄封装，直接复制到 `signed.client_target_root`，`report_transfer_manifest` 只上报元数据（`client.py:514-526`）。
- `_prepare_user_run`（`client.py:629-649`）："A local path is a data-plane source, never an implicit Linux upload."
- 失败路径不回落为 Linux body 上传：`_auto_prepare_direct_transfers` 异常统一映射为 `cluster_direct_transfer_unavailable`/`needs-agent` 等待（`client.py:406-425`）。

`core/cluster_stage_executor.py`：
- `storage_ref_resolver` 注入 `TransferService.resolve_storage_ref`（`cluster_stage_executor.py:104-123`），执行器只对解析出的路径做 stat/size 检查，不传输正文（`cluster_stage_executor.py:1900-1930`）。

### 6.3 契约测试（Linux 不接收正文的回归证据）

`tests/test_control_data_plane_contract.py`：
- `test_web_user_run_never_uploads_task_file_bodies_to_linux`
- `test_sdk_existing_cluster_local_paths_never_use_linux_body_uploads`
- `test_linux_sdk_posix_sources_use_direct_transfer_hint_not_linux_body_route`
- `test_linux_control_plane_does_not_import_server_visible_existing_cluster_bodies`
- `test_existing_cluster_agent_resolution_never_calls_linux_body_upload_helpers`
- `test_web_and_sdk_share_local_zero_transfer_scheduling`
- `test_missing_direct_transfer_capability_blocks_with_stable_status_not_http_upload`
- `test_transfer_progress_and_plan_access_are_owner_isolated`

`tests/test_transfer_service.py`：
- `test_file_body_like_metadata_is_rejected`：把文件正文当元数据提交即被拒。

---

## 7. 已实现 + 已测试 vs 仅代码级 / 需真实部署验收

| 项 | 状态 |
|---|---|
| 单文件直接传输（checksum + 原子 rename） | 实现 + 测试通过 |
| 批量多文件 / 多 role（dataset/runtime_bundle/runtime_xml/mat_filter/adapter） | 实现 + 测试通过 |
| 源变化（size/mtime/digest）检测与 discard partial | 实现 + 测试通过 |
| 断点续传（.partial 分离、校验 offset、原子 rename、空闲续租） | 实现 + 测试通过 |
| 重复 chunk / 重复请求幂等（plan/manifest/chunk 三级） | 实现 + 测试通过 |
| 服务/Connector 重启恢复（SQLite 持久） | 实现 + 测试通过（DB 层） |
| 共享路径 / UNC / dataset:// / shared:// 逻辑路径 | 实现 + 测试通过 |
| Linux 不接收大文件正文 | 实现 + 契约测试通过 |
| 目标磁盘满（ENOSPC） | 仅代码级配额/空闲预检，**需要真实部署验收** |
| 250+ 文件直接传输 + Manifest 对账 | 代码支持（max_files=20000），**需要真实部署验收** |
| 真实中断网络（半途断网续传） | 单元级覆盖，**需要真实部署验收** |
| 真实 UNC/共享挂载 + Linux probe 双命名空间 | 逻辑测试通过，**需要真实部署验收** |
| 真实服务重启窗口（重启恰在 chunk 落盘与 manifest 之间） | **需要真实部署验收** |

---

## 8. 风险等级与未解决项

| 风险 | 等级 | 说明 |
|---|---|---|
| 目标磁盘满路径未真实验证 | P2 | 代码有 `min_free_bytes` 预留（`dataset_store.py:285-287`），但直接传输内核与 artifact 上传无 ENOSPC 实测；真实写满磁盘时需确认返回稳定错误码且 `.partial`/temp 保留语义正确，不残留"看似完整"文件。 |
| 250+ 文件真实批量未验收 | P2 | `max_files=20000`、无固定直接传输上限；真实 250+ MF4 目录（含子目录）传输与目标端文件数/checksum 对账未做，发布门禁需补。 |
| 真实断网/重启窗口 | P2 | 单元测试覆盖"进程崩溃后从校验 offset 续传"逻辑，但真实断网（TCP 中断）+ 服务/Connector 重启的组合窗口未在活体环境验证。 |
| 真实 UNC/共享挂载双命名空间 | P2 | `client_target_root`（UNC）与 `server_probe_root`（Linux mount）双命名空间逻辑测试通过；真实部署的挂载一致性、权限、断线未验收。 |
| Linux 控制面不接收正文依赖元数据白名单维护 | P3 | `allowed_item_fields`（`api_v1.py:1850`）与 `_metadata_dict` 是 fail-closed 闸门；未来新增字段若漏入白名单可能放行非元数据，需在 `test_control_data_plane_contract` 上保持回归。 |

未解决/需补做：
1. 真实部署验收：磁盘满、250+ 文件批量、断网续传、服务重启窗口、UNC/共享挂载（第 7 节清单）。
2. 建议新增直接传输侧的 250+ 文件 Manifest 完整性单元测试（当前只有批量结果侧有 250 输入测试）。
3. 保持 `file_body_rejected`/`metadata only` 契约回归，防止未来 API 字段放宽破坏"Linux 不中转大文件"。

## 9. 复测命令

```bash
cd /d/RamboStar/idea/radar-sim
.venv/Scripts/python.exe -m pytest tests/test_direct_transfer.py tests/test_direct_transfer_clients.py tests/test_transfer_service.py tests/test_dataset_store.py tests/test_dataset_upload_service.py tests/test_artifact_store.py tests/test_artifact_upload_service.py tests/test_control_plane_transfer_api.py tests/test_control_data_plane_contract.py -q
# 预期：174 passed, 3 skipped, 1 warning in ~30s
```

## 10. 关联文档

- `docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md` 第 4A.2、9（9.1/9.2）、10（风险 9/16）、11（Task G）、12 节
- `docs/handoffs/2026-08-17-non-engine-failure-audit.md` 第 6 节（prepare_data 与 direct-transfer 故障树与当前保护）
- `docs/audits/2026-08-17-cluster-long-run-audit.md`（Task J，Cluster 结果收集侧 250 输入测试）
