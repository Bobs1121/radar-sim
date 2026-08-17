# radar-sim 多用户、认证与资源隔离审计（Task C）

日期：2026-08-17
范围：owner 来源（`core/user.py`）、HTTP 认证（`core/http_auth.py`、`core/api_v1_fastapi.py`）、Agent owner/device 绑定（`core/api_v1.py` register_agent、`cli/agent.py`、`core/user.py`）、Job/Transfer/Result 查询与下载授权（`core/api_v1.py`、`core/control_service.py`、`core/transfer_service.py`、`core/local_results.py`）、正式多用户上线门禁（执行任务书第 7.4 节）。
审计方式：AUDIT ONLY，未修改任何源代码，未提交任何内容。
复测命令与结果见第 5 节：`98 passed, 1 warning in 12.86s`。

## 1. 结论先行

**当前为“受信内网试用，不满足正式多租户”。**

`radar-sim` 的资源隔离层（Job / TransferPlan / Result / Agent 全部按 owner 限定作用域）是**健全**的：即使把请求中的 owner 换掉，也无法跨 owner 读取、取消、重试或下载别人的 Job/结果（返回 404/403）。但隔离的前提是“owner 正确”，而当前生产部署 `authentication_required=false`，**owner 不是认证结果，而是客户端可任意填写的 `X-Rsim-User` 请求头**。认证基础设施（Bearer `HttpTokenAuthenticator`）已实现但未启用，`serve-v1` 默认在 loopback 上以 no-auth 模式运行（`cli/server.py:346-360,643`）。

因此：

- 资源授权链路本身可信（审计第 3 节全部为代码级强制）；
- 但身份来源在 no-auth 模式下完全可伪造（审计第 2 节给出可复现代码路径）；
- 任务书 7.4 门禁多数项在**代码/单测层面**具备实现与回归，但没有任何**真实双 owner 部署验收**，且身份来源项本身未通过，故结论必须为“受信内网试用，不满足正式多租户”。

## 2. Owner 来源与 X-Rsim-User 伪造证明

### 2.1 owner 来源链路（no-auth）

1. 常量与归一化：`core/user.py:20` 定义 `USER_HEADER = "X-Rsim-User"`；`normalize_user()`（`user.py:24-39`）只做文件名安全化，**不是认证**；`current_user()`（`user.py:42-54`）优先级为 `RSIM_USER` 环境变量 > OS 登录用户 > `default`；`stable_user_identity()`（`user.py:57-71`）把 Web/SDK/Connector 统一成 `user-<小写id>` 命名空间。
2. HTTP 适配层取 owner：`core/api_v1_fastapi.py:298-315` 的 `owner(request)`：
   - 当 `authenticator is not None`：`return authenticator.authenticate_user(request.headers.get("Authorization")).owner`（Bearer 认证，`api_v1_fastapi.py:299-305`），X-Rsim-User 被完全忽略；
   - 否则（no-auth）：`raw_user = request.headers.get(USER_HEADER, "").strip()`，直接以客户端提供的 `X-Rsim-User`（或 `current_user()`）作为 owner（`api_v1_fastapi.py:306-315`）。注释原文明确：`In the trusted no-auth deployment this remains a caller-controlled grouping label, not authentication.`
3. Agent 身份同理：`agent_principal()`（`api_v1_fastapi.py:317-325`）在无 authenticator 时返回 `None`；`agent_identity()`（`api_v1_fastapi.py:327-337`）在 principal 为 None 时直接返回 `(owner(request), claimed_agent_id)`——**no-auth 模式下 Agent 的 owner 和 agent_id 都来自客户端请求体/请求头**。
4. 下载类接口：`user_or_agent_owner()`（`api_v1_fastapi.py:339-352`）在无认证时同样回退到 `owner(request)`。
5. 服务端健康面：`/api/v1/health` 返回 `authentication_required: authenticator is not None`（`api_v1_fastapi.py:420`）。

### 2.2 伪造证明（代码级）

`authentication_required=false` 时，`create_app(...)` 未注入 `authenticator`（`cli/server.py:347,564-565` 仅在 `--auth-file` 存在时注入）。此时对任意端点的身份**只取决于 HTTP 头**：

