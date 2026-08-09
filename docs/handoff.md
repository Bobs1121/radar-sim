---
title: radar-sim 项目交接文档
description: 项目现状、架构、已知问题和后续 TODO
---

# radar-sim 项目 Handoff

## 0A. 2026-08-05 控制面/数据面分离改造发布记录（已上线，接手者先读）

> 状态：**代码已提交推送，真实 Cluster 黑盒成功；生产 8877 当前运行 `f94d9aa`（包含 `eff321a` 主链路与无黑框 Connector 修复）**。本节记录当前共享工作区的真实进度，优先级高于下方所有历史“上传到 Linux”描述。下方旧记录只用于解释历史问题，不能作为当前实施合同。

### 2026-08-09 Windows Connector 黑框遗留收口

- 工作区遗留的 `bootstrap.ps1` 已把计划任务 Action 改成 `wscript.exe + run_hidden.vbs`，但 `run_hidden.vbs` 当时未进入仓库，Connector 打包器也不允许 `.vbs`；若直接发布，新用户重装后计划任务会引用不存在的文件。
- 已补齐通用无窗口启动器 `scripts/run_hidden.vbs`：首参数是 PowerShell 脚本，后续参数逐个安全加引号；以 `WScript.Shell.Run(..., 0, False)` 和 PowerShell `-WindowStyle Hidden` 双重隐藏，不改变 supervisor、watchdog、重连或日志逻辑。
- `scripts/build_windows_connector_bundle.py` 已显式允许并强制包含 `scripts/run_hidden.vbs`；`tests/test_release_deployment.py` 覆盖 scheduled-task 合同、真实 VBScript -> PowerShell spaced-argument 启动和 ZIP 内容。
- 本轮验证：Windows VBS 实际启动测试通过；Connector ZIP 成功生成并包含 155 files；release/API 组合 `83 passed, 1 warning`；PowerShell parser 与 `git diff --check` 通过。
- 修复已提交并推送为 `f94d9aa Hide Windows connector scheduled tasks`，生产 `radar-sim-v1.service:8877` 已切到 `/home/hoz2wx/radar-sim-f94d9aa`。服务端 Connector ZIP 为 588,109 bytes、155 files、SHA-256 `7e88e13bc3633c83e6d50429b6cec68c06499dc6eee52ca667b6f038f462865a`；HTTP header checksum 与下载文件一致，ZIP 已确认包含 `scripts/run_hidden.vbs`。
- 当前 Windows 已通过生产 Web/SDK 的真实“一键连接”入口重新安装 light Agent。安装完成后 `RadarSimConnector-HOZ2WX` 和 Watchdog 的 Action 均为 `wscript.exe`；主动触发 watchdog 前后 Agent PID 未变化，相关 PowerShell/Python `MainWindowHandle=0`；生产 `/api/v1/capabilities` 返回 `windows_light.available=true,count=1,reconnecting=false`。因此本轮不是只修改源码，现有用户和新下载包均已验证。

### 2026-08-05 真实直传黑盒最新现场（额度/上下文中断时从这里接续）

- 已提交并推送 `codex/new-branch`：`26354a9 Implement direct client-to-cluster data plane`。生产 `radar-sim-v1.service:8877` 没有被覆盖；验收使用隔离目录 `/home/hoz2wx/radar-sim-direct-26354a9`、隔离 `RSIM_HOME=/home/hoz2wx/.rsim-direct-acceptance`、隔离服务 `radar-sim-direct-acceptance.service:8879`。
- 部署方双命名空间已实际验证为同一数据面：Windows 写入 `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\direct-transfer`，Linux 只从 `/mnt/cluster/loc/szh/Isilon3/Cluster/radar-sim/direct-transfer` 探测；`/mnt/cluster` 是 `//abtvdfs2.de.bosch.com/ismdfs` 的 CIFS mount。Linux 防火墙未修改，本机通过 SSH tunnel `127.0.0.1:18881 -> server 127.0.0.1:8879` 调用隔离控制面。
- 真实输入：392,930,344-byte MF4、670,780,294-byte 完整 Selena 目录（19 files，含 exe/DLL/PDB/ILK）、98,780-byte Runtime XML、5,726-byte MatFilter。SDK 任务 `job_9360a14cbda7` 的四个 TransferPlan 均已 `completed`，`resolved_spec.decisions.transfers.resources` 已出现 `dataset/runtime_bundle/runtime_xml/mat_filter`；证明文件由 Windows 直接写 Cluster 共享盘，Linux API 只收 plan/progress/manifest。
- 此次黑盒暴露两个外围缺陷，**尚未允许发布**：
  1. `source=existing + target=cluster + direct runtime_bundle` 完成直传后，旧 DAG 仍保留 `resolve_spec`、`register_artifact` 为 queued，`environment_check` 依赖 resolve，导致任务错误显示 `windows_connection_required`。正确 DAG 应以 `prepare_data` 作为直传屏障，随后 Linux `environment_check -> preflight -> Cluster`；existing 直传不得再要求 Windows Agent。当前修复子任务：`fix_existing_cluster_dag`。
  2. SDK/Agent 每复制 1 MiB 就 POST 一次 `/progress`，文件正文虽不经过 Linux，但控制请求过密。必须按时间/百分比/字节阈值节流并保证最终态。当前修复子任务：`throttle_transfer_progress`。
