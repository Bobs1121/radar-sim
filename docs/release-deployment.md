# radar-sim V2 发布与部署

> 当前公共版本只有 project-free V2。历史 `light/full`、V1、project/profile/recipe 和独立 Web/Agent 部署方式不再是发布入口；需要追溯时使用 Git 历史。

## 1. 唯一 Linux 入口

Linux 只运行一个 `serve-v1` 控制面进程，同时提供 Web、REST/SDK、Job/Stage 调度、统一 Windows Connector 接口和 Cluster executor。Linux 不编译 Selena、不执行 Windows 本地仿真，也不接收大文件正文。

当前受信内网验收地址：`http://10.190.171.44:8877`。当前不可变 release：`/home/hoz2wx/radar-sim-93947c8`；用户级 `radar-sim-v1.service` 为 `active/running`、`NRestarts=0`。结果水位已显式配置为 `RSIM_RESULT_MIN_FREE_BYTES=1073741824`。

注意：Cluster executor/gateway 心跳在线不等于外部 Cluster 可提交。此次验收曾发现服务机到 `SZHRADAR01:8123` 的 Manager XML-RPC 端口关闭；恢复后真实 readiness 返回 `cluster_ready`、`can_submit=true`。如果 Manager 再次不可达，服务必须保持 blocked 并返回可重试错误，不允许绕过检查。

开发或临时 Linux 环境可以用仓库脚本启动一个独立实例：

```bash
bash scripts/linux_deploy.sh --yes
bash scripts/linux_deploy.sh status
bash scripts/linux_deploy.sh test
```

该脚本默认使用 `~/radar-sim` 和端口 `8878`，不会切换当前 `10.190.171.44:8877` 的生产 user-level systemd release。脚本参数通过 `RSIM_INSTALL_DIR`、`RSIM_HOME`、`RSIM_PORT`、`RSIM_PUBLIC_URL`、`RSIM_DEPLOYMENT_CONFIG` 和认证环境变量外置。

生产发布必须创建新的不可变 release 目录，候选测试通过且无活动任务后，更新 `radar-sim-v1.service` 的 `WorkingDirectory`，执行 `systemctl --user daemon-reload` 和受控重启；保留上一目录作为回滚，不在运行目录原地覆盖。切换后必须重新检查 `active/running`、`NRestarts=0`、health、capabilities、Connector 包和 Job 列表。

`--insecure-no-auth` 模式下，`X-Rsim-User` 仍然是 owner 路由标签。检查 Connector 能力和用户 Job 时必须使用与 Web/SDK 相同的稳定 owner，例如：

```bash
curl -H 'X-Rsim-User: user-<ntid>' http://127.0.0.1:8877/api/v1/capabilities
curl -H 'X-Rsim-User: user-<ntid>' 'http://127.0.0.1:8877/api/v1/jobs?limit=100'
```

省略该 header 会按服务进程的 Linux 用户落到另一个 owner，可能把真实在线的 Windows Connector 误显示为 `count=0`；这不是有效的用户侧验收。

## 2. project-free 硬约束

- Web、YAML、SDK、REST、Job、Stage、TransferPlan 和仿真指令中都没有业务项目选择。
- 不读取 `config/projects/*`，不按路径猜出项目后套用 adapter、recipe 或专用参数。
- 只从用户提供的 Selena 文件夹、编译脚本、Runtime、数据、MatFilter/Adapter 和文件元数据做通用推导。
- 推导不足时只请求缺失的具体输入，不请求项目名。
- 新 Selena 工程接入不允许增加项目注册表或项目专用 DAG。

## 3. 用户设备矩阵

| 输入与目标 | 用户设备要求 | 行为 |
|---|---|---|
| 全部输入为 Cluster 可读路径，目标 Cluster | 无需 Connector | Linux 登记引用并调度 Cluster |
| Windows 本地输入，目标 Cluster | 一次安装统一 Connector | 源电脑直写 Cluster 数据面，Linux 只下发计划 |
| Linux SDK 调用机本地输入，目标 Cluster | 安装 SDK，无需 Windows | SDK 调用机直写 Cluster 数据面 |
| Windows 本地输入，目标本地 | 一次安装统一 Connector | 数据不上传，Connector 准备外围参数并下发 Selena 指令 |
| 选择本地编译 | Windows + 用户自己的编译环境 | Connector 执行用户给定脚本、确认 Selena.exe 与 DLL，再按目标路由 |

用户只安装一个统一 Connector，不选择能力档位。Web 下载“连接这台电脑”，或 SDK 调用 `download_windows_connector_for_run()`；安装一次后保存稳定 owner、服务地址、登录自启、单实例和断线重连。普通服务端补丁不要求用户重复安装。

Connector 包端点：

- `/api/v1/windows-connector/connect.cmd?mode=unified`
- `/api/v1/windows-connector/package.zip`

发布必须验证包 SHA-256 和 Range `206`，避免企业网络中断后整包重下。

## 4. 共享路径与点对点传输

用户 YAML 始终填写原始路径，不选择“本地/共享盘”。Linux 管理员只在部署配置中维护 Cluster 客户端目标命名空间与 Linux 探测命名空间，例如 UNC 目标根和已挂载 CIFS 探测根。

TransferPlan 只保存 owner、Job/Stage、资源角色、相对路径、大小、校验和、目标引用和进度；正文不经过 Web/API。相同逻辑请求幂等复用同一计划，失败、取消、过期或输入变化才签发新计划。

## 5. 环境依赖边界

- Visual Studio、Selena、Runtime/DLL 和实际仿真环境由用户或 Cluster 提供。
- Connector 安装器优先复用用户现有 Python/包，再使用发布包内 wheel，最后使用用户企业包源/代理。
- Connector 只解决连接、路径、传输、外围配置、指令下发和结果收集；不把仿真工具链做成框架依赖。
- 编译失败时展示用户脚本输出与可操作诊断，不按项目修改脚本。

## 6. 发布门禁

1. 工作区无未提交修改，提交已推送到发布分支。
2. 全仓测试零失败；平台特异测试必须在对应平台解释并单独留证。
3. 候选 Linux release 运行 TransferPlan/API/Cluster Stage 门禁。
4. 切换前确认无 queued/running/waiting Job。
5. 切换用户级 systemd unit，确认 health、Web/API、executor/gateway、`active/running`、`NRestarts=0`。
6. 验证 Connector ZIP SHA-256、Range `206` 和现有 Connector 自动恢复轮询。
7. 真实任务必须分别记录外围框架、Cluster 基础设施和 Selena 内部逐输入结果，不互相伪装。

## 7. 安全和未发布边界

当前 `--insecure-no-auth` 仅限受信内网；`X-Rsim-User` 是可伪造的 owner 路由标签，不是认证。对不受信用户开放前必须启用 Bearer/SSO 并验证跨 owner 拒绝。

首版未发布：远端输入到本地的 `source_to_local`、Cluster 结果反向直传并解压到任意用户设备、独立 MCP/Skill 包、关机或睡眠设备远程唤醒。当前 SDK 和 owner-scoped ZIP 是后续 AI 接入的稳定底座。
