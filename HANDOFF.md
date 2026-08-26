# radar-sim 当前交接

> 更新时间：2026-08-26
> 当前代码分支：`main`（`d26cefb`）
> 当前正式 release：`d26cefb-agenttools-20260826-r17`，Skill 默认返回校验后的本地结果地址
> 当前 Linux release：`/home/hoz2wx/radar-sim-d26cefb-agenttools-20260826-main`
> 回滚 release：`/home/hoz2wx/radar-sim-a51f54c-agenttools-20260825-r16`
> 线上地址：`http://10.190.171.44:8877`

这是当前状态的唯一入口。历史审计、旧 handoff 和停用部署文档统一在 [`docs/archive/`](docs/archive/README.md)，不能把归档文档当作当前操作步骤。

## 产品定位

radar-sim 是 Selena 编译与仿真的外围自动化框架，不实现 Selena 内部算法，不修改仿真服务器排队逻辑。Web 和 Python SDK 使用同一 `UserRunConfig 2.0`、同一 `/api/v1`、同一 Job/Stage/Manifest。

支持：

- 多用户逻辑隔离；
- 单条和批量 MF4；
- `existing` / `build` Selena 来源；
- 用户填写 Selena 编译脚本、代码目录、Runtime XML 和可选输出/依赖信息；
- `local` / `cluster` / `auto`；
- Windows/SDK 源到 Cluster 直传和共享路径零复制；
- 长任务、取消、断线恢复、partial 逐输入恢复；
- Web/SDK 结果 Manifest、ZIP、retention、GC 和磁盘水位。

不在范围：Selena 内部结果内容、点云正确性、认证安全、远端到本地 Windows 的通用 `source_to_local` 和关机唤醒。MCP/Skill 已作为 SDK 薄封装交付，不复制调度或传输逻辑。

## 当前实现状态

- Connector 合同版本：`16`；旧 Connector 会被显式标记为需要更新；当前线上 Windows Connector 在更新完成前 `available=false`、`update_required=true`；
- Cluster Linux executor：`2`；platform gateway：`2`；
- 结果水位：`RSIM_RESULT_MIN_FREE_BYTES=1073741824`；
- YAML import/export 支持完整或不完整草稿，返回 `complete`、`missing_fields`、`validation_errors`；
- `validate_run()`、`submit_run()`、`submit_yaml()` 仍严格要求完整配置；草稿不会创建 Job；
- SDK 提供 `import_yaml()`/`export_yaml()`、长任务等待、直传恢复、partial 失败输入重试、结果下载；
- Cluster readiness 在提交前和 preflight 前均检查；build+Cluster readiness 失败不会继续编译/传输；
- readiness 探测采用单飞缓存和有界请求等待；外部 Cluster 挂起时返回可重试 blocker，不占住 Web/SDK 提交请求；
- Cluster 外部 Manager 曾在本次验收中短暂不可达；最新复核已恢复 `SZHRADAR01:8123`，真实 `/api/v1/run-configs/validate` 返回 `cluster_ready`、`can_submit=true`。readiness 单飞缓存和 blocker 降级保护仍保留；不能只依据 executor/gateway 心跳放行；
- `/api/v1/cluster/readiness` 已作为 Web 目标门禁；Cluster 不 ready 时禁用 Cluster 选项，若本地 Connector 也不可用则自动、本地、Cluster 和提交按钮全部禁用；直接 API/SDK 提交也在创建 Job 前返回 `503`；
- 本地 Selena Agent v16 已解析 `MDF-Scheduler done/total` 进度并上报 Stage；明确引擎错误会立即收敛为 `selena_failed`，不会继续等待 DataRecorder；Web 现已消费 Job/Stage 进度并显示总进度、阶段进度和状态徽标；
- 共享 UNC 路径在服务端挂载探测成功时优先走 shared-reference，客户端直传提示不再强制复制同一份数据；直传块默认 8 MiB，多文件传输最多 2 路并发且保持 Manifest 顺序；
- Web 顶部存在 Connector 必要更新提示；当前只有合同版本过旧才阻断，兼容包更新提示仍是后续增强项。
- Web 已完成 Simulation Engineering Workbench 三轮视觉重设计：创建任务使用横向配置工作区 + 固定 Inspector，任务中心使用紧凑 Master-Detail；Inspector 展示执行位置、Dataset、Selena、Runtime、校验状态和主要操作，任务详情展示总进度与逐阶段进度条；所有字段 ID、API 调用、SDK 语义和任务状态保持不变。
- Skill 已实现静默执行与 active profile：`get_simulation_state` 静默恢复上次配置，`check_agent_tools`/`check_windows_connector` 自动准备能力，成功时不向用户展示 `allow`、版本、服务地址或自查日志；`scripts/start_mcp.py` 保持 stdout 仅用于 MCP JSON-RPC，日志写入 `agent-tools.log`。
- Cluster 重试去重：`cluster_stage_executor` 对同 role/同内容重复 transfer manifest 去重，`DatasetRef` 层按路径/大小/checksum 去重，冲突时返回 `CLUSTER_DATA_TRANSFER_CONFLICT` 并保留稳定诊断，不再压缩为无信息 `cluster_stage_failed`。

