# 已知问题（后续维护）

## KI-1: 云端仿真（Cluster）端到端 — 已验证通过（历史 job），当前 cluster 排队

**状态**：✅ 端到端链路已由历史 job `cloud_batch_0117`（2026-07-01 18:16）真实跑通。当前用相同数据+配置新提交的 `e2e_pass_cbna_0117` 因 cluster worker 排队未即时跑完（非 rsim bug）。

### 已验证的成功 job（铁证）
`cloud_batch_0117`（Cluster job_id=10325，worker=szhradar27）：
- 数据：`\\...\BYD_SR\12-5-26_CBNA\12-5-26_CBNA\Gen5_2009-01-01_04-10_0117.MF4`（CBNA，RadarFL/CFL）
- `successfull = 1`（worker 脚本返回成功）
- `out MF4 = 537269680B`（537MB 完整输出）
- selena.log 277 行，末尾 "Thank you for using Selena and have a nice day!"
- `error_message =`（空）
- profile: cloud-build（copy-selena，UNC 数据原地引用）

同批次 `cloud_smoke_v5_radarfl`、`cloud_batch_0116` 同样成功（537MB 输出，selena 正常结束）。

### 之前的误判（已纠正）
- ❌ 原误判"simulation_state=4 = 失败"。实际 `simulation_state=4` 在成功 job 里也出现（`cloud_batch_0117` 就是4），它不是失败码，是"completed"类状态
- ❌ 原误判"Returncode=-1 = selena 启动即崩"。实际 selena 跑完了（selena.log 320 行）
- ❌ 原误判"out_size=0 = 无产出"。实际 out_size 是 manager 的某个统计字段，和 worker 实际产出的 out.MF4（537MB）无关
- 真正的成功判据：`successfull=1` + out.MF4 大小 >100MB + selena.log 正常结束

### 6/30 失败 job 的真实原因
`smoke_fr_20260630_safeout`（job_id=10319）失败是因为用了**不同数据**：`28-4-26_DMS_FCW/Vehicle_FR5CP...MF4`（RadarFR/CFR），这条数据缺 runtime 期望的信号（`g_Golf_Fct_Hmi_RunnableHmi_internalstates`）。**数据选型问题，非代码 bug**。用 CBNA 数据（RadarFL/CFL）则成功。

### 端到端链路验证（2026-07-03 复盘）
| 环节 | 状态 | 证据 |
|------|------|------|
| prepare | ✅ | Config.cfg + SIMULATION_RADAR_SIM.py + selena(88MB) + assets，0 warnings |
| submit | ✅ | xmlrpc `addSimulation` value=1，SZHRADAR01:8123 可达 |
| worker 执行 | ✅ | selena.log 277 行，init24s + sim50s，正常结束 |
| 产出取回 | ✅ | result.ini + selena.log + out.MF4(537MB) 拷回共享盘 |
| dependency_paths | ✅ | cluster check 全 OK，3个 UNC DLL 目录（MATLAB/Boost/Qt）可达 |

### 当前状态（2026-07-03）
- `e2e_pass_cbna_0117`：用与成功 job 相同的 CBNA_0117 数据+配置，已 xmlrpc 提交（value=1），cluster worker 排队中（cluster 负载高，smoke_e2e_v2/v3 也在排队）
- 等 cluster worker 空闲后会跑完，预期 success（配置与 cloud_batch_0117 一致）


## KI-2: 控制面并发模型与 dead-agent 回收

**状态**：✅ 对当前规模（单 server + 数十 agent）健全；已补 dead-agent 回收与 busy_timeout。**已在真实 Linux（Ubuntu 22.04 + Python 3.10）上验证 server 端全链路。**

### 架构边界（重要）
控制面 server 是**纯 Python stdlib**（`core/control_service.py` / `control_http.py` / `user.py` / `cli/server.py`），可跨平台跑在 Linux。但 **build/sim 执行链全 Windows**（`cmd /c *.bat`、`selena.exe`、MSVC、`lib64-msvc-14.0`）。Linux 只做调度/存储/路由，不做编译/仿真。见 `docs/linux-server-deploy.md`。

### 并发分析结论
| 环节 | 设计 | 评价 |
|------|------|------|
| claim 原子性 | `ControlService._lock`(RLock) 串行 + SQL `WHERE status='queued'` 条件 UPDATE 双保险 | 健全 |
| SQLITE_BUSY | 单进程单 RLock 下访问串行，busy 几乎不可能；已加 `PRAGMA busy_timeout=5000` 兜底 | 已加固 |
| 用户隔离 | 每用户独立 SQLite DB（`_control_<user>.db`）+ `X-Rsim-User` 头路由 | 健全 |
| dead-agent 回收 | **已补** `reclaim_stale_tasks`：running task 若 assigned agent 心跳超时→重排队；超 `max_attempts`→failed | 已加固 |
| RLock 瓶颈 | 一把 RLock 串行所有 DB 操作 | 中小规模无问题，超大 agent 数（数百+）需评估 |

### dead-agent 回收用法
agent 崩了会留下 task 卡在 `running` 永不结束。定期跑（cron 或手动）：
```bash
rsim server reclaim --stale-after 300 --max-attempts 3
# stale-after: agent 心跳超过该秒数视为失联（默认 300）
# max-attempts: 重排队超过该次数则判 failed（默认 3，0=无限重试）
```
也可在 server 进程内周期调用 `service.reclaim_stale_tasks(...)`。

