---
title: radar-sim 项目交接文档
description: 项目现状、架构、已知问题和后续 TODO
---

# radar-sim 项目 Handoff

## 0. 2026-08-04 当前发布交接（优先阅读）

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
- 换电脑、换 Linux 服务地址、切换 full/light 或卸载后才重新连接；Visual Studio 始终由用户自行安装，Agent 只检测/提示并做脚本参数适配。
- SDK 调用方在 Windows 上对已有 Selena + Cluster 可直接通过 `RadarSimClient` 上传本地目录、Runtime、资产和数据，不强制安装 Agent；SDK 调用方在 Linux 上只能使用共享/Linux 可读路径，不能读取 Windows `C:/`、`D:/`。

### 代码、测试与线上证据

- 重点代码：`core/api_v1.py`、`core/control_service.py`、`radar_sim_web/static/app.js`、`scripts/bootstrap.ps1`、`scripts/start_windows.ps1`。
- 回归测试：`python -m pytest -q tests/test_api_v1_service.py tests/test_existing_selena_agent_resolution.py tests/test_api_v1_fastapi.py` → `82 passed, 1 warning`；`node --check radar_sim_web/static/app.js` 通过。
- 线上服务：`http://10.190.171.44:8877`，systemd user service `radar-sim-v1.service`，当前单一监听进程，`GET /api/v1/health` 返回 200。
- 新用户无 Agent 的实际验证：能力快照只显示 Cluster 可用、不显示他人的 Windows Agent；提交含 Windows 本地路径的 `existing + cluster` 配置返回 `windows_path_access_required`，并明确“不需要 Visual Studio 或编译依赖，只需要文件读取/上传连接”。
- 线上发布以 `origin/main` 当前提交 `2d9614e` 为基线；未把用户的 `output/`、`.claude/` 等未跟踪诊断产物纳入提交。

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
