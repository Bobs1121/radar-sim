# radar-sim 全服务场景、链路审计与后续 AI 执行任务书

日期：2026-08-17  
用途：交给后续 AI 继续审计、修改、部署和验收。  
范围：从 Web/SDK 提交仿真，到 Windows Connector、Selena 编译、本地/Cluster 执行、批量结果、结果下载和故障恢复的完整链路。  
明确排除：Selena/仿真引擎内部算法错误本身不由框架修复，但框架必须正确识别、归档、展示、保留成功结果，并且不能把框架故障伪装成 Selena 失败。

本材料的入口定义已经冻结：这里的“前端/后端”不是浏览器和 Linux 的职责划分，而是两种用户调用方式——Web 调用和 Python SDK 调用。两者必须使用同一个后端 API、同一个 Job/Stage 状态机和同一个结果合同。这里的“云端仿真”仅指现有 Cluster，不引入额外云厂商抽象。

## 0. 给后续 AI 的执行要求

这不是一份只需要阅读的背景介绍，而是一份执行合同。后续 AI 必须：

1. 先读取本文件、`PRD.md`、`docs/PRODUCT_CONTRACT.md`、`docs/V2_ARCHITECTURE.md`、`docs/DETAILED_DESIGN.md` 和最近的 handoff，再检查当前 Git、服务端、Connector、Job 数据库和真实任务状态。
2. 任何“已支持”“已修复”“链路已打通”都必须同时给出代码位置、自动化测试、真实部署证据和真实 Job/Manifest/结果引用。单元测试通过不能替代真实 Windows/Cluster 验收。
3. 不把固定等待时间当作仿真完成判断。只能对 HTTP 请求、提交握手、进程回收等局部操作设置安全边界；编译、Cluster 排队、本地批量仿真和结果复制必须由心跳、进程存活、进度、文件证据和外部 terminal 状态驱动。
4. 用户必须填写并确认 Selena 编译脚本。脚本位于用户选择的项目文件夹中，脚本内部包含构建目录、配置目录和输出目录。框架必须以用户填写的脚本为执行入口，解析和校验脚本相关的构建/输出边界，但不能替用户硬编码项目名、Jenkins 文件名、目录模板或项目专用参数。
5. 遇到无法证明输入、产物、结果或外部副作用是否完整时，必须 fail-closed，并返回稳定错误码、原因、下一步动作；不能为了让 UI 变绿而放宽校验。
6. 只考虑仿真内部错误是不合格的。必须覆盖多用户、批量、重试、断网、重启、取消、重复提交、Connector 安装/升级、SDK 结果下载和权限隔离。
7. 每个修改项都要有明确交付物。没有代码修改时也必须交付审计结论、风险等级、验证命令和未验收项，不能只写“建议后续测试”。

## 1. 当前基线和已知真实证据

### 1.1 当前代码/部署基线

- 代码分支：`codex/new-branch`。
- 分支编译防护和输出状态漏检修复：`d3de370`。
- 控制面取消后重试依赖闭包修复：`e88f0f9`。
- handoff 记录提交：`3d70c90`。
- 当前服务器候选 release：`/home/hoz2wx/radar-sim-d3de370`。
- 当前用户级服务：`radar-sim-v1.service`，必须检查 `active/running`、`NRestarts=0` 和真实 `WorkingDirectory`，不能误查系统级同名服务。
- 当前 Connector contract：15；统一 Connector，Windows 1 个，Cluster 2 个。
- 最近全量回归：`1637 passed, 12 skipped, 1 warning`。
- 最近定向 build/control 回归：`72 passed`。

### 1.2 真实 Job `job_26028465ebeb` 的故障证据

该 Job 是本次“分支切换仍走增量编译”问题的真实证据，不要重新解释成仿真内部失败：

- 旧分支 Bundle provenance：
  `feature/CRGVBYDPF-13580-selena-environment-setup-and-simulation-for-byd_ovrs25_cr5cb_bl16_rc71`。
- 当前请求分支：
  `feature/BYD_OVRS25_CR5CB_BL16_RC25_selena`。
- 旧实现使用 `max_candidates=512` 在授权 output root 中递归找 `selena.exe`；真实入口位于深层配置目录，探测漏掉后错误认为没有既有产物。
- 旧 attempt 的脚本第 73 行实际是注释：
  `rem radar-sim: clean command disabled; ... R2D2.py ... -clean`。
  日志里的 `echo Cleaning` 不能证明 clean 真正执行。
- 修复后，本机真实 runtime 使用实际 binding、实际 Bundle DB 和实际 Job payload 预演得到：
  `full_rebuild_required=True`、`reason=selena_branch_changed`、`clean=True`。
- 最终重试 attempt=4 的真实事件已出现：
  - `Selena build policy: full (selena_branch_changed)`；
  - `full Selena rebuild required: selena_branch_changed`；
  - 本机第 73 行恢复成实际可执行的 `...R2D2.py ... -clean`。

在 Job 没有达到最终 `succeeded`/`partial`/`failed`/`cancelled` 前，不能把“编译策略正确”写成“仿真成功”。

## 2. 产品定位和完成定义

`radar-sim` 是仿真编排和交付系统，不是 Selena 引擎。系统负责：

- 统一 `UserRunConfig 2.0`；
- 资源识别、路径授权和执行路由；
- Job/Stage 持久状态和恢复；
- Windows Connector 安装、连接、编译、外围准备和本地仿真；
- Cluster 提交、排队观察、结果收集；
- 单条/批量输入的逐项结果、partial 状态和结果归档；
- Web/SDK 的同一提交、等待、重试、诊断和下载能力。

“系统完成”必须同时满足：

1. 用户能从 Web 或 SDK 提交同一份 YAML，得到同一 `spec_hash`、同一 DAG 和同一 owner 语义。
2. 单条数据和多条数据都能完成；批量中某条 Selena 内部失败不会抹掉其他成功条目。
3. 单次安装 Connector 后，用户登录、自启动、断线重连、服务升级和重启都不会要求重复填写路径或重新注册一台逻辑电脑。
4. 多用户之间 Job、Agent、workspace、data、TransferPlan、Runtime Bundle、日志和结果不串 owner；正式多租户必须有真实认证，不能把 `X-Rsim-User` 当认证。
5. 编译策略可解释、可验证、可重现：新 Selena 分支不能复用旧分支产物做增量；同一构建槽位只有在 provenance 完整一致时才可增量。
6. 仿真结束后 Web 和 SDK 都能得到可校验的 Job 级结果包、Manifest、逐输入状态和可操作的失败原因。

