# 端到端 Runbook：Linux server（cluster-only）+ Windows agent + 集群仿真

> 本 runbook 验证**模式 A**架构：**Linux 只跑控制面 server 且仅支持 cluster 仿真链路**，Windows agent（最小依赖，无 MATLAB/Qt/Boost/VS）把 job 提交到 SZHRADAR 集群执行，多用户隔离。这是 Task #54。
>
> 需要本地编译/仿真（local.build_selena / local.run_sim）的场景走[模式 B：Windows 本机仓一键部署](../README.md)，不在本 runbook 范围。

## 前置条件

- 一台 Linux 机器（能被 Windows 机器通过 HTTP 访问），装 Python 3.9+
- 一台 Windows 机器（你本地），装 Python 3.9+ + PyYAML + rsim 代码仓（用于 `rsim agent` 和 `rsim cluster` 链路）——**无需 MATLAB/Qt/Boost/VS/selena.exe**
- SZHRADAR 集群可达，集群共享路径（`\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster`）可写
- 两台机器网络互通（Linux 的 8877 端口对 Windows 开放）

## Step 1：Linux 上起控制面 server（cluster-only）

```bash
# 在 Linux 机器上
# 方式 A：zipapp（最简，stdlib-only）
scp dist/rsim_server.pyz user@linuxsrv:/opt/rsim/
ssh user@linuxsrv
export RSIM_HOME=/var/lib/rsim
mkdir -p $RSIM_HOME/results
# --allowed-task-types cluster.run：模式 A，只接受 cluster 仿真任务
python3 /opt/rsim/rsim_server.pyz server serve --host 0.0.0.0 --port 8877 \
  --allowed-task-types cluster.run

# 方式 B：源码（开发/调试更灵活，可跑测试）
# 同步代码（Windows 无 rssync，用 tar over ssh）：
#   tar -czf - --exclude='__pycache__' --exclude='results' --exclude='dist' \
#       --exclude='*.MF4' --exclude='*.db' cli core config docs platforms plugins scripts tests rsim.py setup.py \
#     | ssh user@linuxsrv 'mkdir -p ~/radar-sim && cd ~/radar-sim && tar -xzf -'
ssh user@linuxsrv
cd ~/radar-sim
mkdir -p ~/rsim_data
nohup python3 rsim.py server serve --host 0.0.0.0 --port 8877 \
  --db-path ~/rsim_data/cross.db --allowed-task-types cluster.run \
  > ~/rsim_server.log 2>&1 &

# 方式 C：Docker（镜像 CMD 已带 --allowed-task-types cluster.run）
docker run -d --name rsim-server -p 8877:8877 -v rsim-data:/var/lib/rsim rsim-server
```

server 起来后自检（在 Linux 本机）：
```bash
curl http://localhost:8877/health                # 期望: {"ok":true}
curl http://localhost:8877/api/jobs?limit=5 -H "X-Rsim-User: smoke"
# 期望: {"jobs":[]}

# 白名单生效？投 local task 应 400
curl -X POST http://localhost:8877/api/jobs \
  -H "Content-Type: application/json" -H "X-Rsim-User: smoke" \
  -d '{"job_type":"local.run_sim","payload":{"project":"ovrs25"}}'
# 期望: {"error":"task_type 'local.run_sim' not allowed on this server ..."} (400)
```

> **⚠️ 投 job 到远程 server 必须用 HTTP POST `/api/jobs`**（curl / `RemoteControlClient` / web `--server-url` 模式）。
> `rsim server create-job` CLI 只写**本地** DB（它绑定 `--db-path`），不会把 job 投到远程 server。
> 跨机投递示例（模式 A 只投 cluster.run）：
> ```bash
> curl -X POST http://<linux>:8877/api/jobs \
>   -H "Content-Type: application/json" -H "X-Rsim-User: alice" \
>   -d '{"job_type":"cluster.run","payload":{"project":"ovrs25","dataset":"cbna_0117"}}'
> ```
> `rsim server get-job/get-logs/list-agents/reclaim` 同理只读/写本地 DB；远程查询用 curl 或在 Linux 本机执行 CLI（`--db-path` 指向 server 的 DB）。

## Step 2：Windows 上起 agent 连 Linux server

```bat
:: 在 Windows 本机（用户 alice）
set RSIM_USER=alice
rsim agent --server-url http://<linux-server-ip>:8877
```

agent 日志应显示注册成功。在 Linux 上验证 agent 注册到位：
```bash
# 方式 A — HTTP（任意机器）
curl http://localhost:8877/api/agents -H "X-Rsim-User: alice"
# 期望: {"agents":[{"agent_id":"agent-alice-<hostname>","status":"idle",...}]}

# 方式 B — CLI（在能读到 DB 的机器上）
rsim server list-agents --db-path <control.db 路径>
```
agent 投递任务期间 `status` 会变为 `busy`，`current_task_id` 指向正在跑的 task。

