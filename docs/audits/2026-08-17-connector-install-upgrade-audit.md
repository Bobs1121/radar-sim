# Task E：Agent 一键安装、升级、重连与单实例审计

日期：2026-08-17  
审计对象：`radar-sim` Windows Connector 安装/升级/watchdog/单实例路径  
审计方式：静态代码审计（带 `file:line` 引用）+ 定向 pytest 回归；**本机为开发机，非全新 Windows 用户，真实干净机安装/升级/重启/断网验收标记为「需要真实 Windows 验收」**。  
审计结论：**代码实现基本完备，发现 2 个 P2 级缺口（config 损坏仅能恢复「缺失」、杀毒诊断缺失）+ 1 个多会话单实例边界。**

---

## 1. 结论先行

一键安装/升级/重连/单实例链路在**代码层面**已经覆盖执行任务书 §7.2 的绝大多数检查项：

- 包下载 checksum/大小校验（`install_windows_connector.ps1.in` 202-209，服务端 `api_v1_fastapi.py` 512-521）。
- 安装目录 ACL 限制到当前用户（`bootstrap.ps1` 510-512）。
- install metadata + recovery copy 双写与原子写（`bootstrap.ps1` 495-504）。
- 升级先停旧 supervisor/watchdog、杀父/子 Python 进程树、清 `.pyc`（`install_windows_connector.ps1.in` 102-158、`bootstrap.ps1` 114-167）。
- watchdog 单实例 + 配置/pid 自愈（`watch_windows_connector.ps1` 43-110）。
- 实际 Python import 的 `WINDOWS_CONNECTOR_CONTRACT_VERSION` 与源码一致才放行（`bootstrap.ps1` 169-177 + 628-641）。
- 服务端以「exact agent_id + owner + contract + 最近心跳」判定在线，不按 owner 聚合数量冒充在线（`api_v1.py` 2335-2380）。

**缺口（P2，不阻断受信内网单用户部署，需真实 Windows 验收时补证）：**

1. **config 损坏（存在但 JSON 非法）不会自动从 recovery copy 恢复**。`start_windows.ps1:19` 与 `watch_windows_connector.ps1:44` 只在 `install.json` **不存在**（`Test-Path` 为假）时从 `data/install.backup.json` 恢复；如果 `install.json` 存在但内容损坏，`ConvertFrom-Json` 抛错、没有回退到 backup。与任务书 §7.2「配置损坏时从 recovery copy 恢复」存在偏差。
2. **缺少企业杀毒/Defender 拦截诊断**。全脚本无 `antivirus/Defender/杀毒` 字样；下载/解压/pip 被拦截时只会得到通用网络或 pip 错误，不满足 §7.2「企业杀毒拦截时返回可操作诊断」。

**边界（P2）：** 单实例互斥锁 `Local\RadarSimConnector-<用户SID>`（`start_windows.ps1` 132-139）是 **session 级命名互斥体**。同一 Windows 用户在快速用户切换 / 多 RDP 会话同时登录时，两个会话各自持有同名互斥体，理论上可并存两个 supervisor。watchdog 的 `Find-ConnectorSupervisor` 是全机扫描 `start_windows.ps1 -supervise`（`watch_windows_connector.ps1` 30-41），会降低但无法在架构上根除该竞态。常规单登录用户无此问题。

---

## 2. 审计范围与代码位置

### 2.1 安装器链路

| 文件 | 作用 |
|---|---|
| `scripts/connect_windows.cmd.in` | 用户双击入口，从服务端拉取 `install.ps1?mode=unified` 并执行（重试 5 次） |
| `scripts/install_windows_connector.ps1.in` | 主安装器：健康检查→Python→下载包→校验→替换→调 bootstrap |
| `scripts/bootstrap.ps1` | 依赖/配置/注册自启/watchdog/contract 校验 |
| `scripts/build_windows_connector_bundle.py` | 服务端打包白名单 + 确定性 sha256 manifest |
| `core/api_v1_fastapi.py` 422-528 | 对外提供 install.ps1 / connect.cmd / package.zip / status |

### 2.2 运行时与单实例

