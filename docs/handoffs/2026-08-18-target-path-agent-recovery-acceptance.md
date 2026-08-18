# 2026-08-18 路径语义、Agent 恢复与 Web 空任务修复验收

## 结论范围

本次只处理以下用户指定范围：

1. 解释并修正本地路径与 Cluster 源到源传输的路径语义；
2. 不处理 Selena 输出内容/点云正确性问题；
3. 修复 Agent 丢失注册后的自动恢复；
4. 修复 Web 任务中心空列表状态；
5. 建立本次修复后的发布验收矩阵。

认证不作为本次结论条件；当前服务仍按受信内部网络模式运行。

## 1. 路径问题的根因和修复边界

此前 `Path.resolve()` 同时承担了两种不应混用的职责：

- 授权、目录包含关系、MF4 扫描和 Cluster 直传，需要规范化/规范路径；
- 本地 Selena 子进程需要用户填写的原始 Windows/DFS/UNC 路径。

在 DFS/UNC 场景中，`Path.resolve()` 可能把用户填写的别名，例如 `\\server-alias\\share\\data`，解析为后端服务器名。Agent 自己可能仍能扫描规范路径，但 Selena 子进程使用该规范名时可能得到 `The network name cannot be found`。

现在分为两条明确路径：

| 场景 | 使用的路径 | 是否进入公共 Job/Manifest |
|---|---|---|
| 授权、发现、内容校验 | canonical `source_path` | 否 |
| 本地 Selena 输入 | 原始 `source_path_text` | 否，仅 Agent 本地私有运行记录 |
| Windows → Cluster 直传源 | 原始 UNC 源优先，受签名 TransferPlan 约束 | 否 |
| Cluster 仿真输入 | 签名目标根下的 `cluster-staging://` 引用/部署目标路径 | 只保留逻辑引用 |

因此，Cluster 不会收到或使用 Windows 源路径；本地 Selena 也不会被迫使用 DFS 后端解析名。源到源传输仍由 Agent/SDK 直接写入部署配置的 `client_target_root`，Linux 控制面只处理计划、进度和 Manifest 元数据。

涉及代码：

- `core/agent_data_lease.py`：新增 `source_path_text`，兼容旧 SQLite 表自动迁移；
- `core/agent_local_run.py`：本地运行输入使用原始路径；
- `core/direct_transfer.py`：UNC 源复制不再在打开前强制 canonicalize；
- `cli/agent.py`：数据直传优先使用原始源路径，目标仍由签名计划约束。

## 2. Agent 注册恢复

Agent 启动时和运行中都使用同一个注册函数。轮询遇到明确的：

```text
HTTP 404 / connector_not_registered
```

会执行以下动作：

1. 使用原来的 `agent_id`、owner、hostname 和 contract 重新注册；
2. 重新发布当前 workspace/data/asset bindings；
3. 尝试冲刷本地 result outbox；
4. 清空本次轮询故障计数；
5. 继续轮询，不重新执行已经完成的任务。

普通 404、身份错误和任务错误不会被误判为注册丢失；短暂网络错误仍使用原有退避重连策略。

涉及代码：`cli/agent.py`。

## 3. Web 空任务状态

`radar_sim_web/static/app.js` 新增 `jobsLoaded` 状态。第一次成功返回 `jobs=[]` 时，即使轮询签名没有变化，也强制调用 `renderJobs()`，显示：

```text
当前筛选条件下没有任务
```

服务不可达时仍显示服务错误信息；长时间运行任务仍由轮询观察，不受浏览器请求超时影响。

## 4. 自动化验收矩阵

| 验收项 | 证据 | 结果 |
|---|---|---|
| Lease 保存原始路径且不泄露到 public dict | `tests/test_agent_data_lease.py` | 通过 |
| 本地运行使用原始数据路径 | `test_local_execution_preserves_original_data_path_spelling` | 通过 |
| Agent 丢失注册后自动重注册 | `test_run_reregisters_after_connector_registration_is_lost` | 通过 |
| Web 空任务成功渲染 | `test_web_task_center_paints_empty_successful_page` | 通过 |
| 直接传输、TransferPlan、storage ref 回归 | `tests/test_direct_transfer.py`, `tests/test_direct_transfer_clients.py` | 通过 |
| SDK/Web/控制面数据平面合同 | 全量测试覆盖 | 通过 |
| 全量 Python 测试 | `python -m pytest -q` | `1654 passed, 12 skipped, 1 warning` |
| Web JavaScript 语法 | `node --check radar_sim_web/static/app.js` | 通过 |

## 5. 真实部署门禁

代码和自动化测试通过不等于 Selena/Cluster 内部仿真已经成功。部署后仍需记录以下证据：

| 场景 | 必须记录 |
|---|---|
| Web + existing + local + UNC/DFS 数据 | Job、关键 Stage、最终 Selena 输入路径、结果状态 |
| SDK + existing + Cluster + 本地数据 | TransferPlan、Manifest、目标文件 checksum、Cluster Job |
| 服务重启/Agent 注册记录丢失 | Agent 重新注册日志、capabilities 恢复、无重复任务 |
| 新用户 Web 空任务 | 页面显示“当前筛选条件下没有任务” |
| 单条与多条数据 | 输入数量、输出数量、Manifest、结果下载 checksum |

第 2 项点云/信号缺失问题按用户要求不在本次修复和放行判断中。

## 6. 一键安装的 owner 绑定恢复

Web 生成的 `connect.cmd`/`install.ps1` 已经携带当前 Web 页面 owner，用户不需要再次填写 NTID。针对同一 Windows profile 之前绑定过其他稳定 `user-*` owner 的情况，一键安装器现在会：

- 查询旧 owner 的未完成任务；
- 旧 owner 没有 queued/running/waiting/cancelling 任务时，自动调用 `bootstrap.ps1 -ForceRebind`；
- 让 bootstrap 基于当前 Web/SDK owner 生成新的稳定 Agent ID，并重新注册；
- 旧 owner 存在未完成任务时停止安装并给出明确提示，避免静默抢占导致任务丢失。
- 如果后续下载包替换、依赖检查或启动步骤失败，安装器会尝试用保留的安装配置重新启动原 Connector，避免“安装失败 + 原 Agent 离线”。
- 安装更新现在先备份旧 app 源码和 `install.json`/credentials/recovery metadata，再停止旧进程；失败时恢复这些内容并重新注册/启动旧 Connector。
- 事务备份目录位于本次下载的临时目录，安装成功后随临时目录清理；安装失败时优先恢复旧文件和旧 owner，再尝试启动旧 Connector。

同 owner 的普通更新仍复用原 Agent ID，不触发 rebind。
