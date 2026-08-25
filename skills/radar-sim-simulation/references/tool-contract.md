# Radar-sim MCP 工具合同

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

- 工具参数 `confirm=true`；
- MCP 进程环境变量 `RADAR_SIM_ALLOW_CONNECTOR_INSTALL=1`；
- MCP 进程运行在 Windows；
- 安装器完成后 exact-device status 显示当前 Connector 已在线且合同版本正确。

认证开启的服务如果返回 `connector_pairing_required`，Skill 不得绕过认证或把长期 Token 写入安装脚本；应交给部署方提供短期配对流程。

`check_agent_tools` 可以自动调用。`update_agent_tools` 必须同时满足：

- 工具参数 `confirm=true`；
- MCP 进程环境变量 `RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE=1`；
- Bundle Manifest 校验值通过；
- 新版本 SDK/MCP import 验证通过；
- 激活只切换版本指针，不覆盖当前正在运行的 MCP 进程；
- 更新后返回 `restart_required=true`，由 Agent 重启 MCP 后生效。
- 如果 Agent 有固定 Skill Registry，可通过 `RADAR_SIM_SKILL_ROOT` 指定；未指定时使用 Agent Tools 返回的本地 `skill_path`。

Agent Tools Manifest 还包含 `mcp_tool_contract_version` 和 `mcp_dependency_version`。前者用于判断工具输入/输出合同，后者用于审计底层第三方 MCP 依赖；若本地适配器不支持合同版本，必须先更新 MCP/Skill，不能静默按旧工具参数调用。

## Skill 对用户隐藏的外围准备

Skill 应把以下工具调用视为一次仿真任务的自动准备步骤，而不是要求用户填写的配置项：

1. `check_agent_tools`：确认当前 MCP/SDK/Skill 合同；必要时在允许本机更新后调用 `update_agent_tools`，并在重启 MCP 后继续；
2. `check_windows_connector`：当任务需要 Windows 代码、Windows 编译或本地执行时自动检查；
3. `install_or_update_windows_connector`：只有本机变更策略要求显式授权时，向用户请求一句通用的“允许安装/更新仿真连接组件吗”，不要求用户理解安装器；
4. `get_simulation_capabilities`、`get_simulation_readiness`：自动确认当前任务的执行能力；
5. `validate_simulation`：最终 YAML 确认后自动执行，任何缺失或冲突只回问对应业务字段。

用户不需要提供服务器 URL、Connector 路径、Agent ID、Stage ID、TransferPlan、Runtime Bundle ID 或 Cluster 内部参数。工具返回的内部字段由 Skill 消化；只有 `job_id`、状态、进度、等待动作、Diagnosis、Manifest 和结果路径对用户有意义。

兼容性更新不是每次任务的阻塞条件：当前 MCP 能够满足本次工具合同且服务端接受配置时，可以先执行任务，再在后台或下一次会话更新兼容版本；只有合同不兼容、工具缺失或安全修复要求时才必须先更新并重启 MCP。