## 测试与线上证据

- 平台无关回归：`1662 passed, 12 skipped, 1 deselected, 1 warning`；被排除项是当前 Linux 环境缺少 `asammdf` 的 Windows/Gen5 平台测试；warning 是 Starlette/httpx 弃用提示；
- Web/SDK/API/身份相关回归：`214 passed, 1 warning`；
- 线上 health：`ok=true`；
- 线上 capability：Windows 1、Cluster 2；
- 线上 Web 首页：HTTP `200`；
- 线上 SDK partial YAML import/export round-trip：通过；完整 YAML round-trip：通过；未创建测试 Job；
- 线上 Connector 包：`8425051 bytes`，SHA-256 `8cd8472bfc30c43501e4d7730bc9a5a79678e1acee130aa28528122a40640036`，合同 `16`；当前 exact-device Connector 已通过 MCP 更新并复核；
- UI release `7c78b64`：线上浏览器已复核 `styles.css?v=20260820-engineering`、`app.js?v=20260820-engineering`，浅色工程主题、Inspector、任务中心空态和桌面视口无横向溢出；临时任务数据黑盒复核了 Master-Detail、总进度 `64%` 和阶段进度 `100/100/64/0`；UI/API 回归 `88 passed, 1 warning`；
- 线上真实成功任务：`job_2a147e561d24`，单条 MF4、最终 `succeeded`，Manifest 可用；总耗时约 `1660s`，其中 `run_simulation` 约 `1355s`，成功证据保留；
- 两个并发 `/api/v1/run-configs/validate` 黑盒请求在 Manager 不可达期间均约 `8.2s` 返回 `200` 和 `cluster_readiness_unavailable`，未创建 Job；Manager 恢复后真实复核 `1.768s` 返回 `200`、`can_submit=true`、`cluster_ready`；
- 服务器当前 release 为 `d26cefb-agenttools-20260826-r17`（user-level systemd `active`，`NRestarts=0`，`health 200`），保留 `a51f54c-agenttools-20260825-r16` 作为回滚 release；能力 `windows 0` / `cluster 2`（executor 2 / gateway 2），合同 `16`；
- Agent Tools Bundle：`a7ddde6b24930b5434928978aa17f0c3b92428aaf209c2d317f8f26c0e7a33b8`，Connector 包：`2a3862e4b18a91e083c45aca90d2e12d68248ad4e6bd1a6a2e53d9d0f75329a6`；SDK `4.0.0` / MCP `0.1.0` / Skill `0.4.2`；
- 线上真实成功任务：`job_2a147e561d24`（历史）、`job_651a7887b5ab`（Skill 真机 `537269680 bytes`）、`job_2b9a6b6452b7`（Cluster 重试后 `succeeded`，见下）；
- 当前控制库无活动 `queued/running` Job；Cluster 外部 Manager `SZHRADAR01:8123` 已恢复，`/api/v1/cluster/readiness` 返回 `cluster_ready`/`can_submit=true`。

## 当前文档入口

