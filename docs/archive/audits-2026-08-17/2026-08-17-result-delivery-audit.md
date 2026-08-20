# radar-sim 结果归档、下载与 retention 审计（Task I）

日期：2026-08-17
范围：Windows 本地与 Cluster 结果的归档（`core/local_results.py`）、可续传上传（`core/result_upload_service.py`、`core/artifact_store.py`）、本地解压交付（`core/result_delivery.py`）、manifest/result/diagnosis/download API（`core/api_v1.py`、`core/api_v1_fastapi.py`）、Web 下载（`radar_sim_web/static/app.js`）、SDK 下载/上传（`radar_sim_sdk/client.py`）、retention/GC/磁盘水位。
审计方式：AUDIT ONLY，未修改任何源代码，未提交任何内容。
复测命令与结果见第 6 节：定向回归 `73 passed + 3 skipped`（`test_local_results.py`/`test_result_delivery.py`/`test_result_upload_service.py`/`test_artifact_store.py`）+ `59 passed`（`test_api_v1_service.py`）+ `7 passed`（`test_sdk.py` 结果下载子集）。

## 1. 结论先行

结果链路的**核心不变式成立且已被测试覆盖**：

1. **Server-side 归档 ZIP 是唯一真相，本地 `result.path` 是便利交付**。`catalog.publish()`（或 `import_archive()`）先于本地交付发生，交付失败被捕获为稳定状态码而不会抛出，因此 **`result.path` 不可写不会丢失 server-side ZIP**（代码路径见第 3.1 节，`cli/agent.py:2863 -> 2871`）。
2. **SDK 下载采用 临时文件 + 流式 SHA-256 + checksum 比对 + 原子 `replace`**，断流后不留有效命名文件（`client.py:897-927`，测试 `test_sdk.py:786`）。
3. **`retain_until` 过期在读取层强制**（`local_results.py:344`）：过期结果 `get()` 报 `result retention has expired`、`list()` 过滤；Web 显示“结果不存在、尚未登记或已过期”（`app.js:1201`）。
4. **存在明确缺口（P1）**：**没有结果归档的磁盘 GC、磁盘水位和告警**。`retain_until` 只“隐藏”过期结果，过期 ZIP 文件与 DB 行不会真正从磁盘回收；结果 catalog 存储没有 `dataset_store` 那种 `min_free_bytes` 水位检查；没有管理员告警。Cluster 结果 publish 未传 `retain_until`（默认 0 = 永不过期），与 Windows 本地默认 30 天不一致（见第 4 节）。
5. **下载 checksum 不一致的稳定错误码缺口（P2）**：SDK 校验失败抛的是裸 `ValueError`（`client.py:923`），不是带稳定 code 的结构化错误；且**结果**下载的 checksum-mismatch 没有专属测试（只有 config-asset 的 `test_sdk.py:640`）。

逐项核实：

- Web 下载使用 `requestHeaders()`（owner 身份）取 `/results/{ref}/download`，经 `triggerBlobDownload`（`app.js:1176-1213`），避免 `*.crdownload` 残留（对应 handoff `f32cf99` 修复）。
- 服务端下载端点 `GET /api/v1/results/{result_ref}/download`（`api_v1_fastapi.py:1001-1010`）每次 `resolve_archive` 都重新校验 archive checksum/size（`local_results.py:363-370`），下载端读到的是不可变归档，**客户端断流不会删除/损坏服务端文件**。
- Manifest / result_ref / catalog-ZIP 三者通过“确定性摘要 + 归档条目逐文件校验 + 同 run 不可变”保持一致（见第 3.5 节）。
- 服务重启后结果从 SQLite + 不可变归档恢复，不丢失（`local_results.py` 建表/读库；上传 session 有 24h 空闲 lease 续租；outbox 可离线缓冲后冲刷）。
- 重复下载是只读且幂等；SDK 临时文件名带 uuid 避免并发碰撞；Web `downloadResult` 有 in-flight 去重（`app.js:1182-1186`）。

以下场景**需要真实部署验收**（本机不可用）：真实大 ZIP 网络下载、真实浏览器断流、真实服务重启期间下载、真实磁盘写满/水位告警、双用户跨 owner 大文件下载并发。

