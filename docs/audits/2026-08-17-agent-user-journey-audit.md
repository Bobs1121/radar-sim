# Task O：Agent 安装、恢复与长期使用验收

日期：2026-08-17  
审计对象：`radar-sim` Windows Agent 从「全新 Windows 用户」到「长期复用」的完整用户旅程  
审计方式：静态代码审计（`file:line`）+ 定向 pytest 回归；**本机为开发机，非全新 Windows 用户，真实「从零开始」的干净机验收标记为「需要真实 Windows 验收」**。  
审计结论：**用户旅程的每一步在代码层面均有明确实现与守卫；未发现「重复注册 / 双 Agent / 身份漂移」的代码路径；发现 1 个 P2 级配置损坏恢复缺口（与 Task E 相同）和 1 个多会话单实例边界。**

---

## 1. 结论先行

从「全新 Windows 用户」开始的关键环节，代码实现完整且带服务端二次确认：

1. 下载入口 → 安装 → 依赖检查 → 稳定 owner → 绑定 → 自动启动，每一步都有 `file:line` 证据（见 §2）。
2. 单实例、watchdog、recovery copy、旧进程树终止、`.pyc` 清理、实际 import 的 contract 版本，均已实现（见 §3）。
3. Web 与 SDK 提交任务**复用同一个已安装 Agent，不会重复注册**：Agent 只在进程启动时注册一次（`cli/agent.py` 274-304），此后以同一 `agent_id` 轮询 `claim_next_task`；任务按 `required_agent_id`/`assigned_agent_id` 精确归属（`core/control_service.py` 1839+、`api_v1.py` 3714-3734），SDK 与 Web 使用同一稳定 owner（`core/user.py` 57-87），因此天然复用同一 Connector（见 §4）。
4. `agent_id + owner + device + contract` 证据链完整（见 §5）。

**P2 缺口（需真实 Windows 验收时补证）：**

- 配置损坏（`install.json` 存在但 JSON 非法）不会自动从 `install.backup.json` 恢复——与任务书 §7.2 要求有偏差（`start_windows.ps1:19-26` 只处理「文件缺失」）。
- 单实例互斥体为 session 级（`Local\...`），快速用户切换/多 RDP 会话同用户登录时存在理论上的双 supervisor 竞态（`start_windows.ps1` 132-139），常规单登录用户不受影响。

---

## 2. 用户旅程逐步证据