本项目的第一优先级不是增加更多功能，而是保证“仿真流程不会被框架错误打断，也不会把未知状态错误判成失败或成功”。任何新增功能都必须先证明不会破坏以下事实：Agent 仍在线、Transfer 仍在推进、Stage 仍有真实外部活动、Cluster 任务仍存在、结果仍可追踪、旧 attempt 不会重复执行。

## 2A. 已冻结的最小框架范围

为了避免后续 AI 把系统扩展成复杂平台，第一阶段只允许有以下组件：

| 组件 | 必须做什么 | 不做什么 |
|---|---|---|
| Web | 编辑/导入 YAML、校验、提交、显示状态、取消/重试、查看 Manifest、下载结果 | 不在浏览器执行编译或搬运大文件 |
| Python SDK | 用同一 YAML/API 完成校验、提交、等待、取消/重试、诊断、下载 | 不实现第二套 DAG、第二套状态机或项目 Adapter |
| Linux 后端 | owner/auth、Job/Stage、路由、TransferPlan、Cluster submit/collect、结果目录和 API | 不编译 Selena，不执行 Windows 本地仿真，不接收大文件正文 |
| Windows Agent | 一次安装、路径授权、读取用户脚本、编译、准备外围、本地仿真、向 Cluster 目标直传、结果落盘 | 不安装 Visual Studio/Selena，不替用户修改项目工程 |
| Cluster | 接受配置和数据引用、排队、执行、提供状态和结果 | 不由框架假设固定完成时间 |

第一阶段只支持两种调用入口：Web、SDK；两种仿真目标：Windows local、现有 Cluster。`existing/build` 是 Selena 来源，不是项目类型。所有用户填写的项目目录、Selena 脚本、构建目录和输出目录必须被当作运行时输入进行授权和校验。

### 2A.1 项目无关但支持多 workspace

“不区分项目”不是“所有项目共享一份缓存”，而是：

- 用户不填写或选择项目注册表；
- 不写 `if project == ...`、项目专用 DAG、项目专用 clean 规则；
- 每次根据用户提供的 Selena 脚本、脚本所在 workspace、脚本引用的构建/输出目录、Runtime 和数据形成执行身份；
- 不同 workspace 可以并行；同一 workspace/同一输出目录必须串行；
- 两个 workspace 即使目录名不同，也必须各自保存 provenance，不能使用一个全局“最近一次编译分支”。

## 3. 用户和核心用户故事

### 3.1 Web 用户

1. 新用户打开 Web，下载统一 Connector，完成一次安装后看到“当前这台电脑、当前用户、contract 版本均已连接”。
2. 用户提交 `selena.source=existing` + `simulation.target=local` + 单个 MF4，系统在本机使用已有 Selena，不上传不必要的大文件，最终把结果写入 `result.path/<job_id>` 并保留 ZIP 下载。
3. 用户提交 `selena.source=build` + `simulation.target=local` + 多个 MF4，系统识别工作区和脚本；已有产物同分支可按策略增量，新分支必须全量 clean；编译完成后再运行批量。
4. 用户提交本地 Windows 数据到 Cluster，Connector 或 SDK 直接把正文写到 Cluster 可读数据面，Linux 只下发 TransferPlan，不把 MF4 经过 Web 中转。
5. 用户提交共享路径到 Cluster，系统原地登记或按计划复制，不因 Linux 看不到 Windows 路径而误判资源不可用。
6. 批量中 8 条成功、2 条 Selena 内部失败，用户看到 `partial`、8 个成功结果仍可下载、2 个失败输入及日志尾，并能只重试失败输入。
7. 编译排队、Cluster 排队或结果复制超过几十分钟，页面仍显示 heartbeat/进度/当前阶段，不因固定总超时错误终止。
8. 用户取消 Job 时，当前进程被终止，已完成的结果证据保留；再次重试时只重置必要 Stage，不重复编译、传输或 Cluster submit。

### 3.2 SDK/自动化用户

1. SDK 在 Linux、Windows 或 CI 中使用与 Web 相同的 YAML 和 `/api/v1`，不需要理解内部 project/profile/recipe/Agent mode。
2. SDK 能先 `validate_run()`，看到缺少 Connector、数据不可达、Cluster 根目录缺失、旧 contract 或参数问题，再决定是否提交。
3. SDK 使用稳定 owner、idempotency key 和 request hash；网络重试不会生成两个 Job 或两次外部仿真。
4. SDK 能用事件 cursor 或自适应轮询等待任意长的编译/仿真，区分 queued、running、needs_input、partial、failed、cancelled 和 succeeded。
5. SDK 能下载 Job 结果，校验 checksum，把 ZIP 和 Manifest 保存到用户指定目录；结果目录交付失败时仍能下载服务端归档。

### 3.3 多用户/多项目用户

1. 两个认证 owner 同时提交不同 workspace，两个独立构建槽位可以并行。
2. 两个 Job 使用同一个 workspace/output root，编译必须串行，第二个等待或明确提示占用，不能互相 clean/覆盖。
3. 同一电脑上不同用户提供的不同 Selena 脚本/项目文件夹，只有在 output root 不同且 provenance 独立时才可分别编译；若多个脚本共享同一输出目录，必须视为同一构建槽位，不能仅按“项目名”放行增量。
4. 同一项目切换 Selena 分支，旧分支产物不能被当成新分支的增量基础。
5. 同一 owner 的不同电脑不能互相冒充执行；正式认证模式下，owner 从认证主体派生，客户端不能用 header 改身份。

这里的“多项目”只表示用户可能提交多个不同 workspace，不表示系统增加项目配置层。项目文件夹和 Selena 脚本由用户提供，框架只做通用授权、脚本执行、产物确认和 provenance 管理。

## 4. 完整业务链路和状态合同

固定十阶段：

```text
resolve_spec
  -> environment_check
  -> prepare_source
  -> prepare_data
  -> build_selena
  -> register_artifact
  -> preflight
  -> run_simulation
  -> collect_results
  -> finalize_manifest
```