## 2. 下载示例与临时文件/原子 rename 证据

### 2.1 SDK 下载（`radar_sim_sdk/client.py`）

`download_job_result()`（`client.py:824-882`）：Manifest -> `result_ref` -> `download_result()`。无 `result_ref` 时按 Job 状态区分“尚未就绪(queued/running/needs_input/blocked/cancel_*)”与“终态但无归档”，抛 `ValueError("result_unavailable: ...")`（`client.py:852-861`）。

`download_result()`（`client.py:897-927`）核心：

```python
temporary = target.with_name(target.name + f".part.{uuid.uuid4().hex}")   # client.py:905
digest = hashlib.sha256()
with self._client.stream("GET", f"/api/v1/results/{result_ref}/download") as response:  # client.py:909
    self._raise_for_status(response)
    with temporary.open("wb") as handle:
        for chunk in response.iter_bytes():
            handle.write(chunk); digest.update(chunk)
# httpx.TransportError -> RadarSimTransportError，不自动重试（大文件避免静默重复拉取）  client.py:915-920
if checksum != str(metadata.get("archive_checksum") or ""):
    raise ValueError("downloaded result checksum does not match catalog")   # client.py:922-923
temporary.replace(target)                                                  # client.py:924 原子 rename
finally:
    temporary.unlink(missing_ok=True)                                       # client.py:927 失败清理
```

- 临时文件名：`<目标名>.part.<uuid>.hex`，与最终名不同 → 断流/校验失败时不会留下可被误用的完整文件名。
- 最终名由 checksum 派生：`radar-sim-result-<archive_checksum 前 12 位>.zip`（`client.py:902-903`，测试 `test_sdk.py:726`）。

### 2.2 Web 下载（`radar_sim_web/static/app.js`）

```js
async function downloadResult(resultRef) {          // app.js:1176
  const inFlight = state.resultDownloadsInFlight.get(key);
  if (inFlight) { showToast("结果正在下载，请勿重复点击"); return inFlight; }   // 重复点击去重 app.js:1182-1186
  const blob = await fetchBinary(`/results/${encodeURIComponent(key)}/download`,
      { headers: requestHeaders(), timeoutMs: 10*60*1000 });                 // owner 身份 app.js:1192-1196
  triggerBlobDownload(blob, "radar-sim-result.zip");                          // app.js:1197
  // 404 -> "结果不存在、尚未登记或已过期；请先确认任务已完成" app.js:1200-1201
}
```

`triggerBlobDownload`（`app.js:94-101`）延迟释放 object URL，避免 `Unconfirmed *.crdownload` 残留（handoff `2026-08-11-result-delivery-mcp-acceptance.md` 记录 `f32cf99` 修复）。Web 静态内容断言测试：`test_api_v1_fastapi.py:645-650`。

### 2.3 服务端下载端点（`core/api_v1_fastapi.py` / `core/api_v1.py`）

- `GET /api/v1/results/{result_ref}/download`（`api_v1_fastapi.py:1001-1010`）：`service.get_result()`（owner 校验 + 过期检查）-> `service.result_archive()` -> `FileResponse(archive, media_type="application/zip", filename=radar-sim-result-<digest12>.zip)`。
- `result_archive`（`api_v1.py:2568-2574`）-> `resolve_archive`（`local_results.py:363-370`）：每次下载 `_verify_archive_file` 重校验 size + sha256。
- `get_result`（`api_v1.py:2260-2265`）：`ResultCatalogError` -> `ApiV1Error("result_unavailable", ..., 404)`。过期结果同样落到这里（`local_results.py:344-345`）。
- 关键：FileResponse 只读流式，客户端断开不影响服务端不可变归档文件。

## 3. 关键机制逐项核验

### 3.1 “result.path 不可写不得丢失 server-side ZIP”证明（代码路径 + 测试）

**代码路径（Windows 本地，`cli/agent.py:2848-2940`）顺序固定：**