| 文件 | 作用 |
|---|---|
| `scripts/start_windows.ps1` | supervisor：单实例 mutex、孤儿 Agent 清理、5 秒重启子进程 |
| `scripts/watch_windows_connector.ps1` | 独立 watchdog：恢复 config/pid、确认 supervisor、重启任务 |
| `scripts/run_hidden.vbs` | 无窗口启动 PowerShell（隐藏控制台修复） |
| `cli/agent.py` | Agent 主循环：注册、poll 重连、owner/device 绑定迁移 |
| `core/agent_policy.py` | `WINDOWS_CONNECTOR_CONTRACT_VERSION = 15`（第 96 行）+ contract 判定 |
| `core/api_v1.py` 2267-2414 | 服务端注册、owner 绑定、windows_connector_status |
| `core/control_service.py` 700-781 | 注册 UPSERT + owner 切换临界区 |

---

## 3. 场景矩阵

标记说明：**I** = 代码已实现（附 file:line）；**T** = 有自动化测试；**R** = 需要真实 Windows 验收（本机无法做）。

| # | 场景 | 状态 | 代码证据 | 测试证据 | 说明 |
|---|---|---|---|---|---|
| 1 | 干净 Windows 用户首装 | I+T, R 真实 | `install_windows_connector.ps1.in` 169-221；`bootstrap.ps1` 196-773 | `test_release_deployment.py` 56-169 静态断言；`test_identity_unification.py` 112-158 | 从 Web 下载入口→安装→服务端确认「本机+用户+contract」 |
| 2 | 已有旧版本升级 | I, R | `install_windows_connector.ps1.in` 102-158（停旧任务/杀进程树/替换源码保 .venv）；`bootstrap.ps1` 545-560、153-167（清 .pyc） | 静态断言 `test_release_deployment.py` 127-152 | 升级不丢 owner/agent_id/binding |
| 3 | 服务地址变化 | I, R | `bootstrap.ps1` 442-451：`server_url` 不一致且无 `-ForceRebind` 则 `Fail` | 无专门测试 | 不能静默改服务器 |
| 4 | owner 变化 | I+T, R | `bootstrap.ps1` 386-408（仅迁移 legacy `web-*`/`sdk-*`，否则 Fail）；服务端 `api_v1.py` 2299-2318 返回 409 `connector_owner_mismatch` | `test_identity_unification.py` 104-110 | 正式用户不可互相抢占 |
| 5 | 进程崩溃（Agent 子进程） | I, R | `start_windows.ps1` 172-200：`while($true)` 5 秒重启；计划任务 `RestartCount 999`（`bootstrap.ps1` 564-567） | 无真实进程崩溃测试 | watchdog 另兜底 |
| 6 | 断网 / 服务不可达 | I, R | `cli/agent.py` 274-304（注册重试）、313-354（poll 退避重连）；`start_windows.ps1` 100-117（reconnect 模式）；`Invoke-WithRetry`（install 28-46） | `test_control_agent.py`（传输重试分类） | 心跳/lease 兜底 |
| 7 | 电脑重启 | I, R | 计划任务 `AtLogOn`（`bootstrap.ps1` 562-569）；登录即 `start_windows.ps1 -Supervise` | 无真实重启测试 | 需真实 Windows 验收 |
| 8 | 安装目录部分损坏 | I（缺失恢复）/ I 部分 + R | `start_windows.ps1` 19-26、`watch_windows_connector.ps1` 43-63（恢复缺失的 config/pid）；`install_windows_connector.ps1.in` 148-158（重装全量替换源码） | 静态断言 `test_release_deployment.py` 113-120 | **P2 缺口：仅覆盖「缺失」，未覆盖「损坏」** |
| 9 | 父/子 Python 进程清理 | I, R | `install_windows_connector.ps1.in` 84-100（`taskkill /T /F`）、102-146；`bootstrap.ps1` 114-139（先杀子后杀父） | 静态断言 `test_release_deployment.py` 127-129 | 防孤儿 Agent |
| 10 | 旧 supervisor 终止、避免双 Agent 同 ID | I, R | `install_windows_connector.ps1.in` 102-146；`bootstrap.ps1` 545-560；`start_windows.ps1` 132-161（mutex + 孤儿清杀） | 静态断言 `test_release_deployment.py` 108-111 | 同一安装路径才杀 |
| 11 | `.pyc` 陈旧缓存 | I+T | `bootstrap.ps1` 153-167（Clear-ConnectorPythonCache）、628-641（contract 对比） | `test_release_deployment.py` 150-153 | 升级后 import 新版本 |
| 12 | recovery metadata | I+T | `bootstrap.ps1` 495-504（双写 + 原子 Move-Item）；`start_windows.ps1` 19-34 | 静态断言 `test_release_deployment.py` 99-100、113-118 | 只有缺失才恢复 |
| 13 | identity 保留 | I+T | `bootstrap.ps1` 411-424（复用 existing.agent_id；确定性 `agent-用户名-机器名-sha256(owner)[:12]`） | `test_identity_unification.py` 136-158 | 同一 owner+机器重装 ID 不变 |
| 14 | `agent_id+owner+exact device+contract` 语义 | I+T | `api_v1.py` 2335-2380（exact agent_id + owner 匹配 + contract + 120s 心跳）；`_execution_capabilities_internal` 1375-1427（同 hostname 折叠防「一台电脑伪装两台」） | `test_agent_cli_policy.py`、`test_api_v1_fastapi.py` | 不能仅按 owner 数量判在线 |
| 15 | 包 checksum/大小 | I+T | `install_windows_connector.ps1.in` 202-209（`X-Content-SHA256`）；`api_v1_fastapi.py` 512-521；`build_windows_connector_bundle.py` 65-90 | 静态断言 `test_release_deployment.py` 136-141 | 确定性 zip + manifest |
| 16 | 安装/数据目录权限 | I, R | `bootstrap.ps1` 510-512（`icacls /inheritance:r /grant:r` 仅当前用户） | 无真实 ACL 测试 | credentials 不放 YAML |
| 17 | install metadata | I+T | `bootstrap.ps1` 477-506（version=2, mode, agent_id, owner, server_url…） | 静态断言 `test_release_deployment.py` 61-100 | |
| 18 | watchdog 单实例 | I+T | `watch_windows_connector.ps1` 65-81（确认 supervisor 命令行为 `-supervise` 才跳过）；`start_windows.ps1` 132-139（mutex） | 静态断言 `test_release_deployment.py` 113-120 | 见 §4 多会话边界 |
| 19 | 配置损坏恢复 | I（缺失）/ **P2 缺口（损坏）** | `start_windows.ps1` 19-26、`watch_windows_connector.ps1` 43-63 | 静态断言 113-118 | 损坏 JSON 不会回退 backup |
| 20 | 无 Python / 版本不匹配 | I, R | `install_windows_connector.ps1.in` 177-191（winget 静默装 3.12 / 提示软件中心）；`bootstrap.ps1` 184-191 | 静态断言 160-163 | |
| 21 | 无网络 / 代理 | I, R | `install` 162-167、`start` 51-56（`NO_PROXY` 加 server host）；`Invoke-WithRetry` | 静态断言 133-135 | 防公司代理错接管内网 IP |
| 22 | 企业杀毒拦截 | **P2 缺口** | 全脚本无诊断 | 无 | 见 §4 |
| 23 | 不装 Selena/VS/Runtime/DLL | I | `bootstrap.ps1` 519-536（VS 仅检测提示）；`install` 224 | 静态断言 73、152 | VS/引擎属于用户环境 |
| 24 | 可选依赖缺失不阻断连接 | I+T | `bootstrap.ps1` 244-369（缺失写 `optional_dependency_error`，仍连接）；`agent.py` 154-166 | `test_release_deployment.py` 79-92 | 缺包时任务前返回 `connector_dependency_missing` |
| 25 | 安装完成服务端确认上线 | I+T | `bootstrap.ps1` 649-683（注册探测）、744-773（30s 内轮询 status 确认 `available && contract_current`） | `test_release_deployment.py` 156-158 | 不以本地探测替代服务端确认 |

