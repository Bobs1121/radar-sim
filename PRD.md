# radar-sim — 仿真辅助与数据分析工具

## PRD (Product Requirements Document)

---

## 1. 文档基础信息

| 项目 | 内容 |
|------|------|
| 产品名称 | radar-sim |
| 版本 | **4.0.0** |
| 日期 | 2026-06-14 |
| 状态 | v3→v4 重设计：对齐真实工作流，移除偏差功能 |

### 重大变更说明 (v4.0.0)

v4 是基于用户描述的真实工作流**完全重设计**的版本。v3 的核心假设（selena 无头自动运行、工具控制仿真全流程）与实际不符，v4 修正为：

| 维度 | v3 设计（错误） | v4 设计（正确） |
|------|---------------|---------------|
| 仿真执行 | 命令行自动跑 selena.exe nogui | **VS 中手动打开仿真，用户控制输入** |
| HEX 编译 | 完整编译或跳过 | **可中断式编译（进度开始后可中断）** |
| 工具定位 | 全自动化验证流水线 | **编译辅助 + 仿真后数据分析** |
| 参数扫描 | Phase 3 核心功能 | **不做——这不是扫描/测试工具** |
| 回归测试 | Phase 3 核心功能 | **不做——这是仿真辅助工具** |
| corner case | 变异生成边界场景 | **不做——与真实需求无关** |
| 数据分析 | 固定 pipeline（extract → rule → AI → report） | **插拔式分析插件模块** |
| 配置文件 | 散落 C:/tools/ 各处 | **集中到项目 assets/ 目录管理** |
| 项目隔离 | 单 config.yaml | **每个项目独立 config + assets 目录** |

### 术语表

| 术语 | 解释 |
|------|------|
| Selena | Bosch 内部雷达仿真框架，基于 runnable 架构 |
| selena.exe | Selena 编译后的仿真可执行文件 |
| HEX 编译 | 固件编译流程（testbuild_BaseC0S_SINGLE.bat），耗时较长 |
| Selena 编译 | 仿真工程编译（R2D2.py / jenkins_selena_build.bat），产出 selena.exe |
| MF4 | Vector MDF4 格式，雷达仿真输入/输出数据格式 |
| Runtime XML | 定义 runnable 集合和连接关系的仿真环境配置文件 |
| patch | 修改后的代码文件拷贝到构建目录的流程 |
| Analysis Plugin | 数据分析插件，插拔式设计，对 MF4 输出进行分析 |

---


## 1.5 项目最顶层设计架构 Matrix (Top-Level Design Matrix)

为了满足不同开发团队与用户的多元需求，本项目致力于构建一个**多端协同、自适应调度、灵活组合的仿真辅助与分析平台**。系统支持以下三种典型核心场景与组合，并统一提供 Web 前端与 API 接口。

### 1.5.1 场景矩阵 (Architectural Scenarios)

| 场景类型 | 客户端环境 | Selena 编译方式 | 仿真运行后端 | 数据路径配置 |
| :--- | :--- | :--- | :--- | :--- |
| **场景 A：全本地高自研**<br>(T1 核心开发) | 有 Windows 电脑<br>有编译和仿真环境 | 本地 clone 源码编译<br>或选用已有 Selena 可执行文件 | 本地仿真运行<br>或服务器/Cluster 运行 | 本地硬盘数据<br>或云端/服务器共享数据 |
| **场景 B：跨端托管编译**<br>(T2 轻量开发) | 有 Windows 电脑<br>无本地仿真环境 | 本地 clone 源码编译<br>或选用已有 Selena 配置 | 通过 Linux 服务端调度<br>在 Cluster 服务器进行仿真 | 通过本地 Agent 上传<br>或引用云端/服务器共享数据 |
| **场景 C：纯云端无头运行**<br>(T3 业务用户) | 无 Windows 电脑<br>或无本地源码与环境 | 无需本地编译<br>直接选用已有/云端 Selena 运行 | 完全由 Linux 服务端调度<br>在 Cluster 服务器进行仿真 | 完全使用云端/服务器共享数据 |

### 1.5.2 核心调用接口 (Interface Uniformity)
无论是“全本地 Windows 调度”还是“完全 Linux 调度”，系统均必须统一对外暴露两套调用方式：
1. **Web 前端调用 (Web UI Console)**：供用户在网页端交互式配置项目、分支、数据路径和 Selena 选项，一键运行、中止并查看实时信号分析、AI 异常诊断和 KPI 曲线。
2. **API/后端调用 (API Integration)**：提供标准化、无头的 HTTP JSON API 端点，支持与其他自动化 CI/CD 流程、脚本工具链进行无缝集成。


## 1.6 智能自适应调度与强约束兼容性校验设计 (Smart Scheduling & Compatibility Validation)

在大端与云端解耦的设计中，用户虽然拥有“任意配置、选择与组合”的绝对自由，但仿真系统底层存在**极强的物理制约与强关联依赖**。为保障“乱配不崩溃，配错能拦截”，系统必须在调度最前端构建一套**“智能航前兼容性校验引擎 (Pre-flight Compatibility Engine)”**。

### 1.6.1 四维强约束关联拓扑 (The 4D Dependency Graph)

雷达仿真链的核心约束是由以下四个维度交织而成的强关联网，任意一个环节失配都会导致仿真“无声崩溃”（生成垃圾数据）或“显式崩溃”（系统死机）：

```
   ┌──────────────────────────────────────────────────────────┐
   │ 1. 代码分支 (Branch) ──► 决定编译出的 2. 可执行文件 (Binary) │
   └──────────────────────────┬───────────────────────────────┘
                              │ 
                              │ (约束: Runnable 与接口布局必须一致)
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │ 3. 运行拓扑 (Runtime.xml) ◄─► 4. 总线信号 (Dataset.MF4)   │
   │    (定义组件的连接关系)          (提供物理输入与总线协议版本)  │
   └──────────────────────────────────────────────────────────┘
```