阶段可能 `skipped`，但不能为不同项目复制出另一套隐藏 DAG。后续 AI 必须逐阶段检查以下输入/输出：

| Stage | 主要职责 | 成功证据 | 失败/重试边界 |
|---|---|---|---|
| `resolve_spec` | 规范化 YAML、选择 route、识别 workspace/data/assets | canonical spec、spec hash、binding/路由证据 | 配置问题进入 `needs_input`，不启动编译 |
| `environment_check` | Connector、VS/Python/Perl/CMake、脚本、输出根、Runtime | path-free readiness checks、script checksum、build policy | 环境缺失可修复后重试；不搬大文件 |
| `prepare_source` | 选定 branch/worktree/source lease | requested ref、commit、worktree lease | source lease 失败只重试 source，不重跑无关数据 |
| `prepare_data` | 发现输入、快照、校验、TransferPlan | 完整文件集合、size/checksum、transfer manifest | partial transfer 可续租；输入变化必须丢弃 partial |
| `build_selena` | 按策略执行脚本并确认产物 | full/incremental policy、clean proof、exe/DLL/Bundle checksum | build 脚本/工具链错误与 Selena 内部错误分开；同一 stage 可重试 |
| `register_artifact` | 注册本机 Bundle 或 Cluster 传输 | stable Bundle ID/transfer ref | 不能重复提交已成功的 Bundle |
| `preflight` | 检查完整数据、Runtime、MatFilter、Adapter、执行权限 | all roles resolved、execution plan | 不通过不能启动 Selena |
| `run_simulation` | 本地或 Cluster 执行 | process/Cluster terminal + per-input evidence | 本地/Cluster 执行可长时间运行；取消只终止本次执行 |
| `collect_results` | 收集输出、`result.ini`、MF4、逐输入状态 |完整 result manifest、result ref、checksum | 只能重试收集，不重新 submit Cluster |
| `finalize_manifest` | 固化最终业务状态和下载引用 | immutable manifest、status 一致 | finalizer 重试不重新编译/仿真 |

每个 Stage 的回调必须带 `job_id/stage_id/attempt/agent_id/owner`，控制面必须 fence 旧 attempt；服务重启后必须能从 SQLite 状态、outbox 和 handoff 恢复。

## 4A. 仿真正确性优先的状态审查

历史问题反复发生在“Agent 卡住、传输卡住、流程卡住、结果拿不到、运行中被判 fail”。后续 AI 必须把状态拆成三层，不能用一个 `status` 字段互相覆盖：

1. **控制面状态**：Job/Stage 是否被创建、queued、claimed、running、terminal，是否有合法 attempt。
2. **执行状态**：Agent 进程/Connector、Windows 子进程、Transfer、Cluster 外部 Job、结果复制是否仍有真实活动。
3. **业务结果状态**：输入是否成功、失败、部分成功，Manifest/Checksum 是否完整，结果是否可下载。

### 4A.1 Agent 卡住不能直接判失败

必须区分：

- Agent 心跳正常但暂时没有日志：`running/observing`，继续等待；
- Agent 心跳断开但 execution lease/PID/外部 Job 仍能证明执行：`reconnecting/unknown`，继续恢复观察；
- Agent 进程确认退出、子进程树已结束、没有可接管 lease：才允许进入 stale recovery；
- stale recovery 必须先 fence 旧 attempt，再决定重试；旧 callback 晚到时不能重复启动；
- 不能用“超过 N 分钟没有日志”替代存活证据；
- 不能把控制面暂时不可达、Agent 离线和 Selena 返回非零混成同一个 `failed`。

### 4A.2 数据传输卡住不能丢数据

每个 TransferPlan/resource role 必须有：

- source fingerprint、目标引用、size/checksum；
- 已发送 offset、最近成功 chunk 时间、最近心跳、重试次数；
- `.partial` 文件和最终文件分离；
- 源文件变化时丢弃旧 partial，而不是继续拼接；
- 服务/Connector 重启后能从校验过的 offset 继续；
- 已完成 role 幂等重放，不重复创建目标根；
- 长时间无进度时先进入 `stalled/observing` 或告警，必须有 deployment policy 才能转 retryable failure；
- 传输未完整时不得进入 `preflight`/`run_simulation`。

### 4A.3 仿真流程卡住不能误判为 fail

本地和 Cluster 都必须有可恢复观察点：

- `run_simulation` 记录 execution token、PID/Job ID、启动时间、最近心跳、最近日志/进度、子进程树或 Cluster 唯一查询键；
- Cluster submit 先保存 submission receipt，控制面重启后按唯一 Config 路径反查，不重复 submit；
- Cluster 排队时间不设置总时长上限；状态页短暂不可达只降低观察能力，不立即失败；
- local batch 每个输入有 checkpoint，Connector 重启后只恢复未完成输入；
- cancel 是用户动作，必须终止当前 attempt 并保留已固化结果，不得被后台 stale 逻辑误转成普通 failure；
- 只有明确证明进程/外部 Job 已终止且没有结果证据时，才可最终失败。

### 4A.4 运行状态和业务结果不能互相误判

- 进程退出码为 0 不是唯一成功证据；必须有完整 Manifest、输出文件、checksum 和逐输入结果；
- 进程仍在运行不能因为阶段没有新日志被判失败；
- Cluster 页面显示 finished 不能代替结果共享目录中的完整 `result.ini`/MF4；
- 结果目录不可写不能抹掉 server-side ZIP；
- partial 只能由真实 Selena per-input 混合结果产生，不能由框架故障产生；
- `finalize_manifest` 只能消费已固化 result_ref/summary，不重新运行仿真，也不能把旧 Manifest 覆盖成新状态。

后续 AI 必须为每一种“卡住/未知/恢复/终态”建立事件序列测试，并证明最终不会出现：Stage 永久 running、Job 永久 queued、已提交 Cluster Job 丢失、结果已生成但 Web/SDK 无法下载、运行中被错误标成 failed、retry 重复运行已成功输入。

## 5. 编译策略：多项目、分支和产物的明确规则

### 5.1 构建槽位，而不是项目名

编译互斥和缓存键必须以真实构建边界为准：

```text
build_slot = physical_device/workspace_binding_id/selena_script/output_root
```

逻辑 execution identity/project 只用于索引和追踪，不能作为“这个目录安全”的唯一证据。后续 AI 必须确认：

