# 2026-08-17 产品合同与用户故事审计（Task A：产品场景矩阵）

> 日期：2026-08-17
> 任务：执行任务书（`docs/handoffs/2026-08-17-radar-sim-service-scenarios-ai-execution-brief.md`）的 Task A —— 产品合同和用户故事审计
> 审计人：AI 审计代理（未修改任何源码，未提交任何 commit）
> 上游文档：执行任务书 §0/§2/§2A/§3/§4/§6/§8/§12/§13、`PRD.md`、`docs/PRODUCT_CONTRACT.md`、`docs/V2_ARCHITECTURE.md`、`docs/DETAILED_DESIGN.md`
> 代码基线：`20ba6b7`（HEAD）、分支 `codex/new-branch`

---

## 0. 结论摘要

- 当前代码在 **字段 / 十阶段 DAG / 最终状态集合 / 错误信封 / Web-SDK 同合同** 上与四份文档高度一致，绝大多数公共合同项有**代码实现 + 自动化测试**背书。
- 存在 **5 项真实不一致/缺口**，其中最值得注意的是：**不存在独立的“只重试失败输入”API/SDK/Web 能力**（任务书 §6.3 明确要求交付），当前只有“阶段级重试 `retry_stage`”；以及 **partial 状态在控制面 DB 与公共 API 之间的表达不一致**（DB 存 `failed`，API 投影为 `partial`）。
- 全部“四组合 × 单/批”场景中：**代码+单测背书**的组合齐全；但 **Windows 真实首装/重启、真实 Selena 编译、真实 Cluster 提交/收集、250+ 批量、跨 owner 认证隔离** 均需真实部署验收，本机无法替代，一律标记为“需真实部署验收”，不能据此宣称已通过。

### 0.1 测试证据基线（本机实际执行）

| 测试文件 | 结果 | 说明 |
|---|---|---|
| `tests/test_v2_public_contract.py` `test_user_config.py` `test_run_config_resolution_flow.py` `test_simulation_spec.py` | **71 passed** | V2 合同、UserRunConfig 2.0、run-config 解析流程 |
| `tests/test_v1_cluster_yaml_sdk.py` | **5 passed** | 现有 V1 Cluster YAML SDK 链路 |
| `tests/test_sdk.py` | **55 passed** | SDK validate/submit/wait/cancel/retry/download |
| `tests/test_control_stages.py` + `tests/test_agent_local_run.py` | **44 passed** | 控制面阶段重试/取消/partial、本地批量混合失败/checkpoint 恢复 |
| `tests/test_api_v1_service.py` | **59 passed** | 公共 API、partial 投影、owner 隔离、重试 attempt 历史 |
| `tests/test_api_v1_fastapi.py` | **33 passed** | HTTP 路由、错误信封、重试/取消路由 |
| **合计** | **267 passed, 0 failed** | 本次 Task A 定向回归基线 |

环境备注：`.venv` 中原本未安装 pytest，审计时临时 `pip install pytest pytest-timeout` 后方可运行；未影响任何项目源码。

---

## 1. 四份文档 + 代码的字段/状态/错误码对照

### 1.1 配置字段（UserRunConfig 2.0）

