---
title: radar-sim 项目交接文档
description: 项目现状、架构、已知问题和后续 TODO
---

# radar-sim 项目 Handoff

## 0. 2026-08-04 当前发布交接（优先阅读）

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