- 首轮 SDK 提交进程 cell `1409` 已终止，证据任务 `job_9360a14cbda7` 已在完整 Manifest 落库后主动取消，避免旧 DAG 永久占用；SSH tunnel cell `1397` 仍用于隔离验收（接续环境若保留 cell，结束时需 terminate）。修复后必须用新的 idempotency key 重跑，不能复用旧任务来伪造通过。
- 两项代码修复已落盘并经主代理组合回归：`306 passed, 2 skipped, 1 warning`（`61.82s`），关键模块 `py_compile` 和 `git diff --check` 通过。修复后阶段合同是：提交前 `current_stage=prepare_data` 且 Web 无 Connector 时仍显示连接提示；四个 role Manifest 完成后 `current_stage=environment_check`、`waiting=None`，Linux 执行器继续。SDK/Agent 共享 core 节流器；本地 callback 保留逐 chunk，HTTP 只在首条、约 1 秒/5%/64 MiB 任一阈值、校验后最终态上报。
- 第二轮隔离黑盒使用提交 `0b5fe39`、owner `direct-acceptance-20260805-v2`、任务 `job_6eca01c46a43`。无 Windows Agent，四个真实 TransferPlan 全部完成；`resolve_spec/register_artifact` skipped，`prepare_data/environment_check/preflight` succeeded，证明 DAG 修复和数据面有效。首次 `run_simulation` 失败是隔离 systemd unit 未继承生产使用的 `/home/hoz2wx/.rsim-v1-cluster.env`；生产 8877 已配置，隔离 v3 后续只引用该 EnvironmentFile，未读取/输出 secret。
- 凭据补齐后重试，Cluster 明确拒绝 `datafile_path`：生成的 Config.cfg 将 `\\abtvdfs2...` 折叠成 `\abtvdfs2...`。根因是 Linux 上 `os.path.commonpath()` 把 UNC 双前导 `//` 当 POSIX path 折叠为 `/`。已改为 Windows/UNC 显式使用 `ntpath.commonpath`，POSIX 保持原逻辑，并补单/多 entry、不同 UNC share/drive 测试；另新增 Cluster credential 早期预检，只报告 configured/not configured，缺失时在 `environment_check` 给出部署动作而不是提交时 generic failure。主代理合并回归：`219 passed, 1 warning`，py_compile/diff-check 通过。下一步部署新提交，重跑 preflight 或新 Job 验证 Config.cfg 双斜杠并完成真实 Cluster。
- 上述 UNC 与凭据预检修复已提交并推送为 `581c5b5 Preserve UNC roots in cluster submissions`。隔离验收当前使用 `/home/hoz2wx/radar-sim-direct-581c5b5`、`radar-sim-direct-acceptance-v4.service:8879`、`RSIM_HOME=/home/hoz2wx/.rsim-direct-acceptance-v2`；生产 `8877` 仍未切换。
- 第三轮真实任务 `job_c21ed41e6074`（owner `direct-acceptance-20260805-v3`）已证明外围全链路可工作：四类本地输入由 Windows/SDK 直写 Cluster，共约 1.06 GB；`environment_check`、`preflight`、`run_simulation`、`collect_results` 均 succeeded，Cluster 接受修复后的双斜杠 UNC；`finalize` 因 Selena 返回 `-1` 正确标记失败，诊断为缺少 `g_Golf_Per_SppHvm_RunnableSppHvm_internalstates`。结果包已下载到本地 `output/job_c21ed41e6074-result.zip`（18,102 bytes），没有伪装成功。Linux 进程读取字节保持 `read_bytes: 0`，进度请求约 79 条，证明正文未经过 Linux 且节流有效。
- 第四轮 `job_007a3a702194`（owner `direct-acceptance-20260805-good`）改用历史上曾成功的 `...17-22_0118.MF4`，仍在 Selena 内部报告同一 missing signal。外围阶段仍全部成功。这说明不能再盲换数据；必须对比历史成功任务与当前任务的实际运行配置/提交包，确认薄适配层是否遗漏字段。
- 已从生产控制库快照确认两个历史成功任务：`job_e026d3e5b82e`（数据 `...15-08_0115.MF4`）和 `job_b38ca58d9ddf`（数据 `...17-22_0118.MF4`）。两者使用相同 Selena/Runtime；MatFilter 解析为 `config-asset://sha256/e3027700150870ea1eb5368cbc66ff37c88a6f0eeb2a1a1167ea9e264226340d`。当前直接传输 MatFilter 的 checksum 也是该值，因此不能把问题简单归因于 MatFilter 文件不同。Selena.exe、DLL 和 Runtime checksum 也与历史 bundle 一致。
- **当前唯一发布阻塞项**：从生产 `/home/hoz2wx/.rsim-v1-git-smoke/artifacts/.store/cluster_runs.db` 查询历史成功任务的 `job_dir/config_path`，取出历史 `Config.cfg`，与 `/mnt/cluster/.../radar-sim/run-config-v2/job_007a3a702194/Config.cfg` 及提交参数逐字段对比；修复必须是通用的“由用户输入/文件内容推导”适配，禁止恢复 `ovrs25/bydod25/xpeng` 项目硬编码。修复后用 `...17-22_0118.MF4` 新建任务，要求最终 business status succeeded，才允许切生产 `8877`。
- 上述发布阻塞项已定位并完成代码修复，尚待真实重跑：历史/当前 MF4（443,266,984 bytes）、Runtime、MatFilter、9 个 Selena exe/DLL 的 size/SHA-256 全部一致；worker 脚本除 Job 路径外语义完全一致。唯一业务配置差异是历史 `radar=RadarRL, mountingPosition=CRL`，当前为空；当前 paramconfig 因此缺少 `source=RadarRL` 与 `userparam=mountingPosition=CRL`，Selena 退化选择 `RadarFC` 后失败。
- 通用修复不增加 YAML 字段：数据拥有端（Python SDK 或 Windows Agent）从 MF4 acquisition source group 顺序推导 Radar；真实 `...17-22_0118.MF4` 同时含 `[RadarRL, RadarRR]`，与历史成熟行为一致选择第一个 `RadarRL/CRL`，仅在无 acquisition source 时回退安装位置信号。SDK/Agent 只把 `radar_source/radar_mounting_position` 放入 dataset TransferPlan metadata；API 白名单归一化后投影到 owner/job-bound transfer resource；Cluster 薄适配只写 Config，不在 Linux 打开 MF4。公共 YAML、项目识别和文件传输合同均未扩大。
- 修复后主代理无并发修改地完成组合回归：`315 passed, 2 skipped, 1 warning`（75.39s），目标模块 `py_compile`、`git diff --check` 通过。真实本机 MF4 helper 输出 `{'radar_source': 'RadarRL', 'radar_mounting_position': 'CRL'}`。下一步必须部署到隔离 8879，新建 Job 并确认生成 Config/paramconfig 与最终 business status succeeded；在这之前仍不得切生产 8877。
- **真实重跑及生产发布已完成**：提交 `eff321a Preserve inferred radar context in direct transfers` 已推送。隔离 8879 通过 SDK 提交 `job_444f050a55c4`（owner `direct-acceptance-20260805-fixed`），无需 Windows Agent；MF4、完整 Selena 目录、Runtime、MatFilter 均从本机直接写 Cluster 数据面。最终任务与 Cluster run `cluster-run:afc9405404d94c978164be7e8614f2c8` 均 `succeeded`，结果 `result:sha256:56a08eb906ae83fad339bb714189e3d31d5de109fcc1ad15b1b8c2df7cbfed88`，6 files，总结果 239,086,595 bytes；下载包 `output/job_444f050a55c4-result.zip` 为 12,173,265 bytes。
- 新任务生成的 `Config.cfg` 已确认 `radar="RadarRL"`、`mountingPosition="CRL"`，输入输出规模与历史成功任务一致；诊断 API 返回 `job_succeeded`、`artifacts_available=true`、`consistency.state=consistent`。这是真实 business success，不只是外围 Stage 成功。
- 生产 `radar-sim-v1.service:8877` 已切到 `/home/hoz2wx/radar-sim-direct-eff321a`，继续使用原 `RSIM_HOME=/home/hoz2wx/.rsim-v1-git-smoke` 和原 EnvironmentFile；生产 deployment 已加入 direct-transfer 双命名空间。外部 `http://10.190.171.44:8877/` 返回 200，health 正常，SDK `validate_run` 返回 `valid=True`、Cluster 10-step plan。旧任务/数据库未清空。
- 回滚材料位于 `/home/hoz2wx/radar-sim-release-backups/20260805-eff321a/{radar-sim-v1.service,deployment.yaml}`；旧生产代码目录 `/home/hoz2wx/radar-sim-v1-result-upload` 保留。隔离 v5 已停止，旧隔离代码包/临时 tar/部署脚本已清理；验收 RSIM_HOME 与 Cluster 结果证据保留，避免删除任务审计数据。
- 收尾状态：SSH tunnel cell `1397` 已结束/超时关闭，隔离 v5 已停止，生产 8877 独立可访问。后续不要删除生产 `/home/hoz2wx/.rsim-v1-cluster.env`，也不要在日志或 handoff 中输出其中 secret。
- 发布门禁已通过：定向回归、同一真实配置 Cluster submit/collect/finalize、Linux 不读取约 1 GB 文件正文、结果下载、handoff、commit/push 和带备份切换 8877 均完成。

### 本轮不可改变的目标

