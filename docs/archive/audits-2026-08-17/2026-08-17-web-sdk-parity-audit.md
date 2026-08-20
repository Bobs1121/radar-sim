# radar-sim Web / SDK / API 同合同与等待机制审计（Task F）

日期：2026-08-17
范围：Web JS（`radar_sim_web/static/app.js`）、Python SDK（`radar_sim_sdk/client.py`、`errors.py`、`events.py`、`models.py`）、REST 合同（`core/api_v1.py`、`core/api_v1_fastapi.py`）、幂等/owner 存储（`core/user_config.py`、`core/control_service.py`）、十阶段 DAG（`core/stages.py`）的单一合同、spec_hash/DAG 对等、base URL 约定、owner/认证、idempotency、event cursor、自适应等待、cancel/retry、partial 与结果下载。
审计方式：AUDIT ONLY，未修改任何源代码，未提交任何内容。
复测命令与结果见第 6 节：定向回归 `151 passed + 72 passed`（含 `test_sdk.py`、`test_api_v1_service.py`、`test_run_config_resolution_flow.py`、`test_v2_public_contract.py`、`test_http_auth.py`、`test_user.py`、`test_identity_unification.py`、`test_control_stages.py`）。

## 1. 结论先行

`radar-sim` 的 Web 与 SDK 共享**同一份 `/api/v1` 合同**：同一个 `UserRunConfig 2.0`（`core/user_config.py`）、同一个 canonical YAML / fingerprint（spec_hash）、同一个十阶段 DAG（`core/stages.py`）和同一个错误 envelope（`core/api_v1.py:3602`）。Web 只负责浏览器侧的提交/展示/下载，SDK 在此基础上额外封装了数据面直传与结果校验下载，二者均**未实现第二套 DAG/状态机/等待规则**。

逐项核实：

1. base URL 约定正确：`RadarSimClient` 的 base URL **不带** `/api/v1`，客户端在所有请求路径前追加 `/api/v1` 前缀（`client.py:98,108,112,...`）。Web 端 `const API = "/api/v1"`（`app.js:3`）。
2. idempotency 三要素 `(owner, idempotency_key, request_hash)` 贯穿创建、网络重试和服务重启：SDK 幂等创建测试（`test_sdk.py:57`）、并发唯一 Job 测试（`test_api_v1_service.py:1173,1194,1215,1238`）与 SQLite 唯一索引（`control_service.py:452-454`）一致。
3. SDK 网络错误重试**不会重复提交**：`_request` 只对 `GET/HEAD` 自动重试（`client.py:1221`），状态变更请求（POST）不重试（`test_sdk.py:1392-1413` 断言 `attempts == 1`）。
4. SDK 等待机制无固定仿真总时长：`watch()/wait()` 的 `timeout` 是调用方观察窗口（`client.py:743-801`），超时只抛 `TimeoutError`、不终止 Job；事件 cursor（`since/next_cursor/terminal`）优先，传输失败降级为轮询。
5. 结果下载：临时文件 + 流式 SHA-256 + checksum 比对 + 原子 `replace`（`client.py:897-927`），并有下载测试（`test_sdk.py:656,687,735,770,786`）。
6. **命名/能力缺口（GAP-1）**：SDK **没有名为 `wait_job()` 的方法**，实际是 `wait()` / `watch()`；`wait_job` 只作为 diagnosis 的 `action.type` 出现（`api_v1.py:4052`）。且 `watch()` 使用固定 `poll_interval`，**没有指数退避**（与任务书 8.2 “退避轮询兜底”的字面描述不完全一致，但功能上以 cursor 优先 + 轮询兜底满足等待语义）。
7. **失败输入重试（GAP-2）**：不存在“按失败输入粒度”的独立 API 或 SDK 方法；失败输入重试通过 `retry_stage(run_simulation)` 实现，由 Agent 本地 checkpoint 保证只重跑无有效终端 check 的输入（`agent_local_run.py:591-624`）。任务书要求“失败输入重试”与 `get_manifest()/get_diagnosis()` 并列交付，当前属于**实现正确但接口粒度不同**，需在真实验收中确认 partial 语义（见第 7 节）。
8. 真实 SDK 独立端到端调用（对本机 live server 的真实 Job）**在本机不可用**，标记为“需要真实部署验收”（见第 7 节）。

## 2. Web vs SDK vs REST 合同对比表

