# Windows 一键连接（首版）

## 用户看到什么

普通用户不需要理解 Agent、Control Plane、Server URL、Agent ID 或 Token。

1. 在 Linux Web 中点击“连接这台 Windows 电脑”。
2. 下载 `RadarSim-连接本机.cmd`。
3. 双击运行，看到“本机已经连接”后回到 Web 继续任务。

公开入口只有一个“统一连接组件”。它会按任务自动承担本机路径访问、Selena 编译、数据/产物直传、Cluster 调度准备或本地仿真；用户不选择 `light/full`，也不需要知道这两个历史内部节点名。服务器内部仍保留旧模式字段，只用于兼容已有部署。

这不是“每个任务安装一次”的临时程序：安装成功后会在当前 Windows 用户下保存服务地址和用户范围，注册登录自启/断线重连。之后同一台电脑上的 Web 和 SDK 任务都复用这条连接，不要求用户反复填写 Agent、服务器地址或路径绑定。只有换电脑、换 Linux 服务地址或主动卸载时才需要重新连接；新任务的代码/数据路径由任务 YAML 提供，连接组件自动做一次性授权和健康检查。

## 服务端发布闭环

`scripts/linux_deploy.sh` 会在启动服务前构建 `dist/rsim-windows-connector.zip`。同一个 `serve-v1` 进程提供：

- `GET /api/v1/windows-connector/connect.cmd?mode=unified`：给普通用户双击运行。
- `GET /api/v1/windows-connector/install.ps1?mode=unified`：内部和管理员入口。
- `mode=light|full` 仅为旧版本兼容参数，不应出现在用户文案或 YAML 中。
- `GET /api/v1/windows-connector/package.zip`：只包含白名单中的运行文件，不包含工作树其他文件、日志、输出和凭证。

入口按浏览器正在访问的 Linux 地址生成；反向代理部署可设置 `RSIM_PUBLIC_URL`。因此不会再把 Windows 错连到 `127.0.0.1`。

## 安装和恢复行为

- 不依赖 Windows 预先存在 radar-sim 仓库；应用包从 Linux 同源下载。
- 下载后使用服务端 `X-Content-SHA256` 与本机 `Get-FileHash` 比对，再解压安装。
- Python 3.10+ 缺失时，优先用 `winget` 为当前用户静默安装 Python 3.12；被公司策略阻止时，提示从公司软件中心或 Python 官方入口安装后重试。
- Visual Studio 属于用户管理的软件，只检测并提示，不自动安装；实际编译前仍会再次校验和适配。
- 优先注册当前用户的 Windows 计划任务，登录后自动启动、异常退出自动恢复；策略禁止计划任务时退回用户启动目录。
- 后台监督进程负责初次网络不可用时持续重连，并用用户级互斥锁避免重复启动。
- 安装完成必须由 `/api/v1/capabilities` 确认对应 Windows 能力上线，不能只依靠安装探测注册判断成功。
- Linux 服务地址会自动加入连接进程的 `NO_PROXY`，避免公司代理错误接管内网 IP。
- 连接器的轮询、节点注册、路径绑定和点对点传输使用 Python 标准库；`PyYAML`、`httpx`、`pydantic` 只作为编译/本地仿真的可选扩展。首次安装时如果公司包源、代理或网络不可用，页面会显示“本机仍可连接”，安装继续完成，已有 Selena + Cluster 任务不被阻断。
- 如果可选扩展安装失败，连接器会把缺少的包写入 `install.json`，编译或本地仿真任务在执行前返回稳定的 `connector_dependency_missing` 诊断和修复提示，不再让新用户在安装阶段看到裸 `Traceback`。
- 电脑重启后，用户登录 Windows 即由已注册的计划任务启动连接；不需要重新下载、重新配对或重新填写 YAML。
- 电脑关机、睡眠、尚未登录或网络隔离时，Linux/Web/SDK 不能远程启动本机进程或唤醒电源；任务保持“等待连接/自动重连”，电脑恢复并登录后由连接组件继续。

## 当前限制和安全边界

- 本 Sprint 按产品决定关闭登录，`scripts/linux_deploy.sh` 默认使用 `--insecure-no-auth`，只允许部署在受信内网。可设置 `RSIM_INSECURE_NO_AUTH=0` 恢复 Bearer 认证。
- 认证开启时，一键连接接口返回 `409 connector_pairing_required`，不会把长期 Token 写入下载脚本或业务 YAML。下一 Sprint 需要实现短期、单次使用的设备配对协议后再开放。
- SHA-256 能发现传输损坏和服务端包文件被意外替换；HTTP 无法抵抗同时篡改包与响应头的主动中间人。跨不受信网络时必须使用 HTTPS 或后续加入签名清单。
- 公司禁用 `winget` 且没有 Python 时，用户仍需从公司软件中心安装 Python，这是当前唯一保留的本机运行时前置条件。

## 不使用 Web 时的 SDK 入口

SDK 只是 Linux 控制面的调用客户端，不会把编译器或 Selena 仿真引擎偷偷安装到调用机。依赖按调用位置区分：

| SDK 调用位置 | `existing + cluster` 且输入可访问 | 需要本机 Windows 路径/编译/本地仿真 |
|---|---|---|
| Linux/服务器 | `python -m pip install "radar-sim[sdk]"`；共享路径或 Linux 可读路径即可，完全不需要 Windows 组件。SDK 进程会按 TransferPlan 从 Linux 调用机直接写 Cluster 数据面 | Linux 不读取 `C:/`、`D:/`；若输入只在 Windows，本任务应从存放文件的 Windows 电脑一次性连接统一组件，或先放到 Cluster 可访问共享位置 |
| Windows 集成产品 | 同样安装 `radar-sim[sdk]`；SDK 会将本地已有 Selena 目录、Runtime、可选资产和 Cluster 数据按需直传 | `source=build` 或 `target=local` 时，先一次性连接统一组件；SDK 进程本身不承担持续编译/本地仿真调度 |

在没有 Web 的 SDK 集成中，优先使用 SDK 下载同一个一次性连接入口（当前可信内网、未开启认证的服务）：

```python
from pathlib import Path
from radar_sim_sdk import RadarSimClient, UserRunConfig

with RadarSimClient("http://linux-rsim:8877") as client:
    launcher = client.download_windows_connector_for_run(
        UserRunConfig.from_yaml(Path("run.yaml")),
        Path(r"C:\Temp\RadarSim-Connect-Windows.cmd"),
    )
    print(launcher)
```

然后只在实际的 Windows 电脑上执行一次下载的 `.cmd`：

```powershell
& "C:\Temp\RadarSim-Connect-Windows.cmd"
```

如果集成方不方便调用 SDK，也可以用同样的 `Invoke-WebRequest` 请求下载脚本；两种方式生成的内容完全相同。

完成一次连接后，SDK 只需要提交原来的 YAML：

```python
from radar_sim_sdk import RadarSimClient

with RadarSimClient("http://linux-rsim:8877") as client:
    job = client.submit_yaml("run.yaml", idempotency_key="issue-123-run-1")
```

SDK 未传 `user` 时会自动生成同一 OS 用户和机器稳定的 `sdk-...` 身份，连接程序与后续任务使用同一 scope；不会退回 Linux 服务器进程账号，也不会与其他默认 SDK 调用者混用任务和 Agent。

认证开启的正式部署不把长期 Token 写进 YAML；应使用管理员提供的短期配对入口。当前 Sprint 的无认证内网入口已验证上述 SDK/脚本路径，认证配对协议仍是后续发布项。