| 字段 | PRD §4 | PRODUCT_CONTRACT §2 | V2_ARCHITECTURE §3 | 代码（`core/user_config.py`） | 结论 |
|---|---|---|---|---|---|
| `schema_version` | "2.0" | "2.0" | "2.0" | `Literal["2.0"]` (user_config.py:148) | 一致，代码强制 |
| `selena.source` | build/existing | build/existing | existing/build | `Literal["build","existing"]` (user_config.py:31) | 一致 |
| `selena.code_path` | build 必填 | build 必填 | build 必填 | build 校验必填 (user_config.py:52-55) | 一致 |
| `selena.branch` | 可选，仅警告 | 可选，仅警告 | 可选，仅提示 | 仅 trim，不自动切换 (user_config.py:44-47) | 一致 |
| `selena.selena_build_script` | build 必填 | build 必填 | build 必填 | build 校验必填 (user_config.py:54-55) | 一致 |
| `selena.package_build_script` | 可选 | 可选依赖诊断 | 可选依赖诊断 | 可选 (user_config.py:35, to_dict 173) | 一致 |
| `selena.runtime_xml` | 两者必填 | 两者必填 | 必填 | build/existing 都必填 (user_config.py:56-57,64-65) | 一致 |
| `selena.existing_path` | existing 必填 | existing 必填 | existing 必填 | existing 校验必填 (user_config.py:62-63) | 一致 |
| `data.path` | 单一路径 | 单一路径 | 单一路径 | 必填非空 (user_config.py:88-93) | 一致 |
| `simulation.target` | auto/local/cluster | auto/local/cluster | auto/local/cluster | `Literal[...]` 默认 auto (user_config.py:117) | 一致 |
| `simulation.source` | RadarFC/FL/FR/RL/RR | 同左 | 同左 | 别名归一化 RadarX (user_config.py:127-143) | 一致（代码额外接受 fc/fl 小写别名） |
| `simulation.adapter_file` | 可选 | 可选 | 可选 | 可选 (user_config.py:120) | 一致 |
| `simulation.mat_filter` | 可选显式优先 | 可选显式优先 | 可选显式优先 | 可选，不推导 (user_config.py:120) | 一致 |
| `result.path` | 接收端根目录 | 接收端根目录 | 接收端根目录 | 留空=auto，不解析 (user_config.py:96-113) | 一致 |

### 1.2 十阶段 DAG

| 阶段 | 任务书 §4 | DETAILED_DESIGN §6 | 代码 `core/stages.py` | 结论 |
|---|---|---|---|---|
| 顺序与集合 | 固定十阶段 | 固定十阶段 | `STAGE_TYPES` (stages.py:17-28) 完全一致 | 一致 |
| 阶段可 skipped | 是 | 是 | `plan_user_run_stages` (stages.py:174-229) | 一致 |
| build 时 `prepare_source` skipped | — | — | reason `current_workspace_selected` (stages.py:188-190) | 代码实现，文档未细述 |
| existing 时 `prepare_source`+`build_selena` skipped | — | — | reason `existing_selena_uses_registered_artifact` (stages.py:191-193) | 代码实现 |
| Cluster 可见 existing 时 `resolve_spec`+`register_artifact` 可 skipped | — | — | `_CLUSTER_VISIBLE_EXISTING_SKIP_REASON` (stages.py:194-199,81-105) | 代码实现，需真实部署验证 |

### 1.3 最终状态集合（Job 级）

| 状态 | 任务书 §6.2 | V2_ARCHITECTURE §9 | 代码（`core/api_v1.py:_v1_status` 3481-3501） | 结论 |
|---|---|---|---|---|
| `queued/running/needs_input` | 有 | 有 | 有 | 一致 |
| `succeeded` | 有 | 有 | 有 | 一致 |
| `partial` | 有 | 有 | 有，但为**投影**状态（见 §2.2） | 用户侧一致，DB 层不一致 |
| `failed` | 有（归因 simulation/framework） | 有 | 有 | 一致 |
| `cancelled` | 有 | 有 | `cancel_requested`→公共 `cancelling`（api_v1.py:3483-3484），终态 `cancelled` | 有中间态 `cancelling`，文档未列，属实现细节 |
| `blocked` | 未列为 Job 状态 | — | 阶段 blocked→Job `needs_input`（api_v1.py:3497-3500） | 阶段级 `blocked` 是内部态，映射到公共 `needs_input` |

### 1.4 错误信封

| 项 | 任务书 §8.1 | 代码 `format_error_envelope`（api_v1.py:3602-3616） | 结论 |
|---|---|---|---|
| 稳定 code | 要求 | `code` | 一致 |
| detail | 要求 | `detail` | 一致 |
| action | 要求 | `actions`（列表） | 字段名/形状略有出入，语义覆盖 |
| message | — | `message` | 额外字段 |
| request_id | — | `request_id` | 额外字段（可追踪性，加分项） |
| 不泄露本地绝对路径 | 要求 | 公共 Stage 投影 path-free（`_public_run_stage`） | 一致 |