- [文档总入口](docs/README.md)
- [产品合同](docs/PRODUCT_CONTRACT.md)
- [V2 架构](docs/V2_ARCHITECTURE.md)
- [详细设计](docs/DETAILED_DESIGN.md)
- [控制面/数据面合同](docs/CONTROL_DATA_PLANE_PLAN.md)
- [AI/SDK 集成合同](docs/AI_INTEGRATION_CONTRACT.md)
- [发布与部署](docs/release-deployment.md)
- [Windows Connector](docs/windows-one-click-connector.md)
- [当前已知边界](docs/KNOWN_ISSUES.md)
- [SDK/YAML/Skill 准备交接](docs/handoffs/2026-08-20-sdk-yaml-draft-skill-readiness.md)

## 分支与发布

`main` 是正式开发与发布分支。本次发布前已确认专项测试通过、工作区只剩用户明确保留的未跟踪目录、服务器无活动 Job，且旧 release 可回滚。后续功能改动应先在临时分支完成验证，再以 fast-forward 或合并提交进入 `main`。

任何 Connector 合同不兼容变更必须提升合同版本；兼容性代码包更新不能伪装成必要升级。自动热更新尚未开放，当前更新入口是 Web/SDK 下载同源脚本后由 Windows 用户执行一次。

## 工作区注意事项

`.zcode/` 和 `tmp-agent-home/` 是未跟踪目录，属于用户工作环境，本次整理不读取、不提交、不删除。

## 2026-08-20 SDK Agent/MCP 准备补强

本轮只修改 SDK、SDK 依赖的直传路径安全检查、SDK 合同文档和对应测试；未修改 Web/UI 文件。当时尚未创建 MCP/Skill 调度器。