| 步骤 | 用户看到 / 发生什么 | 代码证据 | 状态 |
|---|---|---|---|
| 1. 获取连接入口 | Web 下载 `RadarSim-连接本机.cmd`；SDK `download_windows_connector_for_run()` 下载同一入口 | `core/api_v1_fastapi.py` 457-492（connect.cmd）；`radar_sim_sdk/client.py` 152-168（SDK 下载 unified launcher） | I+T |
| 2. 双击运行 | `.cmd` 从服务端拉取 `install.ps1?mode=unified` 并执行（重试 5 次） | `scripts/connect_windows.cmd.in`（`foreach($i in 1..5)`） | I |
| 3. 输入稳定 owner | 首次输入 NTID，规范化为 `user-<小写>`；SDK 默认用 OS 登录名同格式 | `core/user.py` 57-87（`stable_user_identity`/`connector_owner_identity`）；`core/api_v1_fastapi.py` 298-315（owner()） | I+T（`test_identity_unification.py` 112-158） |
| 4. 健康/地址检查 | 先连 `GET /api/v1/health`，`authentication_required` 则阻断；服务地址写入 `NO_PROXY` | `scripts/install_windows_connector.ps1.in` 169-175、162-167 | I |
| 5. 依赖检查 | 无 Python 3.10+ → winget 静默装 3.12 或提示软件中心；建 `.venv --system-site-packages`，复用已有包；可选依赖（PyYAML/httpx/pydantic）缺失不阻断连接 | `install_windows_connector.ps1.in` 177-191；`bootstrap.ps1` 196-369 | I+T（`test_release_deployment.py` 79-92、160-163） |
| 6. 下载并校验组件包 | 校验 `X-Content-SHA256`（格式 + 值），再解压并确认 `bootstrap.ps1` 存在 | `install_windows_connector.ps1.in` 194-213；服务端 `api_v1_fastapi.py` 494-521；打包 `build_windows_connector_bundle.py` 65-90 | I+T |
| 7. 绑定与身份持久化 | 写 `install.json`（version=2, agent_id, owner, server_url, mode…）+ `credentials.json` + recovery copy；`icacls` 限当前用户 | `bootstrap.ps1` 477-512 | I+T |
| 8. VS / 前置检查 | VS 只检测并提示（不自动安装）；light 模式 capability 校验 | `bootstrap.ps1` 519-536、600-626 | I+T |
| 9. 注册自启 + watchdog | 计划任务 `AtLogOn`（`wscript.exe + run_hidden.vbs` 隐藏启动）+ 每 2 分钟 watchdog；被策略阻止则退回启动目录 | `bootstrap.ps1` 538-598；`run_hidden.vbs`；`docs/windows-connector-hidden-console-fix.md` | I+T（静态断言） |
| 10. 服务端确认「本机在线」 | 注册探测 → 30s 内轮询 `windows-connector/status?agent_id=` 直到 `available && contract_current` | `bootstrap.ps1` 649-683、744-773 | I+T |
| 11. 启动 Agent | supervisor 持互斥体，清孤儿 Agent，写 `connector.pid`，循环拉起 `rsim.py agent` | `start_windows.ps1` 132-200；`cli/agent.py` 237-364 | I |
| 12. 长期复用 / 断线重连 / 崩溃自愈 | 心跳 10s、poll 退避重连、子进程 5s 重启、watchdog 2min 兜底 | `cli/agent.py` 313-354、`start_windows.ps1` 172-200、`watch_windows_connector.ps1` 83-110 | I, R 真实 |

---

## 3. 单实例 / watchdog / recovery / 旧进程树 / .pyc / 实际 import 版本

### 3.1 单实例

- **supervisor 互斥体**：`start_windows.ps1` 132-139 用 `Local\RadarSimConnector-<用户SID>` 命名互斥体，`$created=false` 时直接 `return`「本机已经连接」。
- **孤儿 Agent 清理**：获取互斥体后，`start_windows.ps1` 140-161 全机扫描命令行含**本安装 `rsim.py`** 且 `agent` 词的进程并 `Stop-Process`，再启动新 Agent——避免「Task Scheduler 误判 supervisor 退出 → 新 supervisor 与孤儿 Agent 同 ID 竞争」。
- **watchdog 二次确认**：`watch_windows_connector.ps1` 65-81 `Test-ConnectorSupervisor` 用 `Win32_Process` 按「命令行为本安装 `start_windows.ps1` + `-supervise`」判定 supervisor 存在；存在则 `exit 0` 不重复拉起。

### 3.2 watchdog 与 recovery copy

- `watch_windows_connector.ps1` 43-63 `Repair-ConnectorControlFiles`：`install.json` 缺失时从 `data/install.backup.json` 恢复；pid 元数据缺失/陈旧时从真实 supervisor 进程重建。
- `start_windows.ps1` 19-34：启动时同样做 config ↔ recovery 双向补齐。
- backup 由 `bootstrap.ps1` 495-504 `Save-InstallConfig` 原子写（`.tmp` + `Move-Item`）双写。

### 3.3 旧进程树终止（防双 Agent 同 ID）

- `install_windows_connector.ps1.in` 84-100 `Stop-ProcessTreeIfPresent`：`taskkill /PID /T /F`（父/子树）。
- `install_windows_connector.ps1.in` 102-146 `Stop-PreviousConnector`：停旧计划任务、按 pid 杀、再按「exact AppRoot + start_windows.ps1 / rsim.py agent」全机兜底杀，且**只杀本用户本安装**。
- `bootstrap.ps1` 114-139 `Stop-ConnectorProcessTree`：先杀最深子进程再杀父进程，等所有进程退出后才启动新实例。

