# Radar-sim MCP 工具合同

## Skill-only 首次启动合同（Agent 内部流程）

Skill 可以在本机尚未注册 radar-sim MCP 时完成首启引导。Agent 应先检查
当前会话是否已经暴露 `radar-sim` 工具；如果没有，执行
`scripts/bootstrap_agent_tools.py`，读取它返回的 `mcp_config_path`，再用
宿主 Agent 的 MCP 注册/重载能力注册 `scripts/start_mcp.py` 或生成配置中
指定的 stdio 命令。以上全部是内部动作；成功时不得向用户展示命令、URL、
本机路径、版本、校验值或安装器输出。用户不需要下载源码、创建虚拟环境
或手工执行 pip。

首启引导器的边界：

- 服务地址来自 Skill 的 provider-owned `references/service-profile.json`，
  也可由 Agent/部署环境覆盖；不把服务地址作为仿真 YAML 字段。源码 Skill
  不绑定具体部署地址，安装时由服务端把当前请求地址写入本机 Skill 元数据；
- 只下载公开安装入口和带 Manifest/SHA-256 校验的离线 Bundle；Bundle
  只包含 SDK、MCP、依赖 wheel 和 Skill，不包含 radar-sim 源码；
- 版本安装采用 side-by-side + 原子指针，当前 MCP 进程不被覆盖；
- `scripts/start_mcp.py` 不向 stdout 写启动日志，stdout 始终保留给 MCP
  JSON-RPC；服务短暂不可达时可回退到上一个完整本机版本；
- 如果宿主 Agent 不提供动态 MCP 注册或重载能力，Skill 可以完成下载和
  安装，但无法在同一个已经启动的 Agent 进程中凭空创建新工具连接；这
  是宿主能力边界。Skill 只应在无法自动重载时返回一次性的抽象阻塞，不
  把内部路径或配置细节交给用户。

## 通用输入原则

- `submit_simulation`、`validate_simulation` 和 `resume_simulation_transfer` 接受 `yaml_text` 或 `config`，二选一；
- `submit_simulation` 应提供稳定 `idempotency_key`；
- 文件正文不得作为工具参数；工具只接收路径、YAML、状态参数和逻辑引用；
- 结果下载返回本地路径，不返回 ZIP 内容；
- 工具返回统一包络。

## 工具目录

| 工具 | 主要输入 | 用途 |
|---|---|---|
| `get_simulation_schema` | 无 | 获取 `UserRunConfig 2.0` Schema |
| `import_simulation_yaml` | `yaml_text` | 草稿导入，不创建 Job |
| `export_simulation_yaml` | `config` | 导出规范 YAML |
| `get_simulation_readiness` | 无 | Cluster readiness |
| `get_simulation_capabilities` | 无 | Windows/Cluster/Connector 能力 |
| `get_simulation_state` | `context_path`, `data_path` 可选 | 读取本机 active profile，支持重复运行快速恢复 |
| `check_agent_tools` | 无 | 检查本机 SDK/MCP/Skill 版本 |
| `update_agent_tools` | `confirm`, `timeout_seconds` | 版本化更新本机 SDK/MCP/Skill |
| `check_windows_connector` | 无 | 检查本地 Connector 与 exact-device 状态 |
| `install_or_update_windows_connector` | `confirm`, `timeout_seconds` | 显式授权后安装/更新本机 Connector |
| `validate_simulation` | `yaml_text` 或 `config` | 返回 fingerprint、路由、readiness、Stage plan |
| `submit_simulation` | `yaml_text` 或 `config`、`idempotency_key` | 创建 Job |
| `list_simulations` | `status`, `limit` | 查询任务列表 |
| `get_simulation` | `job_id` | 查询任务快照 |
| `get_simulation_events` | `job_id`, `since`, `limit` | 查询日志、进度和事件 |
| `wait_simulation` | `job_id`, `timeout_seconds` | 等到终态或 `needs_input`；超时返回当前快照 |
| `get_simulation_transfer` | `job_id` | 查询直传汇总 |
| `resume_simulation_transfer` | `job_id`、配置、`retries` | 恢复直传 |
| `cancel_simulation` | `job_id` | 取消任务 |
| `retry_simulation_stage` | `job_id`, `stage_id` | 重试失败 Stage |
| `retry_failed_inputs` | `job_id`, `input_paths` | 只重试失败输入 |
| `diagnose_simulation` | `job_id` | 获取稳定 Diagnosis |
| `get_simulation_manifest` | `job_id` | 获取 Manifest |
| `list_simulation_results` | 无 | 查询结果归档 |
| `get_simulation_result` | `result_ref` | 查询结果元数据 |
| `download_simulation_result` | `job_id`, `destination` | 下载并校验 ZIP |

## 成功包络

```json
{
  "ok": true,
  "data": {
    "job_id": "job_..."
  }
}
```

## 失败包络

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

错误处理优先级：

1. 读取 `code` 和 `actions`；
2. 可恢复错误才重试；
3. 提交响应丢失时复用原 `idempotency_key`；
4. `Timeout` 只表示观察结束，不取消 Job；
5. `partial` 不是全成功；
6. `artifacts_available` 不等于仿真成功。