| 合同维度 | Web（`radar_sim_web/static/app.js`） | SDK（`radar_sim_sdk/client.py`） | REST（`core/api_v1.py` + `api_v1_fastapi.py`） | 对等结论 |
|---|---|---|---|---|
| 合同版本 | `schema_version: "2.0"`，`runConfigFromForm()` 直接生成该 JSON（`app.js:468-479`） | `UserRunConfig` 强制 `schema_version="2.0"`（`user_config.py:148`） | `UserRunConfig` Pydantic 模型，`extra="forbid"`（`user_config.py:27`） | 同一 `UserRunConfig 2.0` |
| canonical YAML | 经 `/run-configs/export`、`/run-configs/import`（`app.js:690-724`），服务端 `config.to_yaml()` + `fingerprint()`（`api_v1.py:200-220`） | `UserRunConfig.to_yaml()` / `fingerprint()`（`user_config.py:206-211`） | `/api/v1/run-configs/{import,export,validate}`（`api_v1_fastapi.py:608-618`） | 同一 canonical 序列化 |
| spec_hash | 提交后从 `job.spec_hash` 读取（`app.js:670-674`）；validate 显示指纹前 19 位（`app.js:633`） | `job.spec_hash == validate.fingerprint == config.fingerprint()`（`test_sdk.py:64-65`） | 创建时 `spec_hash=config.fingerprint()`（`api_v1.py:460,757`）；响应返回 `spec_hash`（`api_v1.py:2969`） | 完全一致（含测试证明，见第 4 节） |
| DAG | 展示 `business_steps`/`stages`（`app.js:1001-1003`）；`stageName()` 覆盖全部 10 阶段（`app.js:1292-1299`） | `job.stages` 长度 10（`test_sdk.py:66`） | `STAGE_TYPES` 固定 10 阶段（`stages.py:17-28`），`plan_user_run_stages` 生成（`stages.py:174-...`） | 同一十阶段 DAG |
| owner | `user-<id>` 稳定标签（`app.js:25,149-172`）；`X-Rsim-User` 头（`app.js:53`） | `stable_user_identity()` 默认 `user-<os-login>`（`client.py:77-85,1308-1321`）；可显式 `user=` | 无认证时从 `X-Rsim-User` 派生（`api_v1_fastapi.py:298-315`）；有 Bearer 时从 principal 派生（`api_v1_fastapi.py:299-305`） | Web/SDK/Connector 同 owner 命名空间；见认证备注 |
| 认证 | Bearer token（`app.js:54,227-247`） | `Authorization: Bearer`（`client.py:87`） | `HttpTokenAuthenticator`（`api_v1_fastapi.py:299-305`）；`/health` 返回 `authentication_required`（`api_v1_fastapi.py:420`） | 一致；无认证部署中 `X-Rsim-User` 仅为可信内网分组标签 |
| 提交端点 | `POST /run-jobs`，`config`+`dry_run:false`（`app.js:670-674`） | `POST /api/v1/run-jobs`（`client.py:201-211`） | `POST /api/v1/run-jobs`（`api_v1_fastapi.py:620-633`） | 同一端点 |
| idempotency | `Idempotency-Key` 头，绑定 YAML 签名并跨刷新保留（`app.js:34,661-678`） | `Idempotency-Key` 头（`client.py:199`） | 服务端 `(owner, idempotency_key)` 唯一索引 + request_hash 冲突校验（`api_v1.py:481-487,754-772`；`control_service.py:452-454,1817`） | 同一幂等合同 |
| 状态/事件 | 轮询 `/jobs` 与 `/jobs/{id}/events?since=&tail=`（`app.js:737,947-950`） | `events()`/`stream_events()`/`watch()`/`wait()`（`client.py:727-801`） | `GET /jobs/{id}/events` 返回 `next_cursor`+`terminal`（`api_v1.py:2227-2253`）；SSE（`api_v1_fastapi.py:647-676`） | 同一事件 cursor |
| cancel | `POST /jobs/{id}/cancel`（`app.js:1263`） | `cancel()`（`client.py:803-804`） | `POST /api/v1/jobs/{id}/cancel`（`api_v1_fastapi.py:678-680`） | 同一端点 |
| retry | `POST /jobs/{id}/stages/{sid}/retry`（`app.js:1271`）；仅 failed/cancelled Stage 显示重试（`app.js:1232-1233`） | `retry_stage()`（`client.py:806-807`） | `POST /api/v1/jobs/{id}/stages/{sid}/retry`（`api_v1_fastapi.py:682-684`；`api_v1.py:1714-1727`） | 同一端点；粒度是 Stage（见 GAP-2） |
| manifest | 详情页请求 `/manifest`，展示 status/input_results/失败原因（`app.js:949-950,1031-1072`） | `manifest()`（`client.py:809-810`） | `GET /api/v1/jobs/{id}/manifest`（`api_v1_fastapi.py:686-688`；`api_v1.py:1729-1740`） | 同一响应结构 |
| diagnosis | Web 通过 Manifest/Stage error 展示（`app.js:1031-1052`）；未直接调用 `/diagnosis` | `diagnosis()`（`client.py:884-888`） | `GET /api/v1/jobs/{id}/diagnosis`（`api_v1_fastapi.py:746-748`；`api_v1.py:2042-2175`） | 同一 path-free 诊断合同 |
| 结果下载 | `fetchBinary('/results/{ref}/download')` + blob（`app.js:1176-1213`） | `download_job_result()`/`download_result()`（`client.py:824-927`） | `GET /api/v1/results/{ref}/download`（`api_v1_fastapi.py:1001-1010`） | 同一下载端点；SDK 额外校验 checksum |
| 错误 envelope | `ApiError` 解析 `message/detail`（`app.js:43-49,403-415`） | `RadarSimApiError.from_envelope`（`errors.py:37-46`；`client.py:1248-1268`） | `format_error_envelope`：`code/message/detail/actions/request_id`（`api_v1.py:3602-3616`；`api_v1_fastapi.py:363-416`） | 同一 envelope 结构 |
| 本地绝对路径泄露 | Web 展示用户自己填写的 `spec` 路径（`app.js:1012-1018`） | 公开 Job 投影 path-free（`api_v1.py:3004-3046`） | 服务端 `_public_resolved_spec` 剥离路径（`api_v1.py:3004-3046`）；diagnosis 不泄露（`api_v1.py:2042`） | 服务端不向 Linux/Web 泄露执行节点本地路径 |