- 不同 workspace 或不同 output root：可并行；
- 同一 workspace + 同一 output root：同一把 OS lock，必须串行；
- 同一 output root 被多个逻辑 project 使用：视为同一槽位，不能并行或分别缓存；
- branch worktree 若使用独立输出目录，必须把 worktree/output root 一起写入 provenance；
- owner 隔离不能替代物理 workspace lock，两个 Connector 进程仍可能同时碰同一目录。
- build script 本身是用户显式输入和执行入口；框架不得替换成内部项目脚本，也不得从项目名推导另一个脚本。

### 5.2 产物 provenance 最小字段

每次可复用构建都必须持久化以下字段，并在 `build_selena` 结果中返回 path-free 摘要：

- `workspace_binding_id`、逻辑 execution identity；
- `workspace_root_fingerprint`、`output_root_fingerprint`；
- Selena `branch/ref`、实际 `commit`、dirty/source fingerprint；
- build mode/config；
- build script checksum 和识别出的 clean command checksum/行号；
- Visual Studio、CMake、Python/Perl 等 toolchain fingerprint；
- `Selena.exe`、同目录 DLL、Runtime XML 的 checksum/size；
- Runtime Bundle ID、创建时间、build stage/attempt；
- `build_policy.mode`：`fresh`、`incremental` 或 `full_clean`；
- `clean_required`、`clean_applied`、`clean_proof`、`reason`。

只依赖“最近一次 Bundle lease”不够。建议保留不可变 `BuildProvenance` 记录或 sidecar，避免旧 archive GC 后无法证明构建来源。

### 5.3 决策矩阵

后续 AI 必须实现并测试下面的矩阵；任何未列出的组合都 fail-closed：

| 条件 | 模式 | 行为 |
|---|---|---|
| output root 为空、无历史 Bundle、无旧 object | `fresh` | 可不执行 clean，但必须明确记录“从空构建状态开始”；不能把它宣传为增量复用 |
| output root 非空但无 provenance | `full_clean` | 有可识别 clean 命令则执行；没有则阻断并要求用户修复脚本 |
| provenance branch 不同 | `full_clean` | 即使 exe 看似可用，也必须先 clean；不能复用旧分支 object |
| branch 相同但 commit/dirty/source fingerprint 改变 | 默认 `incremental`，若依赖图/脚本不能证明安全则 `full_clean` | 记录为什么允许增量；禁止静默猜测 |
| build mode/config/toolchain/build script 不同 | `full_clean` | 旧输出不可直接复用 |
| output root/workspace/script 变化或 provenance 与 artifact checksum 不一致 | `full_clean` | 重新确认整个 Bundle |
| provenance 完整一致、artifact/DLL/Runtime checksum 一致、同一 build slot | `incremental` | 只在 build lock 内运行，记录 `incremental_reused=true` |
| branch/ref/commit 无法解析 | `full_clean` 或 `needs_input` | 不能当作“同一个分支” |
| clean 语义无法识别 | `blocked` | 不得用增量代替全量；提示用户补充通用可执行 clean 入口 |

### 5.4 如何确认真的全量，而不是只打印了 Cleaning

验收必须同时有四类证据：

1. 决策证据：`build_policy.mode=full_clean`、`reason=selena_branch_changed`。
2. 脚本证据：实际执行前第 N 行从注释恢复为 active command；script checksum 与执行版本一致。
3. 运行证据：Agent 事件记录 `clean_applied`/`full Selena rebuild required`；不是只看 `echo Cleaning`。
4. 产物证据：clean 前后的构建状态/目录 generation 或 clean marker 可解释，最终 Bundle 的 branch/commit/checksum 与本次请求一致。

后续 AI 必须增加测试：深层配置目录、多个 `selena.exe`、用户脚本内部构建目录/输出目录、脚本 line continuation、不同注释语法、`R2D2 --clean`、CMake/MSBuild clean、无 clean command、同分支不同 commit、不同 workspace 共享 output root、两个并发 Job。

## 6. 单条、批量和 partial 结果规则

### 6.1 逐输入模型

每个输入必须拥有稳定的：

- `input_index`；
- `input_relative_path`；
- 输入 checksum/size；
- `output_relative_path`；
- `status`：`queued/running/succeeded/failed/skipped/cancelled`；
- return code、稳定 error code、受限日志尾；
- retry count 和最后一次 attempt。

### 6.2 最终状态矩阵

| 情况 | Job/Manifest 状态 | 已成功结果 | 是否可重试 |
|---|---|---|---|
| 所有输入成功且归档完整 | `succeeded` | 全部可下载 | 可按需重新运行，不应默认重复 |
| 至少一条成功、至少一条 Selena 内部失败 | `partial` | 必须保留并可下载 | 只重试失败输入 |
| 全部输入 Selena 内部失败 | `failed`，归因 `simulation` | 可能有诊断包，无成功输出 | 可重试失败输入/整个批次，由用户决定 |
| Connector/工具链/Runtime/Transfer/Manifest 框架失败 | `failed` 或 `needs_input` | 不能伪装成 partial | 修复外部条件后从最近安全 Stage 重试 |
| 用户取消 | `cancelled` | 已完成且已固化的结果保留 | 明确是新 attempt，不重复已完成输入 |
| 仿真成功但 result.path 写入失败 | 业务结果仍应可从 ZIP 获取 | ZIP 必须保留 | 只重试 delivery，不重跑仿真 |
| Manifest/Checksum/归档不一致 | `failed` | 不发布不可信结果 | 重跑收集/归档；不能标 succeeded |

### 6.3 partial 的绝对边界

只有真实 Selena per-input 结果导致混合成功/失败时才允许 `partial`。以下情况不能借“有一个文件成功”变成 partial：

- Connector 依赖缺失；
- paramconfig 生成失败；
- Runtime Bundle 解压/校验失败；
- 输入传输不完整；
- Agent 与控制面失联且没有 execution lease 证据；
- 结果归档或 Manifest 校验失败；
- 所有输入都失败。

后续 AI 必须交付“只重试失败输入”的 API/SDK/Web 行为和实测证据，确保成功输入不会重复消耗编译/仿真资源。

## 7. Agent 安装、永久复用和升级

### 7.1 用户可见目标

用户只需要：