### 1.5 Web / SDK 行为对照

| 能力 | 任务书 §8.2 / §3 | Web（`radar_sim_web/static/app.js`） | SDK（`radar_sim_sdk/client.py`） | 结论 |
|---|---|---|---|---|
| 同一 YAML 提交 | 必须 | `runConfigFromForm` (app.js:432) / `/api/v1/run-jobs` | `submit_run`/`submit_yaml` (client.py:179,607) | 一致 |
| validate/readiness 预览 | 必须 | `validateCurrentSpec` (app.js:623) `/run-configs/validate` | `validate_run` (client.py:170) | 一致 |
| 取消 | 必须 | `cancelJob` (app.js:1261) `/jobs/{id}/cancel` | `cancel` (client.py:803) | 一致 |
| 阶段重试 | 必须 | `retryStage` (app.js:1269) `/jobs/{id}/stages/{sid}/retry` | `retry_stage` (client.py:806) | 一致 |
| **只重试失败输入** | §6.3 明确要求 | **无**（逐条结果只展示状态徽标，app.js:1054-1072，无重试按钮） | **无**（只有 `retry_stage`） | **缺口，仅文档声明** |
| 结果 ZIP 下载 | 必须 | `downloadResult` (app.js:1176) `/results/{ref}/download` | `download_job_result` (client.py:824) / `download_result` (client.py:897) | 一致 |
| 事件 cursor/自适应等待 | §8.2 | 轮询 4s (app.js:1376)，无固定总时长 | `watch`/`wait`（client.py:743,790），事件优先+轮询兜底 | 一致（SDK 默认 600s 见 §2.4） |
| 幂等提交 | §8.1 | `Idempotency-Key` + config signature（app.js:661-674） | `Idempotency-Key` header（client.py:199） | 一致，有测试背书（test_sdk.py:1392 网络错误不重复提交） |
| 错误信封解析 | §8.1 | `ApiError`（app.js:43-49） | `RadarSimApiError`（errors.py） | 一致 |
| base URL 约定 | §8.1 base URL 不带 /api/v1 | `const API = "/api/v1"`（app.js:3） | 各方法硬编码 `/api/v1/...`（client.py:108,112,...），`base_url.rstrip("/")`（client.py:98） | 一致 |

---

## 2. 不一致清单（含 file:line 证据）

### 2.1 【P1】无独立“只重试失败输入”能力（任务书 §6.3 缺口）

- 任务书 §6.3：“后续 AI 必须交付『只重试失败输入』的 API/SDK/Web 行为和实测证据，确保成功输入不会重复消耗编译/仿真资源。”
- 现状：
  - 公共 API 只有 `POST /api/v1/jobs/{job_id}/stages/{stage_id}/retry`（`core/api_v1_fastapi.py:682-685` → `core/api_v1.py:1714-1727` → `core/control_service.py:2885`）。该接口是**阶段级**重试：把目标阶段重置为 `queued` 并重排队下游阶段（control_service.py:2998-3018），重试 `run_simulation` 会**重跑该阶段全部输入**，不区分成功/失败输入。
  - SDK 只有 `retry_stage()`（`radar_sim_sdk/client.py:806-807`），无 `retry_failed_inputs()`。
  - Web 任务详情的逐条结果区（`radar_sim_web/static/app.js:1054-1072`）只渲染状态徽标，**无任何按输入重试按钮**；阶段重试按钮（app.js:1232-1234）仅对 `failed`/`cancelled` 阶段显示。
  - 本地批量的“成功输入不重复执行”目前只体现在 **Connector 重启恢复的 checkpoint 机制**（`test_agent_local_run.py:281` `test_recovery_resumes_after_durable_batch_checkpoint`），这是故障恢复语义，**不是用户主动“只重试失败输入”的公开能力**。
- 判定：**仅文档声明，当前代码未实现独立 per-input 重试接口**。未达到任务书 §6.3 / §8.2 的交付要求。
- 建议：Task H 负责补齐（新增 per-input 重试 API + SDK 方法 + Web 按钮），本审计仅标记。