### 2.1 遗留 Web 前端（`web/`）不属于 V2 合同

仓库中的 `web/app.js`（`D:\RamboStar\idea\radar-sim\web\`）是**遗留的 V1 项目式前端**，调用 `/api/active-config`、`/api/config/...` 等 legacy 端点，使用 `project/recipe/profile` 概念，**与 V2 `UserRunConfig 2.0` 合同无关**。当前 `serve-v1` 挂载的是 `radar_sim_web/static/`（`api_v1_fastapi.py:1012-1028`，`static_root()` 来自 `radar_sim_web`）。`tests/test_web.py` 测试的是 legacy `cli/web.py`（`test_web.py:1-14`），**不是** V2 Web 前端。V2 Web 前端仅由 `tests/test_api_v1_fastapi.py:614`（`test_v1_web_console_is_same_origin_and_legacy_routes_are_not_shadowed`）做静态内容断言（app.js 中存在 `X-Rsim-User`、`Idempotency`、partial 文案等）。

## 3. 关键机制逐项核验

### 3.1 base URL 约定

- `RadarSimClient.__init__`：`base_url=base_url.rstrip("/")`（`client.py:98`），请求路径均以 `/api/v1/...` 开头（`health`→`client.py:108`、`capabilities`→`client.py:112`、`submit_run`→`client.py:202`、`get_job`→`client.py:708`、`download_result`→`client.py:909`）。
- 结论：SDK 调用方传 `http://host:port`（无 `/api/v1`），客户端内部追加版本前缀。与任务书 8.1 约定一致。测试：`test_sdk.py:52` `RadarSimClient("http://testserver", ...)`。

### 3.2 owner 与认证

- SDK 默认 owner：`stable_user_identity(getpass.getuser())` → `user-<login>`（`client.py:1308-1321`）；显式 `user=` 同样走 `stable_user_identity`（`client.py:77-85`）。Web 默认 `user-<id>`（`app.js:168-172`）。
- 无认证部署（`authentication_required=false`，`api_v1_fastapi.py:420`）下 `X-Rsim-User` 只是可伪造的分组标签（`api_v1_fastapi.py:306-315` 注释明确说明）。这与任务书 10 风险 1、Task C 的结论一致：**当前仅“受信内网试用”，不满足正式多租户**。owner 范围测试：`test_api_v1_service.py:1238`（`test_idempotency_is_scoped_by_owner`）、`test_sdk.py:89`（SDK 无显式 user 时取稳定 OS login 身份）。

### 3.3 idempotency：创建 / 网络重试 / 服务重启