1. **编译与二进制强相关 (Branch ──► Binary)**：特定的软件分支（如 `develop_evo` 或 `BL03RC01`）决定了 `selena.exe` 内部的变量布局、数据结构定义（C/C++ struct）和算法Runnable定义。
2. **二进制与环境拓扑强相关 (Binary ◄──► Runtime.xml)**：`Runtime.xml` 定义了各个模块 Runnable 的拓扑连接。如果 XML 引用了在 `selena.exe` 中不存在的接口，或接口名字/数据类型对不上，仿真在 VS 中运行会瞬间闪退或内存非法访问。
3. **二进制与总线数据强相关 (Binary ◄──► Dataset.MF4)**：雷达输入数据 `test.MF4` 承载了特定时期的实车总线（CAN/CANFD/Ethernet）信号。如果 Selena binary 期望读取 `ADCMode_UI_Status` 信号，但数据集里该信号格式改版（位移/位长改变）或根本不存在，Selena 在解码时会读取到垃圾数值（NaN），导致后续分析逻辑陷入死机。

---

### 1.6.2 航前适配校验矩阵 (Pre-flight Validation Matrix)

在 Linux 服务端（或本地 Master）启动调度前，系统必须自动调起以下**三大静态契约校验**，只有 100% 通过（PASS）才允许资源分发与运行：

| 校验层级 | 校验对象 | 底层技术实现原理 | 拦截触发场景 |
| :--- | :--- | :--- | :--- |
| **1. 软件指纹校验**<br>(Fingerprint Match) | **分支 ◄──► 编译产物** | 在 `selena.exe` 编译阶段，通过编译钩子将 **Git Commit Hash、Branch、Timestamp** 作为嵌入式常量（或伴生 `.json` 签名）写入 Binary。Linux 服务端在调度前，通过读取该签名，校验其是否与用户在前端指定的 Branch 一致。 | 用户指定的 `selena.exe` 实际由 `develop` 编出，但用户在配置中声明它是 `BL03RC01` 稳定版分支。 |
| **2. 接口匹配性校验**<br>(Interface Consistency) | **二进制 ◄──► Runtime.xml** | 解析 `Runtime.xml`，提取其定义的所有 `<Runnable>` 接口与连接端点（XML Parser）；同时通过反射或符号表静态分析（Dumpbin/Linux Objdump，或静态分析可执行文件导出的 interface），校验两者接口的一致性。 | 用户选择了 2026 年最新的 `selena.exe`，但是指定了 2025 年的老版 `Runtime.xml`，导致接口缺失。 |
| **3. 信号契约校验**<br>(Signal Contract) | **二进制 ◄──► Dataset.MF4** | 1. 自动提取项目配置文件 `signals.yaml` 下的**硬约束信号名单**（Required Signals）。<br>2. 服务端调用 `asammdf` 快速读取 `input.MF4` 的 Header，校验这组强约束信号是否在数据集中真实存在。<br>3. 调用 `cantools` 校验数据集的 DBC 协议版本是否与 Selena binary 期望的数据结构版本对齐。 | 用户配置了一个老项目的数据集，但指定了全新编译的 Selena，数据集里根本不含 Selena 所需的最新 BCM 信号。 |

---

### 1.6.3 自适应动态资源调度决策机 (Adaptive Scheduler Flow)

当用户点击仿真启动，Linux（或本地）调度器会根据用户动态输入的“极简配置配置项”，自发启动以下决策树：

```text
               [用户在 Web/API 提交极简配置 local.yaml]
                                │
                                ▼
                 [第一阶段：依赖感知与资产发现]
  - 用户配置了 Selena 路径吗？──► (No) ──► 自动从对应 Branch 的共享区捞取匹配 Binary
  - 用户配置了 Runtime 路径吗？ ──► (No) ──► 自动从代码仓 /apl/byd/ 目录下扫描并装载最匹配的 XML
                                │
                                ▼
                 [第二阶段：航前适配校验矩阵 (Pre-flight)]
  - 进行：软件指纹校验、接口匹配性校验、信号契约校验
                                │
                ┌───────────────┴───────────────┐
                ▼ (Any Fail)                    ▼ (All PASS)
           [硬拦截并友好报错]             [第三阶段：自适应资源分发]
      详细指出：哪个信号缺失了、           - 判定运行后端 (Local / Cluster)
      或者是 XML 与二进制不匹配。          - 判定数据流向 (本地/云端)
                                                - 执行一键仿真调度
```

1. **自动补齐（Auto-Filling）**：如果用户缺省了某些复杂配置，系统在“第一阶段”自动去对应代码仓的静态目录（Assets）中，模糊搜索最匹配的 Runtime XML 或数据适配器，实现自动装载。
2. **拦截预警（Early Error Defusal）**：通过“第二阶段”的静态校验，**在仿真还未运行前就卡死一切潜在的运行报错**，并给出极其清晰的人话诊断（例如：*“配置拦截：你指定的 Dataset 9-5-26 中缺少 `g_cnms_fw_Fct` 信号组，这与你指定的 Selena (BL03 分支) 要求的最低物理信号契约不匹配，请更换数据集或调整分支。”*）。
3. **接口开放（Integrations）**：
   - **Web 端**：校验结果以直观的“红/绿”状态指示灯与关联拓扑图实时绘制在 UI 上。
   - **API 端**：校验失败时，直接返回结构化的 `HTTP 400 Bad Request` 和 JSON 格式的错误树（ErrorInfo Tree），供其他自动化 CI/CD 流程瞬间定位问题。


## 1.7 工业级全后端工具链打磨与生命周期管理设计 (Enterprise Toolchain Polish & Lifecycle Management)

