# Linux 控制面 Server 部署指南

## 架构（模式 A：仅 cluster 仿真链路）

本部署把**控制面 server**放在 Linux，且**只支持 cluster 仿真链路**。仿真在 SZHRADAR 集群节点上执行（节点上预装了 selena/MATLAB/Qt/Boost），用户的 Windows 电脑**无需安装任何繁重依赖**——只需 Python + PyYAML + `rsim agent`。

```
Linux server (本指南)            Windows 用户机 (最小依赖)
┌─────────────────────────┐      ┌─────────────────────────────────┐
│ rsim server serve       │◄────HTTP (X-Rsim-User 头路由)─────────│ rsim agent --server-url http://<linux>:8877
│  --allowed-task-types   │      │ - 仅认领 cluster.run task        │
│    cluster.run          │      │ - 打包 job 到集群共享路径         │
│ - job/task 调度         │      │ - 提交 SZHRADAR 集群执行          │
│ - 每用户 SQLite DB 隔离  │      │ - 日志/结果回传 Linux server     │
│ - 日志/结果存储          │      └─────────────────────────────────┘
│ - 多 agent 并发 claim    │                  │
└─────────────────────────┘                  ▼
                                   SZHRADAR 集群节点（预装 selena/MATLAB/Qt）
```

**关键策略**：Linux server 用 `--allowed-task-types cluster.run` 启动，**拒绝** `local.check` / `local.build_selena` / `local.run_sim` 三种 task（HTTP 400）。这三种 local task 需要本机完整工具链（MATLAB/Qt/Boost/VS），不属于 Linux 服务模式——需要本地编译/仿真的用户请走[模式 B：Windows 本机仓一键部署](../README.md)。

**Linux 上不跑 selena、不编译、不仿真。** cluster 仿真在集群节点上跑，Linux 只负责调度/存储/路由。

控制面 server 是**纯 Python stdlib**(无 PyYAML/asammdf/openai),所以 Linux 上不需要装第三方包,只要有 Python 3.9+。

## 部署方式 A:zipapp(最简,推荐试跑)

`dist/rsim_server.pyz` 是单文件 zipapp,拷到任意 Linux 机器直接跑:

```bash
# 1. 构建(在开发机,本仓库根目录)
python scripts/build_server_pyz.py
# → 产出 dist/rsim_server.pyz (~16KB)

# 2. 拷到 Linux 服务器
scp dist/rsim_server.pyz user@linuxsrv:/opt/rsim/

# 3. 在 Linux 上跑(设 RSIM_HOME 明确 DB 位置;不设则默认 ~/.rsim)
ssh user@linuxsrv
export RSIM_HOME=/var/lib/rsim       # 可选,控制 DB 落盘位置
# --allowed-task-types cluster.run: 模式 A,只接受 cluster 仿真任务
python3 /opt/rsim/rsim_server.pyz server serve --host 0.0.0.0 --port 8877 \
  --allowed-task-types cluster.run
```

`RSIM_HOME` 决定每用户 DB 路径:`$RSIM_HOME/results/_control_<user>.db`。未设时 fallback 到 `~/.rsim/results/`(zipapp 内部路径不可写,代码已处理)。

**防火墙**:Linux 若开 UFW,需放行端口:`sudo ufw allow 8877/tcp`。否则外部(Windows agent)连不上,本机 curl 却正常——这是最常见的"本机通、跨机不通"原因。

## 部署方式 B:Docker

```bash
# 在开发机构建镜像
docker build -t rsim-server .

# 在 Linux 服务器跑(挂卷持久化 DB)
docker run -d --name rsim-server \
  -p 8877:8877 \
  -v rsim-data:/var/lib/rsim \
  --restart unless-stopped \
  rsim-server
```

镜像基于 `python:3.11-slim`,零 pip install(纯 stdlib)。`RSIM_HOME=/var/lib/rsim` 已在镜像内设置,挂卷 `rsim-data` 让 job/日志跨重启保留。

## Windows 用户机:连 server + 提交集群仿真（最小依赖）

模式 A 下 Windows 用户机**不需要** MATLAB/Qt/Boost/VS/selena.exe——仿真在集群节点上跑。本机只需 Python 3.9+ + PyYAML + rsim 代码仓（用于 `rsim agent` 和 `rsim cluster` 链路）。clone 代码仓后只需 `pip install -r requirements.txt`（或仅 `pip install PyYAML`）。

> **后续优化**：计划提供 `scripts/build_agent_pyz.py` 把 agent + cluster 链路打成单文件，届时 Windows 端连代码仓都不用 clone。当前暂以 clone 仓方式接入。

```bat
:: Windows 上(各用户自己机器)
set RSIM_USER=alice
rsim agent --server-url http://<linux-server-ip>:8877
```