1. 从 Web 或 SDK 获取同一个 Connector 安装入口；
2. 安装一次，选择/确认当前电脑和稳定 owner；
3. 安装器检查 Python、依赖、网络、服务地址、VS/脚本前置条件；
4. 成功后自动登录启动、断线重连、单实例运行；
5. Server contract 不兼容时一键更新，不重填 YAML、不丢 Agent ID/binding；
6. 在 Web/SDK 中看到真实的 `agent_id + owner + exact device + contract`，不能用同 owner 的另一台电脑冒充在线。

### 7.2 安装/升级必须验证

- 包下载 checksum、大小、Range/断点行为；
- 安装目录、数据目录、credentials、install metadata 和 recovery copy 的权限；
- 旧 supervisor/watchdog/遗留 Python 进程树先停止，避免两个 Agent 用同一 ID 竞争；
- 更新后实际 Python import 的 `WINDOWS_CONNECTOR_CONTRACT_VERSION` 和服务端要求一致；
- 进程异常退出后 watchdog 只恢复同一安装，不重复启动多份逻辑 Agent；
- 配置损坏时从 recovery copy 恢复；恢复失败要给清晰重连动作；
- 无网络、代理、企业杀毒拦截、没有 Python、Python 包版本不匹配、没有 VS、脚本不存在时均返回可操作诊断；
- 不把 Selena、Visual Studio、Runtime 或企业内部 DLL 偷偷安装为框架依赖；它们属于用户环境，必须在 readiness 中检查。

### 7.3 当前重点风险

- 当前 `authentication_required=false` 的内网部署中，`X-Rsim-User` 是可伪造的路由标签，不是认证；这不满足不受信多用户生产要求。
- 必须实测首装、重启、杀掉 supervisor、断网恢复、server 重启、contract 升级、旧 owner 迁移和单实例；不能只读安装脚本。

### 7.4 正式多用户上线门禁

以下任一项未通过，只能称为“受信内网单用户/测试部署”，不能称为“支持多用户”：

- Web 和 SDK 的 owner 都来自同一个已验证认证主体；
- 伪造 `X-Rsim-User`、修改 SDK `user`、猜测 Job/Result/Transfer ID 都无法跨 owner 读取或操作；
- Agent 注册绑定了 owner、稳定 Agent ID、设备/安装实例和服务地址；
- 同 owner 的两台设备不能互相冒充“当前电脑”；
- 两个 owner 同时提交相同输入时，代码、数据、Bundle、临时目录和结果不会串用；
- 审计日志能回答谁在什么时候提交、取消、重试、下载了哪个资源，但不泄露文件正文；
- 认证、owner、授权和结果下载的真实集成测试通过，并有失败时的回归测试。

## 8. Web、SDK 和 API 一致性

### 8.1 单一合同

- Web、SDK、REST 都使用 `UserRunConfig 2.0`；
- Web 和 SDK 生成相同 canonical YAML、`spec_hash` 和十阶段 DAG；
- SDK 不实现第二套等待/重试/结果判定规则；
- SDK base URL 约定必须明确：`RadarSimClient` 的 base URL 不带 `/api/v1`，客户端负责追加版本前缀；
- `idempotency_key + request_hash + owner` 必须贯穿创建、网络重试和服务重启；
- API 错误 envelope 必须给稳定 code、detail、action、job/stage/resource 引用；不把本地绝对路径泄露给 Linux/Web。

### 8.2 SDK 必须交付的能力

- `validate_run()`：只做 readiness/route 预览，不启动编译或仿真；
- `submit_run()`/`submit_yaml()`：幂等创建，返回 Job ID、spec hash、首个状态；
- adaptive `wait_job()`：事件 cursor 优先、退避轮询兜底、无固定仿真总时长；
- `cancel_job()`、`retry_stage()`、失败输入重试；
- `get_manifest()`、`get_diagnosis()`、`download_job_result()`：checksum 校验、临时文件、原子 rename、断点/失败重试；
- 能区分 `needs_input`、Connector offline、Cluster queued、framework failed、simulation partial 和 succeeded；
- SDK 的真实示例和集成测试，不能只验证 HTTP 200。

## 9. 数据传输和结果获取链路

### 9.1 数据链路必须打通

对每个资源角色分别验证：dataset、Runtime Bundle、Runtime XML、MatFilter、Adapter、Result archive。

```text
用户文件/已有共享文件
  -> 识别与 checksum 快照
  -> 绑定 owner + device + role
  -> TransferPlan（或 original_read/shared_zero_copy）
  -> 目标端 partial 文件
  -> chunk 校验、续租、原子 rename
  -> 完整 manifest
  -> preflight
  -> 仿真
  -> result_ref + canonical ZIP
  -> Web 下载 / SDK download_job_result / 可选 result.path
```

必须验证：源文件变化、断网、服务重启、Connector 重启、重复 chunk、上传超时、磁盘满、目标权限、取消、同一请求重复提交、多个资源只完成一部分等情况。

### 9.2 结果获取用户体验

- Web 任务详情显示结果状态、成功/失败输入计数、Manifest、诊断和下载按钮；
- SDK 返回 ZIP/Manifest 的 checksum 和保留时间；
- `result.path` 是便利交付，不是唯一真相；路径不可写时不应丢失 server-side ZIP；
- 大结果下载采用临时文件 + checksum + atomic rename，断流后不留下可被误用的完整文件名；
- 结果过期、GC、磁盘水位和 retention 必须有可见错误及管理员告警；
- 不能只展示“仿真完成”，必须能回答“哪几条成功、哪几条失败、成功文件在哪里、是否可下载”。

## 10. 非引擎框架风险清单

后续 AI 必须逐项输出 `现状/证据/风险等级/修改/测试/未解决`：