```text
GET /api/v1/jobs/{job_id}                    X-Rsim-User: user-alice   → 读取 alice 的 Job
POST /api/v1/jobs/{job_id}/cancel            X-Rsim-User: user-alice   → 取消 alice 的 Job
POST /api/v1/jobs/{job_id}/stages/{s}/retry  X-Rsim-User: user-alice   → 重试 alice 的 Stage
GET  /api/v1/jobs/{job_id}/manifest          X-Rsim-User: user-alice   → 读取 alice 的 Manifest
GET  /api/v1/jobs/{job_id}/diagnosis         X-Rsim-User: user-alice   → 读取 alice 的诊断
GET  /api/v1/results/{result_ref}/download   X-Rsim-User: user-alice   → 下载 alice 的结果 ZIP
POST /api/agents/register                    X-Rsim-User: user-alice   → 以 alice 身份注册设备
```

这些端点全部经 `owner(request)` 派生 owner 后进入第 3 节的 owner 限定查询。由于 owner 值由客户端填写且没有任何签名/服务端会话，攻击者只需把 `X-Rsim-User` 改成目标的 `user-<id>` 即可完整冒充。而 `user-<id>` 是低熵可猜测标签（典型值即 `user-<NTID/登录名>`，`user.py:57-71`），不存在“需要拿到令牌”的门槛。

同 owner 两设备防冒充：no-auth 下 `agent_identity` 放行客户端声明的任意 `agent_id`（`api_v1_fastapi.py:327-330`），因此**两台设备可以互相冒充“当前电脑”**（仅当启用 Bearer 后，`authenticate_agent` 会校验 `claimed_agent_id == principal.agent_id` 并返回 403，`api_v1_fastapi.py:331-337`）。

### 2.3 启用认证时的对应行为（代码 + 单测）

- `core/http_auth.py` 提供完整 Bearer 能力：`AuthPrincipal`（`http_auth.py:40-61`）、`HttpTokenAuthenticator`（`:70-161`）、`from_file` 校验 JSON（`:122-135`）、`authenticate_user`/`authenticate_agent`（`:137-145`）、`load_http_auth`（`:164`）、`create_http_auth_config`（`:178-256`，生成权限 0600 的凭证文件）；token 只存 SHA-256 摘要（`:289-296`）、常量时间比较（`:157`）、拒绝弱/重复/孤儿凭证（`:79-118`）。
- 认证模式单测证明伪造头无效：`tests/test_api_v1_fastapi.py:51-70` `test_bearer_auth_derives_owner_and_ignores_spoofed_user_header`——带 `Authorization: Bearer alice` + `X-Rsim-User: bob` 提交，Job 落在 alice 名下，`bob` 未被创建；无 Bearer 访问 `/api/v1/jobs` 返回 401；Bob 用 `X-Rsim-User: alice` 看 Job 得到空列表。
- Agent 伪造头被拒：`tests/test_api_v1_fastapi.py:72-128` `test_agent_bearer_auth_derives_identity_and_rejects_body_spoof`——agent_id 与 principal 不符返回 403；用户令牌不能注册 Agent（401）；Agent 令牌不能以用户身份操作。
- 角色互不串用：`tests/test_http_auth.py:57` `test_role_tokens_cannot_cross_authenticate`；owner/agent_id 只从 Bearer 派生：`tests/test_http_auth.py:32`。

## 3. 资源隔离（owner 限定）代码路径与跨 owner 判定

以下每条均为代码级强制（“缺省 owner 即 404/403”），并附对应测试证据。

### 3.1 Job 查询 / 取消 / 重试 / Manifest / 事件 / 诊断 / 下载引用