agent 默认 capability 为 `cluster.run`（+ `tcc.*`），**只认领 cluster 仿真任务**。流程:
1. 向 Linux server 注册(带 `X-Rsim-User: alice` 头)
2. 轮询认领 alice 名下 queued 的 `cluster.run` task
3. 本机把 job 打包到集群共享路径，提交 SZHRADAR 集群执行
4. 把日志流式回传、结果回传 Linux server

不同用户(`alice`/`bob`)的 job 完全隔离——各自的 DB,互不可见。

## 用 web 控制台(可选)

浏览器用户也可起 `rsim web` 指向 Linux server,UI 投 cluster job 走 server:

```bat
set RSIM_USER=alice
rsim web --server-url http://<linux-server-ip>:8877
```

前端零改动,web_control 适配层把请求转发到远程 server(见 `core/web_control.py` 的 `set_remote_client`)。模式 A 下 web 的 build/sim 按钮（local task）会被 server 白名单拒绝；cluster 提交正常。

## 用户标识与隔离

- 用户身份:`RSIM_USER` 环境变量 > OS 登录名 > `default`
- 经 `X-Rsim-User` HTTP 头路由到 `$RSIM_HOME/results/_control_<user>.db`
- **注意**:头是信任型的,可信内网可接受;严格鉴权待后续。`--user` 可填别人名字看到别人 job,见已知风险。

## 已知限制

1. **远程模式无内置 agent**:`rsim web --server-url` 只投 job 到 Linux server,执行靠 Windows 那台机器上跑的 agent。若无人起 agent,job 永远 queued。
2. **本机配置仍读本机**:远程模式下 `/api/check`、`/api/cluster/*`、config 校验仍读 web 本机(Windows)的配置——校验/集群状态是本机视角,只有任务执行在 Windows agent。
3. **无鉴权**:`X-Rsim-User` 可伪造,可信内网可接受。
4. **并发瓶颈**:`ControlService` 单把 RLock 串行所有 DB 操作,适合中小规模多用户;超大 agent 数需评估(见 Task #53)。

## 跨端 job 投递:仅 cluster.run,用 OS 无关标识

**模式 A 下 server 只接受 `cluster.run`**。投 `local.check` / `local.build_selena` / `local.run_sim` 会被白名单拒绝（HTTP 400）。需要本地编译/仿真的用户走[模式 B](../README.md)。

**关键约束**:job payload 里的路径是**投 job 的人所在机器的路径**,但执行在另一台机器(Windows agent → 集群节点)。跨机时直接传路径会失败——Linux 路径在 Windows/集群上不存在,反之亦然。

| payload 字段 | 本机投本机 | 跨机投递(Linux投→Windows agent→集群) |
|--------------|-----------|-------------------------------|
| `config_path`(`--config-path`) | ✅ 可用 | ❌ 不要用——改用 `--project` |
| `project`(`--project`) | ✅ | ✅ **用这个**——agent 从自己机器 `config/projects/<project>/` 解析 |
| `input_mf4`(`--input-mf4`) | ✅ 可用 | ❌ 不要用——改用 `--dataset` |
| `dataset`(`--dataset`) | ✅ | ✅ **用这个**——agent 从本地 config 的 `simulation.datasets` 解析出共享路径上的数据 |

跨机投 cluster 仿真 job:
```bash
# 在 Linux 上投,用 project + dataset 名(OS 无关),agent 本地解析成集群共享路径
rsim server create-job cluster.run --project ovrs25 --dataset cbna_0117 --profile cloud-build
```

`--config-path` 和 `--input-mf4` 只在"本机投本机执行"时用(路径在同一台机器上有效)。server **不做路径校验**(路径属于 agent 侧),所以路径错了不会立刻报错——要等 agent 认领执行时才暴露,注意看 agent 日志。

## 验证部署

部署后在 Linux 上快速自检(不需要 selena):

```bash
# server 起来了?
curl http://localhost:8877/api/jobs?limit=5 -H "X-Rsim-User: smoke"

# 投 cluster job 看路由(应 201)
curl -X POST http://localhost:8877/api/jobs \
  -H "Content-Type: application/json" -H "X-Rsim-User: smoke" \
  -d '{"job_type":"cluster.run","payload":{"project":"ovrs25","dataset":"cbna_0117"}}'

# 投 local task 应被白名单拒绝(应 400)
curl -X POST http://localhost:8877/api/jobs \
  -H "Content-Type: application/json" -H "X-Rsim-User: smoke" \
  -d '{"job_type":"local.run_sim","payload":{"project":"ovrs25"}}'
```

返回 `{"jobs":[...]}` 或 cluster job 字典即说明 server 正常；local task 返回 400 即说明白名单生效。完整端到端(Windows agent + 真实数据)见 Task #54。