## Connector 自动安装政策

`check_windows_connector` 可以自动调用。`install_or_update_windows_connector` 必须同时满足：

- 工具参数 `confirm=true`，或官方 Skill-only 安装已启用 `RADAR_SIM_AUTO_PREPARE=1`；
- MCP 进程环境变量 `RADAR_SIM_ALLOW_CONNECTOR_INSTALL=1`；
- MCP 进程运行在 Windows；
- 安装器完成后 exact-device status 显示当前 Connector 已在线且合同版本正确。

认证开启的服务如果返回 `connector_pairing_required`，Skill 不得绕过认证或把长期 Token 写入安装脚本；应交给部署方提供短期配对流程。

官方 Skill-only 安装生成的 MCP 配置还会启用
`RADAR_SIM_AUTO_PREPARE=1`。在该受控本机安装中，Skill 可以直接执行准备
工具而不在每个内部动作前重复询问；`RADAR_SIM_ALLOW_*` 仍是实际本机变更
权限门禁。若宿主或组织策略拒绝该权限，Skill 只返回一个抽象阻塞，不把
内部确认链暴露给用户。

`check_agent_tools` 可以自动调用。`update_agent_tools` 必须同时满足：

- 工具参数 `confirm=true`，或官方 Skill-only 安装已启用 `RADAR_SIM_AUTO_PREPARE=1`；
- MCP 进程环境变量 `RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE=1`；
- Bundle Manifest 校验值通过；
- 新版本 SDK/MCP import 验证通过；
- 激活只切换版本指针，不覆盖当前正在运行的 MCP 进程；
- 更新后返回 `restart_required=true`，由 Agent 重启 MCP 后生效。
- 如果 Agent 有固定 Skill Registry，可通过 `RADAR_SIM_SKILL_ROOT` 指定；未指定时使用 Agent Tools 返回的本地 `skill_path`。

Agent Tools Manifest 还包含 `mcp_tool_contract_version` 和 `mcp_dependency_version`。前者用于判断工具输入/输出合同，后者用于审计底层第三方 MCP 依赖；若本地适配器不支持合同版本，必须先更新 MCP/Skill，不能静默按旧工具参数调用。

## Skill 对用户隐藏的外围准备

Skill 应把以下工具调用视为一次仿真任务的自动准备步骤，而不是要求用户填写的配置项：

1. `get_simulation_state`：按当前代码仓/数据上下文恢复最近确认的 UserRunConfig；用户说“再仿刚刚的数据”时优先使用它，不重新扫描和提问；
2. `check_agent_tools`：确认当前 MCP/SDK/Skill 合同；必要时静默调用 `update_agent_tools` 并重启 MCP；
3. `check_windows_connector`：当任务需要 Windows 代码、Windows 编译或本地执行时自动检查；
4. `install_or_update_windows_connector`：在官方自动准备策略下静默执行，不要求用户理解安装器；
5. `get_simulation_capabilities`、`get_simulation_readiness`：自动确认当前任务的执行能力；
6. `validate_simulation`：最终 YAML 确认后自动执行，任何缺失或冲突只回问对应业务字段。

用户不需要提供服务器 URL、Connector 路径、Agent ID、Stage ID、TransferPlan、Runtime Bundle ID 或 Cluster 内部参数。工具返回的内部字段由 Skill 消化；只有 `job_id`、状态、进度、等待动作、Diagnosis、Manifest 和结果路径对用户有意义。

每次非 dry-run `submit_simulation` 成功后，MCP 自动把确认过的 YAML、配置
fingerprint、代码/产物/数据上下文、Job ID 和最近状态写入本机 active
profile；`get_simulation`/`wait_simulation` 自动更新状态。该文件不进入代码
仓、不上传服务端、不保存文件正文或凭据，Skill 不应向用户展示它的路径或
写入过程。

兼容性更新不是每次任务的阻塞条件：当前 MCP 能够满足本次工具合同且服务端接受配置时，可以先执行任务，再在后台或下一次会话更新兼容版本；只有合同不兼容、工具缺失或安全修复要求时才必须先更新并重启 MCP。

## 编译策略合同

`UserRunConfig 2.0` 表达的是“编译当前代码”还是“使用已有 Selena
产物”，不表达增量/全量实现细节。Skill 将“我改了代码后重新仿真”“编译
当前代码”等语言映射为 `selena.source=build`，将“不要编译、使用已有产物”
映射为 `selena.source=existing`。实际 build policy 必须来自 Stage 的公开
证据：

- 代码和已登记产物的分支、提交、工作区和入口证据全部一致时，允许
  `skipped`；
- 发现代码变化或无法安全判断变化时，执行增量路径；
- 仅在分支/产物 provenance 等正向不兼容证据存在时，才执行全量路径；
- 运行时 XML 不单独触发全量编译；它属于运行资产，除非服务端合同另有
  明确要求；
- Skill 不得凭自然语言猜测或提前宣称实际编译模式，必须等待 build Stage
  的 `build_policy`/事件/Manifest 证据。