- 服务端：`(owner, idempotency_key)` 部分唯一索引（`control_service.py:452-454`）；`submit_user_run` 先查 `get_job_by_idempotency`，request_hash 相同则返回原 Job（`api_v1.py:481-487`），不同则 `409 idempotency_conflict`（`api_v1.py:3592-3599`）；并发下 `sqlite3.IntegrityError` 兜底（`api_v1.py:767-772`）。
- request_hash = SHA-256 of `{"spec": canonical, "dry_run"}`（`api_v1.py:3556-3564`）。
- SDK 网络重试：`_request` 仅对 `GET/HEAD` 重试 3 次（`client.py:1221`），POST 不重试（`client.py:1221` `else 1`）。测试：`test_sdk.py:1365`（读重试 3 次）、`test_sdk.py:1392`（状态变更请求 attempts==1，不重复提交）。
- 服务重启幂等：`test_api_v1_service.py:1173`（`test_durable_idempotency_survives_new_api_service_instance`）、`test_api_v1_service.py:1523`（`test_v1_idempotency_replay_does_not_call_source_provider_again`）。

### 3.4 事件 cursor 与等待机制

- REST：`GET /api/v1/jobs/{id}/events?since=&limit=&tail=&stream=`（`api_v1_fastapi.py:647-676`），响应 `next_cursor` + `terminal`（`api_v1.py:2247-2252`）。
- SDK：`events()`（`client.py:727`）、`stream_events()` SSE（`client.py:732-741`）、`watch()` 组合 cursor+轮询（`client.py:743-788`）、`wait()`（`client.py:790-801`）。
- **无固定仿真总时长**：`watch()`/`wait()` 的 `timeout`（默认 60s/600s）是调用方观察窗口，超时抛 `TimeoutError`，**不会**终止 Job（`client.py:754-755,772-773,786-787`）。任务书 0 第 3 条“只对 HTTP 请求/提交握手/进程回收设安全边界，不对编译/Cluster 排队设固定总时长”在 SDK 侧成立（编译/排队由服务端 heartbeat/liveness 驱动，见 `non-engine-failure-audit.md` 第 9 节）。
- **GAP-1**：`watch()` 使用固定 `poll_interval`（默认 1.0s），无指数退避；且无 `wait_job()` 方法名（仅 diagnosis `action.type`，`api_v1.py:4052`）。测试：`test_sdk.py:852`（watch/wait/cancel/manifest）、`test_sdk.py:902`（SSE 首连失败带 cursor 重试）、`test_sdk.py:926`（轮询传输失败不重复事件）、`test_sdk.py:954`（连续传输失败超时）。

### 3.5 cancel / retry / partial / 结果下载

- cancel：SDK `cancel()`（`client.py:803-804`）→ `api_v1.py:1708-1712`。测试 `test_sdk.py:852`、`test_api_v1_service.py:1135`。
- retry_stage：SDK `retry_stage()`（`client.py:806-807`）→ `api_v1.py:1714-1727` → `control_service.retry_stage`（`control_service.py:2885-3030`，仅允许 failed/cancelled，重置依赖闭包下游）。测试 `test_sdk.py:875`、`test_api_v1_service.py:1619`（`test_retry_stage_api_preserves_attempt_history`）。
- partial：Job/Manifest 状态 `partial` 由真实 per-input 结果产生（`api_v1.py:2110-2140`），诊断 `simulation_partial`（`api_v1.py:2135`）。测试 `test_api_v1_service.py:477`（`test_partial_manifest_is_terminal_downloadable_and_not_reported_as_total_failure`）。
- 结果下载：SDK 临时文件 + SHA-256 + `temporary.replace(target)` 原子 rename（`client.py:905-925`），checksum 不匹配抛错（`client.py:922-923`）；服务端返回 `archive_checksum`（`local_results.py:72,120`）。测试 `test_sdk.py:656,687,735,770,786`。

### 3.6 错误 envelope 与路径不泄露

- 统一 envelope：`{code, message, detail, actions, request_id}`（`api_v1.py:3602-3616`）；FastAPI 各类 handler 统一使用（`api_v1_fastapi.py:363-416`）。
- SDK 解析：`RadarSimApiError.from_envelope`（`errors.py:37-46`）。
- 服务端不向 Linux/Web 泄露 Windows 本地路径：`_public_resolved_spec` 移除 dataset entries 并 path-free 摘要（`api_v1.py:3004-3046`）；diagnosis path-free（`api_v1.py:2042-2044`）。测试 `test_api_v1_service.py:400`（`test_diagnosis_reports_pending_job_without_exposing_user_paths`）。