### 2.2 【P2】`partial` 状态：控制面 DB 与公共 API 表达不一致

- 公共 API 通过 `_v1_status`（`core/api_v1.py:3481-3501`）在 Job `status ∈ {succeeded,failed}` 且 Manifest `status=partial` 时**投影**为 `partial`。
- 但控制面 DB 中该 Job 的持久状态为 `failed`：`tests/test_api_v1_service.py:531-532` 明确断言 `control.get_job(...)["status"] == "failed"` 而 `public_job["status"] == "partial"`。
- 用户侧（Web/SDK/API）读取同一个 API 投影，因此**对外一致**；但任何直接读 DB 的状态逻辑/告警（以及 stale/reclaim）看到的是 `failed`，存在被误判为全失败的风险。任务书 §4A 要求控制面状态与业务结果状态分层，此处实现确实分层了，但分层边界需要在 Task B 验证：DB 的 `failed` 不会被 stale/恢复逻辑当成“仿真全失败”而清理成功结果。
- 判定：**代码实现（分层投影）+ 单测背书，但 DB-API 不一致需 Task B 复核 + 真实部署验证**。

### 2.3 【P2】Web 阶段重试按钮条件与 API `available_actions` 不一致

- API 只为 `status == "failed"` 的阶段下发 `retry_stage` action（`core/api_v1.py:3469-3478`，`failed` 判断 `stage_status == "failed"`）。
- Web 对 `failed` 或 `cancelled` 阶段都渲染“重试”按钮（`radar_sim_web/static/app.js:1232-1234`：`["failed", "cancelled"].includes(stage.status)`）。
- 结果：Web 可能在 API 不提供 `retry_stage` action 时仍显示“重试”（针对 cancelled 阶段）。虽然 `retry_stage` 后端允许 `cancelled` 阶段重试（control_service.py:2895 接受 `failed`/`cancelled`），但 Web 展示依据与 API 下发依据不一致，属于 UI/API 口径不一致。
- 判定：**代码实现，存在轻微不一致**，不影响后端正确性。

### 2.4 【P2】SDK `wait`/`watch` 默认超时 600 秒，与“无固定仿真总时长”表述存在歧义

- `radar_sim_sdk/client.py:790-801` `wait(timeout=600.0)` 与 `watch`（743-788）默认 `timeout=600.0`，超时抛 `TimeoutError`。
- 这是 **SDK 调用方本地轮询循环的边界**，不是服务端/仿真执行的固定总时长上限（服务端没有固定总超时，见任务书 §14 已确认“长编译没有被固定总时长提前杀掉”）。因此不违反任务书“无固定仿真总时长”的本意。
- 但对使用者而言，长编译/长 Cluster 排队时若不显式传大 `timeout`，`wait()` 会在 10 分钟后抛 `TimeoutError`，容易被误读为“仿真被固定时间杀掉”。需要文档/示例明确提示，或提供无界等待的语义。
- 判定：**代码实现；行为需在真实长任务上验收并补文档说明**。

### 2.5 【P3】Web 导入对旧字段 `build_script` 的兼容回退

- `radar_sim_web/static/app.js:490-491`：导入时 `selena.selena_build_script || selena.build_script || ""` 回退到 legacy 字段 `build_script`。
- 这与 `docs/V2_ARCHITECTURE.md` §2 “旧版本字段不会被静默迁移、忽略或猜测，提交旧 YAML 会直接返回校验错误”的严格声明存在边界情形：导入返回里若带 `build_script`，Web 表单会显示，但用户直接提交时 `UserRunConfig`（`extra="forbid"`）会拒绝该字段。属于 Web 显示层的兼容回退，不改变提交校验。
- 判定：**仅影响显示，轻微不一致；非阻断**。

### 2.6 【P3】Web `stageName`/`friendlySkipReason` 仍保留已删除阶段旧名