- 统一入口 `core/api_v1.py:2933-2952` `_get_owned_job(owner, job_id)`：先取 Job，再比较 `job_owner != owner`，不一致返回 404 `not_found`（**不暴露“属于别人”**）。所有 Job 端点都走这里：`get_job`（`:1668`）、`cancel_job`（`:1708`）、`retry_stage`（`:1714`）、`manifest`（`:1729`）、`get_job_transfer_status`（`:1908`）、`diagnosis`（`:2042`）、`events`（`:2227`，见 `_get_owned_job` 调用 `:2236`）。
- Job 列表按 owner 过滤：`core/api_v1.py:1672-1707` `list_jobs` → `control.list_jobs(owner=owner, job_type_prefix="simulation.")`；`core/control_service.py:3144+` 在 SQL 中 `WHERE owner=?`，注释明确“shared ControlService database cannot leak another user's jobs”。
- 幂等键按 owner 隔离：`core/control_service.py:1817-1838` `get_job_by_idempotency(owner, key)` 以 `WHERE owner=? AND idempotency_key=?` 查询。
- 测试：`tests/test_api_v1_service.py:1238` `test_idempotency_is_scoped_by_owner`——alice/bob 用同一 `idempotency_key="k"` 提交，得到**不同 Job id**；`:328` `test_v1_task_center_lists_only_owner_v1_jobs_with_progress_and_filter`；`:1587` `test_v1_rejects_provider_snapshot_for_different_owner`（`source_resolution_owner_mismatch` 409）。

**跨 owner 判定（Job）**：owner B 猜中 owner A 的 Job id 请求 `GET /api/v1/jobs/{id}`、`/cancel`、`/retry`、`/manifest`、`/diagnosis`、`/events`、`/transfers`，一律命中 `_get_owned_job` 的 owner 校验 → 404。**授权在 service 层强制，不依赖客户端自觉。**

### 3.2 TransferPlan

- 读取/进度/清单/取消统一经 `_owned_plan`：`core/transfer_service.py:679-689`——`plan.owner != owner` 或 `owner_scope` 不符即抛 `transfer_owner_mismatch`（403）。
- API 层透传 owner：`core/api_v1.py:1901-1906` `get_transfer_plan` → `transfer.get_plan(transfer_id, owner=...)`（`transfer_service.py:626`）；`report_transfer_progress`/`receive_transfer_manifest`/`cancel_transfer` 同样带 owner（`transfer_service.py:565,583,619`）。
- Transfer 目标根按 owner/job/transfer 隔离：`transfer_service.py:545-556` `owner_scope = generate_owner_scope(owner, job_id)` + `build_isolated_relative_root(...)`。

### 3.3 Result 查询与下载

- 目录表按 `(owner, result_ref)` 唯一：`core/local_results.py:162-173`；`get(result_ref, owner=...)`（`:338-346`）→ `_row`（`:412-426`）执行 `SELECT ... WHERE result_ref=? AND owner=?`，非本 owner 返回 `result is unavailable`（404）。
- `resolve_archive`（`:363-370`）同样带 owner，并对归档路径做 `_ensure_contained` 边界校验。
- API 下载：`core/api_v1_fastapi.py:1001-1010` `download_result` → `service.get_result(owner, result_ref)` + `service.result_archive(owner, result_ref)`，两层都按 owner 强制（`core/api_v1.py:2260-2266, 2568-2575`）。
- result_ref 为 `result:sha256:<64hex>` 内容寻址（`_RESULT_REF_RE` 校验，`local_results.py:412-415`），本身难以枚举。
- 测试：`tests/test_api_v1_fastapi.py:338-358`（no-auth 下 `config-assets` 由 alice 上传，bob 读取返回 404）；SDK 下载 checksum 校验测试见 `tests/test_sdk.py`（`test_sdk.py:656,687,735,770,786`）。

**跨 owner 判定（Result）**：owner B 带 `result:sha256:...` 下载 → `_row` 查不到该 owner 的行 → 404；猜测/枚举无法命中。

### 3.4 Agent owner/device 绑定

- `core/api_v1.py:2267-2327` `register_agent`：对 `windows_agent`/`windows_full` 节点**强制覆盖** `trusted_metadata["user"] = owner`（`:2276-2278`），不使用客户端 metadata 里的 owner；已注册同 agent_id 且 owner 不同 → `connector_owner_mismatch` 409（`:2290-2317`）；仅允许一次性 legacy 迁移（`_connector_owner_transition_allowed`，`:85-105`）。
- 设备身份：agent_id 由安装器持久化（`scripts/bootstrap.ps1` 写 `install.json`，见 `docs/audits/2026-08-17-agent-user-journey-audit.md:36`）；`cli/agent.py:239-240` 用 `connector_owner_identity()`（`core/user.py:74-87`，取 `RSIM_USER` 环境变量，服务端生成安装器写入）派生 owner。
- 测试：`tests/test_api_v1_service.py:106` `test_api_registration_stamps_trusted_windows_owner`；`tests/test_api_v1_fastapi.py:394-440` `test_connector_agent_id_cannot_be_silently_rebound_to_another_owner`。

