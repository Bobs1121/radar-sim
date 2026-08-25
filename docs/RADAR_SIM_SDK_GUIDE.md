# radar-sim Python SDK 指导手册

> 适用版本：`UserRunConfig 2.0` / V2 `/api/v1`
>
> 目的：指导 Python 程序、Copilot Skill、MCP Server 和其他 Agent 通过 SDK 完成 Selena 仿真任务。
>
> SDK/MCP/Skill 不下载源码的安装、发布和 Agent 注册方式见 [`RADAR_SIM_DISTRIBUTION.md`](RADAR_SIM_DISTRIBUTION.md)。

## 1. 文档结论

Python SDK 是 radar-sim 的编程入口。Web 只是同一 `/api/v1` 合同的可视化前台，不是仿真执行依赖。

在用户已经安装并运行统一 Windows Connector 的前提下，Agent 可以不使用 Web 完成：

- 配置草稿导入/导出；
- 配置 Schema、执行能力和 Cluster readiness 查询；
- 配置校验、自动路由和任务提交；
- Windows 本地编译、本地仿真和本地文件读取任务的调度；
- Cluster 直传、共享路径零复制和传输恢复；
- Job、Stage、事件、日志和进度查询；
- 取消、Stage 重试、失败输入重试；
- Diagnosis、Manifest 和结果 ZIP 下载。

SDK 不负责实现 Selena 算法，也不在 Linux 控制面执行 Windows 编译或本地 Selena。SDK 也不会无提示地安装 Visual Studio、执行 Connector 安装器或唤醒离线电脑。

## 2. 适用边界

### 2.1 四类主要执行路径

| Selena 来源 | 目标 | SDK 能否单独完成控制调用 | 实际执行者 |
|---|---|---|---|
| `existing` | `cluster`，所有资源 Cluster 可见 | 可以 | Cluster 执行面 |
| `existing` | `cluster`，SDK 调用机可读本地资源 | 可以发起直传 | SDK 数据面 + Cluster |
| `build` | `cluster` | 可以提交和监控，但必须有 Connector | Windows Connector 编译，Cluster 仿真 |
| `existing/build` | `local` | 必须有 Connector | Windows Connector 本地执行 |

如果 SDK 运行在 Linux，而输入是 `C:/`、`D:/` 或 Windows 本地 UNC 路径，Linux SDK 不能打开该路径。此时应连接实际存放文件的 Windows Connector，或把路径改成执行端可见的共享路径。

### 2.2 数据面边界

MF4、Selena.exe/DLL、Runtime XML、MatFilter、Adapter 和结果文件正文不得进入：

- MCP 参数；
- 模型上下文；
- Linux Web/API 请求体；
- Skill 的中间消息。

控制面只传递配置、文件清单、大小、校验值、状态、进度和逻辑引用。文件由 SDK/Connector 直接写入执行目标。

## 3. 安装与连接

### 3.1 SDK 安装

只使用 SDK：

```powershell
python -m pip install -e "D:\RamboStar\idea\radar-sim[sdk]"
```

开发环境可使用：

```powershell
python -m pip install -e "D:\RamboStar\idea\radar-sim[sdk,dev]"
```

安装 MCP 适配器：

```powershell
python -m pip install -e "D:\RamboStar\idea\radar-sim[mcp]"
```

### 3.2 SDK 客户端初始化

`base_url` 只填写服务根地址，不要写 `/api/v1`，SDK 会自动追加版本前缀。

```python
from radar_sim_sdk import RadarSimClient

client = RadarSimClient(
    "http://10.190.171.44:8877",
    user="user-your-ntid",       # 可信内网无认证模式
    # token="...",               # 正式 Bearer 认证模式
)
```

正式认证模式下，owner 应由 Bearer Token 决定。不要把长期 Token 放入 YAML、MCP 参数或提交日志。

Web、SDK 和 Connector 必须使用同一个 owner。可信内网无认证模式下，`X-Rsim-User` 是隔离标签，不是认证机制，不能将服务暴露到不受信网络。

### 3.3 Connector 前置安装

Connector 需要 Windows 本地路径、Windows 编译或本地仿真时才是必需的。

