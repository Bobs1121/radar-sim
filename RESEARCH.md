# AI 辅助雷达仿真提效 — 调研报告

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档类型 | 技术调研报告 |
| 创建日期 | 2026-06-10 |
| 最后更新 | 2026-06-11 |
| 作者 | Hermes Agent |
| 状态 | 已确认 v2 |

---

## 1. 调研目标

探索 AI 技术如何提升雷达产品仿真调试效率。核心场景：C/C++ 雷达算法代码 + VS + Selena 仿真分支 + MF4 原始数据 replay。

---

## 2. 现有仿真链路分析

### 2.1 完整链路（用户确认版）

```
┌──────────────────────────────────────────────────────┐
│ 1. 配置文件准备                                       │
│    byd_CR_Selena_Config_ovrs.txt                     │
│    - config=Runtime_XML.xml                          │
│    - input=xxx.MF4 (根据录制场景选择: FR/FL/RR/RL)    │
│    - output=xxx_outRL.MF4                            │
│    - log=CRlog.log                                   │
│    - nogui=true, write-mat=true                      │
│    - userparam=mountingPosition=CFR (随数据变化)     │
│    - source=RadarFR (随数据变化: FR/FL/RR/RL)        │
│    Runtime_XML: 34 runnable, 316 connection          │
└────────────────────────┬──────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. 编译 (bat → R2D2.py → CMake → VS Build)                         │
│     python3 R2D2.py -m ROS_PER_SIT_RPM_FCT_RECR.config              │
│                          -ghs_math -use_mat -notests                 │
│                          -bm RelWithDebInfo                          │
│     → 产出 daddy.exe                                                │
│     耗时: 数分钟                                                     │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. 启动仿真 (VS 中 F5 启动)                                         │
│     daddy.exe --platformpluginpath <Qt/plugins/platforms>            │
│            --paramconfig C:\tools\byd_CR_Selena_Config_ovrs.txt      │
│     Path: MATLAB + Qt + Boost + LocalDebuggerEnvironment            │
│     → 加载 .mf4 → 初始化 34 runnable → MDF 调度器回放               │
│     → 输出 .mf4 + .mat + CRlog.log                                 │
│     → 约 10 秒/次                                                   │
└────────────────────────┬─────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. 结果分析 (手动) — 当前瓶颈                                        │
│     A) 打开 .mf4 输出看特定信号波形 (FCTA 状态、制动行为等)           │
│     C) MATLAB 打开 .mat 可视化                                       │
│     → 判断修改是否达到预期                                            │
│     → 用户典型循环: 改代码 → 编译 → 仿真(同一条数据跑2次) → 分析      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 用户工作流确认 (2026-06-11)

| 维度 | 用户确认内容 |
|------|-------------|
| **启动方式** | VS 中启动仿真 (F5) |
| **结果判断** | A) .mf4 输出信号波形 + C) MATLAB .mat 可视化 |
| **断点调试** | 用户自己分析问题时断点多；AI 仿真验证不需要断点，只需验证修改后的结果是否满足需求 |
| **仿真次数** | 单条数据通常修改+仿真跑 2 次，看结果是否达到预期 |
| **参数修改** | 会换 .mf4，根据 mf4 录制的雷达位置调整 (前左/前右/后左/后右等) |
| **AI 边界** | AI 自动编译并仿真，抽取关键信号分析，给出结论。半开放式功能，后续扩展。模块化设计。 |

### 2.3 关键文件清单

| 文件/目录 | 路径 | 用途 |
|-----------|------|------|
| 仿真配置模板 | `C:\tools\byd_CR_Selena_Config_ovrs.txt` | 每次仿真前需编辑 |
| Runtime XML | `C:\tools\Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml` | 34 runnable, 316 connection |
| 编译脚本 | `C:\BYD_OVS_CB\apl\byd\bindings\ovrs25\selena\jenkins_selena_build.bat` | R2D2 构建入口 |
| 构建配置 | `C:\BYD_OVS_CB\apl\byd\selena\cmake_build_cfg\ROS_PER_SIT_RPM_FCT_RECR.config` | CMake 配置 |
| Runtime 配置(JSON) | `C:\BYD_OVS_CB\apl\byd\bindings\ovrs25\selena\config\runtime\config_fw_FULL.json` | runnable 级别配置 |
| 原始数据 | `D:\project\Gen5-BYD-OVRS\CRGVBYDPF-11641\0318\` | .mf4 + .blf + .avi |

### 2.4 Selena 配置文件格式

```
config=路径/to/Runtime.xml           # 运行时配置
input=路径/to/input.MF4              # 输入数据
output=路径/to/output.MF4            # 输出数据
log=路径/to/log.log                  # 运行日志
source=RadarFR                       # 雷达源 (FR/FL/RR/RL)
nogui=true                           # 无头模式
write-mat=true                       # 输出 MATLAB 格式
tolerant=false                       # 严格模式
userparam=mountingPosition=CFR       # 用户参数 (可多个)
disable-sequence-check=false         # 序列检查
enable-multibuffer-border=true       # 多缓冲边界
enable-doorkeeper=true               # 门控机制
matfilefilter=路径/to/filter         # MATLAB 输出过滤
```

### 2.5 仿真日志分析 (CRlog.log)

- 格式: `[时间戳] (thread PID) [级别]: 消息`
- 启动: 打印版本、加载配置、初始化 34 runnable
- 运行: MDF 调度器按 sequence number 驱动回放
- 错误: 如 `Read ... failed: not found in signal cache`
- 完成: `Waiting for DataRecorder to finish writing`

### 2.6 Runnable 体系 (34 个)

| 类别 | 包含 |
|------|------|
| **PER** (Perception) | Mal, Bdm, Loc, Stalin, EnvModel, Parameter, Pme, SppBdm, SppRLoc, SppStalin |
| **FCT** (Function) | FDM, FSM, HMI, SPP, SppScp |
| **SIT** (Situation) | FCTA, DOW, RCTA/RCTB, PreCrashRear, TIPL, DcParameterInput |
| **BC** (Behavior Consistency) | ETL, LMF, DMI, OBE, BehaviorConsistencyLead |
| **BS** (Behavior Strategy) | FM, TIPL |
| **TGU** (Target Gateway Unit) | TguInput, TguObjectCollector, TguOmiRunnable |
| **Other** | CCR (CAN), Genesis, SppHvm, SppHmiInput, MountingPositionProvider, RpmExecution |

---

## 3. AI 提效机会分析

### 3.1 自动化潜力评估

| 环节 | 当前耗时 | 自动化可行性 | AI 介入程度 |
|------|---------|-------------|------------|
| 改代码 | 10-30min | 用户自己做 | - |
| 编译 | 数分钟 | 高 | AI 自动调用 bat 脚本 |
| 配置仿真 | 2-5min | 高 | AI 生成配置文件 |
| 启动仿真 | 1min | 高 | 命令行无头启动 |
| 运行仿真 | ~10s/次 | - | 等待 |
| 分析结果 | 15-60min | **高** | AI 自动提取信号+对比+报告 |
| 判断对错 | 5-15min | 中 | AI 给结论，人确认 |
| **单轮循环** | **~40min-2h** | | **目标: 5-15min** |

### 3.2 核心突破

1. **Selena 支持 nogui=true** — 完全脱离 VS IDE 运行仿真
2. **仿真可以脚本化** — Python 调用 daddy.exe，传入不同配置
3. **MF4 可解析** — python-asammdf 读取信号数据
4. **编译可自动化** — bat 脚本已经是自动化入口

### 3.3 数据可解析性

| 数据类型 | 格式 | 解析工具 | 可行性 |
|----------|------|---------|--------|
| 原始数据 | .mf4 (Vector MF4) | python-asammdf | 高 |
| 输出数据 | .mf4 (Vector MF4) | python-asammdf | 高 |
| 运行日志 | .txt (自定义) | 正则解析 | 高 |
| MATLAB 数据 | .mat | scipy.io / h5py | 高 |

---

## 4. 技术可行性验证

### 4.1 仿真引擎封装 (概念验证)

```python
import subprocess