为了将 `radar-sim` 雕琢为一套兼具**极高工程鲁棒性**与**用户极致体验**的工业级工具链，系统必须对不同项目的多维配置、环境依赖、Selena 动态 DLL 伴生打包、实时编译与仿真进度流、以及仿真归档 manifest 物理成果清册进行全生命周期设计。

### 1.7.1 跨项目自适应环境隔离 (Multi-Project Configuration Isolation)
每个雷达项目（如 `bydod25`, `ovrs25`）在磁盘上应具备**绝对独立的静态资产沙箱**：
- **资源隔离**：各项目目录采用独立物理拓扑：
  ```text
  config/projects/<project_name>/
    ├── config.yaml    # 本项目的编译、网盘、仿真与集群基础配置
    ├── signals.yaml   # 本项目关注提取的 8-15 个核心 KPI 信号名单
    ├── rules.yaml     # 本项目的 KPI 指标逻辑校验规则 (如 FCTA 激活时间、角度误差)
    └── assets/        # 本项目特定的模板（Runtime_tmpl.xml, adapter_file）
  ```
- **配置按需重载**：Linux 端自动提供 `/api/config` 接口，API 用户可以直接更新特定项目的 `config.yaml`。启动仿真时，若用户未传局部配置，100% 自动继承该项目的本地 assets 目录，防止项目间资源混淆。

---

### 1.7.2 Selena 编译依赖深度收割机制 (Selena DLL Harvesting Engine)
Selena 编译出的可执行文件不仅仅是单个 `selena.exe`，它在 Windows 下往往强依赖一组伴生动态链接库（如 `Qt*.dll`、`Boost*.dll`、或者算法模块的私有编译 `.dll`）。
- **DLL 深度收割 (Harvesting)**：
  当 Windows 编译 Agent 执行 `.bat` 编译成功后，不能只拷贝 `.exe`。系统必须扫描编译输出路径（如 `build_output` 目录）下的**所有伴生文件**，通过后缀过滤（`.exe`, `.dll`, `.cfg`, `.xml`），将它们整体打包（Harvest）为一个专用的 **`selena_runtime_package.zip`** 压缩包。
- **自适应部署 (Staging)**：
  在 Cluster 仿真调度阶段（场景 B 和 C），系统如果是第一次在这个 Job 下调度，或者是 `copy_selena` 为 `true`，系统自动解压该 `zip` 包到共享网盘 `workspace_root` 下的任务运行目录中。这能保证 Cluster 节点在跑 `selena.exe` 时，**能在其当前目录下直接寻入并加载所有依赖的 DLL，100% 杜绝由于环境缺失报错“找不到 XX.dll”的问题**。

---

### 1.7.3 基于 Git Branch 的无人值守编译管线 (Unattended Compilation Pipeline)
对于需要“本地有代码仓，根据指定 Selena 分支自动进行编译”的场景：
- **静默环境准备**：
  Windows Agent 接收到编译任务后，首先检查工作区，若检测到 `git lock` 冲突或有未提交文件，自动执行安全暂存（Stash），随后无干预执行 `git checkout <target_branch>` 和 `git pull`。
- **防锁编译重试**：
  在执行 `jenkins_selena_build.bat` 前，Windows Agent 会调用系统命令检测当前目录下是否有 `selena.exe` 仍被其他仿真进程占用锁死（File Lock）。如果有，自动杀掉僵尸仿真进程，确保编译 100% 顺利写入，无需人工上机排查。

---

### 1.7.4 双端实时进度多路复用流 (Real-time Progress Multiplexing - Build & Sim)
由于 Selena 编译与仿真均属于耗时较长的“长任务”（编译常需 3~10 分钟，仿真常需 5~30 分钟），系统必须提供极佳的双端（Web/CLI）多路复用进度可视化反馈：
1. **编译进度流 (Build Progress)**：
   - 编译脚本向 stdout 打印的日志中，通常包含当前编译文件数（如 `[45/120] Compiling main.cpp`）。
   - Windows Agent 里的正则引擎自动捕获此类特征，实时更新 Task 状态中的 `files_done` 和 `files_total`。
   - Web 前端轮询 `/api/jobs/<id>` 时，直接渲染成精美的进度条和当前编译文件名，完全消除“假死”焦虑。
2. **仿真进度流 (Simulation Progress)**：
   - 仿真运行器会向 `CRlog.log` 持续打点（如帧数计数 `Frame 1200 / 4500`）。
   - 调度器（本地或 Linux 服务端）采用**增量日志分块传输（Log chunking via since cursor）**：
     API 接口 `GET /api/jobs/<job_id>/logs?since=<line_cursor>` 支持客户端增量拉取仿真日志。
   - 网页控制台与 CLI TUI 仪表盘动态读取分块日志并流式更新当前仿真完成帧率百分比，提供“飞线感”十足的实时掌控。

---

### 1.7.5 仿真归档与物理成果清册 (Post-Simulation Manifest)
当仿真任务状态变为 `SUCCESS` 时，系统必须产生一份内容详尽的**归档清单（Simulation Run Manifest）**：
- **物理成果清册 (Artifacts Directory)**：
  系统以 JSON 的形式向 Web 端和 API 端返回完整的运行报告树，其中必须标明以下关键物理实体的**绝对 UNC/Linux 挂载路径**：
  1. **仿真输出文件路径 (Output MF4)**：如 `D:/sim_workspace/bydod25/run_123/output.MF4`。
  2. **配置文件路径 (Applied Config.cfg)**：标明本次仿真合并的所有生效参数以及所套用的 `Runtime.xml` 路径。
  3. **环境日志路径 (Simulation logs)**：指向 `CRlog.log` 以及缺失信号排查日志 `CRlog.log_MissingSignals.txt`。
  4. **数据分析报告路径 (Analysis Report)**：生成的交互式 `report.html`。