### 3.4 `.pyc` 与「实际 import 版本」

- `install_windows_connector.ps1.in` 148-158 `Reset-InstalledApplication`：保留 `.venv`、删除其余全部源码（防止旧 `.pyc` 残留）。
- `bootstrap.ps1` 153-167 `Clear-ConnectorPythonCache`：删 `__pycache__` 与 `*.pyc`。
- `bootstrap.ps1` 169-177 + 628-641：用 venv python **实际执行** `from core.agent_policy import WINDOWS_CONNECTOR_CONTRACT_VERSION` 打印，与源码正则比对，不一致即 Fail「Old Python cache was not replaced」。`core/agent_policy.py:96` 定义当前版本 `15`。

---

## 4. Web 与 SDK 提交复用同一 Agent（不重复注册）

证据链：

1. **Agent 只注册一次**：`cli/agent.py` 274-304 的 `while True` 循环中，`register_agent` 只在启动时成功一次即 `break`，之后不再调用注册，只 `poll(agent_id)`（313-354）。
2. **agent_id 稳定**：由 `install.json` 持久化（`bootstrap.ps1` 411-424），Web/SDK 换浏览器、多次提交都不改 ID。
3. **owner 一致**：Web 输入 NTID、SDK 默认 OS 登录名，都落到同一 `user-<小写>`；Connector 用 `RSIM_USER`+`RSIM_OWNER_BOUND=1` 保持 exact owner（`core/user.py` 74-87）。服务端 `api_v1.py` 298-315 无认证时按 `X-Rsim-User` 归一到同一 owner DB。
4. **任务按 exact agent 归属**：`claim_next_task(agent_id)`（`core/control_service.py` 1839+）只返回当前 Agent 的 `current_task_id` 或 `assigned_agent_id` 匹配的任务；调度器 `_matching_windows_agent`（`api_v1.py` 3714-3734）按「workspace binding 的 project+binding_id」选中同一 Agent。`simulation.run_config.v2` 任务在 claim 时还要 `windows_connector_contract_is_current` + owner 匹配（`control_service.py` can_resume）。
5. **不会重复注册**：`register_agent` 服务端是 UPSERT（`control_service.py` 700-781），同一 `agent_id` 反复注册只是覆盖同一行；`_execution_capabilities_internal`（`api_v1.py` 1375-1427）按 hostname 折叠，旧注册不会让「一台电脑」变成「两台在线」。

因此：Web 任务与 SDK 任务共享同一个已安装、已注册、正在轮询的 Agent 进程，无第二套注册逻辑。

---

## 5. `agent_id + owner + device + contract` 证据链

| 元素 | 来源 | 证据 |
|---|---|---|
| stable agent_id | 安装时写入 `install.json`；丢失时按 `agent-<USERNAME>-<COMPUTERNAME>-sha256(owner)[:12]` 确定性重建 | `bootstrap.ps1` 411-424 |
| owner | `RSIM_USER`（安装器写入）+ `RSIM_OWNER_BOUND=1`；Connector 每次请求带 `X-Rsim-User` | `start_windows.ps1` 47-48；`core/user.py` 74-87；`test_identity_unification.py` 136-158 |
| exact device | 注册 metadata 带 hostname + node_kind；服务端按 hostname 折叠防冒充 | `cli/agent.py` 252-291；`api_v1.py` 1375-1427 |
| contract | 注册 metadata `connector_contract_version=15`；服务端 `windows_connector_contract_is_current` 判 `>=15` | `cli/agent.py` 286；`core/agent_policy.py` 341-351 |
| 在线判定 | `windows_connector_status`：exact agent_id + owner 匹配 + contract current + 心跳≤120s + status≠offline | `api_v1.py` 2335-2380 |

> 结论：Web/SDK 展示的「在线」不是「同 owner 的 Agent 数量聚合」，而是 **exact agent_id 这台机器**的实时状态（`api_v1.py` 2368-2373）。同 owner 的另一台电脑不能互相冒充（注册时 `connector_owner_mismatch` 409，`api_v1.py` 2299-2318、`control_service.py` 731-742）。

