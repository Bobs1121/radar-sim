# 发布部署：Linux 统一入口与 Windows 统一连接组件

发布入口已经收敛为同一个 `serve-v1` 进程。它同时提供 Web、REST/SDK、Job/Stage 调度、Windows Agent 接口和平台 Cluster executor。legacy `server serve`、单独的 `rsim web` 和 `rsim_server.pyz` 只保留兼容用途，不再作为 Linux 发布默认入口。

## 当前用户部署矩阵

| 用户环境 | 安装 | Selena | 仿真 |
|---|---|---|---|
| 没有 Windows | 不安装客户端，直接打开 Linux Web 或调用 SDK | 填写 Cluster 可访问的已有 Selena 文件夹、Runtime 和配置 | Cluster |
| Windows 有本地路径、编译或仿真环境 | 一键安装一个 `unified` 连接组件 | 按任务读取已有 Selena，或执行用户提供的编译脚本 | 本地或 Cluster；Cluster 准备完成后不依赖用户电脑在线 |
| Selena、Runtime、配置和数据都在 Cluster 可达共享位置 | 不安装客户端 | 直接填写这些路径 | Cluster |

`已有 Selena + Cluster` 不需要 Visual Studio 或编译脚本依赖。如果所有 Selena、Runtime、MatFilter 和数据路径都能被 Cluster 访问，用户不安装 Windows 组件；如果路径只在 Windows 本地，则统一连接组件只做文件访问和点对点准备，不会强行要求编译依赖。

用户不选择 `light/full`。这两个名称只保留为内部节点兼容字段；统一连接组件按任务动态领取本地阶段，Linux/Gateway 负责 Cluster 运行期。Linux 永不声明 Selena build capability。

## 当前 Windows 连接方式

1. 在 Linux Web 的“连接这台电脑”提示中下载并双击一次连接脚本；SDK 集成也可调用 `download_windows_connector_for_run()` 得到同一脚本。
2. 安装器自动绑定当前 Web/SDK owner 和 Linux 地址，注册登录自启、异常重连和单实例监督；用户不填写 Agent ID、模式、令牌或内部项目名。
3. 之后 Web 与 SDK 共用这一条持久连接。任务若是本地仿真，由连接组件读取本机环境；任务若是 Cluster，连接组件只把本地 Selena/Runtime/Adapter/MatFilter/数据直接准备到 Cluster，Linux 只传递计划、进度和 Manifest。
4. 无 Windows 用户直接在 Linux Web/SDK 中填写 Cluster 可访问路径；浏览器不能读取另一台电脑的本地路径，这是浏览器边界，不是 Linux 数据中转方案。

连接器脚手架依赖的处理顺序是：复用用户 Python 中符合版本的 `PyYAML/httpx/pydantic`；缺少时使用发布包内的 Windows wheel；最后才使用用户已有的 pip 配置、企业包源和代理。Selena、Visual Studio、runtime/DLL、Cluster 仿真引擎不在 Agent 安装范围内，由用户或 Cluster 环境负责。

当前验收服务为 `http://10.190.171.44:8877`，生产部署请以部署方提供的 `serve-v1` 地址为准。当前 Sprint 运行在受信内网的无认证模式；不应暴露到公网。

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

## 历史 full/light 安装参数（仅兼容旧部署，不作为普通用户入口）

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
