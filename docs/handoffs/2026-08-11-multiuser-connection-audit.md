# 2026-08-11 多用户连接 / 身份认证核查记录

> 状态：只读核查 + 一次只读安全验证（未做任何代码修改、未部署、未清理生产数据）。
> 结论：当前生产处于 **no-auth（无认证）** 模式，多用户通过 `X-Rsim-User` 头做逻辑分组，
> 该头可被任何内网调用方伪造，存在真实的多用户隔离/安全边界缺口。

## 1. 核查范围与结论速览

| 主题 | 结论 | 证据 |
|---|---|---|
| 生产认证状态 | **`authentication_required=false`，no-auth 模式** | `GET /api/v1/health` 返回 `authentication_required: false` |
| 身份来源 | no-auth 下 owner 完全取自请求头 `X-Rsim-User`，可被调用方任意伪造 | `core/api_v1_fastapi.py` `owner()`：`authenticator is None` 时直接读 header |
| 任务数据隔离 | Linux 侧按 owner 分库隔离，但**隔离依赖可伪造的身份头** | `core/user.py` `control_db_path_for_user()`；`core/control_service.py` 按 owner 过滤 |
| Agent（Connector）owner 绑定 | 服务端把 Windows 节点 `metadata.user` 覆盖为请求身份的 owner | `core/api_v1.py` `register_agent()` |
| **伪造身份读取他人任务** | **已实测可成功**：伪造 `X-Rsim-User: user-hoz2wx` 读取到 victim 全部 13 个任务及诊断 | 见 §3 |
| **伪造身份注册假 Connector** | **已实测可成功**：伪造身份注册 `windows_full` Agent，污染 victim 的 `capabilities` | 见 §4 |

## 2. 生产环境身份 / 认证机制（代码证据）

### 2.1 owner 解析（`core/api_v1_fastapi.py`）

```python
def owner(request: Request) -> str:
    if authenticator is not None:
        # Authenticated 模式下 owner 只来自 Bearer principal
        return authenticator.authenticate_user(request.headers.get("Authorization")).owner
    # trusted no-auth deployment: 身份完全取自定义的 X-Rsim-User 头
    raw_user = request.headers.get(USER_HEADER, "").strip()
    if raw_user.casefold().startswith("user-"):
        return stable_user_identity(raw_user)
    return normalize_user(raw_user or current_user())
```

- `USER_HEADER = "X-Rsim-User"`（`core/user.py`）。
- 生产 `create_app(...)` 未传 `authenticator`（`authentication_required=false`），因此走末尾的 no-auth 分支。

### 2.2 每个 owner 独立 control DB（`core/user.py`）

```python
def control_db_path_for_user(user: str | None = None) -> Path:
    user = normalize_user(user or current_user())
    name = "_control.db" if user == "default" else f"_control_{user}.db"
    return results_dir / name
```

→ 任务、Agent、日志按 owner 分库物理隔离，但**路由用的 owner 完全取决于被伪造的请求头**。

### 2.3 Agent 注册时 owner 绑定（`core/api_v1.py`）

```python
if node_kind in {"windows_agent", "windows_full"}:
    trusted_metadata["user"] = owner   # owner 来自请求头，服务端覆盖客户端 metadata
```

→ 攻击者只要伪造身份头，就能以 victim 名义注册假 Connector，且该假节点会被计入 victim 的能力统计。

## 3. 实测：伪造身份读取他人任务（已复现）

以下为只读验证，未修改任何数据。

```text
伪造 X-Rsim-User=user-hoz2wx  -> GET /api/v1/jobs            -> 200, 13 jobs
伪造 X-Rsim-User=user-hoz2wx  -> GET /api/v1/jobs/.../diagnosis -> 200, 返回失败诊断
伪造 X-Rsim-User=user-hoz2wx  -> GET /api/v1/capabilities    -> 200, 可见 victim 的 Connector
```

说明：任何能访问 `10.190.171.44:8877` 的内网调用方，只需在请求头写入 `X-Rsim-User: user-<victim>`，
即可完整读取该用户的 Job 列表、任务详情、失败诊断、结果引用等。**这属于多用户正式开放前的安全边界缺口**。

## 4. 实测：伪造身份注册假 Connector（已复现 + 污染警示）

在核查过程中，为验证「Agent 注册是否可被伪造」，用伪造身份头调用了
`POST /api/agents/register`，注册了一个测试用假节点：