def run_selena_simulation(input_mf4, output_dir, config_overrides=None):
    # 1. 生成临时配置文件
    config = generate_config(input_mf4, output_dir, config_overrides)
    # 2. 设置环境变量 (MATLAB + Qt + Boost)
    env = setup_selena_env()
    # 3. 调用 daddy.exe 无头运行
    result = subprocess.run(
        ["daddy.exe",
         "--platformpluginpath", "C:\\TCC\\Tools\\qt\\5.8.0_WIN64\\5.8\\msvc2015_64\\plugins\\platforms",
         "--paramconfig", config],
        capture_output=True, text=True, timeout=300, env=env
    )
    # 4. 收集结果
    return {
        "exit_code": result.returncode,
        "output_mf4": f"{output_dir}/output.mf4",
        "log": parse_log(f"{output_dir}/log.log"),
        "duration": measure_time()
    }
```

### 4.2 MF4 信号提取 (概念验证)

```python
from asammdf import MDF

mdf = MDF("output.mf4")
# 列出所有信号
for ch in mdf.channels:
    print(ch.name, ch.unit)

# 提取关键信号
fcta_state = mdf["FCTA_State"]
braking_behavior = mdf["BrakingBehavior"]
timestamps = mdf.latency
```

---

## 5. 产品设计方向

### 5.1 产品定位

**CLI 工具 (`rsim`)**: 自动化 "编译 → 仿真 → 分析" 闭环，AI 辅助判断结果。

不是替代 VS 调试，而是**验证性仿真** — 用户改完代码后，让 AI 快速验证修改是否有效。

### 5.2 核心能力

1. **编译自动化** — 调用现有 bat/R2D2 脚本，监控编译结果
2. **仿真自动化** — 生成配置文件 → 无头运行 daddy.exe → 收集输出
3. **信号提取** — 从 .mf4 输出中抽取关键信号
4. **结果分析** — 对比分析信号，给出通过/失败结论
5. **报告生成** — HTML 报告展示关键信号波形和结论

### 5.3 差异化

| 维度 | radarAnalyze | radar-sim |
|------|-------------|-----------|
| 核心场景 | 录制备查 (事后诊断) | 仿真 replay (实时验证) |
| 输入 | 录制备查数据 | 原始 .mf4 + 代码修改 |
| 输出 | 诊断报告 | 验证结论 + 信号对比 |
| AI 角色 | 分析员 | 实验员 (控制仿真 + 分析) |

---

## 6. 结论

**技术可行性：高。** 整条仿真链路可完全自动化:
1. 编译 → bat 脚本自动化
2. 仿真 → nogui 模式命令行运行
3. 分析 → MF4 解析 + 信号对比 + AI 判断

**核心价值:** 把单轮验证循环从 40min-2h 缩短到 5-15min。用户改完代码，AI 自动跑编译 → 仿真 → 分析，给出"通过/不通过 + 原因"的结论。

---

## 7. 编译产物实地探索 (2026-06-11)

### 7.1 编译产物结构 — 实际编译结果

本次编译使用 `jenkins_selena_build.bat` (config=ROS_PER_SIT_RPM_FCT_RECR, buildmode=RelWithDebInfo)。

实际编译产物路径: `C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\`

```
ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/
  DACore-ROS_PER_SIT_RPM_FCT_RECR.sln    # VS 解决方案
  daddy.vcxproj                          # daddy 工程 → 产出 .lib 静态库 (不是 exe!)
  daddy.dir/RelWithDebInfo/
    daddy.lib                            # 静态库
    daddy.pdb                            # 调试符号
    *.obj                                # 目标文件
  dc_tools/selena/core/RelWithDebInfo/
    selena.exe                           # ★ 主可执行文件 (2MB, ~4MB on disk)
    selena_dll.dll                       # ★ 功能模块 DLL (72MB) — 34个runnable都在这里
    selena_core.dll
    selena_gui.dll
    Mdf4Lib_x64.dll                      # MDF4 库 (4MB)
    MdfLibSort_x64.dll
    MDFSort_x64.dll
    XmlParser_x64.dll
    a2l_missing_signals.txt              # A2L 信号缺失报告 (9267 行, ~3.5MB)
    *.pdb                                # 调试符号 (selena_dll.pdb 达 312MB)
  log/logfile_r2d2.txt                   # R2D2 构建日志
  generated_src/                         # 自动生成的代码