- 权威产品合同是 `docs/PRODUCT_CONTRACT.md`，实施计划是 `docs/CONTROL_DATA_PLANE_PLAN.md`。Linux 只能处理 YAML/JSON、Job/Stage/Event、心跳、TransferPlan、进度、Manifest 和逻辑引用；MF4、Selena.exe/DLL、Runtime、MatFilter、Adapter 正文不得经过 Linux Web/API 或落到 Linux 私有 staging。
- Cluster 目标：共享路径原地引用；Windows/Linux SDK 或 Windows Connector 持有的本地文件直接写入部署方管理的 Cluster UNC/共享 staging。`source=existing` 传完整 Selena 目录，不只传 exe。`source=build` 只能传编译后的实际产物目录，绝不能传代码仓。
- 本地仿真：本机可读输入零传输；Windows full 原地编译/执行。Linux 与 Cluster 不产生输入文件副作用。不同资源来源必须独立解析，不能按项目、盘符或“任一资源直传则全部直传”做分支。
- Web 和 Python SDK 共用 `/api/v1` 和同一份 `UserRunConfig 2.0`。后续 MCP/Skill 只能包装控制面 API，不能承载文件正文。

### 当前已落盘的实现（仍需整体回归）

1. `core/direct_transfer.py` + `core/transfer_service.py`
   - canonical `TransferPlan`/`TransferManifest`，部署双命名空间 `client_target_root`（客户端 UNC）与 `server_probe_root`（Linux mount）；客户端 Plan 不含 probe root。
   - owner/Job/Stage 隔离、不可猜测相对根、路径越界/设备路径/符号链接防护、`.partial` 续传、取消、源 size/mtime 校验、流式 SHA-256、原子发布、Manifest 幂等。
   - `ClusterWorkspaceWhitelist.from_config()` 已支持 `cluster.direct_transfer.{client_target_root,server_probe_root}`；Windows 测试机也能校验 POSIX probe path。
2. Linux Transfer API（`core/api_v1.py`、`core/api_v1_fastapi.py`、`core/control_service.py`、`cli/server.py`，正在收尾）
   - 路由固定为：`POST /api/v1/jobs/{job}/stages/{stage}/transfers`、`GET /api/v1/transfers/{id}`、`POST .../progress`、`POST .../manifest`、`POST .../cancel`。
   - owner 来自认证上下文；job/stage/root/mode 不允许客户端 body 自报。一个 `prepare_data` Stage 可等待 dataset/runtime_bundle/runtime_xml/mat_filter/adapter 多个 role，全部 Manifest 到齐才完成。
   - Manifest 投影到 `resolved_spec.decisions.transfers.resources`；dataset/runtime_bundle 同时生成 path-free、稳定的内部 `decisions.data`/`decisions.selena` 引用，避免结果清单继续依赖 Linux 中央上传目录。
   - 没有部署直传根时稳定返回/阻断 `cluster_direct_transfer_unavailable`，不得退回 legacy `dataset-uploads`、runtime bundle upload 或 config asset body upload。
3. SDK/Windows Connector（`radar_sim_sdk/client.py`、`cli/agent.py`，正在收尾）
   - SDK/Agent 复用 `core.execute_transfer`，控制请求只发清单、进度和 Manifest；现有 Selena 目录、Runtime、MatFilter、Adapter 和 MF4 分 role 申请 Plan。
   - `RadarSimClient.submit_run(..., auto_transfer=True)` 已开始实现“提交 Job 后，本机可读时自动直传；不可达时保持可恢复等待”，不再隐式调用 Linux body upload。
   - Agent 新 Cluster 路径不再调用旧 artifact/runtime/dataset/config body upload；本地仿真路径保持零上传。
4. Cluster 薄适配（`core/cluster_stage_executor.py`、`core/cluster.py`、`core/datasets.py`，正在收尾）
   - `ClusterStageContext` 可注入 `TransferService.resolve_storage_ref`/`server_probe_root`；Linux 对文件只做 owner-bound resolve + `stat/size`，不哈希、不解析 MF4、不归档 Selena。
   - 直传资源调用成熟 `prepare_cluster_job` 时使用零复制开关；数据、Selena、Runtime、MatFilter、Adapter 按 role 独立选择 direct/shared/catalog 来源。

### 已由主代理复核的测试证据

- `python -m pytest tests/test_direct_transfer.py tests/test_transfer_service.py -q`：`51 passed, 2 skipped`。
- `python -m pytest tests/test_control_data_plane_contract.py tests/test_direct_transfer.py tests/test_transfer_service.py tests/test_direct_transfer_clients.py -q`：`60 passed, 2 skipped, 1 warning`。
- `python -m pytest tests/test_sdk.py tests/test_api_v1_service.py tests/test_api_v1_fastapi.py tests/test_control_service.py -q`：`125 passed, 1 warning`。
- 上述通过只证明内核、API/SDK 定向合同；**不等于真实 Cluster 端到端完成**。

### 2026-08-05 合并收敛后的最新验证（晚于下一节的 5 个历史红项）

- 三个 Luna 切片合并后，主代理运行 direct/transfer/control/API/SDK/Agent/Cluster/Dataset/V1 入口组合回归：`278 passed, 2 skipped, 1 warning`，耗时 `59.51s`。
- 旧 `tests/test_agent_cli_policy.py` 中 4 条 Linux body upload 断言已改为 direct-transfer policy；主代理复核：`24 passed`。没有为了测试恢复 `_upload_v5_artifact` 或 `upload_data_lease`。
- Cluster 组合（含 server executor）主代理复核：`80 passed`。`DatasetRef.source_kind` 已正式加入 `direct_transfer`，不再用 `agent_upload` 冒充直传来源。
- SDK/Agent/V1 直传组合主代理复核：`49 passed, 1 warning`；旧不可达测试代码已删除，不再断言 `/existing-selena-imports`、dataset/config body upload。
- 关键生产模块 `py_compile` 通过，`git diff --check` 通过（只有 Windows CRLF 转换提示）。
- 尝试完整 `python -m pytest -q`，在 `304s` 命令门限内未完成且无最终摘要，被外层命令超时终止。该结果既不是失败证据，也不是全绿证据；接手者如需发布必须在更长门限下重新跑并保存输出。
- **仍未完成发布门禁**：尚未在 `10.190.171.44` 配置真实 direct-transfer UNC/probe mount，尚未用真实大 MF4/完整 Selena 做“客户端直写 Cluster、Linux API 字节不增长、真实 Cluster 成功”的黑盒。因此当前仍不得提交部署或宣称最终交付。

### 已解决的上一轮主回归红项（保留根因，避免回归）

最近一次命令：

```text
python -m pytest tests/test_cluster_direct_refs.py tests/test_cluster_stage_executor.py tests/test_cluster.py tests/test_datasets.py tests/test_server_cluster_executor.py tests/test_v1_cluster_yaml_sdk.py -q
```

当时结果：`5 failed, 77 passed, 1 warning`。以下五项现已解决；保留当时根因供回归：

1. `test_existing_bundle_cluster_pipeline_finishes_without_windows_or_adapter`：`finalize_manifest` 报 `DatasetRef is unavailable for manifest`。已通过 preflight 前 metadata-only resolve/回写及 direct synthetic decision 修复。
2. `test_linux_service_imports_a_server_visible_shared_selena_path`：已改为共享引用/元数据合同，Linux 不再归档正文。
3. `test_linux_service_maps_authorized_unc_selena_to_its_mount`：已改为 UNC zero-copy 路由验证。
4. `test_submit_cluster_yaml_is_one_call_and_prepares_all_local_inputs`：已改为一次 SDK 调用完成 TransferPlan/Manifest，明确禁止 body upload。
5. `test_one_sdk_call_reaches_cluster_submission_with_existing_selena`：已拆成 SDK 一次调用完成 direct manifests 与 direct refs 进入 Cluster preflight 两项验收；无不可达 legacy block。