---

## 4. 关键发现

### 4.1 单实例与身份保留缺口（重点）

**多会话互斥边界（P2）：** `start_windows.ps1` 132-139 使用 `Local\RadarSimConnector-<用户SID>` 命名互斥体。`Local\` 前缀表示互斥体绑定到**当前终端会话命名空间**。同一用户通过快速用户切换或多 RDP 会话同时登录时，两个会话各持一个同名互斥体，理论上可并存两个 supervisor、两个 `rsim.py agent` 以同一 `agent_id` 注册。缓解：

- watchdog 的 `Find-ConnectorSupervisor`（`watch_windows_connector.ps1` 30-41）全机扫描 `start_windows.ps1 ... -supervise` 命令行的**最低 PID**，并修复 pid 元数据；
- 服务端 `control_service.register_agent`（700-781）是 UPSERT + owner 临界区，同一 agent_id 重复注册只是覆盖行、不新增行，不会出现「两台逻辑电脑」。

但**架构上未根除**：两个会话可同时在线。常规单登录用户不受影响；真实多会话验收时应把该场景列为待验证项。

**身份保留（已实现）：** 升级时 `bootstrap.ps1` 411-413 复用 `install.json` 里的 `agent_id`；即便 install.json 丢失，424 行按 `agent-<USERNAME>-<COMPUTERNAME>-sha256(owner)[:12]` 确定性重建，同一 owner+同一机器+同一用户名得到的 agent_id 与旧 ID 完全一致。配套的 owner 绑定迁移在 `core/api_v1.py` 85-105（`_connector_owner_transition_allowed`）与 `core/control_service.py` 83-100 双处守卫。

### 4.2 `agent_id + owner + exact device + contract` 语义（已实现，符合 §7.1 第 6 条）

- `windows_connector_status`（`api_v1.py` 2335-2380）只按 **exact agent_id** 查询，依次校验：node_kind 是 windows、注册 owner 匹配、`windows_connector_contract_is_current(metadata)`（`agent_policy.py` 341-351，`>=15`）、最近心跳 ≤120s、status != offline。只有全部通过才 `available=true`。
- `_execution_capabilities_internal`（`api_v1.py` 1375-1427）对**同 hostname 的 owner 行做折叠**（rank = contract_current → online → 最近心跳），防止一次重装产生的旧注册让「一台电脑被当成两台在线」。因此 Web/SDK 看到的 `windows.count` 是按物理设备折叠后的结果，不是 owner 聚合数量。
- 安装完成确认（`bootstrap.ps1` 744-773）轮询的是 **exact agent_id 的 status**，返回 `connector_owner_mismatch` / `windows_connector_update_required` 时给出明确动作。

### 4.3 `.pyc` 与「实际 import 版本」

- `install_windows_connector.ps1.in` 的 `Reset-InstalledApplication`（148-158）**保留 `.venv`、删除其余全部源码**，避免「时间戳/大小合法但内容旧」的 `.pyc` 残留。
- `bootstrap.ps1` 的 `Clear-ConnectorPythonCache`（153-167）删除 cli/core/sdk/web/platforms/plugins 的 `__pycache__` 与 `*.pyc`。
- 升级后 `Get-ConnectorRuntimeContract`（169-177）用 venv python 实际执行 `from core.agent_policy import WINDOWS_CONNECTOR_CONTRACT_VERSION` 打印，再与源码正则（633）比对，不一致即 `Fail "Old Python cache was not replaced"`（638-640）。**这是「实际 import 版本」的直接证据。**

### 4.4 配置损坏恢复缺口（P2）

`start_windows.ps1:19-23` 与 `watch_windows_connector.ps1:44-53` 的恢复条件是 `-not (Test-Path install.json) -and (Test-Path data/install.backup.json)`——只处理**文件被删除**。若 `install.json` 存在但为非法 JSON（磁盘坏块、人工改坏、半写），`start_windows.ps1:27` 的 `ConvertFrom-Json` 在 `ErrorActionPreference=Stop` 下直接抛错，**不会回退 backup**。与任务书 §7.2「配置损坏时从 recovery copy 恢复」要求有偏差。建议后续在 `ConvertFrom-Json` 失败时以 recovery copy 重建 `install.json`。

### 4.5 杀毒诊断缺口（P2）

全安装/启动/watchdog 脚本中没有对 Defender / 企业杀软（下载被隔离、解压被拦、exe 被拦、pip 被拦）的可操作诊断。`pip install` 失败（`bootstrap.ps1` 343-359）只报告「可选依赖缺失，仍可连接」；下载被拦表现为通用网络异常。建议在安装器输出中加入「若下载/解压/执行被安全软件拦截，请将 %LOCALAPPDATA%\radar-sim 加入白名单」提示。

---

## 5. 测试证据（本机运行）

本机为开发机（Windows），非全新 Windows 用户；以下为可执行回归，真实首装/升级/重启/断网需在干净机器验收。

| 测试文件 | 覆盖 | 结果 |
|---|---|---|
| `tests/test_agent_binding_cli.py` | agent-binding CLI 注册/列表/健康/删除 | 通过 |
| `tests/test_control_agent.py` | Agent 控制流、重试分类、owner/device 绑定 | 通过 |
| `tests/test_agent_bindings.py` | workspace binding store、binding_id 算法、路径校验 | 通过 |
| `tests/test_identity_unification.py` | owner 迁移、Connector 请求用同一 owner、installed 保留 exact owner | 通过 |
| `tests/test_agent_cli_policy.py` / `test_agent_policy.py` | mode→capability→node_kind 策略、light 边界 | 通过 |
| `tests/test_api_v1_fastapi.py` | /api/v1 端点（含 windows-connector/status 路由注册） | 通过 |
| `tests/test_release_deployment.py` | 安装器/watchdog/启动脚本静态断言（§56-169 全部关键行） | 12 passed |

汇总：`81 passed, 2 skipped`（agent_binding_cli + control_agent + agent_bindings）；`140 passed`（identity/policy/api_v1_fastapi）；`12 passed`（release_deployment 静态脚本校验）。

> 注意：`tests/test_agent_store_paths.py` **不存在**（任务书提到的该测试文件未在仓库中找到）；`default_agent_binding_db_path`（`core/agent_store_paths.py`）被 `core/agent_bindings.py` 引用，其行为由 `test_agent_bindings.py` 间接覆盖。
> PowerShell 运行时行为（mutex、scheduled task、icacls、taskkill、VBS）**没有自动化测试**，仅 `test_release_deployment.py` 做字符串断言 + `test_hidden_launcher_runs_powershell_and_preserves_spaced_arguments`（Windows 下运行 VBS 冒烟）。真实 Windows 验收必须人工执行。

---

## 6. 未验收项（需真实 Windows 验收，本机不可做）

1. 干净 Windows 用户从零首装（含无 Python 时 winget 静默安装）。
2. 旧版本→新版本原地升级（含 running supervisor 在升级时被停、.pyc 清理、contract 从旧版升到 15）。
3. 电脑重启后 AtLogOn 自动启动、配置从 backup 恢复。
4. 断网 → 自动重连 → 恢复后继续领取任务。
5. 杀进程（supervisor 或 Agent）→ watchdog 恢复 → 不产生双 Agent。
6. 服务地址变更 / owner 变更的 Fail/409 行为。
7. 安装目录人为删除部分文件（config、pid、源码）后的自愈。
8. 企业杀软/代理/无 Python/无 VS 的端到端诊断输出。

---

## 7. 用户 1 页安装 + 故障恢复说明（中文）

### 首次连接（一次性）

1. 在公司内网打开 radar-sim Web，点「连接这台 Windows 电脑」，下载 `RadarSim-连接本机.cmd`。
2. 双击运行，按提示输入公司 NTID（只输入一次，作为本机 owner）。
3. 安装器自动：检查/安装 Python 3.10+ → 下载并校验组件包 → 检查 VS（缺失只提示不安装）→ 注册「登录自启 + 2 分钟 watchdog」。
4. 看到「本机已经连接，返回 Web 继续」即完成。Web 顶部显示「当前这台电脑 + 当前用户 + contract 15 已连接」。

### 日常故障恢复

| 现象 | 处理 |
|---|---|
| 电脑重启后 | 无需任何操作，登录 Windows 自动重连；任务保持「等待连接/自动重连」。 |
| 断网一段时间 | 自动重试重连，无需人工；网络恢复后自动续跑。 |
| Agent 偶尔退出 | supervisor 5 秒内自动重启；再不行 watchdog 2 分钟兜底重启整个连接任务。 |
| 升级提示「更新本机组件」 | 回 Web 点「一键更新」，再双击下载的入口；不重填 YAML、不丢 agent ID 与路径绑定。 |
| 误删 `%LOCALAPPDATA%\radar-sim\install.json` | watchdog 自动从 `data\install.backup.json` 恢复；若仍失败，回 Web 重新连接一次（保留同一 agent_id）。 |
| 提示「本机已绑定另一个账号」 | 用原来注册的 NTID 打开 Web；或请管理员执行显式 rebind。 |
| 提示缺少 Python | 安装 Python 3.12（公司软件中心或 python.org）后重跑入口。 |
| 可选依赖缺失 | 电脑仍可连接，仅 build/本地仿真任务在执行前会提示缺哪个包；先装 `PyYAML/httpx/pydantic` 即可。 |
| 下载/解压/执行被安全软件拦截 | 将 `%LOCALAPPDATA%\radar-sim` 加入杀软白名单后重跑；仍失败联系管理员。 |

### 换电脑 / 换服务器 / 卸载

- 换电脑：新电脑重新连接一次即可（每台机器有独立 agent_id）。
- 换服务器地址：需要管理员显式 rebind（`-ForceRebind`），安装器不允许静默改服务器。
- 卸载：任务计划程序里删除 `RadarSimConnector-<用户名>` 与 `-Watchdog` 两个任务，再删除 `%LOCALAPPDATA%\radar-sim` 文件夹即可。