## 4. 形式化多用户上线门禁（任务书 7.4 逐项）

| 门禁项（任务书 7.4） | 现状 | 判定 | 证据 |
|---|---|---|---|
| Web 与 SDK 的 owner 都来自同一个已验证认证主体 | 认证未启用；owner 来自可伪造的 `X-Rsim-User`/SDK `user=` | **未通过** | `api_v1_fastapi.py:298-315`；`cli/server.py:346-360,643`（生产 loopback no-auth） |
| 伪造 `X-Rsim-User`、修改 SDK `user`、猜测 Job/Result/Transfer ID 无法跨 owner 读写 | 资源层 owner 校验健全（404/403）；但 no-auth 下身份本身可伪造；认证模式单测已证明伪造头无效 | **需要真实部署验收**（no-auth 下实质**不满足**） | 第 3 节全链条；`test_api_v1_fastapi.py:51-70,72-128` |
| Agent 注册绑定 owner、稳定 Agent ID、设备/安装实例与服务地址 | 代码实现：owner 强制覆盖、owner transition 校验、agent_id 持久化、contract 校验 | **通过（代码级）**，真实验收仍需 | `api_v1.py:2267-2327,85-105`；`cli/agent.py:239-240` |
| 同 owner 两台设备不能互相冒充“当前电脑” | no-auth 下 agent_id 由客户端声明，**可冒充**；启用 Bearer 后 `agent_identity` 校验 agent_id 返回 403 | **未通过**（当前部署）；代码已支持 | `api_v1_fastapi.py:327-337`；`test_api_v1_fastapi.py:72-128` |
| 两个 owner 同时提交相同输入，代码/数据/Bundle/临时目录/结果不串用 | 每用户独立 SQLite（`user.py:90-98`）、owner 隔离 transfer root（`transfer_service.py:545-556`）、owner 隔离结果目录（`local_results.py:208-209`） | **通过（代码/单测）**，真实验收仍需 | `test_api_v1_service.py:1238`；`test_identity_unification.py` |
| 审计日志能回答谁在何时提交/取消/重试/下载了什么，不泄露文件正文 | 存在结构化 Job 事件（`api_v1.py:2227+`），但**无“下载审计”专用记录与验收** | **需要真实部署验收** | `events` 实现；无下载审计单测 |
| 认证、owner、授权和结果下载的真实集成测试通过并有失败回归 | 认证模式单测齐备（`test_http_auth.py`、`test_api_v1_fastapi.py`、`test_api_v1_service.py`、`test_identity_unification.py`，合计 98 passed）；但**无真实双 owner 部署测试** | **未通过**（缺真实部署） | 见第 5 节 |

门禁汇总：**0 项完全满足“正式多租户”**。代码层具备认证能力且资源授权健全，但身份来源、双设备防冒充、真实双 owner 集成验收三项未通过/未验收。按任务书 7.4 语义，只能称为“受信内网单用户/测试部署”。

## 5. 跨 owner 拒绝测试报告（本次实跑）

命令（一次性）：

```text
.venv/Scripts/python.exe -m pytest tests/test_http_auth.py tests/test_user.py tests/test_identity_unification.py tests/test_api_v1_service.py -q
```

结果：**`98 passed, 1 warning in 12.86s`**（1 个 warning 为 starlette/httpx 弃用提示，非失败）。

覆盖的跨 owner / 认证关键用例（全部通过）：