### 已发现并已下达修复、必须复查的组合问题

- `source=build` 不能把 `config.selena.code_path` 当 runtime bundle；只允许 `register_artifact` 从真实编译输出传完整 Selena 目录。
- shared dataset + 本地 Runtime/MatFilter/Adapter：Agent 不能无条件创建本地 DatasetLease；没有 local dataset role 时只传本地配置 role。
- local dataset + shared Selena，以及 shared dataset + local Selena：资源必须独立路由；不能因存在任一 transfer resource 就要求全部资源直传。
- Existing Selena 目录通常只有 exe/DLL，用户的 `runtime_xml` 是独立必填资源；Cluster preflight 优先使用 `transfers.resources.runtime_xml`，不能要求 Runtime XML 一定在 bundle manifest 内。
- Direct runtime 的 `environment_check` 不能先调用旧 RuntimeBundle catalog；应使用全局/通用 Cluster 配置只检查调度环境，并从 direct entries 中大小写不敏感定位 `Selena.exe`。
- SDK 缺少 Cluster 共享访问时提示必须跨平台；Linux SDK 不能固定提示安装 Windows Agent。
- 大 MF4 不能“预扫描 SHA-256 + 复制时再 SHA-256”。Plan item checksum 可空，扫描只收集 path/size/mtime，复制流一次计算 Manifest SHA-256。

### 接手后的严格顺序

1. 先等/检查三个 Luna 子任务的最终结果：`integrate_control_plane`、`integrate_clients`、`integrate_cluster_refs`；不要相信局部通过，重跑上述主回归。
2. 解决全部五个红项，再运行：
   - direct/control/client 定向测试；
   - API/SDK/Control 回归；
   - Cluster/Dataset 回归；
   - 与 Agent policy、本地仿真、发布脚本相关测试；
   - 最后完整 `pytest`（若时间允许，记录所有既有失败和本轮失败的区分）。
3. 合同审计：新 `UserRunConfig` Cluster 任务不得调用 `/dataset-uploads`、`/runtime-bundle-uploads`、`/existing-selena-imports`、`/config-assets` 上传正文；本地仿真不得创建 TransferPlan/Cluster staging。
4. 部署前在目标 Linux 配置 `cluster.direct_transfer.client_target_root` 与 `server_probe_root`，确认 Windows/SDK 能写 UNC、Linux 只读 mount。不得用 `workspace_root` 静默回退。
5. 用真实大 MF4 + 完整 Selena 文件夹 + 独立 Runtime/MatFilter/Adapter 做黑盒：确认目标共享目录出现正确 bytes；Linux API 入站字节/内存不随 MF4 大小增长；Manifest 后 Windows 可离线；真实 Cluster 结果成功。
6. 真实验证通过后才更新本节为“已交付”，再提交、推送、部署。不要把当前 dirty worktree 或局部测试直接发布。

### 工作区卫生

- 当前工作树包含本轮改造以及之前用户/其他 Agent 的改动，不能 `reset --hard`、不能整体 checkout。
- 本轮新增的核心文件目前可能仍是 untracked：`core/direct_transfer.py`、`core/transfer_service.py`、`tests/test_direct_transfer.py`、`tests/test_transfer_service.py`、`tests/test_direct_transfer_clients.py`、`tests/test_control_plane_transfer_api.py`、`tests/test_cluster_direct_refs.py`、`docs/CONTROL_DATA_PLANE_PLAN.md`、`tests/test_control_data_plane_contract.py`。
- `.claude/`、`.playwright-cli/`、`CHECKPOINT.md`、大量 `output/*`、`docs/REFACTORING_PLAN.md`、`docs/WIZARD_IMPLEMENTATION_PLAN.md` 与本轮交付无关，不要误提交或删除用户文件。

## 0. 2026-08-04 当前发布交接（优先阅读）

### 2026-08-05 控制面/数据面分离产品决策（最新，后续实现必须遵守）

- 用户确认 Linux 服务本质是自动化脚手架和控制面，不是 MF4、Selena Bundle 或配置资产的中转文件服务器。Cluster 任务所需的本地文件必须由文件所在 Windows/Linux 客户端直接写入 Cluster 可访问 UNC/共享 staging，或调用 Cluster 上传网关；文件正文不得经过 Linux Web/API 端口或先落入 Linux 私有存储。
- `本地编译 + Cluster`：Windows light/full 在本机编译、校验 Selena.exe/DLL，并把 Selena、MF4、Runtime、MatFilter、Adapter 直接传到 Cluster 数据面；Linux 仅下发 TransferPlan、接收进度/Manifest、登记引用并提交 Cluster。
- `已有 Selena + Cluster`：共享路径零复制；本地路径由 Windows Connector 或调用端 SDK 直传。existing 路径不检查 VS/编译依赖。
- `本地编译 + 本地仿真` 与 `已有 Selena + 本地仿真`：所有输入和执行留在 Windows full；禁止上传到 Linux/Cluster，传输 Stage 必须为 `transfer_skipped_local_execution`。
- 上一条的“所有输入留在 Windows”指本机已有/可直接读取的输入不做无意义搬运。用户补充了远端数据+本地仿真、本地数据+远端 Selena+本地仿真等组合：不可原地读取的远端输入可以由源端直接送到 Windows full 受控缓存，但仍不得经过 Linux。统一算法是先选执行端，再对 Selena/Runtime/MatFilter/Adapter/MF4 分别做可达性解析，原地读取优先，必要时源端直传执行端。
- 多项目不能形成传输分支。项目识别只用于 Selena 编译命令、环境依赖与产物路径推导；仿真输入路由使用项目无关的资源图。Agent 必须保持轻薄：只做受控脚本调用、产物发现、流式直传、本地仿真（full）和状态上报，不运行第二套调度器/项目库/Web，也不解析完整 MF4。
- 纯浏览器不能读取任意本地路径。未连接本机组件时，Web 进入可恢复等待并提供一次连接入口；不得静默回退成浏览器把大文件上传到 Linux。Linux 工作站 SDK 应使用已挂载 Cluster 共享或直传适配器，缺少能力时返回 `cluster_direct_transfer_unavailable`，不能提示安装 Windows 组件。
- 新的权威实施计划为 `docs/CONTROL_DATA_PLANE_PLAN.md`。现有 Agent/SDK `POST/PATCH /api/v1/dataset-uploads`、Linux `DatasetStore` 及 Runtime Bundle 中央上传属于与新产品合同冲突的旧数据面，后续 P0 必须停止用于新 Cluster 任务；不能在它们之上继续做性能优化来固化错误架构。
- 直传期间 Linux 只处理 YAML/JSON、心跳、状态、进度、校验摘要和逻辑引用；大文件传输不能影响其他用户的 Web、SDK、任务列表或 Agent polling。发布门禁必须用真实大 MF4 证明 Linux API 入站字节和内存不随文件大小增长。

### 2026-08-05 新用户 Web/SDK 首次接入稳定性（最新）