- **自动归功与通知**：
  通过在配置文件中增加 `notifications` 配置，支持在仿真结束后自动调用 Webhook，将包含归档清单、AI 追问快捷入口和 KPI 核心检查结果（如 `FCTA Check: PASS`）的卡片通知，一键推送至用户指定的平台。

## 2. 产品定位

### 2.1 一句话定义

**radar-sim 是雷达仿真开发流程的辅助工具：帮你管编译流程、仿真环境配置，并在仿真跑完后自动分析输出数据。**

不是自动化测试工具，不是参数扫描工具，不是 CI/CD 流水线。

### 2.2 真实工作流

```
┌─────────────────────────────────────────────────────────────────────┐
│                          用户的真实工作流                             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  1. 改代码                                                           │
│     ↓                                                               │
│  2. 编译 HEX（rsim build hex）                                       │
│     → 时间长，进度条开始后 Ctrl+C 中断即可                            │
│     ↓                                                               │
│  3. 编译 Selena（rsim build selena）                                 │
│     → 产出 selena.exe + VS 工程                                     │
│     ↓                                                               │
│  4. 在 Visual Studio 中打开仿真工程                                   │
│     → rsim open-vs  或手动打开                                       │
│     → 配置输入数据、仿真参数                                          │
│     → F5 运行仿真                                                   │
│     ↓                                                               │
│  5. 仿真完成，产出 output.mf4                                        │
│     ↓                                                               │
│  6. 数据分析（rsim analyze output.mf4）                              │
│     → 自动提取信号，生成默认分析报告                                  │
│     → 支持追问："FCTA 为什么没有激活？"                               │
│     → 支持对比："跟上次结果比有什么变化？"                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 工具在每个环节的角色

| 环节 | 工具角色 | 是否核心 |
|------|---------|---------|
| HEX 编译 | **封装调用 + 进度显示 + 支持中断** | 是 |
| Selena 编译 | **封装调用 + 环境检查 + VS 工程生成** | 是 |
| VS 仿真 | **不控制** — 用户手动操作，工具提供 `open-vs` 辅助 | 否 |
| 仿真输出 | **等待用户告知 MF4 路径** | 否 |
| 数据分析 | **自动提取 + 插件分析 + AI 问答** | 是 |

### 2.4 成功指标

| 指标 | 现状 | 目标 |
|------|------|------|
| 编译流程 | 手动找 bat 脚本，手动配环境 | 一条命令，自动环境配置 |
| 仿真环境 | 手动复制 runtime XML、config template 到 C:/tools/ | 集中管理在/assets/，自动使用 |
| 数据分析 | 手动打开 MF4 可视化工具看波形 | 自动提取关键信号 + 报告 + AI 问答 |
| 多项目切换 | 手动改各种路径 | config 切换即完成 |

### 2.5 范围界定

| In Scope | Out of Scope |
|----------|-------------|
| 编译辅助（HEX + Selena） | 不控制仿真运行 |
| 仿真环境配置管理（runtime/config 集中管理） | 不做自动化仿真回放 |
| MF4 数据分析（提取 + 规则 + AI） | 不做参数扫描 |
| 分析结果对比（两次仿真） | 不做回归测试套件 |
| AI 问答（针对仿真结果提问） | 不做代码自动生成 |
| 多项目隔离 | 不做 CI/CD 集成 |
| 插件式分析模块 | 不做 corner case 生成 |

---

## 3. 用户场景

### 3.1 核心场景：改代码后验证

```
场景：用户修改了 FCTA 激活逻辑，想验证效果

1. 编译 HEX（大改动时先做，小改动可跳过）
   $ rsim build hex
   [进度条] HEX 编译中... 45%
   （Ctrl+C 中断 — 拷贝已完成，继续下一步）

2. 编译 Selena
   $ rsim build selena
   ✓ Selena 编译成功 (2m15s)

3. 在 VS 中打开并运行仿真
   $ rsim open-vs
   → Visual Studio 已打开，F5 运行

4. 仿真完成后，分析输出
   $ rsim analyze results/20260614/output.mf4
   ✓ 提取 8 个关键信号
   ✓ 规则检查: 3/3 PASS
   ✓ 默认报告已生成: results/20260614/report.html

5. 追问分析
   $ rsim ask "FCTA 激活时间跟上次比有什么变化？"
   → FCTA_State 在 t=11.2s 进入 ACTIVE（上次 12.3s），提前了 1.1s
     这与你的代码修改（降低距离阈值）一致。

6. 对比
   $ rsim diff results/20260614/ results/20260613/
   → FCTA_State: 激活时间 11.2s → 12.3s（提前 9%）
   → FCTA_Obj_Distance: 峰值 15.2m → 14.8m（变化 -2.6%）
```

### 3.2 多项目场景

```
场景：同时维护 BYD_OVS_CB（五代）和另一个项目

$ rsim --project ovrs25 build selena     # 五代项目
$ rsim --project other build selena      # 另一个项目

每个项目有独立的:
  - config.yaml（编译路径、环境配置）
  - assets/（runtime XML、config template 等仿真资源）
  - signals.yaml（关注的信号列表）
  - rules.yaml（检查规则）
```

### 3.3 纯分析场景

```
场景：已有仿真输出 MF4，只想看分析