1. `published = catalog.publish(owner=..., run_ref=lease_ref, source_root=..., files=..., retain_until=now+retain_days*86400)`（`cli/agent.py:2863`）—— **先固化 server-side owner-scoped ZIP**。`publish` 内部临时文件 + `os.replace` 原子发布（`local_results.py:211-226`）。
2. `delivery = _materialize_local_result(task, source_root=..., local_result=..., result_ref=published.ref, ...)`（`cli/agent.py:2871`）—— 之后才做 `result.path` 的本地解压交付。
3. `_materialize_local_result`（`cli/agent.py:2769-2826`）捕获 `ResultDeliveryError` 并**返回** `{"status":"failed","file_count":0,"checksum":"","code":...}`（`agent.py:2821-2826`），**不抛出、不阻止 finalize**。`cancelled` 单独处理（`agent.py:2816-2817`）。
4. 上传阶段（`agent.py:2885-2934`）对 5xx/408/409/429 最多重试 3 次，耗尽才 `RuntimeError("result_upload_failed")`——但此时本地 catalog 已有归档，Web/SDK 下载仍可用。

**设计注解**（`result_delivery.py:1-5`、`agent.py:2779-2782`）：`result.path` 交付对业务结果而言是 best-effort；路径冲突/不可用返回稳定 path-free 状态，调用方仍可 finalize catalog ZIP。

**测试证据：**

- `tests/test_result_delivery.py:57` `test_materialize_is_atomic_idempotent_and_preserves_manifest`：临时目录 + `os.replace`，幂等重试返回 `already_present`。
- `tests/test_result_delivery.py:99/114/120`：不覆盖无关目标、拒绝源/目标重叠、取消时在复制前中断。
- `tests/test_result_upload_service.py:15`：Windows 归档上传到 central catalog 后 `sdk.download_result` 逐字节一致。
- `tests/test_api_v1_service.py:416` `test_diagnosis_keeps_failed_simulation_artifacts_downloadable`：`failed` 仿真仍 `artifacts_available=True`，`action={"type":"download_result",...}`。
- `tests/test_api_v1_service.py:477` `test_partial_manifest_is_terminal_downloadable_and_not_reported_as_total_failure`：`partial` 终态可下载。
- `tests/test_windows_full_local_e2e.py:47`：partial 本地结果 collect + finalize，`result.path` 目录实际落地。
- `tests/test_agent_result_outbox.py:32`：控制面不可达时结果进 outbox 缓冲，恢复后冲刷。

**缺口（P2）**：没有一条把 `result.path` 显式制造为“不可写/冲突”并断言“server ZIP 仍可下载”的**端到端回归测试**。机制由“publish 先于 delivery + delivery 失败被捕获”代码保证，但缺少该故障注入的自动化证明（见第 4 节 GAP-3）。

### 3.2 HTTP 流中断、重复下载、checksum 不一致

| 场景 | 实现 | 证据 |
|---|---|---|
| ZIP 生成后 HTTP 流中断 | 服务端 FileResponse 只读流式，客户端断开不影响归档；SDK 临时 `.part.<uuid>` 文件 + 失败 `unlink`，不留完整文件名 | `client.py:905,915-920,924,927`；`test_sdk.py:786`（mock 中途 `httpx.ReadError`，断言 `(tmp/result.zip).exists() is False`） |
| 重复下载 | 服务端只读不可变归档，幂等；SDK 临时名带 uuid 并发不碰撞；Web in-flight 去重 | `local_results.py:363-370`；`client.py:905`；`app.js:1182-1186`；`test_sdk.py:656` |
| 下载 checksum 不一致 | SDK 比对 `archive_checksum`，失败抛 ValueError 且临时文件清理 | `client.py:921-923,927`；实现存在，**但结果下载无专属测试**（见 GAP-4） |
| 上传 chunk 网络失败 | SDK 上传循环先重读 resumable session 再按 exact offset 重发，最多 4 次；服务端 offset 校验 | `client.py:1007-1042`；`result_upload_service.py:92-102`（offset 冲突 -> `result_upload_offset_conflict` 409） |
| 上传/归档完成但 HTTP response 丢失 | 会话/结果幂等：`create` 复用同 run 会话，`finalize` 重入返回既有结果，`_register` 对同 run 同内容幂等 | `result_upload_service.py:61-82,104-140`；`local_results.py:372-410`；`test_result_upload_service.py:67` |