## 4. spec_hash / DAG 对等证明（代码 + 测试）

### 4.1 代码路径

- canonical 与 fingerprint：`UserRunConfig.to_dict()`（`user_config.py:162-204`）+ `fingerprint()` = SHA-256 of `json.dumps(sort_keys, separators=(",",":"))`（`user_config.py:209-211`）。
- 服务端 validate 返回 `fingerprint`（`api_v1.py:232-235`）；submit 时 `config_hash = config.fingerprint()` 并写入 `payload.spec_hash`（`api_v1.py:460,757`）；Job 响应 `spec_hash` 从 payload 读出（`api_v1.py:2969`）。
- DAG：`STAGE_TYPES` 固定十阶段（`stages.py:17-28`），依赖 `STAGE_DEPENDENCIES`（`stages.py:30-45`），`plan_user_run_stages`（`stages.py:174-...`）为 `UserRunConfig` 生成 `execution_plan`（10 项）。
- Web 与 SDK 使用同一份 config JSON：Web 表单直接生成该 JSON（`app.js:468-479`），SDK `_run_config_payload` 走同一 `UserRunConfig`（`client.py:1270-1278`）。

### 4.2 测试证据

`tests/test_sdk.py:57` `test_sdk_validate_and_submit_run_share_v2_hash_with_web_json`（已通过）：

```python
validation = sdk.validate_run(config)
job = sdk.submit_run(config, dry_run=True, idempotency_key="sdk-key")
assert validation.fingerprint == config.fingerprint()
assert job.spec_hash == validation.fingerprint   # SDK 侧 hash 与 fingerprint 一致
assert len(job.stages) == 10                      # SDK 侧 10-stage DAG
assert job.spec == config.to_dict()
submitted = sdk.submit_run(config, dry_run=True, idempotency_key="sdk-key")
assert submitted.id == job.id                     # 幂等：同 key 返回同一 Job
```

`tests/test_api_v1_service.py:768` `test_project_free_run_config_validate_and_submit_waits_for_node_recognition`（已通过）：`len(validation["execution_plan"]) == 10`、`job["spec_hash"] == validation["fingerprint"]`、`job["stages"][0]["stage_type"] == "resolve_spec"`。

`tests/test_v2_public_contract.py:130` `test_explicit_source_and_result_path_are_identical_in_user_api_and_sdk_roundtrip`（已通过）：同一 `UserRunConfig 2.0` 在用户 API 与 SDK roundtrip 后字段一致。

结论：**Web 与 SDK 从同一 `UserRunConfig 2.0` 生成同一 canonical YAML、同一 `spec_hash`（= fingerprint）和同一十阶段 DAG，有代码路径与自动化测试共同证明。**

## 5. SDK 能力清单：已实现 / 已测试 / 仅文档

| SDK 能力 | 方法 | 实现位置 | 自动化测试 | 状态 |
|---|---|---|---|---|
| `validate_run()`（不启动编译/仿真） | `validate_run` | `client.py:170-177` | `test_sdk.py:57`；`test_api_v1_service.py:758` | 已实现 + 已测试 |
| `submit_run()` 幂等创建 | `submit_run` | `client.py:179-223` | `test_sdk.py:57,426,536,1108`；`test_api_v1_service.py:1135,1194` | 已实现 + 已测试 |
| `submit_yaml()` | `submit_yaml` | `client.py:607-627` | `test_sdk.py:426` | 已实现 + 已测试 |
| 自适应等待（事件 cursor + 轮询，无固定总时长） | `events`/`stream_events`/`watch`/`wait` | `client.py:727-801` | `test_sdk.py:852,902,926,954` | 已实现 + 已测试；**无指数退避、无 `wait_job` 方法名**（GAP-1） |
| `cancel_job()` | `cancel` | `client.py:803-804` | `test_sdk.py:852`；`test_api_v1_service.py:1135` | 已实现 + 已测试 |
| `retry_stage()` | `retry_stage` | `client.py:806-807` | `test_sdk.py:875`；`test_api_v1_service.py:1619` | 已实现 + 已测试 |
| 失败输入重试（partial） | 经 `retry_stage(run_simulation)` + Agent checkpoint | `client.py:806-807`；`agent_local_run.py:591-624` | partial：`test_api_v1_service.py:477`；无“按失败输入粒度”的独立测试/API | **接口粒度不同**（GAP-2），语义正确，需真实验收 |
| `get_manifest()` | `manifest` | `client.py:809-810` | `test_sdk.py:852`；`test_api_v1_service.py:1135` | 已实现 + 已测试 |
| `get_diagnosis()` | `diagnosis` | `client.py:884-888` | `test_api_v1_service.py:400,416,477,548,613,646`；`test_sdk.py:820` | 已实现 + 已测试 |
| `download_job_result()`（checksum+临时文件+原子 rename） | `download_job_result`/`download_result` | `client.py:824-927` | `test_sdk.py:656,687,735,770,786` | 已实现 + 已测试 |
| 数据面直传（不经过 Linux HTTP body） | `issue_transfer_plan`/`execute_transfer_plan` 等 | `client.py:464-605` | `test_sdk.py:963,1029,1108,1237,1303,1336`；`test_v1_cluster_yaml_sdk.py:124` | 已实现 + 已测试 |
| 断点续传（result/artifact/dataset upload） | `upload_result_archive` 等 | `client.py:929-1048` | `test_result_upload_service.py` 等（本组定向回归通过） | 已实现 + 已测试 |