$ rsim analyze D:/sim/output.mf4
$ rsim analyze D:/sim/output.mf4 --plugin default,signal-trend
$ rsim ask "TGU 检测到几个目标？"
```

---

## 4. 架构设计

### 4.1 整体架构

```
radar-sim/                          # 项目根目录
├── rsim.py                         # 入口（调度器，<100 行）
├── cli/                            # CLI 命令模块
│   ├── __init__.py
│   ├── build.py                    # rsim build [hex|selena|all]
│   ├── analyze.py                  # rsim analyze <mf4>
│   ├── diff.py                     # rsim diff <dir1> <dir2>
│   ├── ask.py                      # rsim ask "问题"
│   ├── check.py                    # rsim check（环境检查）
│   └── open_vs.py                  # rsim open-vs（打开 VS 工程）
├── core/                           # 核心层
│   ├── config.py                   # 配置加载 + 多项目支持
│   ├── models.py                   # 数据模型
│   ├── tui.py                      # 终端 UI（进度条、颜色）
│   └── analysis_runner.py          # 分析插件执行引擎
├── platforms/                      # 平台后端（按雷达代际隔离）
│   ├── __init__.py                 # 平台注册表
│   └── gen5_selena/                # 五代雷达
│       ├── __init__.py
│       ├── builder.py              # 编译（HEX + Selena）
│       ├── mf4_reader.py           # MF4 信号提取
│       └── log_parser.py           # 日志解析
├── plugins/analysis/               # 分析插件（插拔式）
│   ├── __init__.py
│   ├── default_report.py           # 默认分析报告
│   ├── rule_check.py               # 规则检查
│   ├── signal_summary.py           # 信号摘要
│   ├── ai_qa.py                    # AI 问答
│   └── trend_analysis.py           # 趋势分析（可选）
├── config/                         # 配置
│   ├── projects/                   # 多项目配置
│   │   ├── ovrs25/                 # 项目 ovrs25
│   │   │   ├── config.yaml         # 编译/环境配置
│   │   │   ├── signals.yaml        # 监控信号
│   │   │   └── rules.yaml          # 检查规则
│   │   └── other/                  # 另一个项目
│   │       ├── config.yaml
│   │       ├── signals.yaml
│   │       └── rules.yaml
│   └── default.yaml                # 全局默认配置
├── assets/                         # 仿真资源（集中管理）
│   ├── ovrs25/                     # 按项目隔离
│   │   ├── runtime.xml             # Runtime XML（复制自 C:/tools/）
│   │   ├── selena_config.txt       # Selena 配置模板
│   │   └── matfilefilter.txt       # MATLAB 过滤器
│   └── other/
│       └── ...
├── results/                        # 分析结果
│   └── <project>/<timestamp>/      # 按项目隔离
│       ├── signals.json
│       ├── report.html
│       └── analysis.json
└── tests/
```

### 4.2 核心设计原则

1. **配置集中管理**：runtime XML、config template 等仿真资源从散落的 C:/tools/ 复制到项目 `assets/<project>/` 下统一管理
2. **项目隔离**：每个项目有独立的 `config/projects/<name>/` 和 `assets/<name>/`，数据和结果也按项目隔离
3. **分析插件插拔**：分析模块是插件，通过 `--plugin` 指定加载哪些，默认加载 `default_report`
4. **仿真不自动化**：工具不控制 selena.exe 运行，只在 VS 仿真完成后分析 output.mf4
5. **编译辅助而非替代**：封装编译脚本调用和环境配置，不替代 VS 中的构建流程

### 4.3 配置系统

#### 全局默认配置 (`config/default.yaml`)

```yaml
# 全局默认
default_project: "ovrs25"
analysis:
  ai:
    enabled: true
    base_url: "http://bcsc-openai.apac.bosch.com:30001/llm/model/v1"
    model: "hermes"
    timeout: 120
    max_tokens: 4096
    temperature: 0.1
  default_plugins:
    - signal_summary
    - rule_check
    - default_report
```

#### 项目配置 (`config/projects/ovrs25/config.yaml`)

```yaml
project:
  name: "BYD_OVS_CB"
  platform: "gen5_selena"

paths:
  project_root: "C:/BYD_OVS_CB"
  binding: "ovrs25"
  build_output: "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR"

compile:
  hex_script: "C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/buildscripts/testbuild_BaseC0S_SINGLE.bat"
  selena_script: "C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/selena/jenkins_selena_build.bat"
  r2d2_script: "C:/BYD_OVS_CB/ip_dc/dc_tools/R2D2.py"
  build_config: "ROS_PER_SIT_RPM_FCT_RECR"
  vs_sln: "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/selena.sln"

environment:
  boost_root: "C:/TCC/Tools/boost/1.63.0_WIN64"
  qt_path: "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64"
  matlab_path: "C:/Program Files/MATLAB/R2023b"

assets:
  # 仿真资源路径 — 相对于项目根目录
  runtime_xml: "assets/ovrs25/runtime.xml"
  config_template: "assets/ovrs25/selena_config.txt"
  matfilefilter: "assets/ovrs25/matfilefilter.txt"

results_dir: "results"
```

#### 信号配置 (`config/projects/ovrs25/signals.yaml`)

```yaml
signals:
  - name: "FCTA_State"
    group: "fcta"
    description: "FCTA 状态机"
  - name: "FCTA_Obj_Distance"
    group: "fcta"
  - name: "TGU_OUT_ObjectList"
    group: "tgu"
  - name: "BSD_Alarm"
    group: "bsd"
  - name: "DOW_State"
    group: "dow"

groups:
  fcta:
    - FCTA_State
    - FCTA_Obj_Distance
  tgu:
    - TGU_OUT_ObjectList
  bsd:
    - BSD_Alarm
  dow:
    - DOW_State
```

#### 规则配置 (`config/projects/ovrs25/rules.yaml`)

```yaml
rules:
  - name: "fcta_activates"
    signal: "FCTA_State"
    condition: "reaches value 1"
    severity: "P0"
    description: "FCTA 应该进入 ACTIVE 状态"

  - name: "no_critical_error"
    source: "log"
    condition: "no [ERROR] entries"
    severity: "P0"
```

### 4.4 分析插件系统

#### 插件接口

```python
# core/analysis_plugin.py — 所有分析插件实现此接口
from abc import ABC, abstractmethod

class AnalysisPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """插件名称，如 'signal_summary', 'rule_check'"""

    @abstractmethod
    def analyze(self, signals: dict, context: AnalysisContext) -> PluginResult:
        """执行分析。signals 是提取的信号数据，context 包含 MF4 路径、规则等"""

    def ask(self, question: str, signals: dict, context: AnalysisContext) -> str:
        """可选：回答用户提问。默认返回 '此插件不支持问答'"""
```

#### 内置插件

| 插件名 | 功能 | 是否默认 |
|--------|------|---------|
| `signal_summary` | 提取信号统计（均值、极值、转折点） | 是 |
| `rule_check` | 对 signals.yaml 中的规则进行检查 | 是 |
| `default_report` | 生成 HTML 报告（整合所有插件结果） | 是 |
| `ai_qa` | AI 问答（需要 LLM） | 配置决定 |
| `trend_analysis` | 与历史结果对比趋势 | 可选 |

#### 加载方式

```bash
# 使用默认插件
rsim analyze output.mf4

# 指定插件
rsim analyze output.mf4 --plugin signal_summary,rule_check

# 关闭 AI
rsim analyze output.mf4 --no-ai
```

### 4.5 平台后端接口

```python
# core/platform.py — 所有雷达代际实现此接口
from abc import ABC, abstractmethod