- `RadarSimClient.submit_yaml()` 现在同时接受 YAML 文本和 YAML 文件路径，适合 MCP 工具的受限文本输入；
- SDK 新增 `user_run_config_schema()`/`run_config_schema()` 和 `cluster_readiness()`，Skill 可独立完成 Web 的配置合同查询与 Cluster 提交门禁检查；
- SDK 的本地 MatFilter 推导只用于源端直传，不写回 `UserRunConfig`，因此 Web/SDK 的配置、`spec_hash` 和 DAG 保持一致；
- 新增 `wait_until_actionable()`，在终态或 `needs_input`/Connector 等待返回，避免 Agent 无限阻塞；长任务仍可用 `wait_job()`，观察超时不取消服务端 Job；
- Job/Event/Validation/Diagnosis/Manifest 等主要模型提供 JSON-safe `to_dict()`，并提供 `terminal`、`needs_input`、`progress_percent` 便捷判断；
- 长任务事件轮询会对 transient `5xx/408/429` 做恢复，对永久 4xx 保留结构化 `RadarSimApiError`；
- 修复 Windows 长路径下合法嵌套直传目标被 `Path.resolve()` 的 `C:\`/`\\?\` 前缀差异误判为越界。

验证：SDK 专项 `71 passed, 1 warning`；SDK/直传专项 `112 passed, 2 skipped, 1 warning`；排除一条属于当前 UI 文案范围的断言后全仓 `1689 passed, 12 skipped, 1 deselected, 1 warning`。尚未做新的真实 Selena/Cluster 纵向任务验收，以上不等同于线上发布确认。

## 2026-08-21 正式 SDK/MCP/Skill 封装

本轮继续不修改 Web/UI；新增正式 Agent 集成资产：

- `docs/RADAR_SIM_SDK_GUIDE.md`：SDK 安装、配置、接口、调用时序、状态、进度、传输、异常、幂等、结果和验收手册；
- `docs/RADAR_SIM_DISTRIBUTION.md`：用户不下载源码时的 wheel/企业包仓、MCP 注册、Skill 分发和远程 MCP 方式；
- `radar_sim_mcp/`：基于 `RadarSimClient` 的薄 MCP Server，统一 `{ok,data,error}` 工具返回，不复制调度和传输逻辑；
- `skills/radar-sim-simulation/`：可分发 Skill，包含配置引导规则和 MCP 工具合同；
- MCP 支持本机 `check_windows_connector`；安装/更新工具要求 `confirm=true` 且进程环境显式设置 `RADAR_SIM_ALLOW_CONNECTOR_INSTALL=1`，安装完成后通过 exact-device status 和能力接口确认当前电脑上线；
- Agent Tools 分发面提供 `/api/v1/agent-tools/manifest`、`package.zip`、`install.py` 和 `install.ps1`；Bundle 版本化安装到本机独立目录，校验通过后切换稳定 MCP 启动器，失败保留旧版本；
- MCP/Skill 更新使用 `check_agent_tools`/`update_agent_tools`，要求 `confirm=true` 和 `RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE=1`，更新完成后返回 `restart_required=true`；
- Linux deploy 和 Docker build 现在在发布阶段构建 Agent Tools Bundle；如果部署未提供有效 Bundle，Agent Tools 接口 fail-closed 返回 `agent_tools_unavailable`，不暴露内部路径。
- `setup.py` 增加 `[mcp]` 可选依赖和 `radar-sim-mcp` 入口；
- 当前 MCP 不修改 Web 的下载/安装逻辑，认证开启的服务仍需部署方提供短期 Connector pairing。

本轮 SDK/API/直传/MCP/Agent Tools 定向回归为 `173 passed, 2 skipped, 1 warning`；最终安装器补丁后，Skill validator、Python/PowerShell 安装模板解析、Python compile、`bash -n scripts/linux_deploy.sh` 和 `git diff --check` 通过。全仓最近一次回归为 `1702 passed, 12 skipped, 1 failed, 1 warning`；唯一失败是既有 Web HTML 文案断言 `tests/test_control_data_plane_contract.py::test_web_user_run_never_uploads_task_file_bodies_to_linux`，不属于本轮 SDK/MCP/分发范围，未修改 Web/UI。

线上已切换到最终 release `/home/hoz2wx/radar-sim-10b6317-agenttools-20260825-r13`，systemd `active`、health `200`。Agent Tools Manifest 当前 release 为 `10b6317-agenttools-20260825-r13`，Skill `0.3.0`，Bundle `56737091 bytes`，SHA-256 `e7007cf8b9860804b9a2fbcfa36458eb0eba771aea0dc54267b0d422ddb5c9c5`；Connector 包为 `8431175 bytes`，SHA-256 `4f95d4f0c081257f310fc3815bb93f713b986150ccc36f558c2cda2bac1797a5`。

## 2026-08-21 Selena 编译策略修正

用户选择 `selena.source=build` 后，公共 Build Stage 采用三态代码变更决策：

- 明确无代码变更且历史 Bundle 与当前实际产物、编译入口、构建模式一致：跳过编译；
- 明确有代码变更：执行增量编译；
- 无法检测代码变更、历史 provenance 缺失、产物路径需要兜底解析：仍执行增量编译；
- 历史产物分支与用户预期分支不一致，或当前工作区分支与历史产物不一致：执行全量清理；
- 只有明确检测到分支或构建模式不兼容时才执行全量清理。

因此 `existing_artifact_provenance_unavailable`、`existing_artifact_location_unverified` 和 Git 状态读取失败不再设置 `clean=true`。Agent 会在工作区锁内二次准备和验证；跳过编译时仍执行 artifact 校验和 Runtime Bundle 重新登记。`source=existing` 仍然在 DAG 层跳过编译阶段。

本次代码覆盖 `core/agent_build_stage.py`、`core/agent_runtime_bundle_lease.py`、`core/environment_snapshot.py` 和 `cli/agent.py`，未修改 Web/UI。新增策略矩阵测试后，编译/环境/脚本策略专项为 `57 passed`，公共 Agent 相关回归为 `208 passed`。r9 已完成服务器 release 切换和 MCP→Connector 黑盒更新；真实 `source=build` Job `job_92f8b591521a` 的 Agent 日志记录 `Selena build policy: incremental (selena_build_script_changed_incremental)`，随后进入 R2D2/CMake，任务在无 full-clean 证据后取消；同一 Job 的环境检查先前因输出根扫描上限失败的问题已修复并重新通过。

随后又补充了 `expected_branch` 与历史产物分支的显式比较，新增策略测试达到 `58 passed`。r11 已包含该补丁。

Windows 黑盒验收已通过：从线上地址下载并校验 Bundle；隔离临时根目录创建 Python 3.13 venv，按 wheel tags 离线选择并安装 SDK/MCP/`pywin32`；`radar_sim_sdk`、`radar_sim_mcp`、`mcp` 导入成功；MCP 注册 `26` 个工具，stdio `initialize`/`notifications/initialized`/`tools/list` 握手成功；Skill 文件和稳定启动器状态存在；Token 未写入 `install.json`/`mcp-config.json`；`check_agent_tools` 返回 `installed=true`、`update_available=false`；重复安装返回 `already_current`、`restart_required=false`；模拟旧版本更新返回 `restart_required=true`、`skill_updated=true`。

当前仍未配置外部 PyPI 或固定远程 MCP URL；但用户不下载源码、仅使用 `http://10.190.171.44:8877` 的 `install.py`/`install.ps1` 安装本机 MCP/Skill 已经可用。认证开启部署的 Connector pairing 仍需部署方提供短期配对流程。