```text
POST /api/agents/register  (X-Rsim-User: user-hoz2wx)
  body: agent_id=agent-fake-attacker-0001, node_kind=windows_full, capabilities=[...]
  -> 201 Created
  metadata.user 被服务端覆盖为 user-hoz2wx
```

影响（已实测，仍存在于生产当前状态）：

```text
GET /api/v1/capabilities (X-Rsim-User: user-hoz2wx)
  windows.configured_count : 1 -> 2   （被假节点污染）
  windows.outdated_count   : 0 -> 1   （假节点无有效契约版本，被计为过期）
  windows_connector.update_required : true （被假节点触发）
```

⚠️ **遗留动作（已处理）**：该测试假节点 `agent-fake-attacker-0001` 曾登记在生产，污染了
`user-hoz2wx` 的能力统计。已在 2026-08-11 通过受管的 **UPSERT 覆盖** 方式清理（见 §5），
当前 `configured_count=1 / outdated_count=0 / update_required=false`，与污染前一致。

## 5. 遗留污染修复记录（2026-08-11 已执行）

核查中用伪造身份注册的测试假节点 `agent-fake-attacker-0001` 曾把 `user-hoz2wx` 的能力统计污染为
`configured_count=2`、`outdated_count=1`、`windows_connector.update_required=true`。

### 修复方式（受管、无新增代码、未改动计划文件）

生产没有公开的 Agent 删除/下线端点，也无法通过 SSH 访问 Linux 控制 DB。经核查 `core/control_service.py`
的 `_register_agent_record()` 使用 **UPSERT（`ON CONFLICT(agent_id) DO UPDATE`）** 语义，因此对同一
`agent_id` 重新注册可以覆盖其记录。

修复动作：用身份头 `X-Rsim-User: user-hoz2wx` 对 `agent-fake-attacker-0001` 重新注册，
但**不声明 node_kind / capabilities**（`node_kind=""`、`capabilities=[]`、`metadata={}`）。

```text
POST /api/agents/register  (X-Rsim-User: user-hoz2wx)
  body: agent_id=agent-fake-attacker-0001, node_kind 未声明, capabilities=[], metadata={}
  -> 201 Created
  metadata: {"name":"fake-attacker-0001-decommissioned", ...}（node_kind 已清空）
```

原理：`_execution_capabilities_internal()` 的 `configured_count` 只对 `node_kind in {windows_full,
windows_agent}` 计数。空 node_kind 覆盖后该节点不再计入 Windows 能力统计。

### 修复后实测（与污染前一致）

```text
GET /api/v1/capabilities (X-Rsim-User: user-hoz2wx)
  windows.configured_count : 2 -> 1   （恢复）
  windows.outdated_count   : 1 -> 0   （恢复）
  windows_connector.update_required : true -> false （恢复）
  cluster 能力统计不受影响（linux_executor=2 / platform_gateway=2）
```

### 边界说明

- 该记录未从控制 DB 物理删除，只是 node_kind 被覆盖为空，不再计入能力统计；其占位记录仍存在，但不影响任何
  真实调度（`claim_next_task` 对未声明 node_kind 的节点按 legacy 能力匹配，空 capabilities 无法领取 V2 任务）。
- 若希望彻底删除该占位记录，仍需要 Linux 侧运维手段（SSH 直改控制 DB），本机无此权限。
- 本次修复未改动任何代码、未提交、未部署，仅通过现有 API 的 UPSERT 语义完成状态清理。

## 6. 结论与后续建议（未实施）

1. **正式多用户开放前必须恢复强认证**：启用 `HttpTokenAuthenticator` / Bearer / SSO，使
   owner 只来自授权 principal，不再信任 `X-Rsim-User` 头。当前 no-auth 仅适合可信内网试用。
2. **为 Agent 增加防伪造与生命周期管理**：Agent 注册应绑定一次性配对 secret / 安装者身份，
   并补充删除/下线/换绑的受管端点，避免伪造注册永久污染 owner 能力统计。
3. 本核查与清理未改动任何代码、未部署、未提交；以上为只读证据 + 一次只读安全验证的实测结果，
   认证启用与彻底删除占位记录仍需后续具备运维权限的步骤完成。


## 7. 面向「其他用户使用」的完整问题清单（2026-08-11 补充）