class PlatformBackend(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台标识，如 'gen5_selena'"""

    @abstractmethod
    def check_environment(self, config: dict) -> list[str]:
        """环境检查，返回问题列表"""

    @abstractmethod
    def build_hex(self, config: dict) -> BuildResult:
        """HEX 编译"""

    @abstractmethod
    def build_selena(self, config: dict) -> BuildResult:
        """Selena 编译"""

    @abstractmethod
    def extract_signals(self, mf4_path: str, signal_names: list[str]) -> dict:
        """从 MF4 提取信号"""

    @abstractmethod
    def open_vs(self, config: dict) -> bool:
        """打开 VS 工程（可选）"""
```

### 4.6 项目隔离模型

```
# 每个项目完全独立:
config/projects/<project_name>/
  ├── config.yaml       # 编译、环境、路径
  ├── signals.yaml      # 信号配置
  └── rules.yaml        # 规则配置

assets/<project_name>/
  ├── runtime.xml
  ├── selena_config.txt
  └── ...

results/<project_name>/
  └── <timestamp>/
      ├── signals.json
      └── report.html
```

切换项目只需 `--project` 参数，所有配置自动切换。

---

## 5. CLI 设计

### 5.1 核心命令

```bash
# 编译
rsim build hex                    # HEX 编译（可中断）
rsim build selena                 # Selena 编译
rsim build all                    # 先 HEX 再 Selena

# 仿真执行（v4 扩展，详见 §11）
rsim run <input.mf4>                      # 单文件本地仿真
rsim run <dir> --select                   # 扫描目录选 MF4 本地仿真
rsim run --dataset <name>                 # 数据集批量本地仿真
rsim run --profile <name> ...             # 用指定 profile 仿真
rsim cluster run --profile <name> --select --execute   # 云端 prepare→submit→wait→fetch
rsim cluster prepare|submit|wait|fetch|status|web-status ...  # 集群分步操作

# 分析
rsim analyze <mf4>                # 分析仿真输出
rsim analyze <mf4> --plugin p1,p2 # 指定插件
rsim analyze <mf4> --no-ai        # 不启用 AI

# 对比
rsim diff <dir1> <dir2>           # 对比两次分析结果
rsim diff <mf4> <mf4>             # 对比两个 MF4

# AI 问答
rsim ask "FCTA 为什么没有激活？"       # 基于最近分析结果
rsim ask --results <dir> "问题"        # 指定结果目录

# 辅助
rsim check                        # 环境检查
rsim check --backend local|cluster --profile <name>   # 后端定向检查
rsim open-vs                      # 打开 VS 工程
rsim config list-projects         # 列出所有项目
```

### 5.2 全局选项

```
--project <name>      # 指定项目（默认: default_project）
--config <path>       # 指定配置文件
--verbose, -v         # 详细输出
```

### 5.3 使用示例

```bash
# 切换项目编译
rsim --project ovrs25 build all

# 分析并指定插件
rsim analyze D:/sim/output.mf4 --plugin signal_summary,rule_check,ai_qa

# 对比两次仿真
rsim diff results/ovrs25/20260614_100000/ results/ovrs25/20260613_150000/

# 追问
rsim ask "这次 FCTA 的激活时间和之前相比有什么变化？"
```

---

## 6. 数据模型

### 6.1 分析上下文

```python
@dataclass
class AnalysisContext:
    mf4_path: str                  # 输入 MF4 路径
    project: str                   # 项目名称
    platform: str                  # 平台名称
    timestamp: datetime            # 分析时间
    signals_config: dict           # signals.yaml 内容
    rules_config: dict             # rules.yaml 内容
    log_path: Optional[str]        # 仿真日志路径（可选）
    user_context: Optional[str]    # 用户提供的上下文信息
```

### 6.2 插件结果

```python
@dataclass
class PluginResult:
    plugin_name: str
    success: bool
    data: dict                     # 分析结果数据
    summary: str                   # 人类可读的摘要
    errors: list[str] = field(default_factory=list)
```

### 6.3 信号数据

```python
@dataclass
class SignalData:
    name: str
    timestamps: list[float]
    values: list[float]
    unit: str = ""
    summary: dict = field(default_factory=dict)  # 均值、极值、转折点
```

### 6.4 对比结果

```python
@dataclass
class DiffResult:
    signal: str
    base_value: float              # 基准值（如激活时间、均值）
    current_value: float           # 当前值
    change_pct: float              # 变化百分比
    interpretation: str            # 变化说明
```

---

## 7. 实现计划

### Phase 1: 核心基础（当前 v4 需要重做）

**目标：编译辅助 + MF4 分析跑通**

| 模块 | 任务 | 工时 | 状态 |
|------|------|------|------|
| 配置系统 | 多项目配置 + assets 目录管理 | 1d | 需重写 |
| 编译模块 | HEX/Selena 编译封装 + 进度显示 | 1d | 需重写 |
| MF4 读取 | asammdf 信号提取 | 0.5d | 已有 |
| 日志解析 | Selena 日志解析 | 0.5d | 已有 |
| 分析引擎 | 插件系统 + analysis_runner | 1d | 新建 |
| CLI | build/analyze/diff/ask/check/open-vs | 1d | 需重写 |
| 默认插件 | signal_summary + rule_check + default_report | 1d | 需重写 |
| **合计** | | **6d** | |

### Phase 2: AI 问答 + 对比分析

| 模块 | 任务 | 工时 | 状态 |
|------|------|------|------|
| AI QA | 基于分析结果的智能问答 | 1d | 新建 |
| Diff 分析 | 两次结果对比 + 变化解读 | 1d | 需重写 |
| 历史管理 | 分析结果存档 + 快速检索 | 0.5d | 新建 |
| **合计** | | **2.5d** | |

### Phase 3: 体验打磨

| 模块 | 任务 | 工时 | 状态 |
|------|------|------|------|
| VS 集成 | `open-vs` 命令 + VS 工程自动定位 | 0.5d | 新建 |
| 环境自检 | 启动时自动检测所有依赖 | 0.5d | 已有 |
| 配置向导 | `rsim init` 交互式初始化项目 | 0.5d | 新建 |
| 测试 | 全量测试覆盖 | 1d | 需重写 |
| **合计** | | **2.5d** | |

### 不做的事情

| 功能 | 原因 |
|------|------|
| 自动化仿真回放 | 用户在 VS 中手动仿真，不需要自动回放 |
| 参数扫描 | 不是扫描工具 |
| 回归测试套件 | 不是测试框架 |
| corner case 生成 | 不是测试用例生成器 |
| preset 管理 | 不需要 |
| CI/CD JUnit 报告 | 不需要 |
| 代码修改建议 | 超出仿真辅助范围 |

---

## 8. 非功能需求

### 8.1 兼容性

| 平台 | 要求 |
|------|------|
| OS | Windows 10/11 |
| Python | 3.10+ |
| VS | 2019（编译用） |
| Qt | 5.8.0 |
| Boost | 1.63.0 |
| MATLAB | R2023b（环境变量依赖） |

### 8.2 可扩展性

- 平台抽象：`core/platform.py` 定义接口，各代际在 `platforms/` 下实现
- 分析插件：任何实现 `AnalysisPlugin` 接口的模块都可插拔
- 项目配置：新增项目只需 `config/projects/<name>/` + `assets/<name>/`
- LLM 配置：在 `analysis.ai` 下可配置任何 OpenAI 兼容 API

### 8.3 可用性

- 所有命令支持 `--help`
- 编译显示进度条，HEX 编译可 Ctrl+C 安全中断
- 错误信息包含修复建议
- 支持 `--verbose` 详细模式

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| HEX 编译中断后状态不一致 | 可能部分文件未拷贝完成 | 记录中断点，下次 build 时检测 |
| assets/ 与 C:/tools/ 同步 | 外部资源更新后 assets/ 过时 | `rsim config sync-assets` 命令同步 |
| MF4 读取性能 | 大文件（>500MB）读取慢 | asammdf 流式读取，只加载指定信号 |
| AI 分析延迟 | LLM 调用超时 | 超时降级为规则检查结果 |

---

## 10. 产品结论

radar-sim v4 是对齐用户真实工作流的重新设计。核心从"自动化验证流水线"转变为"编译辅助 + 仿真后数据分析"。移除了参数扫描、回归测试等偏离需求的功能，聚焦于编译流程管理、配置集中化和数据分析插件化。

下一步：按 Phase 1 计划重写代码，保持现有可用的 MF4 读取和日志解析模块，重构编译、配置和分析模块。

---

## 11. 仿真执行后端（v4 扩展，2026-07）

### 11.1 定位演进

§2.3/§2.5 中"工具不控制仿真运行、用户在 VS 中手动操作"的表述适用于**单条调试场景**。实践中，批量数据集仿真和云端集群仿真需要工具直接驱动执行，因此 v4 扩展出**本地 + 云端双通路仿真执行后端**，与原"VS 手动仿真"并存：

| 场景 | 推荐路径 |
|------|---------|
| 单条数据调试、断点排查 | VS 手动仿真（§2.3 原路径，`rsim open-vs`） |
| 本地批量数据集仿真 | `rsim run`（本地后端） |
| 大批量 / 公盘数据云端仿真 | `rsim cluster run`（集群后端） |

### 11.2 模块化架构

仿真环境拆为可自由搭配的模块，每个模块来源可独立选择：

```
本地编译 (rsim build selena)
     ↓ 产出 selena.exe
Selena 环境配置 ──┬── 本地：原地引用编译产物
                  └── 云端：编译产物打包推送 / 指向已有共享 Selena
     ↓
数据自适应 ───────┬── 本地数据：原地引用
                  ├── 公盘 UNC 数据：原地引用（不下载）
                  └── 云端需 worker 可达：本地数据按需迁移到共享盘
     ↓
仿真执行 ─────────┬── 本地后端 (cli/run.py)
                  └── 集群后端 (core/cluster.py + cli/cluster.py)
     ↓
环境检查 (core/environment.py) ── 本地 / 集群 统一入口
     ↓
配置化 (core/profiles.py) ── profile 统一描述 Selena来源+数据策略+后端
```

核心模块：

| 模块 | 职责 | 文件 |
|------|------|------|
| 数据自适应 | MF4 发现、可达性校验、按需迁移、信号扫描 | `core/data.py` |
| 统一 profile | profile 解析/叠加，Selena 来源解析，后端判定 | `core/profiles.py` |
| 环境检查 | 本地/集群后端统一检查入口 | `core/environment.py` |
| 本地仿真 | 单文件/批量/数据集执行，进度/重试/校验 | `cli/run.py` |
| 集群仿真 | prepare/submit/wait/fetch，worker 脚本生成 | `core/cluster.py`, `cli/cluster.py` |

### 11.3 Profile 模型

一个项目下定义多个 profile，每个 profile 固定一组（Selena 来源 + 数据策略 + 后端），用户用 `--profile <name>` 切换整组假设，不复杂化配置：

```yaml
profiles:
  - name: local-build
    backend: local
    selena: { source: build }        # 从 build.build_output 派生 selena.exe
    data: { copy: false }            # 公盘/本地数据原地引用
  - name: byd-ovrs-bl01v7-er-shared
    backend: cluster
    selena: { source: path, exe: "\\\\share\\selena.exe" }
    data: { copy: false }
    cluster: { group: Radar, subgroup: PSS1 }
```

向后兼容：旧 `cluster.profiles`（扁平格式）自动转换为统一 profile（backend=cluster）；无 `--profile` 时用 default profile（本地编译 + 原地引用）。

### 11.4 数据自适应策略

- **校验优先**：执行前用 `core.data.check_data_access` 校验数据可读 + 输出区可写，区分本地盘 / UNC / 不可达。
- **原地引用优先**：公盘 UNC 数据默认原地引用，不下载（HANDOFF 记录 UNC 读写偏慢）。
- **按需迁移**：仅当 profile `data.copy=true` 时才把数据复制到本地临时区（本地后端）或共享工作区（集群后端）。
- **本地数据上云**：集群后端遇到本地盘数据时，`copy=true` 迁移到 worker 可达共享盘；`copy=false` 则报错并给出迁移指引，不静默提交一个 worker 看不见的路径。

### 11.5 Selena 来源自适应

- `source=build`：从 `build.build_output` 派生 selena.exe（本地后端原地引用；集群后端自动把 trimmed runtime ≈90MB 打包推送进 job 目录）。
- `source=path`：直接用 profile 指定的 selena.exe（通常是共享盘上已有产物）。

---

## 12. 未来服务化迁移方向：Linux 控制壳 + Windows Agent（规划记录，暂不实施）

### 12.1 迁移边界

Selena 的编译和仿真能力依赖 Windows 本机环境，包括 `selena.exe`、Visual Studio、TCC/itc2、`.bat` 构建脚本、用户本机代码仓库、用户本机/UNC 数据路径等。因此这部分能力**不迁移到 Linux**。

Linux 侧只提供稳定的控制壳：

- Web 前端入口
- 后端 API / SDK endpoint
- 用户、项目、profile、agent 注册管理
- job 创建、排队、分发、状态追踪
- 日志收集、结果索引、产物下载
- cluster 后端的控制面适配（在 Linux 可访问 cluster 资源时）

Windows 用户机器继续承担真实执行：

- 本机环境检查
- Selena 编译
- Selena 本地仿真
- 本机数据访问
- 本机结果收集与上传
- 必要时作为 cluster gateway agent 访问企业内网/共享盘资源

### 12.2 目标架构

```
Web / SDK
   |
Linux rsim-server
   |  下发任务、收日志、收状态、管理结果
Windows rsim-agent
   |  调用用户本机 radar-sim / Selena / TCC / VS / 数据路径
Selena build / local simulation / cluster gateway
```

设计原则：

> Linux 管“谁要做什么、做到哪了”；Windows 管“怎么真的做”。

### 12.3 最小服务接口

第一版只需要稳定 job 生命周期，不直接重写现有执行逻辑：

```text
POST /jobs
GET  /jobs/{id}
GET  /jobs/{id}/logs
POST /jobs/{id}/cancel

POST /agents/register
GET  /agents/{id}/next-task
POST /tasks/{id}/heartbeat
POST /tasks/{id}/logs
POST /tasks/{id}/result
```

### 12.4 Windows Agent 职责

`rsim-agent` 安装在用户 Windows 电脑上，主动连接 Linux server，避免要求用户电脑暴露入站端口。

Agent 第一阶段只做薄封装：

- 轮询 server 获取任务
- 调用现有 `rsim check` / `rsim build selena` / `rsim run` / `rsim cluster ...`
- 持续上报 stdout/stderr、进度、心跳、最终状态
- 支持 cancel
- 上传必要产物和结果索引

Agent 不在第一阶段重新实现 Selena 编译/仿真逻辑，优先复用现有 CLI 和 `core.api`。

### 12.5 非目标

- 不把 Selena 编译迁移到 Linux。
- 不把本地 Selena 仿真迁移到 Linux。
- 不要求 Linux 直接访问用户 Windows 本机路径。
- 不要求用户 Windows 电脑开放公网或内网入站服务。
- 不在本阶段改造现有本地/cluster 执行实现。

### 12.6 建议阶段

1. **Phase A：记录架构边界**  
   仅在 PRD 中沉淀服务化方向，不实施代码改造。

2. **Phase B：最小 server/agent 闭环**  
   新增 `rsim-server` 与 `rsim-agent`，只支持 `check`、`build_selena`、`run_local` 三类任务。

3. **Phase C：前端与 SDK 接入**  
   Web 和 SDK 只访问 Linux server，由 server 分发到 Windows agent。

4. **Phase D：cluster gateway 能力**  
   根据 Linux 是否能访问 cluster SMB/XML-RPC/官方状态页，决定 cluster adapter 运行在 Linux server 还是 Windows gateway agent。