```

### 7.2 实际可执行入口: selena.exe

**确认:** VS 解决方案中有多个工程，调试时选择 **Selena 工程** (非 daddy 工程)。

- `daddy.vcxproj` 编译成 `daddy.lib` (静态库)
- `Selena.vcxproj` 编译成 `selena.exe` (主可执行文件)
- `selena_dll.dll` (72MB) 包含了所有 34 个 runnable 的代码
- `daddy.lib` 链接进 `selena_dll.dll`

### 7.3 VS 调试配置 (用户确认版)

**完整操作流程:**

1. VS 中打开:
   ```
   C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\DACore-ROS_PER_SIT_RPM_FCT_RECR.sln
   ```

2. 启动工程选择 **Selena** (不是 daddy)

3. Configuration 选择 **RelWithDebInfo**

4. VS 中右键 Selena 工程 → Properties → Configuration Properties → Debugging:

   **Command arguments:**
   ```
   --platformpluginpath C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\plugins\platforms --paramconfig C:\tools\byd_CR_Selena_Config_ovrs.txt
   ```

   **Environment:**
   ```
   Path=C:\Program Files\MATLAB\R2023b\bin\win64;C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\bin;C:\TCC\Tools\boost\1.63.0_WIN64\lib64-msvc-14.0;$(Path);$(LocalDebuggerEnvironment)
   ```

5. 配置完成后 F5 启动，可在 runnable 源码中加断点 debug

**命令行等价的无头启动:**
```bash
# 注意: --platformpluginpath 是 Qt 参数，selena.exe 命令行不认识，必须去掉
set PATH=C:\Program Files\MATLAB\R2023b\bin\win64;C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\bin;C:\TCC\Tools\boost\1.63.0_WIN64\lib64-msvc-14.0;%PATH%
selena.exe --paramconfig C:\tools\byd_CR_Selena_Config_ovrs.txt
```

**调试符号:** `selena_dll.pdb` (312MB，完整的调试信息)，RelWithDebInfo 配置下断点可打在 runnable 源码上。

**实测结果 (2026-06-11):** 命令行启动成功，但因 runtime XML 中信号 task 配置与 MF4 数据不匹配（0x133 信号 configured in mC6E75A12 but measured in core0_t10）导致提前退出。详见 §7.10。

### 7.4 a2l_missing_signals.txt — A2L 信号缺失报告

- 9267 条缺失信号记录
- 格式: `missing signal = <signal_path> : <runnable> : <port> : <action>`
- 大部分是 DSP 相关信号 (AntDiag, EvmEvent 等)
- 这个文件在运行时会用于信号容错处理

### 7.5 运行时配置结构 (Runtime XML 1699 行)

`Runtime_BYD_OVS25_CR5CB_BL16_RC36.xml` 定义了:

| 元素 | 数量 | 说明 |
|------|------|------|
| runnable | 34 | 功能模块，各有 init order 和 color (UI) |
| connection | 316+ | 端口间连接，带 task 标识 |
| job | 3+ | task→runnable 映射 (core0_t10, core0_t20, core0_bg) |
| init | ~35 | 初始化顺序 |
| plugins | 3 | mdfplayer, mdfrecorder, mdfscheduler |

**Runnable 分类:**

| 类别 | 颜色 | 关键 Runnable |
|------|------|-------------|
| BYD APL (功能定制) | #FF00FF | RunnableSppHvm, SppHmiInputRunnable, RunnableSpp, RunnableHmi |
| SIT (行为策略) | #00FF00 | RunnableBehaviorConsistencyLead, RunnableCfmFcta, RunnableTipl |
| PER (感知) | #00FF00/#FF00FF | PerUnitedRunnable, PerBdmRunnable, PerSppBdmRunnable |
| FCT (功能控制) | 无颜色 | RunnableFsm, RunnableFdm |
| RPM (执行) | 无颜色 | RpmExecution |
| TGU (输出) | 无颜色 | TguInputRunnable, TguObjectCollectorRunnable, TguOmiRunnable |
| 基础设施 | #3333FF | DataPlayer, DataRecorder |

### 7.6 已知运行问题 (从 CRlog.log)

CRlog.log (270 行日志) 显示:
- 启动: 15:32:20.727
- 版本: Selena 1.18.0 Roberta (Qt 5.8.0)
- 加载: 34 runnable, 316 connection, 0 config error
- **错误**: `not found in signal cache` — NetRx_BYD_PUB_CS_0x133 SenderPort sequenceNumber 缺失
- **错误**: `MultiReadTimeNsAndRawValueDouble failed`
- 完成: 15:32:30.954 (总耗时约 10 秒)
- Warning: 3 个 runnable active but not in init order list (DataPlayer, DataRecorder, Genesis)

### 7.7 构建日志分析

`logfile_r2d2.txt` 显示:
- CMake 3.17.3 + VS2019 (vs16)
- python3 来自 selena_environment/0.1.7_WIN64/MSYS/mingw64
- 构建模式: RelWithDebInfo
- 最终: `BUILD SUCCESSFUL`

### 7.8 环境差异: BYD-SC6H vs BYD_OVS_CB

| 维度 | BYD-SC6H-cr60light | BYD_OVS_CB (CR5CB) |
|------|---------------------|---------------------|
| 平台 | CR60 Light | CR5CB |
| 构建系统 | SCons | R2D2 + CMake 3.17.3 + VS2019 |
| 仿真入口 | daddy.exe (旧版) | selena.exe (新版) |
| 绑定 | 12+ bindings | ovrs25 (当前使用) |
| TGU 模式 | TguOmiRunnable | TguOmiRunnable |
| DLL 分发 | 各 runnable 独立 DLL | selena_dll.dll 整合所有 runnable |
| VS 版本 | 未知 | VS2019 |
| Selena 版本 | 未知 | 1.18.0 Roberta |

### 7.9 需要确认的问题

| # | 问题 | 状态 |
|---|------|------|
| 1 | 启动 exe | ✅ 已确认: `selena.exe` |
| 2 | VS 启动配置 | ✅ 用户已提供完整 Debugging 配置 |
| 3 | 构建频率 — 是否每次都需要完整重编译？ | 待确认 |
| 4 | 输入 MF4 数据来源 | 待确认 |
| 5 | 输出分析工具 | 待确认 |
| 6 | 典型修改模块 | 待确认 |
| 7 | tolerant 模式使用场景 | 待确认 |
| 8 | multi-source 仿真方式 | 待确认 |

### 7.10 Selena 命令行启动实测 (2026-06-11 17:29)

**测试命令:**
```bash
cd "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/dc_tools/selena/core/RelWithDebInfo"
PATH="/c/Program Files/MATLAB/R2023b/bin/win64:/c/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin:/c/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0:$PATH"
./selena.exe --paramconfig "C:/tools/byd_CR_Selena_Config_ovrs.txt"
```

**关键发现:**

1. **`--platformpluginpath` 不能直接传 selena.exe** — selena.exe 报 `Unknown option` 错误。VS 中 Debugging 配置里的 `--platformpluginpath` 参数能工作，说明 Qt QApplication 在 daddy.lib / selena_dll.dll 内部处理了这个参数。命令行直接调用时 selena.exe 的参数解析器不认识它。

2. **仅 `--paramconfig` 即可启动** — 去掉 `--platformpluginpath` 后 selena.exe 正常启动。

3. **启动流程成功:**
   - 版本: Selena 1.18.0 Roberta, Qt 5.8.0
   - 加载: 34 runnable, 323 connection, 0 config error
   - 输入: Gen5_2009-01-01_06-05_0116.MF4 (273MB)
   - User param: mountingPosition = CFL
   - Source: RadarFL

4. **失败原因:**
   ```
   [error] XMdfReaderImpl.cpp.545: Wrong task: g_Golf_Fct_Spp_RunnableSpp_m_netRx_BYD_PUB_CS_0x133_Port_in
     configured in <mC6E75A12>, but measured in <core0_t10>
   ```
   MF4 数据中信号 0x133 的 task（core0_t10）与 runtime XML 配置的 task（mC6E75A12）不一致。
   这是 **runtime 版本不匹配** 导致的问题 — 当前 BYD_OVS_CB Selena 分支的 runtime 配置和录制的 MF4 数据不兼容。

5. **输出:** Gen5_2009-01-01_06-05_0116out.MF4 (757KB — 很小，说明刚跑就停了)

6. **还有 15 个 missing trigger:** VariationPointGetSequencePost 检测到 15 个序列信号缺失。

**启动耗时:** 从 17:29:04 到 17:29:09，约 5 秒（未完整运行就因错误退出）。

**结论:** 命令行启动 selena.exe 的链路已验证通，当前因 runtime 不匹配无法完整仿真。修复 runtime 后即可自动化运行。

### 7.12 自动化编译实测 (2026-06-11 17:44)

**编译环境:** bash (git-bash/MSYS) 下直接调用 R2D2.py

**关键发现 — 环境变量必须完整:**

| 变量 | 必需值 | 说明 |
|------|--------|------|
| `PATH` | `selena_environment/MSYS/mingw64/bin` + boost + Qt | python3 + DLL 依赖 |
| `BOOST_ROOT` | `C:/TCC/Tools/boost/1.63.0_WIN64` | cmake 检查 BOOST_ROOT 环境变量 |
| VS generator | 与已有 CMakeCache.txt 一致 | vs14 已存在的 cache 不能用 vs16 覆盖 |

**首次失败原因:**
1. 第一次: `vs16` generator 与已有 cache 的 `vs14` 冲突
2. 第二次: 没传 `BOOST_ROOT` 环境变量，cmake 报错

**成功命令:**
```bash
cd /c/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin
export PATH="/c/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin:/c/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0:/c/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin:/c/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/lib:$PATH"
export BOOST_ROOT="/c/TCC/Tools/boost/1.63.0_WIN64"
./python3.exe "C:/BYD_OVS_CB/ip_dc/dc_tools/R2D2.py" \
  -m "C:/BYD_OVS_CB/apl/byd/selena/cmake_build_cfg/ROS_PER_SIT_RPM_FCT_RECR.config" \
  -ghs_math -use_mat -notests -bm RelWithDebInfo