1. 认证缺失、owner header 可伪造、跨 owner 读取/下载/重试；
2. Web/SDK owner 不一致、旧 owner 迁移、Agent ID 复用、双 Agent 竞争；
3. 同 workspace 多 Job 并发编译、不同项目共用 output root、clean 互相删除；
4. branch/commit/toolchain/build script/output root provenance 不完整；
5. clean 命令识别漏检、动态脚本、脚本被外部修改、只打印 echo 未真正 clean；
6. build/Cluster/local execution 被固定 timeout 误杀；
7. queued/running 等待期间 heartbeat、stale reclaim、服务重启和旧 callback fencing；
8. cancel 与 success callback 竞态，retry 后下游仍 cancelled/blocked；
9. TransferPlan/上传 session 断点续传、空闲 lease、源变化、重复 manifest；
10. Cluster submit 成功后 control restart 导致重复外部 Job；
11. Cluster 结果目录晚到、状态页面短暂不可达、大批量 result.ini 截断或 O(n²) 扫描；
12. 本地 batch checkpoint、Connector 重启、已成功输入重复执行；
13. partial 被误当 framework success，或 result.path 失败导致整个业务结果丢失；
14. Manifest/result_ref/catalog ZIP 不一致、下载断流、过期和 GC；
15. 用户安装环境没有 Python/VS/Perl/CMake、代理/杀毒/权限阻断、更新后旧代码仍被 import；
16. Linux 控制面把大文件正文当 HTTP body 中转、跨平台路径误传到错误执行节点；
17. 所有状态只在内存或 UI 进度，不具备 SQLite/outbox/lease 的可恢复证据；
18. 只测一个 owner、一个项目、一个文件、一个短任务，无法证明多用户批量生产能力。

## 11. 给后续 AI 的分工任务和必须交付物

后续 AI 可以并行分析，但每个任务必须有独立证据，最后由主 AI 汇总、集成、部署和验收。

### Task 0：先做完整性审查，再改代码

这是所有后续工作的入口任务，不能跳过：

- 从 Web 和 SDK 各提交一次同一配置，记录 `spec_hash`、owner、Job/Stage DAG；
- 按 `resolve_spec -> environment_check -> prepare_data -> build -> register -> preflight -> run -> collect -> finalize` 逐阶段追踪真实事件；
- 对每个阶段回答四个问题：当前状态从哪里来、什么证据证明它、断线/重启后如何恢复、什么条件才允许 terminal；
- 专门制造 Agent 心跳停止、日志停止但进程存活、Transfer 无进度、Cluster 状态页不可达、结果目录晚到、服务重启、旧 callback 晚到和 result.path 不可写；
- 对每个问题区分“真实失败”“暂时未知”“正在恢复”“用户取消”“仿真内部失败”，并确认 Web、SDK 和数据库表达一致。

交付物：

- `docs/audits/<date>-simulation-correctness-gap-matrix.md`；
- 一张“卡住/未知/恢复/失败/成功”状态转移表；
- 每个故障注入场景的 Job/Stage/Event/Manifest 证据；
- P0/P1/P2 风险清单；
- 只有在 Task 0 完成后，其他 AI 才能开始声称某条链路已修复。

### Task 0.1：后端状态必须由证据驱动

行动：

- 找出所有把“没有日志”“HTTP 查询失败”“心跳超时”“进程退出码”“Cluster 页面状态”直接转换为 `failed` 的代码；
- 为 Agent、Transfer、local run、Cluster run、collect、result upload 分别定义 liveness、progress、external terminal、business terminal；
- 把“暂时不可观察”与“已经失败”分开，设计可重试的观察状态和告警；
- 对所有 stale/reclaim/cancel/retry 路径验证不重复执行、不丢结果、不遗留永久 running/queued。

交付物：

- 每个状态判断点的代码位置和证据字段；
- 失败误判回归测试；
- 服务重启和 Agent 重启的恢复测试；
- Web/SDK 对 `observing/reconnecting/needs_input/failed/partial` 的一致展示和 API 响应。

### Task A：产品合同和用户故事审计

行动：

- 对照本文件、PRD、Product Contract 和 V2 Architecture，找出字段、状态、错误码和 UI 行为不一致；
- 补齐 existing/build、local/cluster、single/batch、Web/SDK、partial/cancel/retry 的场景矩阵；
- 标记“当前代码实现”“仅文档声明”“需要真实实测”。

交付物：

- `docs/audits/<date>-product-scenario-matrix.md`；
- 一张覆盖至少 4 种 Selena 来源/目标组合、单条/批量、路径位置和用户入口的验收表；
- 每个场景的输入、预期 Stage、最终状态、结果位置、重试动作。

### Task B：控制面和状态机审计

行动：

- 审查 `core/control_service.py`、`core/api_v1.py`、`cli/server.py` 的 claim、heartbeat、stale、cancel、retry、restart、handoff、outbox；
- 用事件序列验证重启发生在 Stage result commit 和 successor bind 之间时不会丢阶段；
- 验证旧 attempt callback、新 attempt fence、取消/成功竞态和 partial finalize。

交付物：

- 状态转移表和非法转移列表；
- 每个恢复点的 SQLite 证据/测试；
- `tests/test_*control*` 新增或修改的回归测试；
- 失败时明确指出是可自动恢复、需用户 retry 还是必须人工介入。

### Task C：多用户、认证和资源隔离

行动：

- 审查 owner 来源、认证中间件、Agent owner/device 绑定、Job/Transfer/Result 查询和下载授权；
- 证明 `X-Rsim-User` 在无认证部署下可伪造，并给出正式 Bearer/SSO 的启用门禁；
- 实测两个 owner、两台设备、同 workspace、不同 workspace、同 Job id 猜测和结果下载交叉访问。

交付物：

- `docs/audits/<date>-multi-user-security-audit.md`；
- 跨 owner 拒绝测试报告；
- 认证启用后的部署配置、回滚方案和安全验收项；
- 若未启用认证，必须把结论写成“受信内网试用，不满足正式多租户”。

### Task D：多项目、多构建槽位和 Selena provenance

行动：

- 审查 `core/agent_build_stage.py`、`core/build_script_policy.py`、`core/agent_runtime_bundle_lease.py`、`core/build_lock.py`、`core/workspace_recognizer.py`；
- 实现/验证本文件第 5 节决策矩阵；
- 用两个项目、两个 output root、同 root 不同 branch、同 branch 不同 commit、无 provenance、深层 exe、多个 exe 做真实测试；
- 验证 full/incremental 不依赖项目名和固定目录；
- 验证 full 必须有 clean policy、脚本实际 active line、结构化 policy event 和产物 provenance。

交付物：

- `docs/audits/<date>-selena-build-provenance-audit.md`；
- `tests/test_agent_build_stage.py`、`tests/test_build_script_policy.py` 的矩阵测试；
- 一份真实 build log，包含 `build_policy`、clean proof、branch/commit、Bundle ID；
- 若发现无法安全判断，必须阻断，不得默认增量。