## 2026-08-25 r14 最终交互修复

- 最新 main：`a51f54c`；Linux 服务：`a51f54c-agenttools-20260825-r14`，
  health `200`，Skill `0.4.0`；
- MCP 使用稳定 `run_mcp.py`/Agent launcher，不使用 `python -` 交互式 stdin；
  人类可读进度只写 stderr，stdout 保留 MCP JSON-RPC；
- Skill 增加输入闭环闸门：所有数据、Runtime、构建来源等业务字段先一次收齐，
  未闭环前不执行 Connector、传输、编译或提交；
- Skill 仓库：`skillForJob/main=62d735d`，本地 Skill 已同步到 r14 内容；
- 本地和服务器旧 MCP/release 已清理；`.zcode/`、`tmp-agent-home/`、运行数据和
  最终结果文件保留。

## 2026-08-25 最终版本验收

- 远端 `main` 已快进到 `10b6317`；Skill 独立仓 `skillForJob/main` 已发布
  `42b83d3`，Skill 版本 `0.3.0`；
- `job_2b9a6b6452b7` 已通过 Cluster retry 完成，Diagnosis=`job_succeeded`，
  1/1 输入成功，结果 `result:sha256:5f4212527a590b2e9957cb1eb459683016f647f0d3bdb50ba227a292c225ae7f`；
- 结果 ZIP 已校验，SHA-256=`c8f9cdfba1d65edb5703c40155898e427cd7485e16729541819f597eba6baf4f`；
- 服务器只保留 r13 和当前 systemd unit；旧 release、旧 unit 备份、r13 的 docs/tests/build/cache 已清理；
- OpenCode 的 `6152735` 只更新了开发分支 `HANDOFF.md`，未进入 `main`，未改变运行代码或线上服务。

## 2026-08-25 Skill 真机端到端验收与提效

本次验收目标是“不同 Agent 对话框通过 Skill 配置并下发仿真任务”，不要求用户理解外围技术参数。

- 真实代码环境发现：`C:\BYD_OVS_CB` 顶层 Git 分支和嵌套 `apl/byd` Selena 分支均被识别；发现编译脚本、Runtime XML、Selena 输出和 MF4 候选；生成候选时不读取文件正文；
- Skill 优化：完整 YAML 直接走校验快速路径；缺失字段才发现候选；排除 `job_*`、`outputs`、`results` 和日志目录；支持嵌套 Git 仓；大仓达到边界时明确要求确认，不静默猜测；
- Skill 自动处理 MCP/SDK/Connector 检查、能力和 readiness；兼容版本更新不阻塞有效任务；MCP bootstrap 更新增加精确 `NO_PROXY` 主机，降低企业代理环境下的等待；
- 真机 Job：`job_651a7887b5ab`，MCP `validate_simulation` 返回 `valid=true`、`can_submit=true`；数据准备、preflight、真实 Selena、结果收集和 Manifest 全部成功；`build_selena`、`register_artifact` 按 `source=existing` 正确跳过；
- Selena 实际进度：`144193` 条输入，最终 `100%`，`returncode=0`；Diagnosis=`job_succeeded`；Manifest 1 个输入成功结果，结果文件 `537269680` bytes；
- 结果下载：通过 MCP 下载并校验 ZIP，SHA-256 `04827B6737C6976ABDE1CFC739B50E75EC0C427ED222B186C8FD591087F01880`，ZIP 内容可读；
- Skill validator 通过，发现脚本测试 `3 passed`，Agent Tools/MCP/Skill 分发回归 `6 passed, 7 skipped, 1 warning`；最终 Skill 包包含 5 个必要文件，无 `pyc/__pycache__`。

