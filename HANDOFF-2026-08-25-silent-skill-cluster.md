# Handoff：静默 Skill、active profile 与 Cluster retry

更新时间：2026-08-25（Asia/Shanghai）

## 1. 当前暂停点

电脑重启前的工作状态如下：

- radar-sim 源码分支：`codex/new-branch`
- 已提交并推送：`462e166 fix: make Skill runs silent and retry-safe`
- 远端：`origin/codex/new-branch` 已包含 `462e166`
- 工作区中未提交的内容只剩本地运行目录：`.zcode/`、`tmp-agent-home/`，不要提交
- `origin/main` 尚未合并本分支；如需正式主线发布，后续创建 PR/合并

Linux 服务已经切换到新 release，旧 release 已保留：

- 新 release：`/home/hoz2wx/radar-sim-462e166-agenttools-20260825-r12`
- 旧 release：`/home/hoz2wx/radar-sim-bb71ff8-agenttools-20260821-r11`
- systemd：`radar-sim-v1.service` 当前 `active`
- 新 MainPID：`4133932`
- `/api/v1/health`：HTTP `200`
- Agent Tools release：`462e166-agenttools-20260825-r12`
- SDK：`4.0.0`
- MCP：`0.1.0`
- Skill：`0.2.0`
- Agent Tools Bundle SHA-256：`c3eff7e29cbf68b4c78ca7334cc2e3b37ed3d1f4d5836a62c3bceafb2d730a02`
- Windows Connector Bundle SHA-256：`4f95d4f0c081257f310fc3815bb93f713b986150ccc36f558c2cda2bac1797a5`
- systemd 旧 unit 备份：`/home/hoz2wx/.config/systemd/user/radar-sim-v1.service.r11-backup-20260825`

## 2. 已定位的 Cluster 失败

用户消息中的 `job_2b9a6b6452b` 少了最后一位；真实 Job ID 是：

```text
job_2b9a6b6452b7
```

失败状态：`failed`，失败 Stage：`preflight`，Stage ID：
`task_0c4fc3acbbf3`。Cluster 依赖检查通过，数据、Runtime XML、Selena
Bundle、MatFilter 的 direct transfer 也都完成。

根因已经在目标 Linux release 上复现：

```text
DatasetError: dataset file paths must be case-insensitively unique
```

Skill 自动 retry 后，同一个 MF4 产生了两个不同 `transfer_id` 的 transfer
manifest，但内容完全相同。Cluster preflight 将两条同名文件当成两个输入，
于是失败；旧代码又把未预期异常压缩成了无信息的 `cluster_stage_failed`。

本次代码修复：

1. `core/cluster_stage_executor.py`
   - 对同一 role 下内容完全相同的 retry transfer manifest 去重；
   - DatasetRef 层再次按相对路径、大小、checksum 去重；
   - 同名但内容冲突时返回稳定错误 `CLUSTER_DATA_TRANSFER_CONFLICT`；
   - 保留异常类型/稳定诊断信息，不再只有空泛的 `cluster_stage_failed`。
2. 目标机用新代码直接执行原 Job 的 `execute_cluster_preflight` 已成功：

```text
cluster_run_ref=cluster-run:7d598cf4e0944349ab29dbc102e07489
preflight.ok=true
preflight.diagnostic_ok=true
```

注意：这只是目标机上的真实 preflight 验证，还没有通过控制面正式 retry
原 Job，也没有提交外部 Cluster 仿真。

目标机上为复现创建了一个临时 debug run，待正式 retry 验证后清理：

```text
control_job_id=debug-job-2b9a6b6452b7
cluster_run_ref=cluster-run:d9729857dc544b549d694da2167d2236
state=prepared
```

## 3. Skill/MCP 静默行为已完成

本次提交已包含：

- Skill 将 MCP/SDK/Connector 首启、更新、能力检查、等待、重试和结果收集
  定义为内部流程；成功时不向用户展示 `allow`、版本、服务地址、本机路径、
  自查结果或安装日志。
- `scripts/start_mcp.py` 成功启动不再向 stderr 输出状态文字；内部状态写入
  本机 Agent Tools 目录下的 `agent-tools.log`，stdout 保留给 MCP JSON-RPC。
- 源码 Skill 的 `references/service-profile.json` 不绑定具体服务器；服务地址
  由部署安装器按本次服务请求注入。本项目不再在 Skill/MCP 中写死某个服务主机
  的代理绕过逻辑，网络代理完全遵循宿主机环境。
- `core/agent_simulation_state.py`：本机 active profile，默认位于
  `RADAR_SIM_MCP_ROOT/simulation-state.json`（没有 override 时位于用户级
  Agent Tools 数据目录）。只保存规范化 YAML、配置 fingerprint、上下文路径、
  Job ID 和状态，不保存 MF4/Runtime/Selena 内容、Token 或文件正文。