### Task E：Agent 一键安装、升级、重连和单实例

行动：

- 审查 `scripts/install_windows_connector.ps1.in`、`scripts/bootstrap.ps1`、`scripts/start_windows.ps1`、watchdog、package builder；
- 实测干净 Windows 用户首装、已有旧版本升级、服务地址变化、owner 变化、进程崩溃、断网、电脑重启、安装目录部分损坏；
- 检查父/子 Python 进程、旧 supervisor、`.pyc`、recovery metadata 和 identity 保留；
- 验证 exact `agent_id + owner + contract`，不能只看 owner 聚合数量。

交付物：

- `docs/audits/<date>-connector-install-upgrade-audit.md`；
- 安装器逐步输出和截图/日志；
- 包 checksum、版本、回滚和重连验收记录；
- 首装/升级/重启/断网/单实例自动化或半自动化测试；
- 给用户的 1 页安装操作说明和故障恢复说明。

### Task F：Web/SDK/API 同合同和等待机制

行动：

- 对比 Web JS、SDK、REST schema、API response、error envelope；
- 验证 base URL、owner、认证、idempotency、event cursor、自适应轮询、cancel/retry、partial 和结果下载；
- 让 SDK 在长编译/Cluster 排队中不使用固定总时长；网络错误重试不能重复提交。

交付物：

- `docs/audits/<date>-web-sdk-parity-audit.md`；
- SDK 示例：validate、submit、wait、diagnose、download、retry failed inputs；
- Web 与 SDK 同 YAML 的 `spec_hash/DAG` 对比报告；
- 真实 SDK Job ID、事件 cursor、Manifest 和下载 checksum 证据。

### Task G：数据、TransferPlan、断点续传和批量输入

行动：

- 审查 `core/dataset_store.py`、`core/artifact_store.py`、`core/direct_transfer.py`、`core/transfer_service.py`、`core/cluster_stage_executor.py`；
- 测试单文件、批量、250+ 文件、源变化、断点、重复 chunk、服务重启、目标磁盘满、共享路径、UNC、dataset/shared 逻辑路径；
- 证明 Linux 不接收大文件正文，Connector/SDK 直接写目标数据面。

交付物：

- `docs/audits/<date>-data-transfer-batch-audit.md`；
- TransferPlan 生命周期图和资源 role 清单；
- 断点续传/源变化/重复请求测试结果；
- 大批量 Manifest 完整性报告，包含输入数量和 checksum 统计。

### Task H：本地/Cluster 仿真、partial 和 retry 语义

行动：

- 审查 `core/agent_local_run.py`、`cli/agent.py`、`core/cluster_stage_executor.py`、`core/result_delivery.py`；
- 制造“成功/失败混合”“全部失败”“framework failure + 一个旧成功输出”“取消中断”“Connector 重启”场景；
- 验证成功输入不重复执行，失败输入可独立重试，Cluster collect retry 不重新 submit；
- 区分 Selena 内部失败和 Connector/框架失败。

交付物：

- `docs/audits/<date>-partial-result-audit.md`；
- 每个输入的状态表、最终 Manifest、Job diagnosis、可下载 ZIP；
- 失败输入重试后的 attempt/资源消耗证据；
- 明确哪些情况是 `partial`、`failed`、`needs_input`、`cancelled`。

### Task I：结果归档、下载和 retention

行动：

- 审查 `core/local_results.py`、`core/result_upload_service.py`、`core/api_v1.py` 的 manifest/result/diagnosis/download；
- 测试 result.path 不可写、ZIP 生成后 HTTP 断流、重复下载、checksum 不一致、过期/GC、服务重启；
- 验证成功业务结果不会因本地交付失败而丢失。

交付物：

- `docs/audits/<date>-result-delivery-audit.md`；
- Web/SDK 下载示例、checksum、临时文件和最终文件证据；
- retention/GC/磁盘水位策略和告警清单；
- result delivery 失败时的稳定错误码和用户动作。

### Task J：Cluster 提交、长队列和结果收集

行动：

- 审查 `core/cluster.py`、`core/cluster_runs.py`、`core/cluster_stage_executor.py`；
- 测试 submit 成功后控制面重启、外部 manager 返回 task count、状态页暂时不可达、队列很长、结果目录晚到、大批量 result.ini；
- 证明不会重复外部提交、不会被固定总超时杀掉、不会截断逐输入结果。

交付物：

- `docs/audits/<date>-cluster-long-run-audit.md`；
- submission receipt、Config.cfg 唯一查询路径和 recovery 证据；
- 大批量结果数量/Manifest 对比；
- Cluster 不可达、结果目录晚到和可重试 collect 的诊断样例。

### Task K：端到端发布、部署和 rollback

行动：

- 按 `docs/release-deployment.md` 做 release 目录、候选测试、systemd、health、capabilities、Connector ZIP、无活动 Job 门禁；
- 真实验收 Web + SDK、Windows local existing/build、Cluster existing/build、single/batch/partial、服务/Connector 重启；
- 保留旧 release、systemd backup、Connector 回滚和数据库迁移方案。

交付物：

- `docs/handoffs/<date>-release-acceptance.md`；
- release commit、目录、systemd backup、health/capabilities、ZIP checksum；
- 每个验收场景的 Job ID、Stage/Event、Manifest、结果下载 checksum；
- 上线结论：`可上线/有条件上线/阻断`，阻断项必须有 owner、修复动作和复测方式。

### Task L：Web 和 SDK 两种调用方式的真实闭环

行动：

- 用同一份 `UserRunConfig 2.0` 分别从 Web 和 Python SDK 完成 validate、submit、wait、cancel/retry、diagnosis、manifest 和 download；
- 对比两次请求的 canonical config、`spec_hash`、Stage DAG、owner、TransferPlan、最终 Manifest；
- 在 SDK 进程重启、网络短断、HTTP 5xx/429、长时间 queued/running 时验证幂等和自适应等待；
- 验证 SDK 不要求用户理解内部 project、Agent mode、Runtime Bundle 或 Cluster 拓扑。

交付物：

- `docs/audits/<date>-web-sdk-e2e-audit.md`；
- Web Job ID + SDK Job ID 的逐项对照表；
- SDK 可复制运行的示例和下载 checksum；
- 网络重试不重复提交的事件/数据库证据。

### Task M：源到源传输和 local/Cluster 组合验收