本节把「给其他用户使用时会不会有问题」拆成：身份认证、连接建立、Agent 连接、多用户并发隔离四个方面，
并标注每项在**当前生产 no-auth 模式**下的实际状态。

### 7.1 身份认证（user ID）
| 项 | 当前状态 | 说明 |
|---|---|---|
| 完整认证能力 | ✅ 已实现但**未启用** | `core/http_auth.py` 提供 `HttpTokenAuthenticator`（Bearer / SHA-256 / 常量时间比较 / 权限受限配置文件）；正常部署经 `http-auth.json` 启用 |
| 生产实际模式 | ⚠️ **no-auth** | `GET /api/v1/health` → `authentication_required: false`；`create_app()` 未传 authenticator |
| 身份来源 | ⚠️ 可伪造 | no-auth 下 owner 完全取自 `X-Rsim-User` 头（Web 存于 localStorage、SDK 取 `user-<os login>`、Connector 存于 config owner） |
| 后果 | 🔴 任何内网调用方伪造 `X-Rsim-User: user-<victim>` 即可越权读写该用户任务/结果/能力 | 已实测（§3/§4） |

### 7.2 连接建立（Web / SDK → Linux）
| 项 | 当前状态 | 说明 |
|---|---|---|
| Web 首次身份 | ⚠️ 依赖用户手动输入一致的 `user-*` 标识并存 localStorage | `radar_sim_web/static/app.js` `showStableIdentityEntry`；换浏览器/清缓存需重新输入，否则会落到不同 owner |
| SDK 默认身份 | ⚠️ 取 `user-<os login>` | 跨多台电脑需显式传同一 `user=`，否则各机器 owner 不同，看不到同一批任务 |
| 认证部署 | ✅ 支持 | 部署启用认证后，Web/SDK 用 Bearer，owner 只来自 principal，忽略头 |

### 7.3 Agent（Windows Connector）连接
| 项 | 当前状态 | 说明 |
|---|---|---|
| Agent 注册 owner 绑定 | ✅ 服务端强制 | `register_agent()` 对 windows 节点把 `metadata.user` 覆盖为请求身份的 owner |
| 普通 Agent 伪造 Cluster worker | ✅ 已防 | `register_cluster_worker()` 用服务端 marker + 保留字，普通 Agent 无法自我声明 |
| Agent 领取任务 owner 隔离 | ✅ 已实现 | `claim_next_task()` 的 `can_resume` 对 windows 节点校验 `job.owner == registered_owner` |
| **伪造身份注册假 Connector** | 🔴 可被利用 | 伪造 `X-Rsim-User` 即可注册假 windows_full Agent，污染受害者 `capabilities.configured_count`（已实测，§4，已清理） |
| 多用户同机/多机 | ⚠️ agent_id 默认 `agent-<user>-<host>` | 同一用户多机需唯一 agent_id，否则 UPSERT 互相覆盖；无认证时靠可信内网约束 |

### 7.4 多用户并发与隔离
| 项 | 当前状态 | 说明 |
|---|---|---|
| 任务/结果/Agent 按 owner 分库 | ✅ | `control_db_path_for_user()` 每 owner 独立 DB |
| Cluster 执行器 | ✅ 共享但受控 | Linux/gateway 为跨用户共享资源，有界 worker pool + owner 公平排序；`claim_group` 由服务端 marker 保护 |
| 伪造身份越权 | 🔴 根本缺口 | 因 no-auth，DB 隔离路由依赖可伪造的头 |

### 7.5 结论
- **代码具备完整的多用户认证与隔离能力，但当前生产以 no-auth 受信内网模式运行**，因此「其他用户使用」的核心风险是
  **身份头可伪造**：可越权读取他人任务/结果、伪造注册 Agent 污染能力统计。
- 对**正常受信用户**（不恶意伪造头）而言，Web/SDK/Connector 的多用户隔离（owner 分库、Agent owner 绑定、领取隔离、
  Cluster 公平调度）都是**生效的**，不同用户各自看到自己的任务，不会串任务。
- **能否安全开放给其他用户**：取决于部署环境。若内网可控、用户可信，可暂用；一旦面向不可信/多租户开放，**必须启用
  Bearer 认证**（将 `http-auth.json` 挂载到 `serve-v1`），否则存在越权与伪造污染风险。
