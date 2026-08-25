# radar-sim SDK、MCP 与 Skill 分发规范

## 1. 先区分三个对象

| 对象 | 作用 | 用户/Agent 如何获取 |
|---|---|---|
| Python SDK | MCP Server 和其他 Python 集成的库，调用 Linux radar-sim API | wheel、企业 PyPI/Artifactory、直接 wheel URL |
| MCP Server | Agent 可以调用的工具进程；内部使用 SDK | 本机安装 Python 包后启动，或使用远程 MCP URL |
| Skill | Agent 的配置引导和调用策略 | Agent 的 Skill Registry、Skill ZIP 或本地 Skill 目录 |

SDK 不是一个用户直接连接的服务。真正的服务链是：

```text
Agent
  -> Skill instructions
  -> MCP tools
  -> RadarSimClient
  -> radar-sim Linux /api/v1 service
  -> Connector / Cluster execution
```

## 2. 当前仓库状态

当前代码已经具备：

- `radar_sim_sdk/` Python SDK；
- `radar_sim_mcp/` MCP Server；
- `skills/radar-sim-simulation/` 可分发 Skill；
- `setup.py` 的 `[sdk]`、`[mcp]` extras；
- `radar-sim-mcp` console entry point。

当前尚未配置：

- PyPI 或企业包仓发布地址；
- 面向外部用户的固定远程 MCP URL；
- 认证开启部署的短期 Connector pairing 服务。

因此当前用户不能直接执行没有发布前提的：

```powershell
pip install radar-sim
```

正式对外提供前，必须完成第 4 节中的一种分发方式。

## 3. 推荐分发模式

### 3.1 模式 A：本机 stdio MCP，推荐用于本地代码仓 Agent

适用：

- Copilot/Agent 运行在用户本机；
- 仿真配置包含本机代码路径、数据路径或本地 Selena；
- Agent 需要自动检查、安装或更新本机 Connector；
- 不希望用户下载 radar-sim 源码。

部署结构：

```text
用户本机
  ├─ Agent/Copilot
  ├─ radar-sim-simulation Skill
  ├─ radar-sim[mcp] Python wheel
  ├─ radar-sim-mcp stdio process
  └─ Windows Connector
          │
          └── HTTP/HTTPS
                Linux radar-sim /api/v1
```

用户通过企业包仓安装：

```powershell
python -m pip install --index-url https://packages.example.com/simple `
  "radar-sim[mcp]==<release-version>"
```

或者使用直接 wheel URL：

```powershell
python -m pip install `
  "radar-sim[mcp] @ https://packages.example.com/radar_sim-<release>-py3-none-any.whl"
```

Agent 的本机 MCP 配置使用已安装环境中的 Python：

```json
{
  "mcpServers": {
    "radar-sim": {
      "command": "C:\\Users\\alice\\.venvs\\radar-sim\\Scripts\\python.exe",
      "args": ["-m", "radar_sim_mcp.server"],
      "env": {
        "RADAR_SIM_BASE_URL": "https://rsim.example.com",
        "RADAR_SIM_USER": "user-alice",
        "RADAR_SIM_MCP_TRANSPORT": "stdio",
        "RADAR_SIM_ALLOW_CONNECTOR_INSTALL": "1"
      }
    }
  }
}
```

`RADAR_SIM_ALLOW_CONNECTOR_INSTALL=1` 只表示本机 MCP 允许执行安装器；实际工具调用仍要求 `confirm=true`。

MCP/Skill 自身更新使用独立策略开关：

```powershell
$env:RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE = "1"
```

`update_agent_tools(confirm=true)` 下载服务器签发的 `install.py`，将新 Bundle 安装到新的版本目录，校验 SDK/MCP import 后切换稳定启动器指针。正在运行的 MCP 进程不被覆盖，工具返回 `restart_required=true`；Agent 重启 MCP 后新 SDK 和 Skill 生效。

这是本地代码仓场景的推荐方式，因为 MCP 进程和 Connector 在同一用户电脑上，可以：

- 读取用户提供的本地路径；
- 执行受控 Connector 安装/更新；
- 运行 SDK 直传；
- 等待当前电脑 exact-device 上线。

### 3.2 模式 B：远程 streamable HTTP MCP

适用：

- 多个 Agent 共用一个集中式 MCP 服务；
- SDK 和 MCP 统一部署在服务端；
- 用户只需要提交配置、查询任务和下载结果。

Agent 只配置 MCP URL：

```text
https://rsim.example.com/mcp
```

认证由反向代理/MCP 部署层提供，不将长期 Token 写入 Skill 或 YAML。

远程 MCP 不具备用户本机文件读取能力，也不应试图替用户电脑安装 Connector。若任务需要 Windows 本地路径、编译或本地仿真，应满足以下任一条件：

- 用户已经安装并运行 Connector；
- 另有本机 Local MCP/Agent 负责 Connector 和数据面；
- 配置使用 Cluster 可见的 Selena、Runtime 和数据路径。

### 3.3 混合模式