行动：

- 覆盖 Windows 本地源 -> Cluster 目标、SDK/Linux 源 -> Cluster 目标、共享路径原地读取、Windows local 目标；
- 验证正文不经过 Linux API，TransferPlan role、relative path、checksum、partial、续租和完成 manifest 完整；
- 对 `existing/build` × `local/Cluster` 做组合测试；
- 对当前不支持的远端到 Windows `source_to_local` 验证稳定 `unavailable/needs_input`，不能静默绕路。

交付物：

- `docs/audits/<date>-source-to-source-routing-audit.md`；
- 每个组合的 route/Stage/TransferPlan/目标文件 checksum；
- Linux 请求体大小证明和目标端文件证明；
- 不支持组合的 UI/SDK/API 一致错误样例。

### Task N：用户脚本驱动的项目无关自动编译

行动：

- 准备至少两个目录结构、脚本风格和 output layout 不同的 Selena workspace；
- 用户显式填写脚本路径，脚本包含/引用构建目录和输出目录；框架不得替换脚本或按项目名加载配置；
- 不提供 project 名称，验证系统仍能用脚本、workspace、Runtime、数据和文件证据完成识别；
- 覆盖 fresh、same-branch incremental、same-branch-new-commit、branch switch full clean、不同 workspace 同 output root、不同 root 并行；
- 验证每个构建的 provenance 可解释，换 workspace/脚本/分支不会复用旧 Bundle。

交付物：

- `docs/audits/<date>-project-free-build-matrix.md`；
- 真实脚本和脱敏构建目录矩阵；
- 每个 case 的 policy、clean proof、Bundle ID、产物 checksum；
- 项目专用分支/目录/recipe 依赖清单；发现任何硬编码即阻断。

### Task O：Agent 安装、恢复和长期使用验收

行动：

- 从全新 Windows 用户开始，不依赖开发机残留状态，完成下载安装、依赖检查、稳定 owner、binding、自动启动；
- 模拟断网、Linux 服务重启、电脑重启、杀死 Agent、升级 contract、旧安装目录损坏、代理/杀毒/权限问题；
- 检查 single instance、watchdog、recovery copy、旧进程树、`.pyc` 和实际 import 版本；
- 安装后分别用 Web 和 SDK 提交任务，证明同一 Agent 可长期复用且不重复注册。

交付物：

- `docs/audits/<date>-agent-user-journey-audit.md`；
- 清洁环境安装日志、升级/断网/重启日志；
- exact `agent_id + owner + device + contract` 证据；
- 用户一页安装、升级、重连、卸载和故障排查说明。

## 12. 必须执行的验收矩阵

后续 AI 不能只跑默认单元测试，至少要完成：

| 类别 | 最低验收 |
|---|---|
| 输入规模 | 1 条、3 条、250+ 条 MF4；含一个失败、全成功、全失败 |
| Selena 来源 | `existing`、`build` |
| 目标 | `local`、`cluster` |
| 数据位置 | Windows 本地、UNC、Cluster 可读共享、SDK/Linux 可读 |
| 构建 | 首次 fresh、同分支增量、同分支新 commit、跨分支 full clean、无 provenance、深层输出、多项目同 root/不同 root |
| 用户入口 | Web、Python SDK、REST 直调 |
| 用户/设备 | 两个认证 owner、两台电脑、同 owner 多 Job、同 workspace 并发 |
| Agent | 首装、重启、断网、watchdog、升级、旧版本、配置损坏、单实例 |
| 控制面 | API/Connector/Cluster 临时不可达、服务重启、callback 延迟、stale、cancel、retry |
| 数据/结果 | chunk 重试、源变更、磁盘满、result.path 不可写、ZIP 断流、checksum、retention |
| Cluster | submit receipt、外部 job 已存在、长队列、状态页不可达、结果晚到、大 batch |

每个真实验收至少保留：

- request YAML 的脱敏副本和 `spec_hash`；
- owner、Agent ID、Connector contract、服务 release；
- Job ID、Stage ID、attempt、关键事件 cursor；
- build policy/provenance 或 execution plan；
- Manifest、逐输入状态、result_ref、ZIP checksum；
- 失败时的稳定 code、diagnosis、用户动作；
- 测试时间、环境、是否为 Selena 内部失败；
- 复测命令和结论。

## 13. 最终交付包格式

后续 AI 完成后必须交付一个可被人和另一个 AI 继续使用的 handoff，至少包含：

1. **结论**：当前是否满足“多用户 Web/SDK + 单条/批量 + local/Cluster + 结果下载”。
2. **已完成修改**：commit、文件、关键逻辑、迁移/配置。
3. **真实证据**：部署 release、health、capability、Connector、Job、Manifest、结果下载。
4. **场景矩阵**：每一项 `passed/failed/blocked/not tested`，不能用空白代替。
5. **风险分级**：P0/P1/P2，说明是否阻断正式多用户上线。
6. **未完成事项**：具体文件、具体测试、下一步动作和责任边界。
7. **回滚方法**：代码 release、systemd、Connector、数据库/配置。
8. **不要重复做的事情**：已验证的命令和已知不会解决问题的尝试。

最终结论只能从下面三种中选一个：

- `可上线`：所有 P0/P1 及发布门禁通过；
- `有条件上线`：仅受信内网/单用户/指定场景可用，明确限制；
- `阻断`：存在 owner/auth、结果完整性、重复执行、分支污染、数据丢失或无法恢复问题。

## 14. 当前审计结论

当前已确认：

- 跨 Selena 分支的全量编译策略已在真实 Job attempt=4 上被事件、脚本实际内容和本机策略预演共同证明；
- 取消后重试的下游 Stage 解锁已被控制面测试和真实 Job 状态证明；
- 长编译没有被固定总时长提前杀掉，heartbeat、进程和构建日志正在工作。

当前不能提前宣称：

- `job_26028465ebeb` 最终仿真成功；
- 两个认证 owner 的正式多租户安全已通过；
- 首装/升级/断网/重启/SDK/Cluster/250+ 批量的完整真实链路已全部通过；
- result.path、Web ZIP、SDK 下载和 partial retry 已在生产环境端到端通过。

后续 AI 的第一优先级是完成本文件第 11 节的审计任务和第 12 节的真实验收，不是继续增加项目专用分支或固定等待时间。