SDK 可生成同源安装入口：

```python
from pathlib import Path

launcher = client.download_windows_connector_for_run(
    config,
    Path(r"C:\Temp\RadarSim-Connect-Windows.cmd"),
)
print(launcher)
```

传统安全模式下，SDK 只下载脚本，不执行脚本。MCP 本地适配器可以在显式用户授权和本地策略允许时执行安装/更新，详见 [MCP 与 Skill 指南](#12-mcp-与-skill-集成)。

安装完成的判定不能只看安装器退出码，必须再次调用：

```python
capabilities = client.capabilities()
```

如果服务报告 `windows_connector.update_required=true`，旧 Connector 不应领取新任务，应先执行更新。

如果本机安装目录已经保存了 Connector 的 `agent_id`，MCP/本地集成层可以进一步调用 exact-device 查询：

```python
status = client.windows_connector_status(agent_id)
if status["available"] and status["contract_current"]:
    print("this exact computer is ready")
```

这比只观察同 owner 的聚合能力更可靠，不会把另一台 Windows 电脑在线误判成当前电脑安装成功。

## 4. UserRunConfig 2.0

### 4.1 完整配置结构

```yaml
schema_version: "2.0"

selena:
  source: build                 # build | existing
  code_path: "D:/workspace/repo"
  branch: ""                   # 只做期望分支提醒，不自动切换
  selena_build_script: "D:/workspace/repo/path/to/build_selena.bat"
  package_build_script: ""     # 可选，仅用于依赖诊断
  runtime_xml: "D:/workspace/repo/path/to/Runtime.xml"

data:
  path: "D:/measurements"

simulation:
  target: auto                 # auto | local | cluster
  source: ""                  # RadarFC/RadarFL/RadarFR/RadarRL/RadarRR
  adapter_file: ""
  mat_filter: ""

result:
  path: ""
```

### 4.2 Selena 配置规则

`selena.source=build` 时：

- `code_path` 必填；
- `selena_build_script` 必填；
- `runtime_xml` 必填；
- `existing_path` 不应填写；
- 当前工作区和未提交修改会被使用；
- `branch` 用于核对历史 Selena 产物 provenance，不会执行 checkout、reset 或 stash；若历史产物分支与用户预期分支不一致，公共 Build Stage 执行全量编译；
- 编译命令按用户提供的脚本执行，不由 Skill 添加产品参数；框架只对脚本中的清理命令做安全策略适配。

Selena 编译策略是公共 Web/SDK/MCP/Skill 语义，不是某个前端实现的特殊行为：

| 代码状态与产物证据 | 行为 | 是否允许清理已有输出 |
|---|---|---|
| 明确无代码变更，且分支、提交、构建模式、编译入口指纹和实际 `Selena.exe` 一致 | 跳过编译，复用并重新校验现有 Bundle | 否 |
| 明确有代码变更，或提交发生变化 | 执行用户脚本的增量编译 | 否 |
| 无法读取代码状态、历史 provenance 不完整、产物路径需要兜底解析 | 执行增量编译 | 否 |
| 明确证明现有构建来自不同 Selena 分支，或构建模式不兼容 | 执行全量编译 | 仅恢复脚本中已识别的清理命令 |

“无法确认”永远不能升级成全量清理。执行结果中的 `build_policy` 会记录
`mode=skipped|fresh|incremental|full`、`code_change_status=unchanged|changed|unknown`、
`code_change_reason`、请求/历史分支与提交，以及编译器是否实际执行。
`source=existing` 仍然完全跳过 `build_selena`，不会进入上述编译策略。

`selena.source=existing` 时：

- `existing_path` 必填；
- `runtime_xml` 必填；
- 产物目录必须包含 `Selena.exe` 和依赖 DLL；
- 不要求 Visual Studio 或编译脚本；
- 如果同时填写代码仓、分支或编译脚本作为交叉验证证据，则 `code_path` 也必须填写。

### 4.3 数据配置规则

`data.path` 可以是：

- 单个 `.MF4` 文件；
- 包含多个 MF4 的目录；
- Windows 本地路径；
- Linux 本地路径；
- UNC/DFS/共享路径；
- `shared://` 或 `dataset://` 逻辑引用。

Skill 不应为了配置便利而把数据正文读入上下文，也不应自动选取仓库中“看起来最近”的 MF4。若有多个候选，必须让用户确认。

### 4.4 仿真配置规则

`simulation.target`：

- `auto`：由服务端结合能力、路径可达性和搬运成本选择；
- `local`：必须有完整 Windows Connector；
- `cluster`：要求 Cluster readiness 通过；本地资源需要 SDK/Connector 直传或共享路径可见。

`simulation.source`：

- 用户明确知道源时填写 `RadarFC`、`RadarFL`、`RadarFR`、`RadarRL` 或 `RadarRR`；
- 不确定时留空，由 MF4 acquisition metadata/Runtime 信息推导；
- 不能根据产品名、仓库名或历史 profile 猜测。

`simulation.mat_filter`：

- 显式值优先；
- 留空时，SDK/Connector 可以从代码仓、已有 Selena 目录、编译脚本和 Runtime XML 附近的受控路径推导；
- SDK 的本地推导不会修改提交的 YAML 或 fingerprint，只会影响源端传输资源。

`simulation.adapter_file`：只在本次 Selena 仿真明确需要时填写，不使用项目默认值。

`result.path` 是接收端结果目录，不是 Linux 服务端目录。留空时 SDK 使用本机默认结果目录；Cluster 任务没有反向 Connector 时，最稳定的交付方式是下载 `result_ref` 对应的 ZIP。

## 5. 配置草稿接口

### 5.1 导入草稿

```python
draft = client.import_yaml(yaml_text)

if not draft["complete"]:
    print(draft["missing_fields"])
    print(draft["validation_errors"])
```

草稿导入不会：

- 创建 Job；
- 执行编译；
- 访问本地文件；
- 启动 Cluster readiness；
- 启动传输。

### 5.2 导出草稿

```python
exported = client.export_yaml(
    {
        "selena": {"source": "build", "code_path": "D:/repo"},
        "data": {"path": "D:/measurements"},
    }
)

print(exported["yaml_content"])
print(exported["complete"])
```

`complete=false` 只能表示草稿阶段合法但还不能提交。Skill 不得为了让草稿通过而猜路径、项目、分支或运行参数。

## 6. 提交前检查

推荐顺序：

```python
schema = client.user_run_config_schema()
readiness = client.cluster_readiness()
capabilities = client.capabilities()
validation = client.validate_run(config)
```

重点检查：

1. `validation.valid` 为真；
2. `validation.config` 是规范化后的配置；
3. `validation.fingerprint` 已产生；
4. `validation.execution.selected_target` 是最终路由；
5. `validation.readiness.can_submit` 不为假；
6. `validation.execution_plan` 与用户意图相符；
7. `source=build` 时，Windows Connector 和编译能力可用；
8. `target=cluster` 时，Cluster readiness 通过；
9. 所有路径由正确的执行设备读取；
10. 用户确认了多个候选项中的最终选择。

如果 readiness 已知失败，不应先创建一个必然等待或失败的 Job。若服务端允许任务进入等待，Skill 必须把等待状态如实返回。

## 7. 创建 Job

### 7.1 使用配置对象

```python
job = client.submit_run(
    config,
    idempotency_key="copilot-run-20260821-001",
    auto_transfer=True,
)

print(job.id)
print(job.status)
print(job.progress_percent)
```

### 7.2 使用 YAML 文本或文件

```python
job = client.submit_yaml(
    yaml_text,
    idempotency_key="copilot-run-20260821-001",
)

job_from_file = client.submit_yaml(
    "D:/workspace/run.yaml",
    idempotency_key="copilot-run-20260821-002",
)
```

### 7.3 幂等要求

非 dry-run 提交必须使用业务侧可持久保存的幂等键。网络响应丢失时，使用同一个 key 重试，不要生成新 key。

```python
try:
    job = client.submit_run(config, idempotency_key=key)
except RadarSimTransportError:
    job = client.list_jobs(limit=100)  # 或使用同一个 key 重放提交
```

SDK 未显式提供 key 时会生成临时 key，并通过 `client.last_submission_key` 暴露；对 Agent 长期任务仍推荐由调用方生成并保存稳定 key。

## 8. 直传和 Connector 恢复

SDK 在 `submit_run(auto_transfer=True)` 时，会尝试执行调用机可读的本地资源直传。

```python
job = client.submit_run(config, auto_transfer=True)

if job.needs_input:
    print(job.waiting)
    job = client.resume_direct_transfers(job, config, retries=3)
```

传输过程：

1. 服务端持久化 Job/Stage；
2. SDK 请求 owner/job/stage 绑定的 TransferPlan；
3. SDK 读取源端文件并直接写入目标数据面；
4. SDK 上报元数据进度；
5. SDK 上报带校验值的 Manifest；
6. 服务端根据所有资源角色恢复 Stage。

Linux 不接收 MF4、Selena 或 DLL 正文。

传输状态：

```python
transfer = client.get_job_transfer_status(job.id)
print(transfer["status"])
for plan in transfer.get("plans", []):
    print(plan["source_role"], plan["status"])
```

SDK 不会把永久的 4xx 合同错误伪装成 waiting；网络错误、目标不可达、过期 TransferPlan 等可恢复问题才会进入等待/重试路径。

## 9. 状态、事件和进度

### 9.1 单次状态查询

```python
job = client.get_job(job_id)

print(job.status)
print(job.current_stage)
print(job.progress_percent)
print(job.waiting)
print(job.available_actions)
```

主要公开状态：

| 状态 | 含义 |
|---|---|
| `queued` | 已创建，等待依赖或执行节点 |
| `running` | 至少一个 Stage 正在执行 |
| `needs_input` | 需要 Connector、路径、readiness 或用户动作 |
| `succeeded` | 任务业务成功 |
| `partial` | 批量任务部分成功，结果和失败输入诊断均保留 |
| `failed` | 任务失败；需读取 Diagnosis 区分仿真、配置、基础设施和系统错误 |
| `cancelled` | 用户或系统取消 |

### 9.2 事件和日志

```python
page = client.events(job_id, since=0, limit=200)
for event in page.events:
    print(event.id, event.event, event.stage, event.progress, event.message)
```

长任务可以使用：

```python
for event in client.watch(job_id, poll_interval=2.0, timeout=60.0):
    print(event.event, event.progress, event.message)
```

`timeout` 是观察窗口，不会取消服务器 Job。

### 9.3 Agent 友好等待

```python
current = client.wait_until_actionable(
    job_id,
    timeout=30.0,
    poll_interval=1.0,
)

if current.needs_input:
    # 根据 waiting/action 让用户安装、启动或更新 Connector
    ...
elif current.terminal:
    ...
```

`wait_job()` 适合明确要等到终态的调用。MCP 默认应优先使用 `wait_until_actionable()`，避免因为用户尚未安装 Connector 而无限占用工具请求。

## 10. 任务动作

### 10.1 取消

```python
cancelled = client.cancel(job_id)
```

取消是用户动作，不应由 Skill 因为一次观察超时自动触发。

### 10.2 Stage 重试

```python
retried = client.retry_stage(job_id, stage_id)
```

`stage_id` 应来自 Job `available_actions` 或 Diagnosis 的 action，不要让 Skill 自己猜内部 Stage。

### 10.3 失败输入重试

```python
retried = client.retry_failed_inputs(
    job_id,
    input_paths=["failed/one.MF4"],
)
```

只有批量 `partial` 任务使用该方法。已成功输入不应重复执行。

## 11. Diagnosis、Manifest 和结果

```python
diagnosis = client.diagnosis(job_id)
print(diagnosis.outcome)
print(diagnosis.code)
print(diagnosis.category)
print(diagnosis.action)

manifest = client.manifest(job_id)
if manifest.available:
    print(manifest.manifest)
```

结果判断必须优先使用 Manifest 和 Diagnosis：

1. `partial` 是部分成功，不是全成功；
2. `failed` 可能仍有失败现场和诊断产物；
3. `artifacts_available=true` 不等于仿真成功；
4. 只有存在合法 `result_ref` 时才下载结果。

```python
if diagnosis.artifacts_available and diagnosis.result_ref:
    archive = client.download_result(
        diagnosis.result_ref,
        "D:/copilot-results",
    )
```

更高层的 Job 便捷方法：

```python
archive = client.download_job_result(
    job_id,
    destination="D:/copilot-results",
)
```

下载过程会使用临时文件、有限重试和 SHA-256 校验；校验失败不得把半包文件交给后续 Agent。

## 12. 异常处理机制

| 异常 | 含义 | Agent 处理方式 |
|---|---|---|
| `RadarSimApiError` | 服务端返回稳定错误合同 | 读取 `code`、`status_code`、`detail`、`actions`、`request_id` |
| `RadarSimTransportError` | HTTP 连接、读写或服务暂时不可达 | 保留同一幂等键，重新查询 Job 或重放提交 |
| `RadarSimTransferCancelledError` | 调用方主动取消传输 | 不自动重试，等待用户决定 |
| `RadarSimIntegrityError` | 结果归档校验失败 | 删除临时文件并重新下载 |
| `ValidationError` | 配置不满足严格 `UserRunConfig 2.0` | 读取字段错误，不猜值 |
| `ValueError` | 本地参数、结果目标或调用顺序非法 | 修正调用参数 |
| `TimeoutError` | 本次观察窗口结束 | 查询 `get_job()`，不要报告为仿真失败 |

服务端稳定错误示例：

- `windows_connection_required`；
- `windows_path_access_required`；
- `windows_connector_update_required`；
- `cluster_readiness_unavailable`；
- `cluster_direct_transfer_unavailable`；
- `source_to_local_unavailable`；
- `selena_failed`；
- `simulation_partial`；
- `result_unavailable`。

Skill 不应解析原始堆栈、日志关键字或英文自然语言来判断成功失败。

## 13. MCP 与 Skill 集成

### 13.1 MCP 安装

用户不下载源码时，使用服务器 Agent Tools 分发入口：

```text
GET /api/v1/agent-tools/manifest
GET /api/v1/agent-tools/install.py
GET /api/v1/agent-tools/install.ps1
GET /api/v1/agent-tools/package.zip
```

Windows Agent 可以让 Copilot 下载并运行 `install.ps1`；Windows/Linux Agent 也可以下载 `install.py`。安装器会校验 Bundle、根据本机 Python/platform tags 选择兼容 wheel、离线创建版本化本地 venv、安装 SDK/MCP、保存 Skill，并生成稳定 MCP 启动器。用户不需要下载 radar-sim 源码，也不需要在安装阶段访问包仓。

仅在 SDK/MCP 开发者已经拥有源码工作区时，才使用 editable 安装：

```powershell
python -m pip install -e "D:\RamboStar\idea\radar-sim[mcp]"
```

stdio 运行：

```powershell
$env:RADAR_SIM_BASE_URL = "http://10.190.171.44:8877"
$env:RADAR_SIM_USER = "user-your-ntid"
$env:RADAR_SIM_MCP_TRANSPORT = "stdio"
radar-sim-mcp
```

本地 Agent 允许 Connector 自动安装/更新时，再显式设置：

```powershell
$env:RADAR_SIM_ALLOW_CONNECTOR_INSTALL = "1"
```

允许本机 Agent Tools 自动更新时：

```powershell
$env:RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE = "1"
```

如果 Agent 有固定的本地 Skill Registry，可以额外设置：

```powershell
$env:RADAR_SIM_SKILL_ROOT = "C:\Users\alice\.agent\skills"
```

安装器会把新 Skill 原子切换到该目录；未设置时，Skill 保存在 Agent Tools 版本目录中，并通过安装结果返回 `skill_path`。

MCP 工具仍要求每次安装/更新调用传入 `confirm=true`。这样可以实现“自动检查、授权后自动安装”，不会因为一次普通查询意外修改用户电脑。

### 13.2 MCP 工具目录

正式 MCP 工具定义位于 `radar_sim_mcp/server.py`，主要工具包括：

| 工具 | 用途 |
|---|---|
| `get_simulation_schema` | 获取公开配置 Schema |
| `import_simulation_yaml` | 导入完整/部分草稿 |
| `export_simulation_yaml` | 导出规范 YAML |
| `get_simulation_readiness` | 查询 Cluster readiness |
| `get_simulation_capabilities` | 查询 Windows/Cluster/Connector 能力 |
| `check_agent_tools` | 检查本机 SDK/MCP/Skill、MCP 工具合同、底层 MCP 依赖与服务端 Bundle 版本 |
| `update_agent_tools` | 版本化更新本机 SDK/MCP/Skill；完成后重启 MCP |
| `check_windows_connector` | 检查当前 MCP 主机的 Connector |
| `install_or_update_windows_connector` | 授权后安装/更新本机 Connector |
| `validate_simulation` | 校验完整配置 |
| `submit_simulation` | 创建 Job |
| `list_simulations` / `get_simulation` | 查询任务 |
| `get_simulation_events` / `wait_simulation` | 查询事件、日志、进度和等待状态 |
| `get_simulation_transfer` / `resume_simulation_transfer` | 直传状态和恢复 |
| `cancel_simulation` | 取消任务 |
| `retry_simulation_stage` | Stage 重试 |
| `retry_failed_inputs` | 部分成功后的失败输入重试 |
| `diagnose_simulation` | 业务诊断 |
| `get_simulation_manifest` | Manifest |
| `list_simulation_results` / `get_simulation_result` | 结果目录和结果元数据 |
| `download_simulation_result` | 下载校验后的 ZIP，不返回文件正文 |

所有工具返回：

```json
{
  "ok": true,
  "data": {}
}
```

失败返回：

```json
{
  "ok": false,
  "error": {
    "type": "api_error",
    "code": "windows_connection_required",
    "message": "...",
    "retryable": true,
    "actions": []
  }
}
```

### 13.3 Skill 职责

Skill 的职责只有：

1. 识别用户是 `build` 还是 `existing`；
2. 从当前代码仓只读发现 Git 根目录、脚本和候选 Runtime；
3. 对多个候选向用户提问，不静默选择；
4. 生成或补全 `UserRunConfig 2.0`；
5. 调用 MCP 工具；
6. 解释 `waiting`、`actions`、Diagnosis 和 Manifest；
7. 管理 `job_id`、幂等键和事件 cursor。

Skill 禁止：

- 使用 Web 页面作为前置条件；
- 复制十阶段 DAG；
- 自己实现 SMB/UNC/Cluster 传输；
- 自己修改 Windows 路径分隔符；
- 根据项目名猜 Adapter、MatFilter、Radar source 或 Runtime；
- 把 `partial` 显示成成功；
- 把 Connector 安装器退出码直接当成上线成功；
- 在没有用户确认时自动执行本机安装/更新。

## 14. 发布验收

SDK/MCP 发布前至少验证：

1. Web 和 SDK 对同一 YAML 得到相同 canonical config、fingerprint 和 Stage DAG；
2. `existing/build × local/cluster` 四条路径都能返回正确 readiness 和等待动作；
3. Connector 未安装、离线、旧版本和更新后分别有稳定状态；
4. 本地输入直传不会把正文送入 Linux API；
5. 任务响应丢失后使用同一幂等键不会创建重复 Job；
6. `queued/running/needs_input/succeeded/partial/failed/cancelled` 状态可被 Skill 正确解释；
7. 取消、Stage 重试、失败输入重试和结果下载均有自动化测试；
8. 结果 ZIP 下载做校验并清理半包；
9. 两个 owner 的 Job、Connector、Transfer、Manifest 和 Result 互不可见；
10. 至少完成一条真实 Selena/Cluster 纵向任务，不以单元测试替代。

## 15. 相关文件

- SDK 实现：`radar_sim_sdk/client.py`、`radar_sim_sdk/models.py`；
- MCP 实现：`radar_sim_mcp/server.py`、`radar_sim_mcp/connector.py`；
- 可复用 Skill：`skills/radar-sim-simulation/SKILL.md`；
- 用户配置合同：`core/user_config.py`；
- Web/API 合同：`docs/AI_INTEGRATION_CONTRACT.md`；
- Connector 安装合同：`docs/windows-one-click-connector.md`。