- `radar_sim_web/static/app.js:1296-1298` 保留 `prepare_selena`、`collect_manifest` 等旧阶段名映射；`:1302-1310` 保留旧 skip reason（如 `registered_runtime_bundle_selected`、`existing_selena_kept_on_local_full_agent`、`dry_run_plan_only`）。
- 这些名称不在当前十阶段 DAG（stages.py:17-28）内，属于陈旧 UI 映射，不影响逻辑。
- 判定：**仅文档/UI 陈旧项，非功能问题**。

### 2.7 【P3】Web 状态筛选缺少 `blocked` 选项

- `radar_sim_web/static/index.html:247-250` 状态筛选器提供 queued/running/needs_input/succeeded/partial/failed/cancelled，无 `blocked`；但 `statusName`（app.js:1284-1290）能显示“已阻塞”。
- 阶段 `blocked` 在 Job 级映射为 `needs_input`（api_v1.py:3497-3500），筛选 `needs_input` 可覆盖，因此功能可用；仅“阶段级 blocked”无法直接筛选。
- 判定：**轻微 UI 缺失，非阻断**。

### 2.8 已核对无冲突的项（避免过度报告）

- `data.path` 单一路径语义、`result.path` 接收端语义、`source_to_local` 稳定 `source_to_local_unavailable`（`core/api_v1.py:1799-1804`）、Cluster 直传 `transfer_mode=source_to_local` 拒绝路径、`X-Rsim-User` 仅为路由非认证（app.js:21-27 注释、V2_ARCHITECTURE §8 明确）——代码与文档一致。

---

## 3. 场景矩阵

标记约定：
- **[C]** = 代码实现 + 自动化测试背书（测试文件名/行号给出）
- **[D]** = 仅文档声明（文档有描述，但无代码/无测试对应，或代码缺失）
- **[R]** = 需真实部署验收（代码/单测已有，但真实 Windows 首装、真实 Selena、真实 Cluster、250+ 批量、跨 owner 认证隔离必须实机验收，本机无法替代）
- **[C/D]** = 代码实现，但仅有文档/部分测试，真实链路未验收

> 结论先行：**没有一行是“当前代码完全不支持”的纯文档行**；所有四组合都有代码路径。差异在于**真实部署验收层**（`[R]`）与**“只重试失败输入”这类缺口**（`[D]`）。

### 3.1 四组合总表

