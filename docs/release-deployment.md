# 发布部署：Linux 统一入口与 Windows full/light

发布入口已经收敛为同一个 `serve-v1` 进程。它同时提供 Web、REST/SDK、Job/Stage 调度、Windows Agent 接口和平台 Cluster executor。legacy `server serve`、单独的 `rsim web` 和 `rsim_server.pyz` 只保留兼容用途，不再作为 Linux 发布默认入口。

## 用户部署矩阵

| 用户环境 | 安装 | Selena | 仿真 |
|---|---|---|---|
| 没有 Windows | 不安装客户端，直接打开 Linux Web 或调用 SDK | 选择已有 Runtime Bundle | Cluster |
| 有 Windows 编译环境，不做本地仿真 | `light` | 本机授权代码路径编译，上传 Runtime Bundle | Cluster；上传完成后不依赖用户电脑在线 |
| 有 Windows 本地仿真环境 | `full` | 本机编译或已有 Runtime Bundle | 本地；需要平台 Cluster 时仍由 Linux 中央入口调度 |

`light` 的策略不是 UI 约定：Agent capability policy 会拒绝 `simulation.local`、Cluster runtime/gateway、run/collect/finalize 等能力。`full` 才能声明本地仿真能力。Linux 永不声明 Selena build capability。

## Linux 一键部署

要求 Python 3.10+、git、curl。脚本首次运行创建 venv、安装 `.[v5-server]`、生成仅当前 Linux 用户可读的 Bearer 认证文件，然后启动一个统一进程：

```bash
bash scripts/linux_deploy.sh --yes
bash scripts/linux_deploy.sh status
bash scripts/linux_deploy.sh test
```

首次为 Windows Agent 配置时，由管理员在受信终端显式查看 owner/user token 和 Agent token：

```bash
bash scripts/linux_deploy.sh credentials
```

凭证不能放进仿真 YAML、工单或普通任务日志。多用户/多 Agent 场景由管理员扩展 `RSIM_HOME/http-auth.json`，每个 Agent 使用唯一 `agent_id + token`。

Docker 使用同一个 `serve-v1` 入口，必须挂载认证文件；不提供认证文件时容器不会以未认证的公网服务降级启动：

```bash
docker build -t radar-sim-control .
docker run --rm -p 8878:8878 \
  -v rsim-data:/var/lib/rsim \
  -v "$PWD/http-auth.json:/run/secrets/rsim-auth.json:ro" \
  radar-sim-control
```

### Linux 共享盘映射

用户任务 YAML 只填写原始数据路径，例如 Windows 可访问的 UNC 路径；不要让用户填写 Linux 挂载点或选择“本地/公盘”。Linux 管理员在每台控制服务器配置一次部署级映射：

```bash
mkdir -p "$RSIM_HOME/config"
cp config/deployment.example.yaml "$RSIM_HOME/config/deployment.yaml"
```

`deployment.yaml` 中的 `cluster.linux_mount_map` 把 worker 使用的 UNC 前缀映射到 Linux 已挂载的 CIFS 目录。该覆盖层对所有内部项目识别结果生效，并在项目配置之后合并；它不属于 Web/SDK 导入导出的用户配置。也可用 `RSIM_DEPLOYMENT_CONFIG=/run/secrets/rsim-deployment.yaml` 指向外部只读文件。

部署前应同时验证挂载和目标数据目录，而不只是检查 Windows 可访问性：

```bash
mountpoint /mnt/cluster
find /mnt/cluster/loc/szh/Isilon2/OverseaData -maxdepth 1 -type d
```

## Windows 一键安装

light 连接 Linux，需管理员分配的 `ServerUrl`、`AgentId`、Agent token 和同 owner 的 API token：

```powershell
.\scripts\bootstrap.ps1 -Mode light `
  -ServerUrl http://linux-rsim:8878 `
  -AgentId alice-laptop `
  -AgentToken <agent-token> `
  -ApiToken <user-token> `
  -Start
```

安装器会先读取 Linux `/api/v1/health` 的 `authentication_required`。当前可信内网测试服务关闭认证时，只需 `ServerUrl + AgentId`，安装器不会生成或保存无意义令牌：

```powershell
.\scripts\bootstrap.ps1 -Mode light `
  -ServerUrl http://10.190.171.44:8877 `
  -AgentId alice-laptop `
  -Start
```

full 有两种控制面，Agent 能力相同：

- `ControlPlane=linux`（日常推荐）：full Agent 连接 Linux 统一入口，同一 Web/YAML 可选择本地或 Cluster 仿真。
- `ControlPlane=local`（默认离线模式）：本机启动 loopback `serve-v1`，支持本地编译和仿真，但不伪装 Linux Cluster executor。

离线本地 full：

```powershell
.\scripts\bootstrap.ps1 -Mode full -Start
```

连接 Linux 的 full：

```powershell
.\scripts\bootstrap.ps1 -Mode full -ControlPlane linux `
  -ServerUrl http://linux-rsim:8878 `
  -AgentId alice-full -AgentToken <agent-token> -ApiToken <user-token> -Start
```

当前 Sprint 的 `full + local` 仅监听 loopback，不启用登录或访问令牌，打开 Web 即可测试。`full + linux` 和 `light + linux` 是否需要令牌由 Linux 健康接口返回的认证模式决定；当前 `10.190.171.44:8877` 可信内网测试入口无需令牌，正式部署默认需要管理员分配的用户/Agent token。

安装器持久化的是部署模式、服务地址和 Agent 标识；连接 Linux 时另行持久化受限凭证。它不会创建或要求用户理解内部 project。代码路径、Selena 分支/编译脚本、数据路径、Runtime Bundle、Adapter 和 MatFilter 仍通过统一 Web/YAML 配置。远端凭证单独保存在 `%LOCALAPPDATA%\radar-sim` 且 ACL 收紧，不写入用户 YAML。

Visual Studio 由用户自行安装，Windows Agent 不下载或安装 VS。安装阶段检查是否存在受支持的 C++ compiler；具体任务的 `environment_check` 再根据用户选择的 Selena 脚本和本机 VS 做精确校验，并且只对 R2D2 的 `-vs`/`VS_POSTFIX` 做可见、幂等的脚本适配。其余 TCC、CMake、MinGW、Python、Qt、Boost 等依赖从软件包编译脚本及其 workspace-local batch 调用链解析，并在安全的非交互安装入口存在时自动修复。若软件包脚本旁存在可识别的 `GEN_PAD_PARAMS.bat` 且生成头缺失，Agent 使用已安装的 TCC Perl 在任务子进程内补齐 PATH 并执行 workspace-local PAD generator；不运行交互式整包编译，也不修改全局 PATH。

后续启动：

```powershell
.\scripts\start_windows.ps1             # 前台 Agent
.\scripts\start_windows.ps1 -Background # 后台 Agent
```

`full + local` 的本机服务不伪装 Linux Cluster executor；需要同入口同时选择本地和 Cluster 时，安装为 `full + linux`。完全没有 Windows 的用户不运行这些脚本，直接使用 Linux Web/SDK 和已有 Runtime Bundle。
