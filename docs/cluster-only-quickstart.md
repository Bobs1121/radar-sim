# 模式 A 快速开始：Linux 服务 + Windows 接入 cluster 仿真

> 适用场景：Linux 上提供仿真服务（前端页面 + 后端接口），Windows 用户**无需安装 MATLAB/Qt/Boost/VS**，通过集群完成数据仿真。Selena 用集群共享路径上的预置包（`source: path`），或本机编译的（`source: build`）。
>
> 需要本地编译 + 仿真的用户走[模式 B（Windows 本机仓一键部署）](../README.md#模式-bwindows-本机仓一键部署)。

## 架构

```
Linux server                         Windows 接入机（最小依赖）
┌────────────────────────────┐       ┌──────────────────────────────┐
│ rsim server serve          │◄─HTTP─│ rsim agent --server-url ...   │
│   --allowed-task-types     │       │   (默认 capability: cluster.run)│
│     cluster.run            │       │   打包 job → 提交 SZHRADAR 集群  │
│ + rsim web（前端，可选）    │       └──────────────────────────────┘
└────────────────────────────┘                     │
                                                   ▼
                                    SZHRADAR 集群节点（预装 selena/MATLAB/Qt）
```

- **Linux**：纯 stdlib control server（零 pip install），`--allowed-task-types cluster.run` 拒绝 local task。
- **Windows**：Python 3.9+ + PyYAML + 本仓库（用于 `rsim agent` 和 `rsim cluster` 链路）。**不需要** MATLAB/Qt/Boost/VS/selena.exe。
- **Selena 来源**：profile 里 `selena.source: path` 指向集群共享 selena.exe（推荐，零本机依赖），或 `source: build` 用本机编译的（需模式 B 工具链）。
- **数据**：dataset 指向集群 worker 可见的 UNC 共享路径（如 BYD_SR），免 copy。

## Linux server 部署

见 [`docs/linux-server-deploy.md`](linux-server-deploy.md) 完整指南。核心：

```bash
# zipapp 方式（最简）
python3 /opt/rsim/rsim_server.pyz server serve --host 0.0.0.0 --port 8877 \
  --allowed-task-types cluster.run
```

可选：Linux 上同时跑 `rsim web` 对外提供前端页面（用 `--server-url` 指向自己，或内置）。

## Windows 接入（最小配置）

### 1. clone 仓 + 装 Python 依赖

```bat
git clone <repo> radar-sim
cd radar-sim
python -m venv .venv
.venv\Scripts\activate
pip install PyYAML
:: cluster 链路只需 PyYAML；如需 analyze/diff/ask 再装 asammdf/rich/openai
```

> **后续优化**：`scripts/build_agent_pyz.py` 打包后连仓都不用 clone。当前暂用 clone 方式。

### 2. 确认项目 config 指向集群共享 Selena

`config/projects/ovrs25/config.yaml` 已自带 cluster 配置。模式 A 关键是有一个 `source: path` 的 cluster profile，指向集群共享 selena.exe。例如用历史共享包：

```yaml
cluster:
  workspace_root: "\\\\abtvdfs2.de.bosch.com\\ismdfs\\loc\\szh\\Isilon3\\Cluster"
  software_path: "\\\\szhradar01\\cluster_software"
  # ...其他 cluster 字段
  profiles:
    - name: "byd-ovrs-bl01v7-er-shared"
      selena_exe: "\\\\abtvdfs2.de.bosch.com\\ismdfs\\loc\\szh\\Isilon3\\Cluster\\BYD_OVRS\\BL01V7_ER\\BYD_OVRS_Selena_Master\\selena.exe"
      runtime_xml: "\\\\abtvdfs2.de.bosch.com\\ismdfs\\loc\\szh\\Isilon3\\Cluster\\BYD_OVRS\\BL01V7_ER\\runtime_1r1v.xml"
```

或用顶层 unified profile：

```yaml
profiles:
  - name: "cloud-shared"
    backend: "cluster"
    selena:
      source: "path"                              # 用共享 Selena，不需本机编译
      exe: "\\\\abtvdfs2.de.bosch.com\\...\\selena.exe"
    data:
      copy: false                                 # 数据已在 UNC 共享，免 copy
```

**不需要 `local.yaml`**（无 MATLAB/Qt/Boost/VS 路径要填）。

### 3. 环境校验（cluster-only 视角）

```bat
:: doctor 自动识别为 cluster-only（无 local profile + 无工具链路径），只跑 cluster 检查
rsim --project ovrs25 doctor

:: 或显式指定
rsim --project ovrs25 check --backend cluster
rsim --project ovrs25 doctor --backend cluster
```

校验内容：集群 UNC 共享可达、共享 selena.exe 可达、runtime_xml 可达、profile 合法。**不会**报 VS/MATLAB/Qt/Boost 缺失（那些是 local 工具链，模式 A 不需要）。

### 4. 起 agent 连 Linux server

```bat
set RSIM_USER=alice
rsim agent --server-url http://<linux-server-ip>:8877
:: 默认 capability: cluster.run（+ tcc.*），只认领 cluster 仿真任务
```

### 5. 提交 cluster 仿真

任一方式：

```bat
:: A. CLI 直投（写本地 DB，仅演示；跨机用 curl 或 web）
rsim server create-job cluster.run --project ovrs25 --dataset BYD_SR --profile byd-ovrs-bl01v7-er-shared

:: B. 浏览器 web（指向 Linux server）
set RSIM_USER=alice
rsim web --server-url http://<linux-server-ip>:8877
:: 前端提交 cluster 仿真（build/sim 按钮会被白名单拒绝，正常）
```

```bash
# C. curl 跨机投递
curl -X POST http://<linux>:8877/api/jobs \
  -H "Content-Type: application/json" -H "X-Rsim-User: alice" \
  -d '{"job_type":"cluster.run","payload":{"project":"ovrs25","dataset":"BYD_SR","profile":"byd-ovrs-bl01v7-er-shared"}}'
```

agent 认领 → 打包 job 到集群共享路径 → 提交 SZHRADAR → 集群节点跑 selena → 结果回传 Linux server。

## 配置精简要点

| 项 | 模式 A 需要？ | 说明 |
|----|:---:|------|
| `local.yaml` | ❌ | 无本机工具链路径要填 |
| `environment.matlab_root/qt_path/boost_root/selena_env_path/vs_version` | ❌ | 集群节点上有 |
| `profiles[].selena.source: path` + `exe` | ✅ | 指向集群共享 selena.exe |
| `cluster.workspace_root / software_path` | ✅ | 集群 UNC 路径（config.yaml 已带） |
| `simulation.datasets[]` 指向 UNC | ✅ | BYD_SR 等共享数据集 |
| 本机编译的 selena.exe | ❌ | source=path 时不需要 |

## Selena 分支与数据路径

config 里的 `build.selena_branch` / `profile.selena.selena_branch` 用于**本机编译**场景（source=build）——校验 selena.exe 是否对应目标分支。模式 A 用 `source: path`（共享 Selena 包）时，**分支校验不适用**（共享包的分支由维护者保证），可留空。

数据路径：`simulation.datasets[].input_dir` 指向集群 worker 可见的 UNC 共享（如 BYD_SR）。本地盘数据（如 `D:/data/...`）worker 看不到，需 `data.copy: true` 或 `--copy-data` 复制到共享路径。

## 故障排查

- **agent 认领后 job 失败**：看 `rsim server get-logs <job_id>`，常见原因：① 共享 selena.exe 路径错（worker 找不到）② dataset 不在 UNC 上（worker 看不到）③ runtime_xml 路径错。
- **doctor 报 cluster UNC 不可达**：本机没连 Bosch 内网/VPN，或 UNC 路径变更。
- **投 local task 返回 400**：server 启用了 `--allowed-task-types cluster.run` 白名单，local task 走模式 B。