## 2026-08-25 静默 Skill 与 Cluster 重试验收（r12）

本轮对 `job_2b9a6b6452b7` 的 `DatasetError: dataset file paths must be case-insensitively unique` 根因已修复并在线上验证通过。

- 现象：Skill 自动 retry 后同一 MF4 产生两个不同 `transfer_id` 的相同 manifest，Cluster preflight 将其判为重复输入而失败；旧代码将异常压缩为无信息 `cluster_stage_failed`。
- 修复：`core/cluster_stage_executor.py` 同 role 去重 + `DatasetRef` 去重，冲突时 `CLUSTER_DATA_TRANSFER_CONFLICT`；`core/agent_simulation_state.py` 新增 active profile（`simulation-state.json`），`radar_sim_mcp/server.py` 新增 `get_simulation_state`，`skills/radar-sim-simulation` 实现静默能力准备与重复运行自动恢复；`scripts/start_mcp.py` 静默启动。
- 线上验证（`user-hoz2wx`）：
  - `job_2b9a6b6452b7` 当前 `succeeded`，`cluster_run_ref=cluster-run:7d598cf4e0944349ab29dbc102e07489`，`state=succeeded`，`external_job_id=1`；
  - 事件：`preflight` 连续两次 `failed` 后第三次 `succeeded`，随后 `run_simulation`/`collect_results`/`finalize_manifest` 全部 `succeeded`；
  - `diagnosis`：`job_succeeded`，`manifest_available=true`，`result_ref=result:sha256:5f4212527a590b2e9957cb1eb459683016f647f0d3bdb50ba227a292c225ae7f`，1 输入成功；
  - Manifest 文件 `OUT_.../Gen5_*.MF4out.MF4`、`logfile.txt.zip`、`result.ini` 等 9 文件，`fail_count=0`；
  - 无需再手工 `retry_stage`；`cluster_runs` 仅 2 行，无 `debug-job-2b9a6b6452b7` 残留，预留的 `cluster-run:d972...` 未持久化，无需清理；共享路径 `//abtvdfs2.../run-config-v2/job_2b9a6b6452b7` 为正式结果目录，保留。
- 服务：`http://10.190.171.44:8877` health `200`，capabilities `windows 1`/`cluster 2`，readiness `cluster_ready`，`/mnt/cluster` 挂载正常。
- 回归：定向 `33 passed, 1 warning`（active profile/去重/direct refs/MCP/Skill）；全量 `1705 passed, 19 skipped, 7 failed`（`6` 为缺 `asammdf`，`1` 为既有 Web 文案断言，非本轮引入）。
- Skill 10b6317（`feat: return simulation result address by default`）：将 `SKILL.md` 从 309 行精简至 112 行，默认在 `artifacts_available` 时自动调用 `download_simulation_result` 并返回校验后的本地结果路径与 checksum；`agents/openai.yaml` 同步更新触发描述；该 Skill 变更为纯客户端逻辑，无需服务端重新部署，已同步至独立仓。
- Skill 独立仓：`skillForJob` 远端 `origin/main` 当前 `6cb66b4`，已包含 `1aafe7d`/`51df706` 及 `10b6317` 的 Skill 精简（`SKILL.md` 112 行、`openai.yaml` 新描述）；`solutions/requirements-code-assistant` 的 `provenance/validation` 扩展（`3b9f10b`）已保留并推送；`bosch-data-transfert` 已恢复，`service-profile.json` 在独立仓为部署绑定示例、源码为通用空值，二者已对齐。
