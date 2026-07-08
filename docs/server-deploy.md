# 控制平面服务器部署（Linux）

> 目标：在 Linux 服务器 `10.190.171.44` 上跑 control server，本机 Windows 跑 agent 调本机 `rsim`。
> server 只需 Python 3 标准库（无 PyYAML/asammdf）。
> 支持多用户：每个用户的 job/日志互不可见（独立 SQLite DB）。

## 0. 分发方式（两种任选）

### 方式 A：zipapp 单文件（推荐给 server）

在本机构建一个 14KB 的 `.pyz`，拷到服务器即可，无需 pip/无需仓库：

```bash
# 本机构建
python scripts/build_server_pyz.py
# 产物：dist/rsim_server.pyz（stdlib 单文件）

# 拷到服务器
scp dist/rsim_server.pyz you@10.190.171.44:~/

# 服务器上直接跑（任意 python3.9+）
ssh you@10.190.171.44
python3 rsim_server.pyz server serve --host 0.0.0.0 --port 8877
```

### 方式 B：pip install（推荐给 Windows 本机全栈）

```bash
# 内网 devpi / 共享盘 wheel（离线）
pip install radar-sim-4.0.0-py3-none-any.whl[full]   # Windows 全栈（asammdf+openai+PyYAML）
pip install radar-sim-4.0.0-py3-none-any.whl[control] # server/agent 轻量（仅 PyYAML）
```

构建 wheel：`python setup.py bdist_wheel`（产物在 `dist/`）。

## 1. 服务器侧：部署代码

把 radar-sim 仓库放到服务器（git clone 或 scp）。server 只需要这几个文件，但整个仓库放上去最省事：

```bash
# 在服务器上（10.190.171.44）
ssh you@10.190.171.44
cd ~
git clone <你的仓库地址> radar-sim   # 或用方式 A 的 .pyz
cd radar-sim
```

**最小文件集**（如果不想传整个仓库，只传这些即可）：
- `rsim.py`
- `cli/__init__.py`、`cli/server.py`
- `core/__init__.py`、`core/control_service.py`、`core/control_http.py`、`core/user.py`

## 2. 服务器侧：启动 control server

```bash
cd ~/radar-sim
python3 rsim.py server serve --host 0.0.0.0 --port 8877
```

- `--host 0.0.0.0`：监听所有网卡，允许本机连
- `--port 8877`：默认端口
- 控制 DB：`results/_control.db`（自动创建）
- 看到 `Radar Sim control server: http://0.0.0.0:8877/` 即成功

**后台常驻**（推荐用 nohup 或 systemd）：
```bash
nohup python3 rsim.py server serve --host 0.0.0.0 --port 8877 \
  > ~/radar-sim/server.log 2>&1 &
echo $! > ~/radar-sim/server.pid
```

**防火墙**：确认 8877 端口开放（内网一般直连，无需改）：
```bash
# 如服务器有 firewalld
sudo firewall-cmd --add-port=8877/tcp --permanent && sudo firewall-cmd --reload
# 或 ufw
sudo ufw allow 8877/tcp
```

## 3. 本机侧：启动 agent 连服务器

```bash
# 本机 Windows（在 radar-sim 目录）
python rsim.py agent --server-url http://10.190.171.44:8877 --agent-id my-pc
```

agent 会注册能力（local.check / local.build_selena / local.run_sim / cluster.run / tcc.*），轮询服务器认领任务，调本机 `rsim` 执行，日志回传服务器。

**验证连通**：
```bash
# 本机 curl 服务器健康检查
curl http://10.190.171.44:8877/health
# 应返回 {"ok": true}
```

## 4. 投递任务（任一处）

```bash
# 在服务器或任何能连服务器的机器上
python rsim.py server create-job local.check --project ovrs25 --backend local
python rsim.py server create-job local.build_selena --project ovrs25 --mode RelWithDebInfo --clean
python rsim.py server create-job local.run_sim --project ovrs25 --input-mf4 D:/data/case.MF4 --dry-run

# 查状态 / 日志 / 取消
python rsim.py server get-job <job_id>
python rsim.py server get-logs <job_id>
python rsim.py server cancel <job_id>
```

本机 agent 会自动认领并执行，日志实时写回服务器 DB。

## 5. 本机 web 连远程 server（可选）

目前 `rsim web` 默认内置 server+agent。如果想用浏览器看远程 server 的 job：
- 直接 `rsim server get-job` / `get-logs` CLI 查
- 或后续给 web 加 `--server-url` 参数桥接远程（当前未实现，按需再加）

## 运维

- **DB 路径**：每用户独立 `results/_control_<user>.db`（默认用户 `default` → `_control.db`）。SQLite WAL，自动创建
- **改端口**：`--port` + 防火墙同步开
- **看日志**：`tail -f ~/radar-sim/server.log`
- **停服务**：`kill $(cat ~/radar-sim/server.pid)`
- **agent 重连**：agent 断线自动重试，server 上 queued job 持久化，agent 重启后自动认领

## 多用户隔离

单 Linux server 多用户同时用，job/日志**互不可见**：

- **用户标识**：环境变量 `RSIM_USER`（缺省取 OS 用户名）
- **DB 隔离**：每个 user 一个 `_control_<user>.db`，物理隔离，A 看不到 B 的 job/日志/agent
- **HTTP 传递**：agent/web 发请求带 `X-Rsim-User` 头，server 按头路由到对应 user 的 DB
- **agent 认领**：agent 只在自己 user 的 DB 内认领 task，不会抢别人的

```bash
# 用户 alice 启动 agent
RSIM_USER=alice rsim agent --server-url http://10.190.171.44:8877

# 用户 bob 投 job（互不可见）
RSIM_USER=bob rsim server create-job local.check --project ovrs25 --backend local
# alice 看不到 bob 的 job，反之亦然
```

**RSIM_HOME 重定向**：设 `RSIM_HOME=~/.rsim` 让 results/DB/local.yaml 全部独立于代码仓库，适合共享代码安装的多用户机器：
```bash
RSIM_HOME=/home/alice/.rsim RSIM_USER=alice rsim agent --server-url http://...
```

## 安全提示

`0.0.0.0` 监听意味着内网任何人都能投 job 让你本机跑。当前无鉴权（`X-Rsim-User` 头可伪造），仅适合可信内网。如需鉴权后续可加 token + user 校验。