## Step 3：投 cluster 仿真 job（集群执行，Linux 调度）

> **模式 A 不支持 local.build_selena / local.run_sim**（被 server 白名单拒绝）。需要本地编译的场景走[模式 B](../README.md)。模式 A 下 selena 运行时由集群节点提供（`profile.selena.source=path` 指向集群共享 selena 包，或 `--copy-selena` 从某处复制）。

任一方式投 cluster job：

**方式 A — Windows CLI 直投（写本地 DB，仅演示；跨机用 Step 1 的 curl）：**
```bat
set RSIM_USER=alice
rsim server create-job cluster.run --project ovrs25 --dataset cbna_0117 --profile cloud-build ^
  --copy-selena
```

**方式 B — 浏览器 web：**
```bat
set RSIM_USER=alice
rsim web --server-url http://<linux-server-ip>:8877
:: 浏览器打开 web，提交 cluster 仿真 → job 进 Linux server (alice DB)
:: 注意：web 的 build/sim 按钮（local task）会被 server 白名单拒绝
```

投完后，Windows agent 应在 3s 内认领 `cluster.run` task，把 job 打包到集群共享路径并提交 SZHRADAR 集群。

## Step 4：监控日志/状态（从 Linux server 读）

```bash
# Linux 上（或任何能访问 server 的机器）
JOB_ID=job_xxx  # 从 create-job 返回拿
curl "http://localhost:8877/api/jobs/$JOB_ID" -H "X-Rsim-User: alice"
curl "http://localhost:8877/api/jobs/$JOB_ID/logs?since=0&limit=500" -H "X-Rsim-User: alice"
```
日志是 agent 从 Windows 流式回传的（cluster 打包/提交/集群执行的 stdout）。

## Step 5：真实数据 cluster 仿真（CBNA_0117）

投 cluster job（用已验证的 CBNA 数据，dataset 名指向集群共享路径上的数据）：
```bat
rsim server create-job cluster.run --project ovrs25 --dataset cbna_0117 ^
  --profile cloud-build --copy-selena
```

Windows agent 认领 → 把 job 打包到集群共享路径 → 提交 SZHRADAR 集群 → 集群节点跑 selena → 输出 out.MF4 → 日志/结果回传 Linux。

> ⚠️ **不要给 agent 投递的 cluster 任务加 `--select`**。`--select` 是交互式（要 stdin 输入文件号），agent 子进程 stdin 无连接，`input()` 立即 EOF → "No files selected" → returncode 1。投递自动化任务用 `--input-mf4 <path>` 或 `--dataset <name>`（不带 select，跑该 dataset 全部文件）。见 `docs/KNOWN_ISSUES.md` KI-3.2。

成功判据（与 cloud_batch_0117 一致）：
- task status = succeeded, returncode = 0
- result.out_size > 100MB
- 日志末尾 "Thank you for using Selena and have a nice day!"

## Step 6：验证多用户隔离

第二个人在另一台 Windows（用户 bob）起 agent + 投 job：
```bat
set RSIM_USER=bob
rsim agent --server-url http://<linux-server-ip>:8877
```

验证：
```bash
# alice 看不到 bob 的 job
curl "http://localhost:8877/api/jobs?limit=10" -H "X-Rsim-User: alice"  # 只有 alice 的
curl "http://localhost:8877/api/jobs?limit=10" -H "X-Rsim-User: bob"     # 只有 bob 的
# alice 直接查 bob 的 job_id → 404
```

## Step 7：验证 cancel 与 dead-agent 回收

**cancel：**
```bash
curl -X POST http://localhost:8877/api/jobs/cancel \
  -H "Content-Type: application/json" -H "X-Rsim-User: alice" \
  -d '{"job_id":"job_xxx"}'
```
agent 下次心跳发现 `cancel_requested=true`，终止本机子进程。

**dead-agent 回收：** 强杀 Windows agent（模拟崩溃），task 卡 running。在 Linux 上：
```bash
rsim server reclaim --stale-after 300 --max-attempts 3
# 或在 server 机直接: python3 rsim_server.pyz server reclaim --stale-after 300
```
task 重排队，新 agent 起来后可重新认领。

## 完成判据

- [ ] Linux server 起来（`--allowed-task-types cluster.run`），Windows agent 注册成功
- [ ] 投 local task 被 server 白名单拒绝（400）
- [ ] cluster.run job 投递 → Windows agent 打包提交 → 集群执行 → 日志回传 Linux → succeeded
- [ ] CBNA cluster job 真实跑通（out.MF4 >100MB）
- [ ] alice/bob 互不可见（隔离 404）
- [ ] cancel 能终止 Windows agent 子进程
- [ ] dead-agent reclaim 能重排队卡住的 task

任一项失败，记录现象 + server/agent 日志，进 KNOWN_ISSUES。