| # | Selena 来源 × 目标 | 单/批 | 路径位置 | 用户入口 | 输入（UserRunConfig 2.0） | 预期 Stage（含 skipped） | 最终状态 | 结果位置 | 重试动作 | 标记 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | existing × local | 单 | Windows 本地盘 `C:/` | Web | `existing_path`+`runtime_xml`+`data.path` | resolve_spec→environment_check→(prepare_source:skip)→prepare_data→(build_selena:skip)→register_artifact→preflight→run_simulation→collect_results→finalize_manifest | `succeeded` / `failed` / `partial`(混合) | `<result.path>/<job_id>` 或 `~/RadarSim/results/<job_id>`（本机物化）+ 服务端 ZIP | 失败阶段 `retry_stage` | [C] 见 §4 测试 / [R] 真实 Selena 执行 |
| 2 | existing × local | 批 | Windows 本地盘 | SDK | 同上 + 目录批量 MF4 | 同上（逐输入 checkpoint） | 混合→`partial`；全成功→`succeeded`；全失败→`failed(simulation)` | 同上 | 阶段重试；**无 per-input 重试** | [C] `test_mixed_batch_continues_after_one_input_failure`(test_agent_local_run.py:428)、`test_partial_manifest...`(test_api_v1_service.py:477) / [R] 真实多 MF4 |
| 3 | build × local | 单 | Windows 本地盘（code+data） | Web | `code_path`+`selena_build_script`+`runtime_xml`+`data.path` | resolve_spec→environment_check→(prepare_source:skip,current_workspace_selected)→prepare_data→build_selena→register_artifact→preflight→run_simulation→collect_results→finalize_manifest | `succeeded` / `failed` / `partial` | 同上 | build 失败→`retry_stage(build_selena)`；run 失败→`retry_stage(run_simulation)` | [C] 编译策略/产物确认测试见 §4 / [R] 真实 Jenkins 脚本编译 |
| 4 | build × local | 批 | Windows 本地盘 | SDK / REST | 同上 + 批量 | 同上 | `partial` 可发生 | 同上 | 同上 | [C] / [R] 真实批量编译+仿真 |
| 5 | existing × cluster | 单 | Cluster 可读共享路径（零复制） | Web（无需 Connector） | `existing_path`(shared)+`runtime_xml`(shared)+`data.path`(shared) | (resolve_spec:skip,cluster_visible)→environment_check→prepare_data→(build_selena:skip)→(register_artifact:skip 或 direct_transfer)→preflight→run_simulation→collect_results→finalize_manifest | `succeeded` / `failed` / `partial` | 服务端 owner 隔离 ZIP / `result_ref`（无反向直传） | 阶段重试；**Cluster collect 重试不重新 submit** | [C] `cluster_visible_existing_selena`(stages.py:81-105)、test_cluster_direct_refs.py / [R] 真实 Cluster |
| 6 | existing × cluster | 单 | Windows 本地盘（Selena/数据在 Windows） | Web + Connector 直传 | 同上，本地路径 | 增加 direct_transfer（register_artifact 走 `direct_transfer` 域，stage_routing.py:27-42） | 同上 | 同上 | 阶段重试 | [C] `test_v1_cluster_yaml_sdk.py` / [R] 真实 Connector→Cluster 直传 |
| 7 | existing × cluster | 批 | SDK/Linux 本地数据 → Cluster 数据面 | SDK | 同上 + 批量 + `client_transfer_roles` | SDK 直传 + prepare_data + register_artifact(direct_transfer) + run_simulation(cluster) | `partial` 可发生 | 服务端 ZIP | 阶段重试 | [C] `test_sdk_retry_skips_roles_with_durable_transfer_manifests`(test_sdk.py:1237) / [R] 真实 SDK 直传 |
| 8 | build × cluster | 单 | Windows 本地盘（code）+ Cluster | Web + Connector | `code_path`+`selena_build_script`+`runtime_xml`+`data.path` | resolve_spec→environment_check→prepare_data→build_selena→register_artifact(direct_transfer)→preflight→run_simulation(cluster)→collect_results→finalize_manifest | `succeeded` / `failed` | 服务端 ZIP | build 失败→retry_stage(build)；Cluster 收集失败→retry_stage(collect)，不重 submit | [C] `register_artifact_dispatch_scope`(stage_routing.py:27-42)、test_cluster_stage_executor.py / [R] 真实 Windows 编译+Cluster |
| 9 | build × cluster | 批 | 混合 | SDK | 同上 + 批量 | 同上 | `partial` 可发生 | 服务端 ZIP | 同上 | [C] / [R] 真实批量 |

### 3.2 特殊/横向场景