### 相关代码
- `core/control_service.py:claim_next_task`（290-）—— 原子认领
- `core/control_service.py:reclaim_stale_tasks`（348-）—— dead-agent 回收
- `core/control_service.py:_conn`（48-）—— busy_timeout
- `core/control_service.py:_data_root`（17-）—— zipapp fallback（修复 NotADirectoryError）
- `cli/server.py` `server reclaim` 子命令
- `tests/test_reclaim.py` —— 回收测试（5 例）
- `tests/test_server_pyz.py` —— zipapp + data_root 测试

### 真实跨机端到端验证（2026-07-03,Linux server + Windows agent）
**完整链路实跑通过**:Linux(Ubuntu 22.04, Python 3.10)起 `rsim_server.pyz server serve --port 8080` → Windows 本机起 `rsim agent --server-url http://10.190.171.44:8080`(设 `NO_PROXY=10.190.171.44` 绕过 Bosch 代理)→ Linux 投 job → Windows agent 跨机认领 → 本机执行 → 日志/结果回传 Linux。

验证矩阵:
| job 类型 | payload | 结果 |
|---------|---------|------|
| `local.check` | `--project ovrs25` | ✅ succeeded,"All environment checks passed" |
| `local.run_sim` (dry-run) | `--dataset CBNA_23-4-26-local --dry-run` | ✅ succeeded,3 文件 dry-run,selena 定位/数据解析/paramconfig/runtime.xml 全通 |
| `local.run_sim` (真实) | `--dataset CBNA_23-4-26-local` | ✅ **succeeded,3 个 MF4 全跑通,产出 364.6MB out.MF4,selena "Thank you...have a nice day"** |

跨机端到端测试中暴露并修复的真实问题:
1. **zipapp `_data_root()` 在 .pyz 内 `NotADirectoryError`** — fallback 改为 `~/.rsim`,有测试
2. **Bosch 代理拦截直连内网 IP** — agent 设 `NO_PROXY=<linux-ip>` 绕过(已写进部署文档)
3. **selena.exe 仿真跑完后主进程不退出** — `rsim run` 内置 `stall_timeout`(默认 180s)已处理;agent 层另加 queue-reader + `proc.poll()` 检测,确保即使子进程持有 stdout 管道,task 也能完成不卡死
4. **`local.run_sim` payload 的 `limit` 未被 `rsim run` 消费** — `--limit 1` 实际跑了 3 个文件。非阻塞(仿真成功),待修

Linux UFW 需放行端口(8080 已放行;8877 需 `sudo ufw allow 8877/tcp`)。


## KI-3: 2026-07-07 loopback 端到端回归新发现

**状态**：本次为 loopback（本机 pyz server + agent）回归，非真实跨机。控制面全链路通过（见 HANDOFF.md「端到端 loopback 回归」）。下列两项为新发现的运维问题，未修。

### KI-3.1: `rsim server get-logs` 在中文 Windows 终端崩溃 ✅ 已修复

**现象**：DB 里存的 task 日志含中文（如 check 输出 "环境检查"），`rsim server get-logs` CLI 打印到 cp936 终端时报 `Error: 'charmap' codec can't encode characters in position ...`，无法输出。

**根因**：`cli/server.py:_print_json` 用 `print(json.dumps(..., ensure_ascii=False))`，输出到 Windows charmap stdout 时编码失败。agent 端已修（`cli/agent.py` 子进程强制 UTF-8，见 KI-2/历史记录），但 server CLI 端的打印路径未做 UTF-8 强制。

**修复**（2026-07-07）：`cli/server.py` 加 `_ensure_utf8_stdout()`，在 `_print_json`（被 get-job/get-logs/create-job/list-agents/reclaim 共用）输出前 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。`list-agents` 测试用非 ASCII metadata 验证。现在无需 `PYTHONUTF8=1` 也能正常打印。

### KI-3.2: agent 投递 `--select` 任务必失败（约束已文档化）

**现象**：`local.run_sim` payload 带 `select: true` 时，agent 执行 `rsim run --select` 会因交互式 `input()` 读到 EOF（agent 子进程 stdin 无连接）立即返回 "No files selected"，returncode 1。

**根因**：`--select` 设计就是交互式选文件，不适合无人值守的 agent 执行。

**规避**：agent 投递的 run_sim 任务**不要用 `--select`**，直接传 `--input-mf4 <path>` 或 `--dataset <name>`（不带 select，跑该 dataset 全部文件）。

**状态**：约束已写入 `docs/e2e-linux-windows-runbook.md` Step 5（醒目 ⚠️ 标注）。代码层未阻断（agent 仍会执行 `--select` 命令并如实回传失败），由投递方避免。

### 已修复（2026-07-07 回归中）

- **server create-job project 丢失 bug**：`rsim server create-job --project ovrs25` 和 `--payload-json '{"project":"x"}'` 的 project 曾被空 CLI 默认值覆盖，导致跨机投递无法传 project（违反「跨机投递优先传 project/dataset」约束）。`cli/server.py:_run_create_job` 改为 CLI 字段非空才覆盖 payload_json。回归测试 `tests/test_server_pyz.py::test_create_job_project_flag_lands_in_payload` / `test_create_job_payload_json_project_survives`。详见 HANDOFF.md。
- **KI-3.1 server CLI UTF-8 打印**：见上。
- **P1 可观测性 API**：`list_agents` 全链路（service/HTTP/CLI/web/remote client）补齐，runbook agent 注册验证步骤可用。见 DEVELOPMENT_PLAN.md P1。