远程 MCP 负责 Linux 控制面，本机 Connector 负责 Windows 文件和执行能力。该模式可以运行，但远程 MCP 不能自行完成本机 Connector 安装；通常要由企业设备管理系统、Local MCP 或用户一次性安装完成。

## 4. 发布方必须提供的正式物料

服务端 Agent Tools 分发面保持为四个稳定入口：

```text
GET /api/v1/agent-tools/manifest
GET /api/v1/agent-tools/package.zip
GET /api/v1/agent-tools/install.py
GET /api/v1/agent-tools/install.ps1
```

Manifest 只返回公开版本、兼容合同、Bundle 大小/校验值和同源下载 URL，不返回服务端物理路径、构建目录、仓库地址或依赖内部路径。安装入口只嵌入公开服务 URL；认证 Token 从本机进程环境读取且不写入安装状态。

Agent Tools Bundle 由发布环境生成多平台 wheel 集合：Linux 控制面 wheel、Windows CPython 3.10/3.11/3.12/3.13 wheel，以及 Windows MCP 所需的 `pywin32`。本机安装器根据目标 Python 的 wheel tags 选择兼容文件，离线解压到版本化 venv，不连接包仓、不把 Linux 二进制误装到 Windows，也不把源码放入 Bundle。

每个 release 应提供：

1. `radar-sim` wheel，包含 SDK 和 MCP 包；
2. `radar-sim[mcp]` 安装说明；
3. MCP stdio 配置模板；
4. 远程 MCP URL（如果部署了集中式 MCP）；
5. Skill ZIP 或 Skill Registry 版本；
6. SDK/MCP/Server/API 合同版本；
7. SHA-256、Python 版本和兼容的 `httpx/pydantic/mcp` 版本；
8. Connector contract version 和更新策略；
9. 认证方式、owner 配置和短期 pairing 说明；
10. 失败时的支持入口和回滚版本。

建议的 release 目录：

```text
release/<version>/
  radar_sim-<version>-py3-none-any.whl
  radar-sim-simulation.skill.zip
  mcp-config.stdio.example.json
  checksums.sha256
  RELEASE.md
```

## 5. Agent 如何发现并使用能力

Agent 不应该搜索用户代码仓来寻找 SDK 源码。应按以下顺序：

1. MCP Client 配置中是否存在 `radar-sim` Server；
2. 如果没有，读取组织提供的 MCP 安装清单或工具注册中心；
3. 本机模式下，从企业包仓安装指定版本的 `radar-sim[mcp]`；
4. 注册 `radar-sim-mcp` stdio 命令；
5. 加载 `radar-sim-simulation` Skill；
6. 调用 `get_simulation_schema` 验证工具可用；
7. 调用 `get_simulation_capabilities` 和 `get_simulation_readiness`；
8. 再进入配置、提交和任务生命周期。

Skill 发现文本应明确告诉 Agent：

```text
当用户请求 Selena/雷达仿真时，使用 radar-sim-simulation Skill。
通过 radar-sim MCP 工具调用，不使用 Web 页面，不查找或下载 radar-sim 源码。
提交前使用 UserRunConfig 2.0、readiness 和 capabilities 校验。
如果 Connector 缺失或过期，先检查；只有用户确认且本地策略允许时才安装/更新。
```

## 6. 版本兼容性

MCP/Skill 不应只按包版本判断可用性，还应检查：

- SDK API contract；
- Linux `/api/v1` API version；
- Connector contract version；
- `UserRunConfig` schema version；
- MCP tool contract version。

建议 MCP 启动时暴露只读健康信息或在第一次工具调用时验证：

```text
sdk_version
mcp_version
mcp_tool_contract_version
mcp_dependency_version
api_version
schema_version
required_connector_contract_version
```

不兼容时应返回稳定错误，不应静默降级到 legacy `SimulationSpec`、project/profile/recipe 或旧 Connector mode。

## 7. 安全边界

- Skill 文件可以公开分发，但不包含服务 Token、用户身份密钥或共享盘凭据；
- MCP 配置中的 Token 使用 Agent Secret/环境变量/企业凭据存储；
- stdio MCP 只绑定本机进程；HTTP MCP 必须放在认证和 HTTPS 后；
- `RADAR_SIM_ALLOW_CONNECTOR_INSTALL` 只控制本机安装器权限，不等于服务端认证；
- `RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE` 只控制本机 SDK/MCP/Skill 更新权限，不等于服务端认证；
- `RADAR_SIM_SKILL_ROOT` 是可选的 Agent Skill Registry 根目录；设置后 Bundle 会把新 Skill 原子切换到该目录，未设置时 Skill 保留在 Agent Tools 本地版本目录中，由 Agent 注册器读取 `skill_path`；
- Connector 安装/更新工具必须有 `confirm=true`；
- 安装完成必须验证 exact-device，而不是只看 owner 聚合能力；
- 所有任务结果通过 owner-scoped `result_ref` 和 checksum 下载；
- 不将任务大文件放入 Agent/MCP 消息。