| 测试 | 位置 | 验证点 |
|---|---|---|
| `test_bearer_auth_derives_owner_and_ignores_spoofed_user_header` | `test_api_v1_fastapi.py:51` | 认证模式下 `X-Rsim-User` 伪造头无效、无 Bearer 401、Bob 看不到 Alice Job |
| `test_agent_bearer_auth_derives_identity_and_rejects_body_spoof` | `test_api_v1_fastapi.py:72` | Agent agent_id 冒用 403、用户/Agent 令牌角色隔离 |
| `test_agent_token_downloads_only_owners_config_asset` | `test_api_v1_fastapi.py:128` | Agent 只能下载自己 owner 的资源 |
| `test_idempotency_is_scoped_by_owner` | `test_api_v1_service.py:1238` | alice/bob 同幂等键得到不同 Job |
| `test_v1_task_center_lists_only_owner_v1_jobs_...` | `test_api_v1_service.py:328` | 任务中心只列本 owner Job |
| `test_v1_rejects_provider_snapshot_for_different_owner` | `test_api_v1_service.py:1587` | 跨 owner source 快照 409 |
| `test_api_registration_stamps_trusted_windows_owner` | `test_api_v1_service.py:106` | Agent 注册强制 owner |
| `test_connector_agent_id_cannot_be_silently_rebound_to_another_owner` | `test_api_v1_fastapi.py:394` | agent_id 不可静默易主 |
| `test_user_and_agent_identity_are_derived_only_from_bearer_token` / `test_role_tokens_cannot_cross_authenticate` | `test_http_auth.py:32,57` | 身份只从 Bearer 派生、角色互不串用 |
| `test_user.py`（8 个）、`test_identity_unification.py`（10 个） | — | owner 归一化、DB 路径隔离、Web/SDK/Connector 同 owner 命名空间 |

**说明**：以上测试通过的是“给定 owner 时资源授权与认证模式的拒绝逻辑”；它们**不构成**“no-auth 生产部署下两 owner 真实验收”。由于本机无法起真实双 owner live server，双 owner 并发、两台设备、真实验证码下载等标记为“需要真实部署验收”。

## 6. 部署配置：如何启用认证、回滚与安全验收项

### 6.1 启用路径（已实现，仅需部署）

- `serve-v1` 认证开关（`cli/server.py:168-190`，读取逻辑 `:342-360`）：
  - `--auth-file <versioned JSON Bearer 凭证文件>`：注入 `load_http_auth`（`cli/server.py:350-351`）→ `authenticator` 非空 → `create_app(authenticator=...)`（`cli/server.py:564-565`）。
  - `--insecure-no-auth`：仅在开发时显式允许非 loopback 无认证绑定；非 loopback 且无 `--auth-file` 且未加该 flag 时**直接拒绝启动**（`cli/server.py:355-360`）。
  - 默认 `--host 127.0.0.1`（loopback）+ 无 auth-file = no-auth 模式（当前生产状态）。
- 凭证文件格式与生成：`core/http_auth.py:178-256` `create_http_auth_config(users, agents)` 生成 `{"version":1, "users":{owner:token}, "agents":{agent_id:{owner, token}}}`，0600 权限；加载端 `load_http_auth`（`:164`）。token 只存 SHA-256 摘要（`:289-296`）。
- 现有生产候选流程（`docs/handoffs/2026-08-11-multiuser-connection-audit.md:154,188`）：将 `http-auth.json` 挂载到 `serve-v1` 即启用；`/health` 将返回 `authentication_required: true`（`api_v1_fastapi.py:420`）。
- **注意**：启用认证后，一键安装端点（`/windows-connector/install.ps1`、`connect.cmd`、`package.zip`）会返回 `connector_pairing_required` 409（`api_v1_fastapi.py:430-435,462-467,496-500`），需要先部署短时效 pairing 交换；Agent 需通过 `RSIM_AGENT_TOKEN`/`RSIM_API_TOKEN`（`cli/agent.py:207,212`）携带 Bearer。

### 6.2 回滚方案