- 现场日志里的单次 `timed out` / `WinError 10061` 来自旧 Windows Agent：旧版第一次轮询失败就打印 WARN。验证机已通过同一 Web owner 原地升级，保留 Agent ID、安装目录和自启动配置；安装代码哈希与仓库一致，`RadarSimConnector-HOZ2WX`、独立 Watchdog、`windows_light` 与 Cluster 均在线。
- 当前 Linux `radar-sim-v1.service` 自 2026-08-05 02:56 UTC 连续运行，`NRestarts=0`；两台 Agent 轮询持续返回 200。历史 `address already in use` 来自部署期间并存进程/反复人工重启，不是当前任务执行时持续宕机。
- 新版 Agent 连续三次轮询失败才报告故障；前两次短抖动静默重试，最长 30 秒退避。恢复不会改变任务 owner、Stage affinity 或重复创建任务。
- 一键 `.cmd`、安装脚本的 health/包下载、bootstrap 的认证模式检查均增加有界重试；安装撞到短暂发布窗口不再第一次失败，也不会把网络不通误报成缺 Token。
- SDK 未传 `user` 时自动使用 `sdk-<sha256(login@hostname)[:24]>`，同一用户/电脑重启后稳定，不同调用机不再全部落到 Linux 服务账号。显式 `user` 与 Bearer 行为保持兼容。
- SDK 新增 `download_windows_connector_for_run(config, destination)`：本地仿真自动选择 full，其余 Windows 读取/编译/上传选择 light，集成方不再配置内部部署模式。
- 定向回归：SDK、发布脚本、Agent 策略、FastAPI 共 `84 passed`。

### 2026-08-05 远程 Linux 调用端与 Windows Connector 断线恢复（最新）

- “Linux 用户”指其他 Linux 工作站上的用户：数据可能位于该工作站的 `/home/...`，用户通过网络访问中央 Linux Web/SDK。此类用户不注册 Linux Agent，也不能把远程工作站路径当成中央服务器路径。
- SDK 是首选入口：`RadarSimClient.submit_yaml()` 会在调用端探测可读的 POSIX 本地路径，先通过分块上传接口写入中央共享存储，再把本次提交中的 `data.path` 自动换成 `dataset://...`；调用者的 YAML 仍只填写原始数据路径。独立挂载的共享/Cluster 路径不会被重复上传。
- Web 受浏览器沙箱限制，不能凭文本框中的 `/home/user/data` 直接读取文件；Linux 用户应使用页面的文件夹选择器。浏览器分块上传完成后同样生成 `dataset://...`。如果只手输远程 Linux 本地路径而未选择文件，页面必须提示“选择本机数据文件夹或使用 SDK 上传”，不能提示安装 Windows Connector。
- Windows 日志中的连续 `agent poll failed` 经现场核对不是 Linux 服务持续宕机：8877 监听正常，服务端记录该 Agent 曾每 3 秒稳定返回 200，之后本机计划任务以 `-1073741510`（控制台中断）退出并停在 `Ready`。修复后轮询采用最长 30 秒指数退避，失败日志最多约每分钟一条，恢复时输出一次明确提示。
- 一键安装的计划任务除“登录启动”和“普通失败重启”外，新增每 5 分钟低频修复触发器。运行中的单实例不会重复启动；若用户关闭窗口、策略终止或 Task Scheduler 未把退出识别为可重启失败，组件会在 5 分钟内恢复。

### 2026-08-04 `job_e026d3e5b82e` 卡住复盘与多用户边界（最新）

- 任务使用已有 Selena、本地 Runtime/MatFilter 和一条本地 MF4，目标为 Cluster。最初卡在 Windows `resolve_spec`：一键安装的 light Connector 为了离线可装不安装 PyYAML，但填写代码仓/编译脚本后会进入可选产品识别模块，该模块顶层导入 PyYAML，异常又未进入 Agent 的终态上报路径，造成守护进程反复重启、Stage 长期显示 `running`。
- 修复一：已有 Selena 的产品识别只作为可选追踪信息；缺少可选解析依赖时，自动退回由 Selena.exe、同目录 DLL 和 Runtime XML 形成的稳定通用身份，不阻断上传或仿真。
- 修复二：Agent 任务准备阶段的所有普通异常都会提交明确失败结果，不再让“进程已退出、Stage 仍运行”的假卡死持续发生。
- 修复三：Cluster 主链不再为可选 Runtime/DataPlayer 诊断读取整条 MF4。此次 943286760-byte MF4 曾使 Linux 服务常驻内存约 1.3 GB、预检长时间无进度；现在只保留轻量接入检查，用户选择的 Runtime/数据默认可信，最终以 Selena/Cluster 的输出和 `result.ini` 为准。
- 恢复后同一任务未重新提交：`resolve_spec` 第 4 次尝试成功，Selena Bundle、Runtime、MatFilter 与 943286760-byte MF4 上传成功；Cluster Run `cluster-run:0ad973b1acca4334bbe9c965837cca7a` 完成，`success_count=1`、`fail_count=0`，结果 `result:sha256:0860f031dc8237643a08675a4df31393fe62a02d52c6e06f7cb4ea8a051eaa4f`，包含输出 MF4、`result.ini`、`selena.log` 等 6 个公开文件。
- 多用户规则：Windows Connector 与提交任务的 owner 绑定；另一台电脑/另一个浏览器身份不能领取当前用户的本地路径任务，也看不到其任务结果。Cluster executor 是共享资源，但任务、数据集、Bundle 与结果仍按 owner 校验。当前 `--insecure-no-auth` 只适合内网验证，浏览器本地身份不是正式账号体系；面向广泛多用户发布前必须接入统一登录/令牌映射。同一 Windows 账号被两个不同浏览器身份轮流安装 Connector 仍可能发生重绑定，不能作为正式共享电脑方案。
- Linux 用户不注册“Linux Agent”。Linux 只通过 Web/SDK 使用控制面：若 Selena Bundle、Runtime、MatFilter 和数据已经是 Cluster/共享存储可访问资源，则完全不需要 Windows Connector；若任一输入位于某台 Windows 的 `C:/D:`，必须在那台 Windows 上一次性连接 light Connector；需要编译或本地仿真时也必须使用 Windows，Linux 本身不支持 Selena 编译/执行。
- 回归：`tests/test_agent_cli_policy.py + tests/test_existing_selena.py + tests/test_cluster_stage_executor.py` 共 `62 passed`。

### 2026-08-04 纯净新用户环境与端到端验证（最新）

