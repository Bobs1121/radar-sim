# radar-sim V2 用户指南

> 文件名为历史兼容名称；本指南适用于所有使用 Selena 的项目，不按 OD25/OVRS/Xpeng/BYD 区分流程。

## 1. 使用前准备

每次任务只准备：

- 已有 Selena 文件夹，或代码仓与 Selena 编译脚本；
- 与 Selena 匹配的 Runtime XML；
- 一个 MF4 文件或包含 MF4 的目录；
- 可选 MatFilter、Adapter、Radar source 和结果保存根目录。

如果选择编译，Visual Studio 和项目自身编译依赖由用户环境提供。radar-sim 检查脚手架需要的路径和脚本，并从可选软件包脚本提取依赖提示，不自动安装 VS。

## 2. Web 使用

1. 打开部署方提供的 Linux Web 地址；
2. 首次输入稳定 NTID；
3. 导入 YAML 或填写表单；
4. 若任务需要读取 Windows 路径，页面显示“一键连接本机”；下载并双击一次；
5. 看到“本机已连接”后提交任务；
6. 在任务中心查看四个业务步骤、Stage 日志、诊断和每条数据结果；
7. 浏览器下载 ZIP。若同一任务由 Connector/SDK 接收结果，则接收设备还会使用 `result.path/<job_id>`；纯浏览器不能直接写任意本地目录。

页面刷新后任务仍在 Linux 控制数据库中。Connector 暂时离线时任务等待自动重连，不需要重新提交或重装。

## 3. SDK 使用

安装仓库包后：

```python
from radar_sim_sdk import RadarSimClient

client = RadarSimClient(
    "http://10.190.171.44:8877",
    user="user-your-ntid",  # 无认证内网模式；正式模式改用 token
)

validation = client.validate_run(config)
job = client.submit_run(config, auto_transfer=True)
final_job = client.wait(job.id, timeout=3600, poll_interval=2)
diagnosis = client.diagnosis(job.id)
manifest = client.manifest(job.id)
result_zip = client.download_job_result(job.id)
```

`submit_yaml(path)` 与 Web 导入同一 YAML。Windows 文件需要 Connector；Linux SDK 调用机本地文件由 SDK 执行签名直传。已有 Selena 和数据均为 Cluster 可读共享路径时不需要 Connector。

## 4. 配置示例

### 已有 Selena + 本地仿真

```yaml
schema_version: "2.0"
selena:
  source: existing
  existing_path: "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/dc_tools/selena/core/RelWithDebInfo"
  runtime_xml: "C:/tools/Runtime.xml"
  code_path: ""
  branch: ""
  selena_build_script: ""
  package_build_script: ""
data:
  path: "D:/data/run/one.MF4"
simulation:
  target: local
  source: ""
  adapter_file: ""
  mat_filter: ""
result:
  path: "D:/simulation-results"
```

### 本地编译 + Cluster 仿真

```yaml
schema_version: "2.0"
selena:
  source: build
  existing_path: ""
  code_path: "D:/workspace"
  branch: "feature/example"
  selena_build_script: "D:/workspace/path/to/build_selena.bat"
  package_build_script: ""
  runtime_xml: "D:/data/Runtime.xml"
data:
  path: "D:/data/batch"
simulation:
  target: cluster
  source: ""
  adapter_file: ""
  mat_filter: ""
result:
  path: ""
```

分支只用于差异提醒。系统编译当前工作区，不检查 diff、不清仓、不 reset、不自动切换。

## 5. 路径行为

| 配置情况 | 行为 |
|---|---|
| Windows 本地输入 + local | 原地读取，无上传 |
| Windows 本地输入 + cluster | Connector 直写 Cluster 数据面 |
| Linux SDK 本地输入 + cluster | SDK 直写 Cluster 数据面 |
| Cluster 可读共享输入 + cluster | 零复制登记 |
| 远端输入 + local 且 Windows 可读 | 原地读取共享路径 |
| 远端输入 + local 且 Windows 不可读 | 当前明确 `source_to_local_unavailable`，不经 Linux 绕传 |

正斜杠、反斜杠和 UNC 在读取设备上规范化。Linux 不尝试直接打开 `D:/...`。

## 6. 自动推导

- 已有 Selena：从选择文件夹定位唯一 Selena.exe 和同目录 DLL；不猜项目。
- 编译产物：脚本推导优先；编译后在授权 build 根确认实际 Selena.exe。
- MatFilter：显式值优先；空值在代码仓/已有产物邻近受控位置确定性选择。
- Radar source：显式值优先；支持 RadarFL/FR/RL/RR/FC；空值读取 Runtime/MF4 元数据。

系统在日志中展示最终选择及推导证据，但不会要求用户登记项目。

## 7. 状态和错误

- “等待连接本机”：运行一次统一 Connector；已安装则等待自动重连。
- “组件需要更新”：点击一键更新，不丢失身份和路径绑定。
- build 失败：看编译日志和依赖提示；框架不会替用户安装 VS。
- Selena simulation failed：属于仿真内部结果；任务日志保留受限尾部，批量继续其他数据。
- source_to_local unavailable：当前执行设备读不到远端输入，改为可读共享路径或 Cluster 目标。
- result unavailable：外围收集/Manifest 有问题，Job 不应显示成功；联系部署方检查结果 Stage。

## 8. 新用户验收清单

1. 新浏览器首次输入 NTID；
2. 下载 Connector 并一次安装成功；
3. 重启/重新登录后自动连接且无黑窗；
4. 提交已有 Selena + local，拿到结果目录和 ZIP；
5. 提交已有 Selena + cluster，确认源到 Cluster 直传；
6. 提交 build 任务，确认只执行选择脚本；
7. 两个用户同时提交，互相看不到任务和结果；
8. 批量含一条失败时其余继续，Manifest 分列成功/失败。