| # | 场景 | 输入 | 预期 Stage | 最终状态 | 结果位置 | 重试动作 | 标记 |
|---|---|---|---|---|---|---|---|
| 10 | 远端资源 → 本地 Windows（`source_to_local`） | 远端 Selena/数据 + `target=local` 但本机不可读 | 在 transfer 签发处阻断 | 稳定 `source_to_local_unavailable`，不静默绕路 | — | 改用本机可读路径或共享路径（错误信封带 action） | [C] `core/api_v1.py:1799-1804`、`_block_source_to_local_tasks`(api_v1.py:1274) |
| 11 | `target=auto` 路由选择 | 任意组合 | resolve_spec 内决策 `selected_target` | 依赖所选路径 | — | 决策原因写入 resolved_spec/日志 | [C] `_select_user_execution_target`(api_v1.py:1000)、`selected_execution_target`(stage_routing.py:8-24) / [R] 真实多能力环境 |
| 12 | 批量 8 成功 + 2 失败（任务书 §3.1-6） | 10 条 MF4，2 条 Selena 内部失败 | run_simulation 逐输入继续 | `partial`，8 个成功结果可下载，2 个失败输入有日志尾 | 服务端 ZIP / 本机结果目录 | 只重试失败输入（**当前缺口，见 §2.1**） | [C] partial 投影测试 / **[D] per-input 重试缺失** / [R] 真实 250+ 批量 |
| 13 | 用户取消 → 重试 | 任意 | cancel 终止当前 attempt，保留已固化结果 | `cancelled` → 重试后新 attempt | 已完成结果保留 | 明确新 attempt，不重复已完成输入 | [C] `test_cancellation_is_terminal...`(test_agent_local_run.py:352)、`test_cancel_preserves_skipped_stage...`(test_control_stages.py:155)、`test_retry_source_restores_upstream_cancelled...`(test_control_stages.py:481) / [R] 真实长任务取消 |
| 14 | 网络重试/幂等提交（不产生两个 Job） | 同一 YAML + Idempotency-Key | 创建阶段 | 幂等返回原 Job | — | 网络错误不重发状态变更请求 | [C] `test_sdk_does_not_retry_transport_errors_for_state_changing_requests`(test_sdk.py:1392)、`_raise_idempotency_conflict`(api_v1.py:3592-3599) |
| 15 | 框架失败 ≠ 仿真失败，不得伪装 partial | 传输/Manifest 不一致 | 对应失败 Stage | `failed`/`needs_input`（framework），不标 partial | 不发布不可信结果 | 修复外部条件后从最近安全 Stage 重试 | [C] `_normalize_manifest` 一致性检查（control_service.py:167-176、api_v1.py:2103-2107）、`test_partial_manifest_continues_to_finalize...`(test_control_stages.py:585) |

---

## 4. 代码 + 测试证据 vs 仅文档声明

### 4.1 有代码 + 自动化测试背书（[C]）的项目

| 声称 | 代码位置 | 测试证据 |
|---|---|---|
| UserRunConfig 2.0 唯一模型、extra=forbid、round-trip | `core/user_config.py:26-224` | test_user_config.py（随 71 passed 通过） |
| 十阶段固定 DAG | `core/stages.py:17-45` | test_run_config_resolution_flow.py / test_v2_public_contract.py |
| Web 与 SDK 同一 YAML、同一 /api/v1 | `radar_sim_web/static/app.js:3,432` / `radar_sim_sdk/client.py:170-223` | test_sdk.py、test_api_v1_fastapi.py:181 |
| 幂等创建 + request_hash + Idempotency-Key | `core/api_v1.py:410-487,3556-3563` | test_sdk.py:1392、test_api_v1_fastapi.py |
| 错误信封 code/detail/actions/request_id | `core/api_v1.py:3602-3616` | test_api_v1_fastapi.py:818（route error + owner isolation） |
| 取消 / 阶段重试（保留 attempt 历史、重置下游） | `core/control_service.py:2885-3018`、`core/api_v1.py:1708-1727` | test_control_stages.py:110,148,155,322,363,481；test_api_v1_service.py:1619 |
| partial 由 Manifest 逐输入混合结果产生、可下载、不标全失败 | `core/api_v1.py:3481-3501,2042-2156` | test_api_v1_service.py:477；test_control_stages.py:585 |
| 本地批量单条失败继续 + checkpoint 恢复 | `core/agent_local_run.py:430-463` | test_agent_local_run.py:428,281 |
| 取消是终态、不调用 runner、保留成功证据 | `core/agent_local_run.py` | test_agent_local_run.py:352 |
| Cluster direct transfer 角色域（local_runtime vs direct_transfer） | `core/stage_routing.py:27-42` | test_stage_binder.py / test_cluster_stage_executor.py |
| SDK 事件 cursor + 自适应轮询（无服务端固定总时长） | `radar_sim_sdk/client.py:743-801` | test_sdk.py:852,875 |
| SDK 下载校验 checksum、临时文件、原子 rename | `radar_sim_sdk/client.py:824-897` | test_sdk.py（下载路径） |
| `source_to_local` 稳定 unavailable | `core/api_v1.py:1799-1804,1274` | test_api_v1_service.py 相关失败路径 |
| 阶段 blocked → Job needs_input | `core/api_v1.py:3497-3500` | test_api_v1_service.py |