- 已把验证机恢复为真正的新用户状态：删除 `%LOCALAPPDATA%\\radar-sim` 整个目录（程序、数据缓存、凭据、安装信息全部删除）、`RadarSimConnector-HOZ2WX` 计划任务和所有 Radar Sim Agent 进程；用户代码仓、Selena 产物、MF4 数据和 Visual Studio 未动。
- Linux 控制库中已删除该电脑最后一条 Windows Agent 注册，当前 Windows Agent 数为 `0`；保留 Linux Stage executor 和 Cluster gateway。删除前备份：`/home/hoz2wx/.rsim-v1-git-smoke/artifacts/.store/control_v1.db.before-new-user-agent-cleanup-20260804173637`。
- `hoz2wx` 的旧任务历史已清空（75 个 job、750 个 task 及其事件/日志）；其他用户任务未删除。清理前基线 `job_d2c7917f0c90` 已保存到本地 `output/`，数据库也有独立备份。
- 纯净安装黑盒使用独立身份 `fresh-user-20260804`：从 Linux Web 一键安装 light 连接组件，提交已有 Selena 文件夹、本地 Runtime、本地 MatFilter 和一条新的本地 MF4，任务 `job_b38ca58d9ddf` 全流程成功。`resolve_spec` 打包 Selena/DLL/Runtime，`prepare_data` 上传 `443266984` bytes MF4，随后 Agent 可断开；Cluster Run `cluster-run:b56be79eed5b454892116ea8c47bbe93` 成功，结果 `result:sha256:7257c578a8143f06acf118b97a403103155ca8935bf1f299fc755a9f3da6d9e3`，1/1 数据成功。
- 此次暴露的产品问题不是后端不会等待 Agent，而是 Web 在提交前把 `windows_path_access_required` 仅显示成普通配置错误；任务未创建时用户看不到任务详情中的“一键连接本机”。提交 `85dabf1` 在新建任务页根据执行目标、Selena 来源和 `C:/D:` 路径主动显示“一键连接本机”，已部署到 `http://10.190.171.44:8877/`。
- 右上角状态已拆开：`Linux 服务已连接` 只表示浏览器可访问控制面；另行显示 `本机未连接`、`本机正在自动重连` 或 `本机已连接`，不再把服务连接误解成 Windows Agent。
- 回归：`node --check radar_sim_web/static/app.js` 通过；`tests/test_api_v1_fastapi.py + tests/test_api_v1_service.py` 共 `74 passed`；SDK 全文件 `tests/test_sdk.py` 为 `25 passed`，其中包含按当前 SDK 用户 scope 下载一次性 Windows 连接程序的验证。
- 部署后复验：`GET /api/v1/health` 为 `ok=true`；`hoz2wx` 任务数为 `0`；`windows_light/windows_full` 均 `available=false, configured_count=0`，Cluster 两个角色均在线。无 Agent 校验相同本地路径配置稳定返回 `windows_path_access_required`，不会创建脏任务。

### 2026-08-04 Light Agent 上传黑盒验证与日志修复（最新）

- 发布提交：`0ffec87`，已部署到 `http://10.190.171.44:8877`；服务 `radar-sim-v1.service` 为 `active`，`GET /api/v1/health` 返回 `ok=true`。
- Windows 一键包已重新构建并通过 HTTP 下载校验：`535537` bytes，SHA-256 `40c69f34469e834efeb715ba78cf38e4da230aba3d7418bee886bdde23952e19`。
- `No module named yaml`/`cli.web` 循环加载噪声已通过 Agent 专用 CLI 注册路径消除；轻量 Agent 不再要求 PyYAML、pip 或包索引。
- `core/cluster.py` 的 UNC 路径示例改为原始 docstring，修复 `SyntaxWarning: invalid escape sequence '\\s'`；已用 Windows 安装包内 Python 执行 `python -W error -m py_compile` 通过。
- 新用户黑盒任务 `job_e9574b80faca`：无 Agent 时在提交前返回 `windows_path_access_required`；安装 light Agent 后 `prepare_data` 成功，发现并上传 `1` 个 MF4，大小 `392930344` bytes，事件包含 `local dataset upload completed; Agent may now disconnect`。最终任务未进入仿真，唯一失败原因为用户 MatFilter 未上传/不在授权共享路径：`mat_filter must be uploaded or selected from an authorized shared path`，不是 Agent 上传失败。
- 已验证一次性安装：自动检查 Python 3.12.10 和 VS2015 (v140)，不安装 VS；注册 `RadarSimConnector-HOZ2WX` 登录自启/断线重连。黑盒验证完成后已停止 Agent、删除计划任务和程序/凭证，保留 `%LOCALAPPDATA%\\radar-sim\\data`（62 个文件）。
- 当前未宣称“本地 MatFilter + 未登记 Selena 文件夹”的完整仿真成功；该路径的 `resolve_spec` 曾长时间无可见进度并已取消，新增阶段日志用于后续定位，不能把它归因于 Cluster 仿真内核。

### 2026-08-04 任务中心加载优化

- 问题：任务中心默认请求 `limit=100`，服务端又为每条记录展开完整 Stage、ResolvedSpec 和 Runtime Bundle。当前用户库有 75 条历史任务时，响应约 `1 MB`，请求耗时约 `15 s`，页面长时间停在“正在加载任务”。
- 修复提交：`729c819` 将无状态筛选的服务端数据库查询限制为请求页大小；`b60c488` 将 Web 任务中心限制为最近 `20` 条，并与能力快照并行请求。
- 当前线上版本：`b60c488`，服务 `radar-sim-v1.service` active；日志已确认浏览器请求 `/api/v1/jobs?limit=20` 并返回 `200`。历史任务详情仍通过选择单条任务后调用 `/api/v1/jobs/{job_id}` 获取，不影响 SDK 详情接口。
- 访问地址必须使用 Linux 服务：`http://10.190.171.44:8877/`。`127.0.0.1:8878` 是本机服务，不代表 Linux 控制面；如果浏览器仍停在旧地址或缓存旧脚本，需要重新打开 Linux 地址并刷新页面。
- 当前无认证开发服务按 Linux 进程用户隔离任务，默认用户为 `hoz2wx`；SDK 使用 `X-Rsim-User` 创建的其他用户任务不会在默认身份的 Web 列表中出现。正式多用户发布前必须接入用户身份/令牌映射，不能依赖服务器 OS 用户作为最终产品身份。

### 2026-08-04 无 Windows Agent 的 Cluster 黑盒验证

- 已在验证机卸载 Windows 连接组件：移除 `RadarSimConnector-HOZ2WX` 计划任务、监督进程、残留 Agent 进程、`app` 程序目录和本地凭证；用户代码仓、Selena、Runtime 和 `%LOCALAPPDATA%\\radar-sim\\data` 保留。
- 复用 `job_d2c7917f0c90` 的用户配置，以已登记的 Selena Bundle 和 Cluster 数据集通过 Linux API 模拟 SDK 提交，生成 `job_4938c5511c4a`。
- 无 Windows Agent 时实际流程：`resolve_spec`/`prepare_source`/`build_selena`/`register_artifact` 跳过；`environment_check`、`prepare_data`、`preflight`、`run_simulation`、`collect_results`、`finalize_manifest` 全部成功。
- 结果：`status=succeeded`，Cluster Run `cluster-run:46a382a648ec424ebf0b94c53958f2f6`，结果引用 `result:sha256:7f9389a4e786c0f6d0d5821be43c7a98019870cf594d191fe2e75962341eb047`，结果压缩包可下载并包含 MF4、`selena.log`、`result.ini` 等文件。
- 观察到并修复一个提交响应时序问题：已准备 Bundle 且数据/资产在 Cluster 时，初始响应曾短暂误报 Windows 等待；`6967938` 后仅仍在 Windows 本地的数据或资产才触发等待。

### 本轮目标与边界

- 当前首要产品是一个 Linux 控制面：Web 与 Python SDK 共用 `/api/v1`，接收同一份 `UserRunConfig 2.0` YAML。
- Linux 只做配置解析、路径/资产准备、Stage 编排、Cluster 调度、日志和结果归档；不在 Linux 编译 Selena，也不执行本地 Selena 仿真。
- `source=existing + target=cluster` 且 Selena、Runtime、MatFilter、数据都在 Linux/Cluster 可访问位置时，**不需要 Windows Agent，也不需要 VS/编译依赖**。
- 如果这些输入仍在 Windows 本地，只需要一次性安装并持久运行 light 文件访问/上传连接；只有 `source=build` 或 `target=local` 才需要对应的编译/full 能力。