### 3.3 服务重启

- 结果本体在 SQLite `local_results` 表 + 磁盘不可变归档（`local_results.py:158-175`），服务重启只重连 DB，不丢结果、不丢归档。
- 上传 session 持久化在 `artifact_upload_sessions`（`artifact_store.py`），`expires_at` 为 24h 空闲 lease；`append`/`finalize` 幂等（`artifact_store.py:522-568,625-661`）。
- 结果回调在控制面不可达时进 outbox（`tests/test_agent_result_outbox.py:32`），恢复后冲刷；重复 finalize 不重写已成功状态。
- 结论：**服务重启路径有代码与测试覆盖**；真实重启期间下载需部署验收。

### 3.4 Manifest / result_ref / catalog-ZIP 一致性

- `ResultRef` 是确定性、内容寻址、owner 绑定：`ref = "result:sha256:" + sha256(owner \0 run_ref \0 archive_checksum)`（`local_results.py:227-228,323-324`）。
- 归档条目与公开证据逐文件一致性：`import_archive` 在**不解压**前提下校验 ZIP 条目集合等于 `files`、每个条目 size 与 sha256 与 evidence 完全一致（`local_results.py:612-640`）；`publish`（本地）在写归档时做打开句柄 + 路径 before/after 签名比对，源文件变化即失败（`local_results.py:550-584`，测试 `test_local_results.py:146`）。
- 同 run 不可被不同内容覆盖：`_register` 对 `(owner, run_ref)` 校验 immutable content，冲突报 `result run already has different immutable content`（`local_results.py:379-385`，测试 `test_local_results.py:136`）；幂等重传只延长 retention（`local_results.py:391`）。
- 最终 manifest（`_execute_v5_local_finalize`，`agent.py:2960-2998`）只消费已固化的 `result_ref`/summary/files/delivery，不重新复制大文件；`delivery` 状态透传到 manifest（`delivery.status` 为 `delivered`/`already_present`/`failed`/`not_reported`）。
- RESULT_TRUTH_CONTRACT 归一化：diagnosis 检测 `job/manifest` 状态不一致 -> `job_manifest_outcome_mismatch` 告警、`result_ref` 不可解析 -> `result_reference_unavailable`（`api_v1.py:2103-2108`）。
- 结论：三者在**同一 catalog 事务内**保持一致；public_dict 只含逻辑引用与证据，不含物理路径（`local_results.py:126-128`，测试 `test_local_results.py:33` path-free）。

### 3.5 稳定错误码与用户动作（delivery 失败时）

| 场景 | 稳定 code | HTTP | 用户动作 / 可见文案 |
|---|---|---|---|
| 结果不可用 / 已过期 / 不存在 / 跨 owner | `result_unavailable` | 404 | Web：`结果不存在、尚未登记或已过期；请先确认任务已完成`（`app.js:1200-1201`）；SDK `download_job_result` 抛 `ValueError("result_unavailable: ...")` |
| SDK 未就绪但无归档 | `result_unavailable: job result is not ready/unavailable (...)` | - | 区分 queued/running/needs_input 与终态（`client.py:852-861`） |
| 本地 `result.path` 交付失败 | `result_delivery_failed` / `result_destination_invalid` / `result_destination_conflict` / `result_source_unavailable` / `cancelled` | -（本地） | 返回 `{status:"failed", code:...}`，ZIP 仍可下载，本地可重试（`agent.py:2821-2826`；`result_delivery.py:22,67,109,121`） |
| 结果归档上传校验不一致 | `result_upload_mismatch` | 409 | 重传/重试收集（`result_upload_service.py:133-134`） |
| 上传 offset 冲突 | `result_upload_offset_conflict` | 409 | SDK 自动重读 session 续传（`client.py:1020-1029`） |
| 上传 session 不可用 | `result_upload_unavailable` | 404/409 | 重试（`result_upload_service.py:90,136`） |
| 上传参数非法 | `result_upload_invalid` / `invalid_result_run_ref` / `invalid_result_checksum` | 422 | 修正参数（`result_upload_service.py:84,138-140,146,153`） |
| 结果服务未配置 | `result_service_unavailable` | 503 | 配置 result catalog 后重试（`api_v1.py:2855-2862`） |
| 下载断流（transport） | `RadarSimTransportError` | - | 显式重试，新临时文件（`client.py:915-920`） |
| 下载 checksum 不一致 | 裸 `ValueError`（**无稳定 code**） | - | 见 GAP-4 |