### 5.1 需要真实部署验收（本机不可用）

- 对 live server 的真实 SDK Job 提交/等待/下载闭环（`submit → wait → manifest → download` 全链路，真实事件 cursor 与 checksum 证据）：本机无 live server 与 Windows/Cluster，只能通过 TestClient/MockTransport 做代码级验证。→ 需真实部署验收。
- 长编译 / Cluster 长队列下 SDK `watch()`/`wait()` 长时间运行不误判：代码级验证无固定总时长，需真实长任务验收。
- SDK 断网恢复后的 resumable 上传真实续租：代码级测试通过，需真实网络中断验收。
- partial 场景下“只重试失败输入”的真实 attempt/资源消耗证据：需真实批量 + 部分失败验收。

## 6. 复测命令与结果

```bash
.venv/Scripts/python.exe -m pytest tests/test_sdk.py tests/test_v1_cluster_yaml_sdk.py \
  tests/test_web.py tests/test_api_v1_service.py tests/test_run_config_resolution_flow.py -q
# 151 passed, 1 warning（StarletteDeprecationWarning，来自 fastapi.testclient 依赖，非本仓库告警）

.venv/Scripts/python.exe -m pytest tests/test_v2_public_contract.py tests/test_http_auth.py \
  tests/test_user.py tests/test_identity_unification.py tests/test_control_stages.py -q
# 72 passed, 1 warning
```

说明：`tests/test_web.py` 覆盖的是 legacy `cli/web.py`（V1 项目式 Web），与本审计的 V2 Web 前端无关；V2 Web 前端由 `tests/test_api_v1_fastapi.py:614` 做静态内容断言。

## 7. 风险分级与未解决项

| 级别 | 项目 | 说明 |
|---|---|---|
| P1 | GAP-2 失败输入重试接口粒度 | 任务书要求 `retry_stage()` 与“失败输入重试”并列交付；当前只有 Stage 级 `retry_stage`，失败输入重试靠 run_simulation 的 checkpoint 实现，无 per-input 独立 API/测试。语义正确但接口与文档不完全对应，需要真实 partial 验收并考虑是否暴露按失败输入重试的显式入口。 |
| P1 | 认证 | 无认证部署下 `X-Rsim-User` 可伪造（`api_v1_fastapi.py:306-315`），不满足正式多租户；需按任务书 7.4 启用 Bearer/SSO 门禁（与 Task C 一致）。 |
| P2 | GAP-1 等待命名与退避 | SDK 无 `wait_job()` 方法名（仅 diagnosis action.type）；`watch()` 无指数退避（固定 `poll_interval`）。功能满足“cursor 优先 + 轮询兜底 + 无固定仿真总时长”，但建议对齐文档命名、评估加退避。 |
| P2 | SDK 真实端到端 | SDK 独立端到端调用需真实部署验收（live server + Windows/Cluster + 真实长任务/partial/断网）。 |
| P2 | 结果 download 的二进制流重试 | `download_result` 明确不自动重试大文件（`client.py:915-920`），需调用方显式重试；符合“多 GB 归档不能静默重复拉取”的安全取舍，但文档需提示调用方重试策略。 |

## 8. 未做改动声明

本审计为 AUDIT ONLY：未修改 `radar_sim_sdk/`、`core/`、`radar_sim_web/`、`web/` 任何源代码，未提交任何内容。所有结论基于当前工作树（未提交状态）与定向回归测试结果。