### 2026-08-04 新用户失败复盘与修复

- 失败任务：`job_63b0b7c8844c`、`job_44dae55ce9d6`。
- 失败 Stage：`resolve_spec`；错误：`existing Selena folder does not exist or is not a directory`。
- 根因：新用户没有 Windows Agent，但共享控制面看见旧的 `agent-HOZ2WX-WX8-C-0001A`，旧逻辑允许已绑定 Agent 做 first-use fallback，导致陌生 Windows 路径被错误领取；不是 Selena 或 Cluster 内部仿真错误。
- 修复提交：`9fe13d1`（路径绑定与用户 scope）+ `2d9614e`（Cluster 路由的 full Agent 防错）；之后追加了 Windows 能力按用户 scope 过滤和更明确的无 Agent 提示。
- 当前防呆：匹配不到本次路径时任务在提交前/`resolve_spec` 阶段保持 `windows_path_access_required`，不再让错误路径进入 Agent 后才失败；共享 Cluster 节点仍可被所有用户调度。

### Agent 一次配置规则

- 一键安装将服务地址、用户 scope、部署模式和受限凭证持久化到 Windows 当前用户的 `%LOCALAPPDATA%\\radar-sim`，并注册登录自启/断线重连；后续 Web/SDK 任务不重复安装或填写 Agent。
- 电脑重启后，用户登录 Windows 即由计划任务（受策略限制时为 Startup 目录回退）启动监督进程；电脑关机、睡眠或尚未登录时不承诺远程唤醒，Web/SDK 只保持等待，连接恢复后任务自动继续。
- 换电脑、换 Linux 服务地址、切换 full/light 或卸载后才重新连接；Visual Studio 始终由用户自行安装，Agent 只检测/提示并做脚本参数适配。
- SDK 调用方在 Windows 上对已有 Selena + Cluster 可直接通过 `RadarSimClient` 上传本地目录、Runtime、资产和数据，不强制安装 Agent；SDK 调用方在 Linux 上只能使用共享/Linux 可读路径，不能读取 Windows `C:/`、`D:/`。
- Web/SDK 的用户路径统一做跨平台规范化：`D:\\x\\..\\y`、`D:/y`、重复分隔符以及 `\\\\server\\share`/`//server/share` 会生成同一匹配形式；URI（如 `shared://`、`dataset://`）保留其逻辑语义，不按本地文件系统折叠。

### 代码、测试与线上证据

- 重点代码：`core/api_v1.py`、`core/control_service.py`、`radar_sim_web/static/app.js`、`scripts/bootstrap.ps1`、`scripts/start_windows.ps1`。
- 回归测试：路径/绑定/SDK 组合 → `82 passed, 3 skipped, 1 warning`；V1 服务/路由组合 → `85 passed, 1 warning`；`node --check radar_sim_web/static/app.js` 通过。
- 线上服务：`http://10.190.171.44:8877`，systemd user service `radar-sim-v1.service`，当前单一监听进程，`GET /api/v1/health` 返回 200。
- 新用户无 Agent 的实际验证：能力快照只显示 Cluster 可用、不显示他人的 Windows Agent；提交含 Windows 本地路径的 `existing + cluster` 配置返回 `windows_path_access_required`，并明确“不需要 Visual Studio 或编译依赖，只需要文件读取/上传连接”。
- 线上发布以 `codex/new-branch` 提交 `0ffec87` 为基线；未把用户的 `output/`、`.claude/` 等未跟踪诊断产物纳入提交。

### 后续不得偏移

1. 不要把 Windows Agent 当作所有用户的必需项；先判断路径是否已在 Cluster/Linux 可达。
2. 不要让能力快照、旧 Agent 或项目名替代本次 YAML 的路径匹配。
3. 不要把 VS、项目依赖、Agent ID、Token、Runtime Bundle 引用暴露到用户 YAML。
4. 不要修改 Selena/Cluster 仿真内部判定；外围只负责正确接入、调度、传输、状态和结果真实性。

## 1. 项目定位

radar-sim（命令行 `rsim`）是一个**雷达仿真辅助与数据分析工具**，面向 BYD 雷达项目的研发流程，覆盖：

```
编译 → VS 仿真/Launcher 仿真 → MF4 输出 → 数据分析 → AI 问答/对比
```

目标是替代手动在 Visual Studio 中操作 Selena 仿真的流程，实现一键式编译+仿真+分析。

## 2. 技术栈

- **语言**: Python 3.9+
- **MF4 解析**: asammdf
- **配置管理**: PyYAML
- **AI 问答**: OpenAI-compatible client（Bosch Model Farm）
- **终端 TUI**: 原生 print + sys.stdout（含 spinner）
- **打包**: `pip install -e .`

## 3. 架构总览

```
rsim.py                              # 入口，CLI 注册和分发
├── core/
│   ├── config.py                    # 三层配置加载（全局→平台→项目）
│   ├── models.py                    # 数据模型（BuildResult, SignalData, PluginResult 等）
│   ├── analysis_runner.py           # 插件发现、加载、执行
│   └── tui.py                       # 终端 UI 工具（styled, progress_bar）
├── cli/
│   ├── build.py                     # rsim build [hex|selena|all]
│   ├── analyze.py                   # rsim analyze <mf4>
│   ├── open_vs.py                   # rsim open-vs
│   ├── prepare_sim.py               # rsim prepare-sim
│   ├── diff.py (规划中)              # rsim diff
│   ├── history.py (规划中)           # rsim history
│   └── ask.py (规划中)               # rsim ask
├── plugins/analysis/
│   ├── signal_summary.py            # 信号统计：min/max/mean/transitions/peak
│   ├── rule_check.py                # 规则检查：signal/log/file 三类
│   ├── default_report.py            # HTML 报告生成
│   └── ai_qa.py                     # AI 分析和 Q&A
├── platforms/
│   └── gen5_selena/
│       ├── builder.py               # 统一构建入口 + 共享 helpers
│       └── selena_builder.py        # Selena 编译（调用 R2D2.py）
└── config/
    ├── default.yaml                 # 全局默认
    ├── platforms/gen5_selena.yaml   # 平台默认
    └── projects/ovrs25/             # ovrs25 项目配置
```

### CLI 自动发现机制

`rsim.py` 扫描 `cli/` 目录下所有非 `_` 开头的 `.py` 文件，检查是否有 `register()` 和 `run()` 函数，自动注册为子命令。文件名的 `_` 自动转为 `-`（如 `open_vs.py` → `open-vs`）。

### 插件发现机制

`analysis_runner.py` 扫描 `plugins/analysis/` 下的 `.py` 文件，查找继承 `AnalysisPlugin` 的类，按 `name` 属性注册。

## 4. 核心流程

### 4.1 编译流程（`rsim build selena`）

1. 读取 `r2d2_script`、`selena_config`、`python3_path` 等配置
2. 通过 `_resolve_config_path()` 找到 `.config` 文件
3. 通过 `_build_env_full()` 组装 `PATH` 和 `BOOST_ROOT`
4. 自动检测 VS 版本生成 `-vs vs16` 后缀
5. 调用 `python3 R2D2.py -m <config> -ghs_math -use_mat -notests -bm RelWithDebInfo -vs vs16`
6. 输出 `selena.exe` 到 `build_output/dc_tools/selena/core/RelWithDebInfo/`