diagnosis 动作：`action = {"type":"download_result","label":"Download result artifacts","result_ref":...}`（`api_v1.py:2183`，测试 `test_api_v1_service.py:471`）。

## 4. retention / GC / 磁盘水位 策略分析与告警缺口

### 4.1 现状

- **读取层过期**：`ResultRef.retain_until`（`local_results.py:75,123`）；`get()` 过期抛错（`:344-345`），`list()` 过滤但 `include_expired=True` 可审计（`:348-361`）；测试 `test_local_results.py:121`。
- **Windows 本地**：`retain_days` 从 spec 透传（`core/spec/model.py:150` 默认 30；`core/stage_binder.py:866`；`cli/agent.py:2850`），publish 时 `retain_until=now+retain_days*86400`（`agent.py:2868`）。
- **Cluster 结果**：`core/cluster_stage_executor.py:1372-1378` 的 `publish(...)` **未传 `retain_until`，默认 0 = 永不过期**。
- **上传 session 清理**：`cleanup_expired_sessions`（`artifact_store.py:1005`）只清理**上传会话**（由 `result_upload_service.py:70` 调用），**与结果归档无关**。

### 4.2 缺口

- **GAP-1（P1）没有结果归档 GC**：过期结果只从读取视图隐藏，**磁盘上的 `.zip` 文件和 DB 行不会被删除**。磁盘只增不减。
- **GAP-2（P1）没有结果存储磁盘水位检查**：`dataset_store` 有 `shutil.disk_usage(...).free < min_free_bytes` 预检查（`dataset_store.py:285-287`），但结果 catalog 存储（`RSIM_HOME/results/local-archives`，`local_results.py:445`）**没有**任何水位/配额检查，磁盘写满时只有普通 `OSError`（归档/上传时表现为 `result archive creation failed` / `result_upload_invalid`）。
- **GAP-3（P1）没有管理员告警**：`cli/server.py` 维护线程只做 stale-task reclaim（`server.py:84-140`），对结果过期/磁盘水位**无 logger 告警、无指标**。`local_results.py` 内无任何 `_LOGGER` 告警。
- **GAP-4（P2）Cluster/本地 retention 不一致**：Cluster 结果默认永不过期、Windows 本地默认 30 天，策略不一致且没有集中配置入口。
- **GAP-5（P2）下载 checksum mismatch 无稳定错误码且无结果专属测试**：`client.py:923` 抛裸 `ValueError`；config-asset 有 digest mismatch 测试（`test_sdk.py:640`）但**结果下载没有**。

### 4.3 建议（不改代码，仅记录）

1. 为 `ResultCatalog` 增加**归档 GC**：按 `retain_until < now` 删除归档文件与 DB 行（保留 `include_expired` 审计窗口或延迟删除），纳入 `cli/server.py` 维护线程。
2. 为结果存储增加**磁盘水位预检查**（参考 `dataset_store.py:285-287`）：归档/上传前 `free - 已预留 - 本次 < min_free_bytes` 时返回稳定错误 + 管理员告警。
3. 增加**过期/水位告警**（日志 + 可选指标端点）。
4. **统一 retention 来源**：Cluster publish 也传 `retain_until`，并在 manifest/response 暴露 `retain_until`（SDK 已返回，见 `result_upload_service.py:157-170` / `test_result_upload_service.py:15`）。
5. 为结果下载 checksum mismatch 补稳定错误码与专属测试。

## 5. 与执行合同的对账（任务书 Task I）