```

**编译时间:**
- CMake 配置: 3 分 11 秒
- 编译: 7 分 28 秒
- 总计: 10 分 39 秒

**产物验证:**
- `selena.exe` — 主可执行文件
- `selena_dll.dll` — 72MB 整合所有 runnable
- `selena_dll.pdb` — 完整调试符号

**增量编译:** 有 CMakeCache.txt 和 build 目录时，cmake 配置阶段会快很多（只重新配置，不重新生成），编译阶段只编译变更的文件。

### 7.13 自动化启动 selena.exe 的正确参数组合

| 参数 | 值 | 来源 | 必需性 |
|------|-----|------|--------|
| `--paramconfig` | `C:/tools/byd_CR_Selena_Config_ovrs.txt` | 用户配置 | 必需 |
| `--platformpluginpath` | Qt plugins 路径 | VS 调试配置 | ❌ 命令行不需要，selena.exe 不认识 |
| `PATH` | MATLAB + Qt + Boost | VS 调试配置 | 必需（加载 DLL 依赖） |
| `--nogui` | (已在 paramconfig 中) | config 文件 | 已在 config 中设为 true |
| `--tolerant` | (可选) | 命令行 | 可绕过 signal mismatch 错误 |
| `--log` | CRlog.log | config 文件 | 已配置 |

**最小启动命令:**
```bash
PATH="/c/Program Files/MATLAB/R2023b/bin/win64:/c/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin:/c/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0:$PATH"
selena.exe --paramconfig "C:/tools/byd_CR_Selena_Config_ovrs.txt"
```

---

## 8. 坑点索引

> 所有踩过的坑已整理到 `ADAPTIVITY.md`，开发时必须查阅。快速索引：

| 分类 | 坑编号 | 简述 | 已解决 |
|------|--------|------|--------|
| 编译 | B1 | CMake generator 不匹配 (vs14 vs vs16) | ✅ 检测 CMakeCache.txt |
| 编译 | B2 | BOOST_ROOT 未设置 | ✅ 自动检测 + config.yaml |
| 编译 | B3 | python3 路径错误 | ✅ 用 selena_environment 的 |
| 编译 | B4 | VS 文件锁阻止 clean | ✅ 锁检测 + 友好提示 |
| 编译 | B5 | PATH 不完整 | ✅ 自动组装完整 PATH |
| 仿真 | R1 | --platformpluginpath 无效 | ✅ 不传该参数 |
| 仿真 | R2 | runtime/task 不匹配 | ⚠️ tolerant 模式或修 XML |
| 仿真 | R3 | nogui 是 config 参数 | ✅ config 中设 nogui=true |
| 仿真 | R4 | 可执行文件不是 daddy.exe | ✅ 改为 selena.exe |
| MF4  | M1 | MF4 与 runtime 版本绑定 | ⚠️ 版本对齐检查 |
| MF4  | M2 | 270MB 大文件处理 | ⚠️ asammdf 流式读取 |