### 4.2 仿真流程（VS — 当前可用方式）

在 Visual Studio 中：
1. `rsim open-vs` 打开 `selena.sln`
2. Debug → Start Without Debugging
3. VS 使用以下配置：
   - Args: `--paramconfig "C:\tools\byd_CR_Selena_Config_ovrs.txt"`
   - Environment PATH: 包含 MATLAB, Qt, Boost, selena_environment
4. selena.exe 读取 paramconfig 中的 runtime XML、输入 MF4、输出路径
5. 仿真完成后生成输出 MF4

### 4.3 数据分析流程（`rsim analyze <mf4>`）

1. `AnalysisRunner.run()` 读取 `signals.yaml` 和 `rules.yaml`
2. 通过平台后端的 `extract_signals()` 从 MF4 提取信号数据
3. 依次执行插件：`signal_summary` → `rule_check` → `default_report` → `ai_qa`
4. 结果保存到 `results/<项目>/<时间戳>/`，生成 HTML 报告

## 5. 当前状态

### 已完成

- [x] 三层配置系统（全局→平台→项目）—— `core/config.py`
- [x] Selena 编译流程 —— `cli/build.py` + `platforms/gen5_selena/`
- [x] HEX 编译支持（含 Ctrl+C 中断保护）
- [x] 自动 VS 版本检测
- [x] 环境 PATH 自动组装（MATLAB + Qt + Boost + MSYS）
- [x] `rsim open-vs` 打开 VS 工程
- [x] 信号提取和统计分析 —— `signal_summary` 插件
- [x] 规则检查 —— `rule_check` 插件（支持 signal/log/file）
- [x] HTML 报告生成 —— `default_report` 插件
- [x] AI Q&A —— `ai_qa` 插件
- [x] 插件自动发现机制
- [x] CLI 自动发现机制
- [x] `rsim build selena` 成功编译（14m59s, 45 个项目）
- [x] VS 仿真正常运行并输出 MF4（96105 帧）
- [x] `rsim prepare-sim` 仿真前校验
- [x] `--paramconfig` 仿真参数已纳入 `config.yaml` simulation 段

### 未完成 / 待实现

- [ ] `rsim run` — 命令行直接启动仿真（无需 VS，调用 selena.exe --paramconfig）
- [ ] `rsim diff <base> <current>` — 对比两次分析结果
- [ ] `rsim history` — 查看历史分析记录
- [ ] `rsim ask "问题"` — 基于分析结果的 AI 问答 CLI
- [ ] 编译验证功能 —— 自动对比 rsim 编译 vs 手动 VS 编译的信号是否一致

## 6. 已知问题

### P0 — 需要修复

1. **编译产物信号不一致**
   - 通过 `rsim build selena` 编译的 selena.exe，运行仿真后输出 MF4 中有 23120 个信号丢失（Wrong task 错误）
   - 手动在 VS 中编译（完全相同的源代码和配置）则不会有问题
   - 初步判断：可能是编译环境差异（如 MSVC 版本、CMake cache 残留、环境变量遗漏）
   - 需要排查：`cli/build.py` 的 `_build_env_full()` 组装的环境 VS 手动编译时的环境差异

2. **Selena 仿真需要 `--tolerant` 参数**
   - 不加 `--tolerant` 时 23120 个信号会报错 "not found"
   - paramconfig 文件中 `tolerant=false`，VS 中靠命令行 `--tolerant` 覆盖
   - 实现 `rsim run` 时需要带上此参数

### P1 — 需要优化

3. **`prepare_sim.py` 部分功能未使用**
   - `_setup_assets()` 和 `_check_dependencies()` 在 `run()` 中未被调用
   - 当前只做了配置校验和 VS 启动指引

4. **`config/platforms/gen5_selena.yaml` 中的 assets 路径**
   - `runtime_xml`, `config_template` 等路径推导依赖 `assets.root`
   - 需要确认各项目 assets 目录的实际内容

## 7. 关键文件说明

### 入口和分发

| 文件 | 作用 |
|------|------|
| `rsim.py` | CLI 入口，参数解析，配置加载，命令分发 |
| `core/config.py` | 939 行，三层配置加载 + 路径推导 + 环境检查 |

### 编译

| 文件 | 作用 |
|------|------|
| `cli/build.py` | HEX + Selena 编译 CLI，进度显示，错误提取 |
| `platforms/gen5_selena/builder.py` | 统一构建入口 + 共享 helpers (`_build_env_full`, `_resolve_config_path`, `_detect_vs_postfix`) |
| `platforms/gen5_selena/selena_builder.py` | Selena 编译（R2D2 调用） |

### 分析

| 文件 | 作用 |
|------|------|
| `cli/analyze.py` | 分析 CLI，接收 MF4 路径和插件参数 |
| `core/analysis_runner.py` | 插件发现/加载/执行，结果持久化 |
| `core/models.py` | 所有数据模型定义 |
| `plugins/analysis/signal_summary.py` | 信号统计 |
| `plugins/analysis/rule_check.py` | 规则检查 |
| `plugins/analysis/default_report.py` | HTML 报告 |
| `plugins/analysis/ai_qa.py` | AI 分析+问答 |

### 辅助

| 文件 | 作用 |
|------|------|
| `cli/open_vs.py` | 打开 VS 工程 |
| `cli/prepare_sim.py` | 仿真前校验 |

## 8. 外部依赖

### 编译必需

- `R2D2.py` — BYD 内部构建工具（`C:/BYD_OVS_CB/ip_dc/dc_tools/R2D2.py`）
- Visual Studio 2019 Community（MSVC 编译器）
- MATLAB R2023b
- Qt 5.8 (msvc2015_64)
- Boost 1.63.0
- MSYS/MingW64（通过 selena_environment）

### 仿真必需

- `selena.exe`（编译产物）
- `byd_CR_Selena_Config_ovrs.txt`（paramconfig）
- `Runtime_*.xml`（runtime XML，由 paramconfig 引用）
- 输入 MF4 数据集

### Python 包

```
asammdf        # MF4 解析
PyYAML         # 配置管理
openai         # AI 问答（可选）
```

## 9. 关键路径

```
C:/BYD_OVS_CB/                              # 源码根目录
├── ip_dc/dc_tools/R2D2.py                  # 构建入口
├── apl/byd/selena/cmake_build_cfg/         # 编译配置
├── ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/   # 编译输出
│   └── dc_tools/selena/core/RelWithDebInfo/selena.exe

C:/tools/
├── byd_CR_Selena_Config_ovrs.txt           # paramconfig
├── Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml  # runtime XML
└── CRlog.log                               # 仿真日志

D:/data/byd/                                # MF4 数据集
```

## 10. 下一步建议

优先级排序：

1. **排查编译差异** — 对比 `rsim build selena` 和 VS 手动编译的环境差异，解决编译产物不一致问题
2. **实现 `rsim run`** — 命令行直接调用 selena.exe，传入 `--paramconfig` + `--tolerant` + 正确的 PATH
3. **实现 `rsim diff`** — 对比两次分析结果（已有 `DiffResult`/`DiffSignal` 模型待使用）
4. **实现 `rsim history`** — 扫描 `results/` 目录列出历史记录
5. **实现 `rsim ask`** — 基于历史分析结果进行 AI 对话