- 代码/配置回滚：移除 `serve-v1` 的 `--auth-file`（或换回旧 systemd unit），服务回到 no-auth loopback 模式；`core/http_auth.py` 凭证文件可删除（鉴权与业务数据分离，DB 不依赖该文件）。
- 数据层无需迁移：owner 名在两种模式同命名空间（`user-<id>`），启/停认证不影响已有 Job/Result 的 owner 字段；Bearer 启用的 owner 必须与现有 `user-<id>` 一致（`http_auth.py` 的 `_validate_identity` 允许 `user-` 前缀），否则旧数据查询不到。
- 回滚验收：`/health.authentication_required` 回到 `false`；原 `user-<id>` 仍能读到历史 Job/结果。

### 6.3 安全验收项（启用 Bearer/SSO 后必须执行）

1. 两个认证 owner 各起一套 Web/SDK，双开真实提交，验证 Job/Result/Transfer 完全隔离；
2. 用伪造 `X-Rsim-User: user-<对方>` + 对方 Bearer 场景下验证被忽略（已有单测，需 live server 复现）；
3. 两台物理设备以同 owner 注册，验证不能互相 poll/claim 对方的 Windows 任务（`agent_identity` 403）；
4. 猜测对方 Job id / Transfer id / `result:sha256:` 引用，验证 404/403 且无信息泄露；
5. 结果下载做 checksum 交叉校验（SDK `download_job_result`）；
6. 下载/取消/重试审计日志能定位到具体 owner+资源，且响应不泄露文件正文/本地路径；
7. 失败回归：错误 Bearer、过期 token、孤儿/弱凭证文件拒绝加载（`test_http_auth.py:79,86,119` 已有覆盖）。

## 7. 场景矩阵（双 owner / 双设备 / 同 workspace / 猜 id / 跨 owner 下载）

| 场景 | 期望（任务书 3.3） | 现状 | 判定 |
|---|---|---|---|
| 两 owner 不同 workspace 并行 | 独立构建槽位可并行 | 每用户独立 DB、owner 隔离资源；无真实并发测试 | 需要真实部署验收 |
| 两 Job 同 workspace/output root | 编译串行、不互相 clean | 归属 Task D（build lock），本审计确认 owner 层不会掩盖物理锁（任务书 5.1 末条） | 需要真实部署验收（Task D） |
| 同 owner 两台设备互不冒充 | 不能冒充“当前电脑” | no-auth 可冒充；Bearer 模式 403 | **未通过**（当前部署） |
| 猜另一个 owner 的 Job id | 无法读取/操作 | `_get_owned_job` 404 | 通过（代码级，`api_v1.py:2933-2952`） |
| 猜另一个 owner 的 Transfer id | 无法读取/操作 | `_owned_plan` 403 | 通过（代码级，`transfer_service.py:679-689`） |
| 猜另一个 owner 的 Result ref 并下载 | 无法下载 | `_row` 404 | 通过（代码级，`local_results.py:412-426`） |
| 跨 owner 结果下载（合法 ref） | 拒绝 | `download_result` 两层 owner 校验 → 404 | 通过（代码级） |
| 双 owner 真实集成测试 | 通过且有失败回归 | 仅单测；无 live 双 owner | **需要真实部署验收** |

## 8. 风险分级

- **P0（阻断正式多租户）**：身份来源未认证（no-auth 下 `X-Rsim-User` 可伪造）；同 owner 多设备可互相冒充。对应任务书 10 风险 1、2。
- **P1**：下载/取消/重试审计日志缺失专用验收；认证启用的 pairing 流程未部署。
- **P2**：`create_http_auth_config` 无 CLI 命令/文档化生成入口（仅 Python API 与测试使用）。

## 9. 未完成事项（真实部署验收，本机不可执行）

1. 启用 Bearer（`--auth-file`）后的 live 双 owner Web/SDK 全链路；
2. 两台真实 Windows 设备同 owner 的防冒充验证；
3. 真实结果下载交叉访问与 checksum 验收；
4. 审计日志（提交/取消/重试/下载）的验收与回归；
5. 认证启用后的 Connector pairing 流程。

## 10. 最终结论

**受信内网试用，不满足正式多租户。** 资源隔离层健全、认证基础设施已实现并具备单测回归，但身份来源在 `authentication_required=false` 下可伪造，门禁 7.4 未全部通过，且缺真实双 owner 验收。在启用 `--auth-file`（Bearer）并从认证主体派生 owner 之前，不得对外宣称“支持多用户生产”。
