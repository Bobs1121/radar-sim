# Windows 一键连接（首版）

## 用户看到什么

普通用户不需要理解 Agent、Control Plane、Server URL、Agent ID 或 Token。

1. 在 Linux Web 中点击“连接这台 Windows 电脑”。
2. 下载 `RadarSim-连接本机.cmd`。
3. 双击运行，看到“本机已经连接”后回到 Web 继续任务。

默认 `light` 模式只承担本机路径访问、Selena 编译、数据/产物上传，随后由 Linux 调度 Cluster 仿真。只有用户选择本地仿真能力时，Web 才下载 `full` 模式入口。

## 服务端发布闭环

`scripts/linux_deploy.sh` 会在启动服务前构建 `dist/rsim-windows-connector.zip`。同一个 `serve-v1` 进程提供：

- `GET /api/v1/windows-connector/connect.cmd?mode=light|full`：给普通用户双击运行。
- `GET /api/v1/windows-connector/install.ps1?mode=light|full`：内部和管理员入口。
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

## 当前限制和安全边界

- 本 Sprint 按产品决定关闭登录，`scripts/linux_deploy.sh` 默认使用 `--insecure-no-auth`，只允许部署在受信内网。可设置 `RSIM_INSECURE_NO_AUTH=0` 恢复 Bearer 认证。
- 认证开启时，一键连接接口返回 `409 connector_pairing_required`，不会把长期 Token 写入下载脚本或业务 YAML。下一 Sprint 需要实现短期、单次使用的设备配对协议后再开放。
- SHA-256 能发现传输损坏和服务端包文件被意外替换；HTTP 无法抵抗同时篡改包与响应头的主动中间人。跨不受信网络时必须使用 HTTPS 或后续加入签名清单。
- 公司禁用 `winget` 且没有 Python 时，用户仍需从公司软件中心安装 Python，这是当前唯一保留的本机运行时前置条件。