| 任务书要求 | 现状 | 证据 |
|---|---|---|
| 审查 manifest/result/diagnosis/download | 完成 | 见第 3 节 |
| result.path 不可写不丢 server ZIP | **实现+测试（机制性）**，缺端到端故障注入测试 | `agent.py:2863,2871,2815-2826`；`test_result_delivery.py:57` 等（GAP-5/3.1） |
| ZIP 生成后 HTTP 断流 | 实现+测试（mock transport） | `client.py:915-920,927`；`test_sdk.py:786`；真实断流需部署验收 |
| 重复下载 | 实现+测试（只读幂等、in-flight 去重） | `local_results.py:363-370`；`app.js:1182-1186`；`test_sdk.py:656` |
| checksum 不一致 | 实现；**无稳定错误码 + 结果无专属测试** | `client.py:921-923`（GAP-4） |
| 过期/GC/磁盘水位/告警 | **过期读取层已实现；GC/水位/告警缺失** | `local_results.py:344-361`；GAP-1/2/3 |
| 服务重启 | 实现+测试（DB 持久 + 幂等 + outbox） | `local_results.py:158-175`；`test_agent_result_outbox.py:32`；`test_result_upload_service.py:67` |
| 成功业务结果不因本地交付失败丢失 | 成立 | 第 3.1 节 |

## 6. 复测命令与结果

```bash
# 定向回归（结果归档/交付/上传/artifact 存储）
.venv/Scripts/python.exe -m pytest tests/test_local_results.py tests/test_result_delivery.py tests/test_result_upload_service.py tests/test_artifact_store.py -q
# -> 73 passed, 3 skipped

# API 服务层（diagnosis/partial/结果下载相关）
.venv/Scripts/python.exe -m pytest tests/test_api_v1_service.py -q
# -> 59 passed

# SDK 结果下载子集（download_job_result / download_result / transport 断流）
.venv/Scripts/python.exe -m pytest tests/test_sdk.py -q -k "download_result or download_job_result or lists_gets_and_downloads or result_download or result_archive"
# -> 7 passed
```

所有用例在 Python 3.12（`.venv`）下通过。仅有已知 Starlette/httpx 弃用警告，无失败。

## 7. 未验收项（需要真实部署验收）

1. 真实大 ZIP（数百 MB~GB）经网络从 Web 与 SDK 下载，校验端到端 checksum 与断点/断流行为（本机无 live server，**不可用**）。
2. 真实浏览器点击“下载结果 ZIP”，确认最终 `.zip` 而非 `*.crdownload`（handoff 记录的浏览器验收项仍开放）。
3. 真实服务重启期间下载/上传结果，确认幂等不丢。
4. 真实磁盘写满 / 结果存储不可写，确认返回稳定错误且归档不受损（当前机制由代码保证，未实测）。
5. 双 owner 并发下载同一大归档的隔离与性能。
6. GC/磁盘水位/告警一旦实现后的部署演练（当前为缺口）。

## 8. 风险分级

| 级别 | 项 | 说明 |
|---|---|---|
| P1 | GAP-1/2/3：结果归档无 GC、无磁盘水位、无告警 | 长期运行磁盘只增不减；磁盘写满时无预检、无告警。不阻断单次结果交付，但阻断长期生产 |
| P1 | GAP-4：Cluster 结果默认永不过期 | 与 Windows 本地 30 天不一致，retention 策略不统一 |
| P2 | GAP-5：result.path 不可写缺端到端回归测试 | 机制已实现且单点测试覆盖，缺故障注入回归 |
| P2 | GAP-4b：下载 checksum mismatch 无稳定错误码 + 结果无专属测试 | 行为正确但错误表达与测试覆盖不完整 |
| P2 | 真实大文件下载/断流/重启/双用户 | 需真实部署验收，非代码缺口 |

## 9. 结论

结果归档、下载与“本地交付失败不丢 server ZIP”的**核心保证已实现且有自动化测试**；SDK 临时文件 + checksum + 原子 rename、服务端只读不可变归档、manifest/result_ref/catalog 一致性、服务重启幂等等均已就绪。**阻断正式长期上线的是 retention/GC/磁盘水位/告警缺失（P1）与 retention 策略不统一（P1）**；在补齐这些之前，当前结论为“结果交付功能可受信内网使用，但 retention/容量管理不满足长期生产”。