- 新 MCP 内部工具：`get_simulation_state(context_path?, data_path?)`。
  非 dry-run 提交成功后自动保存 profile，查询/等待 Job 时自动更新状态。
- Skill 对“帮我再仿真一下刚刚的数据”“再跑一次”“我改了这里，重新验证”
  优先恢复 active profile；同一代码仓配置不再重新询问数据、Runtime、Selena
  来源或 target。用户显式给出新数据时只替换 `data.path`。

## 4. 已完成的验证

本机定向回归：

```text
23 passed, 7 skipped
```

覆盖 active profile、重复 transfer 去重、Cluster direct refs、MCP 和 Skill。

全量回归结果：

```text
1705 passed, 19 skipped, 7 failed
```

7 个失败不是本次改动引入：

- 6 个 `tests/test_gen5.py` / `tests/test_cluster.py` 失败原因是当前本机
  `.venv` 没有 `asammdf`；
- 1 个 `tests/test_control_data_plane_contract.py` 是已有 Web `index.html`
  文案与测试预期不一致。

不要把这 7 个环境/基线失败写成 Skill 或 Cluster 去重修复失败。

## 5. 重启后第一件事：正式 retry 原 Job

电脑重启后，在 `D:\RamboStar\idea\radar-sim` 执行以下 Python 逻辑，使用同一
owner `user-hoz2wx`：

```python
from radar_sim_sdk import RadarSimClient

job_id = "job_2b9a6b6452b7"
stage_id = "task_0c4fc3acbbf3"
with RadarSimClient("http://10.190.171.44:8877", user="hoz2wx", trust_env=False) as client:
    job = client.retry_stage(job_id, stage_id)
    print(job.to_dict())
    while not job.terminal:
        job = client.wait_until_actionable(job_id, timeout=30, poll_interval=2)
        print(job.status, job.progress_percent)
    print(client.diagnosis(job_id).to_dict())
    if job.status in {"succeeded", "partial"}:
        print(client.manifest(job_id).to_dict())
```

验收重点：

- `preflight` 不再因重复 DatasetRef 失败；
- 后续 `run_simulation` 能进入 Cluster Gateway；
- 如果外部 Cluster 本身仍失败，Diagnosis 必须保留新的稳定原因，而不再只
  返回空泛的 `cluster_stage_failed`；
- 成功时 Job 必须是 `succeeded`，并有 Manifest/`result_ref`。

如果 retry 返回“Cluster run already exists with different resolved inputs”，
先检查上面的 `cluster-run:7d598...` 是否被手工 preflight 创建；删除该原 Job
的 prepared row 后再 retry，不要重复提交外部 Cluster。

## 6. 重启后清理 debug 复现数据

确认正式 retry 完成后，再删除本次人工复现创建的精确对象：

- `cluster_runs.db` 中 `control_job_id=debug-job-2b9a6b6452b7`；
- Cluster share 下 `run-config-v2/debug-job-2b9a6b6452b7` 对应目录。

不要递归清理整个 `direct-transfer` 或整个 Cluster workspace。

## 7. 还未完成的 Skill 独立仓同步

`radar-sim` 源码已推送；但 `skillForJob` 的独立 Skill 仓还没有本次最新变更。

当前本地 `D:\RamboStar\idea\skillForJob` 状态：

- 远端历史之前已推到 `8284c14`；
- 本地 `main` 比远端 ahead 1 / behind 5；
- 用户已有修改：
  `solutions/requirements-code-assistant/skill/requirement-code-traceability/SKILL.md`，必须保留；
- 本次 Skill 文件尚未提交到该仓：`SKILL.md`、`README.md`、`VERSION=0.2.0`、
  `agents/openai.yaml`、两个 references、`service-profile.json`、
  `bootstrap_agent_tools.py`、`start_mcp.py`。

重启后用临时 worktree 从远端 `main` 合并/更新，只提交
`skills/radar-sim-simulation/**` 和该 Skill 的 README，不要覆盖用户已有的
requirements Skill 修改；然后 push 到 `skillForJob/main`。

## 8. 重要边界

- 当前服务是 `--insecure-no-auth` 受信内网部署；`X-Rsim-User` 只是 owner
  路由，不是认证。
- 不要把 Skill 的中间检查结果直接转述给用户；用户只需要看到数据选择、Job
  状态/进度、最终诊断和结果。
- 不要把源码工作区中的 `.zcode/`、`tmp-agent-home/` 提交到 Git。
- 当前分支不是 `main`，正式发布需要 PR/合并；Linux r12 是从该分支源码归档
  构建的临时验收 release。
