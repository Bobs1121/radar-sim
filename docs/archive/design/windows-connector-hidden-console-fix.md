# 修复：radar-sim Windows Connector 周期性弹出黑色终端窗口

- 日期：2026-08-06
- 修改人：opencode（HOZ2WX 会话）
- 影响范围：radar-sim Windows Connector 自启动任务（`start_windows.ps1` / `watch_windows_connector.ps1` / `bootstrap.ps1`）
- 功能影响：无。连接器功能保持不变，仅不再弹出可见控制台窗口。

## 现象

每隔十几秒到几十秒，桌面会闪现黑色终端框。实际包含两类：

1. **一个常驻/反复出现的框**：显示 `light Agent will use Linux control plane: http://<server>:8877/`（来源是 `start_windows.ps1` 第 96 行的 `Write-Host`）。这是连接器 supervisor 窗口，每次 watchdog 误判 supervisor 缺席并重启连接器任务时，就会弹出一个新的可见窗口。
2. **一个快速闪过的框**：`RadarSimConnector-<用户名>-Watchdog` 每 2 分钟触发一次，每次 `powershell.exe` 启动都会瞬间闪现一个控制台窗口，随即自行关闭。

## 根因

radar-sim 在 Windows 上安装时通过 `bootstrap.ps1` 注册了两个计划任务：

| 任务名 | 触发器 | 动作 |
| --- | --- | --- |
| `RadarSimConnector-<用户名>` | 登录时 | `powershell.exe -File start_windows.ps1 -Supervise`（常驻 supervisor） |
| `RadarSimConnector-<用户名>-Watchdog` | 每 2 分钟 | `powershell.exe -File watch_windows_connector.ps1`（健康检查） |

两个任务的 Action 都以 `powershell.exe` 直接启动，且计划任务以交互式令牌（InteractiveToken）运行，因此：

1. Watchdog 每 2 分钟触发一次，每次都会创建一个新的 powershell 控制台窗口（黑色弹框）；
2. 当 watchdog 检测到 supervisor 缺席时，会 `Stop-ScheduledTask` + `Start-ScheduledTask` 重启连接器任务，再次弹出一个新控制台窗口；
3. 叠加其他触发源（如 `HermesWebUI-Watchdog-Minutely`，但它用 `pythonw.exe` 无窗口，不影响），用户感知为"每隔十几秒/几十秒弹黑框"。

证据：`C:\Users\HOZ2WX\AppData\Local\radar-sim\logs\watchdog.log` 显示连接器每 2 分钟被检测缺席并重启；任务计划事件日志（Id=200）显示 `RadarSimConnector-HOZ2WX-Watchdog` 每 2 分钟启动一次。

## 修改内容

### 1. 已注册的计划任务（即时生效）

源码最终采用 `wscript.exe + run_hidden.vbs` 启动两个 PowerShell 脚本。`wscript.exe` 本身不创建控制台，VBS 再以窗口样式 `0` 和 `-WindowStyle Hidden` 启动 PowerShell，避免 PowerShell 解析参数前的瞬时黑框：

- `RadarSimConnector-HOZ2WX`
  - 程序：`wscript.exe`
  - 参数：`"<InstallRoot>\app\scripts\run_hidden.vbs" "<InstallRoot>\app\scripts\start_windows.ps1" -InstallRoot "<InstallRoot>" -Supervise -NoBrowser`
- `RadarSimConnector-HOZ2WX-Watchdog`
  - 程序：`wscript.exe`
  - 参数：`"<InstallRoot>\app\scripts\run_hidden.vbs" "<InstallRoot>\app\scripts\watch_windows_connector.ps1" -InstallRoot "<InstallRoot>" -ConnectorTaskName "RadarSimConnector-HOZ2WX"`

其中 `<InstallRoot>` = `C:\Users\HOZ2WX\AppData\Local\radar-sim`。

修改后已重启 `RadarSimConnector-HOZ2WX` 任务，新的 supervisor 以隐藏窗口运行（PID 45568，2026-08-06 13:25 启动），agent 链路（powershell → python agent）正常。

### 2. 安装脚本（防止重装后回归）

在 `bootstrap.ps1` 的任务注册段将两个 Action 都改成 `wscript.exe`，并由同目录的 `run_hidden.vbs` 统一启动 PowerShell。Connector 打包器已显式允许并强制包含该 VBS 文件，避免安装包缺文件。

- 安装副本：`C:\Users\HOZ2WX\AppData\Local\radar-sim\app\scripts\bootstrap.ps1`（第 269、298 行附近）
- 源码仓库：`D:\RamboStar\idea\radar-sim\scripts\bootstrap.ps1`（第 285、314 行附近）

这样即使以后重新安装连接器（`bootstrap.ps1 -RegisterStartup`），注册出来的两个任务也不会创建可见控制台窗口。

## 验证

- 修改后观察桌面：不再出现周期性黑色终端框。
- 连接器运行状态：任务状态 `Running`，进程树 `powershell.exe -WindowStyle Hidden ... → python.exe (venv) rsim.py agent → python.exe (Python312) rsim.py agent` 保持存活。
- watchdog 仍正常按 2 分钟触发（任务计划事件 Id=200 可见），但不再误判 supervisor 缺席、不再反复重启（`watchdog.log` 无新增 restart 记录），因此不再产生新窗口。
- 运行中的 supervisor 进程 `MainWindowHandle = 0`（无可见窗口）。
- 触发时间线：修复于 13:25 生效；13:29 之后 watchdog 每 2 分钟触发均正常通过，无弹窗。
- watchdog 日志仍正常写入：`C:\Users\HOZ2WX\AppData\Local\radar-sim\logs\watchdog.log`。

## 回滚

如需恢复原行为（显示控制台窗口），重新注册任务时去掉 `-WindowStyle Hidden` 即可；或手动重建任务：

```powershell
# 示例（去掉 -WindowStyle Hidden 即为原样）
$installRoot = "C:\Users\HOZ2WX\AppData\Local\radar-sim"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument `
  "-NoProfile -ExecutionPolicy Bypass -File `"$installRoot\app\scripts\start_windows.ps1`" -InstallRoot `"$installRoot`" -Supervise -NoBrowser"
Set-ScheduledTask -TaskName "RadarSimConnector-HOZ2WX" -Action $action
```

## 备注

- 最终选择 VBS 而不是 `pythonw.exe`，因为 Connector 的监督入口仍是 PowerShell；VBS 只负责无窗口启动，不改变监督、重连和日志逻辑。
- `HermesWebUI-Watchdog-Minutely`（每 1 分钟）与此现象无关，其使用 `pythonw.exe` 不会产生可见窗口。