### 4.2 仅文档声明（[D]），当前代码未满足/未对应

| 声称 | 文档 | 现状 |
|---|---|---|
| “只重试失败输入”的 API/SDK/Web 行为与实测 | 任务书 §6.3 / §8.2 | **无独立 per-input 重试接口**（详见 §2.1） |
| 正式多用户认证下 owner 从认证主体派生 | 任务书 §7.4、V2_ARCHITECTURE §8、PRODUCT_CONTRACT §7 | `X-Rsim-User` 仍只是路由标签；Bearer 认证为可选部署，**本机未验证启用** → 只能称“受信内网试用” |
| 250+ 批量、断点续传、源变更的真实指标 | 任务书 §12 | 单测覆盖部分逻辑，**无真实大批量实测** |
| Web 对“只重试失败输入”的按钮 | 任务书 §3.1-6 | Web 逐条结果区无重试按钮（app.js:1054-1072） |

### 4.3 需真实部署验收（[R]）——本机无法替代，不得宣称通过

- 真实 Windows 用户首装统一 Connector（干净环境）→ 自启/重连/单实例/升级（任务书 §7/§E/§O）；
- 真实 Selena 编译（Jenkins 脚本、full_clean 真实执行、深层 `selena.exe`、跨分支）——本机仅有策略预演与事件证据，**真实 Job `job_26028465ebeb` 已 succeeded 的结论来自任务书 §14，非本次审计复核**；
- 真实 Cluster 提交/排队/结果收集（现有 Cluster 网关）——本机无 Cluster 访问；
- `existing/build × local/cluster` 四组合在真实环境纵向执行；
- 两个认证 owner 并发、跨 owner 读取/下载/重试拒绝（认证开启后）；
- 250+ MF4 批量与 result.ini 大批量完整性；
- 断网/服务重启/Connector 重启/结果目录晚到等故障注入。

---

## 5. 风险分级（Task A 视角）

| 级别 | 风险 | 说明 | 责任任务 |
|---|---|---|---|
| P0 | — | 本次审计未发现会直接导致“结果丢失/重复执行/数据越权”且已确认的 P0 缺陷；但真实多租户认证未验收，若直接对外宣称“支持多用户”即构成 P0 风险 | Task C |
| P1 | “只重试失败输入”能力缺失 | 任务书 §6.3 明确交付项，当前无 per-input 重试 API/SDK/Web | Task H |
| P1 | partial 在 DB( failed )与 API( partial )不一致 | 用户侧一致，但 DB 直接消费者/stale 逻辑可能误读为全失败 | Task B |
| P2 | Web 重试按钮条件与 API `available_actions` 口径不一 | app.js:1232-1234 vs api_v1.py:3469-3478 | Task F |
| P2 | SDK `wait` 默认 600s 易被误解为固定总时长 | 本地轮询边界，非服务端；需文档/示例澄清 | Task F/L |
| P3 | Web 旧字段回退 / 旧阶段名映射 / 筛选缺 blocked | 显示层陈旧项，非功能 | Task F |

---

## 6. 未验收项与后续动作

1. 真实部署验收清单（§4.3）全部未在本机完成，需按任务书 §12 在目标 Linux `10.190.171.44` + 真实 Windows + 真实 Cluster 上执行，并留存 Job ID / Manifest / checksum。
2. `partial` 分层表达需 Task B 复核 stale/reclaim 不会因 DB `failed` 误清理成功结果。
3. “只重试失败输入”能力需 Task H 交付后回填本矩阵 §3.2 行 12 的标记。
4. 认证启用后的跨 owner 隔离测试需 Task C 完成后回填。
5. 建议后续在 `docs/handoffs/` 追加最终交付 handoff，按任务书 §13 格式给出 `passed/failed/blocked/not tested` 完整矩阵。

---

*本审计未修改任何源码，未提交任何 commit。所有 file:line 基于审计时 HEAD `20ba6b7`。测试在临时安装 pytest 的 `.venv` 中执行（267 passed / 0 failed）。*