---

## 6. 测试证据（本机运行）

本机为开发机；真实「全新 Windows 用户从零安装、升级、断网、重启」需在干净机器验收。

| 测试文件 | 覆盖 | 结果 |
|---|---|---|
| `tests/test_agent_binding_cli.py` | agent-binding CLI 全子命令 | 通过 |
| `tests/test_control_agent.py` | Agent 控制流、重试、owner/device 绑定 | 通过 |
| `tests/test_agent_bindings.py` | workspace binding store、binding_id、路径校验 | 通过 |
| `tests/test_identity_unification.py` | 稳定 owner、legacy owner 迁移、Connector 请求 owner 一致 | 通过 |
| `tests/test_agent_cli_policy.py` / `test_agent_policy.py` | mode→capability→node_kind 策略 | 通过 |
| `tests/test_api_v1_fastapi.py` | /api/v1 端点 + windows-connector 路由 | 通过 |
| `tests/test_release_deployment.py` | 安装器/启动/watchdog 静态断言 + VBS 冒烟（Windows） | 12 passed |

汇总：`81 passed, 2 skipped`（前三个）+ `140 passed`（identity/policy/api）+ `12 passed`（release_deployment）。  
> `tests/test_agent_store_paths.py` 不存在；`core/agent_store_paths.py` 由 `test_agent_bindings.py` 间接覆盖。PowerShell 运行时行为（计划任务、互斥体、taskkill、icacls、VBS 隐藏启动）无自动化测试，需真实 Windows 人工验收。

---

## 7. 用户 1 页安装 / 升级 / 重连 / 卸载 / 故障排查说明（中文）

### 安装（一次性，约 1 分钟）

1. 内网打开 radar-sim Web → 点「连接这台 Windows 电脑」→ 下载 `RadarSim-连接本机.cmd`。
2. 双击运行 → 输入公司 NTID（只会问一次）→ 等待「本机已经连接」。
3. 自动完成：检查/安装 Python → 校验组件包 → 注册登录自启 + 2 分钟 watchdog。无需手动配任何路径。

### 升级（contract 更新时）

1. Web 顶部出现「更新本机组件」横幅时点击，下载新的连接程序并双击运行。
2. 安装器原地更新代码：**不丢 agent ID、owner、路径绑定，不重填 YAML**。
3. 完成后横幅自动消失（服务端通过能力轮询确认新版本上线）。

### 断网 / 重启 / Agent 崩溃

| 情况 | 行为 |
|---|---|
| 断网 | 自动退避重连；网络恢复后自动继续，无需人工。 |
| 电脑重启 | 登录 Windows 自动自启（计划任务 AtLogOn），无需重装。 |
| Agent 进程退出 | supervisor 5 秒内自动重启；再不行 watchdog 每 2 分钟兜底重启整个任务。 |
| 误删 `install.json` | 自动从 `data/install.backup.json` 恢复；仍失败则回 Web 重新连接（agent_id 不变）。 |

### 卸载

1. 打开「任务计划程序」，删除 `RadarSimConnector-<用户名>` 与 `RadarSimConnector-<用户名>-Watchdog`。
2. 删除 `%LOCALAPPDATA%\radar-sim` 文件夹即可。

### 故障排查速查

| 现象 | 处理 |
|---|---|
| 提示缺少 Python | 装 Python 3.12（公司软件中心 / python.org）后重跑。 |
| 提示「已绑定另一个账号」 | 用原 NTID 打开 Web；或管理员显式 rebind。 |
| 提示「本机组件版本过旧」 | 回 Web 一键更新后重跑下载的入口。 |
| 下载/解压被安全软件拦截 | 把 `%LOCALAPPDATA%\radar-sim` 加入杀软白名单后重跑。 |
| 可选依赖缺失 | 电脑仍可连接；build/本地仿真前会提示缺哪个包（PyYAML/httpx/pydantic）。 |
| 换电脑 | 新电脑重新连接一次（每台机器独立 agent_id）。 |
