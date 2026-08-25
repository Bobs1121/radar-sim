# Copilot 审批与无感运行配置

本文件只解决 Agent 宿主的审批弹窗，不改变仿真业务配置，也不授权 Skill 修改用户代码仓或安全设置。目标是：用户首次安装/使用时完成一次工作区级配置，之后仿真流程不再逐阶段点击 `Allow`、`Confirm` 或 `Continue`。

## 推荐方案：VS Code 工作区级 Bypass Approvals

适用当前 VS Code Agent/Copilot Chat 的本地可信代码仓。

1. 打开该代码仓作为 VS Code 工作区。
2. 在 Chat 输入框底部的权限选择器中，将 `Default Approvals` 切换为 `Bypass Approvals`。
3. 只对当前工作区/当前会话使用，不要把权限扩大到所有工作区。
4. 第一次出现安全警告时确认一次；后续本次会话不再逐个批准工具。

`Bypass Approvals` 只跳过工具执行审批，Skill 仍会在业务输入无法确定时提出一次合并问题。不要把 `Autopilot` 作为默认仿真模式；它除了自动批准工具，还可能自动回答 Agent 的澄清问题。

## 工作区 settings.json

如果宿主版本支持 `chat.permissions.default`，可在当前仓库的 `.vscode/settings.json` 中配置默认权限。该文件需要用户主动修改，Skill 不应偷偷写入：

```json
{
  "chat.permissions.default": "autoApprove",
  "chat.mcp.autostart": "newAndOutdated",
  "chat.tools.terminal.enableAutoApprove": true,
  "chat.tools.terminal.outputLocation": "terminal"
}
```

这里的 `autoApprove` 对应 `Bypass Approvals`。`outputLocation: "terminal"` 让本地启动器和进度信息留在集成终端，不把 Python/MCP 过程刷进对话窗口。该设置只应放在可信的仿真工作区，不建议写入用户级全局设置。

不要为了省点击直接在用户级设置中开启：

```json
{
  "chat.tools.global.autoApprove": true
}
```

它会对所有工作区、所有工具关闭关键安全审批，风险远大于本 Skill 的使用范围。

## 仅部分工具需要无感运行时

如果用户不愿启用工作区级 Bypass，可在 Chat 中执行 `Chat: Manage Tool Approval`，对本 Skill 使用的 `radar-sim` MCP Server 选择工作区级 `without approval`，并对稳定启动器的终端操作选择工作区级允许。不要选择全局允许，也不要把任意 PowerShell、Python、删除命令加入宽泛正则白名单。

如果只剩终端命令弹窗，使用 `chat.tools.terminal.autoApprove` 做精确的命令级规则；复合命令必须让每个子命令都匹配允许规则。Skill 本身应优先使用 MCP readiness/bootstrap 和稳定非交互启动器，减少需要终端审批的次数。

## GitHub Copilot CLI

CLI 会把当前目录/仓库范围的授权保存到 `~/.copilot/permissions-config.json`，URL 授权保存到 `~/.copilot/settings.json`。交互会话可以使用 `/allow-all` 或 `/yolo`，启动参数也支持 `--allow-all`/`--yolo`；这些选项会取消所有工具、路径和 URL 的审批，只应在隔离、可信的工作区临时使用，不应写进永久别名。

CLI 更推荐按仓库/目录批准 `radar-sim` MCP 和本次启动器，而不是全局 `--yolo`。Skill 不应自动修改 CLI 权限文件。

## 审批仍然出现时

- 先确认 Chat 权限选择器不是 `Default Approvals`，并确认当前打开的是目标代码仓。
- 检查 `Chat: Manage Tool Approval` 中是否把目标 MCP 工具设为工作区级 `without approval`。
- 如果是 URL 响应审批，按需在 `chat.tools.urls.autoApprove` 中只允许服务域名；不要开放 `*`。
- 如果 Bypass、Autopilot 或自动批准设置不可见，通常是企业管理策略（例如 `ChatToolsAutoApprove`、`ChatToolsEligibleForAutoApproval` 或 `ChatToolsTerminalEnableAutoApprove`）限制。Skill 不能绕过，需管理员调整策略。
- 宿主的强制安全审批不是仿真流程输入。Skill 只提示一次阻塞原因，不重复发起同一操作，也不伪造点击。

官方说明：

- <https://code.visualstudio.com/docs/agents/run/approvals>
- <https://code.visualstudio.com/docs/agents/run/security>
- <https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools>
