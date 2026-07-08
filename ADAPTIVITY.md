# radar-sim 环境与适配性指南

> 本文档记录自动化过程中发现的所有环境坑点、兼容性问题和适配策略。
> 开发 `radar-sim` 代码时**必须**参考此文档，避免重蹈覆辙。

---

## 1. 编译环境适配 (R2D2.py + CMake + MSVC)

### 1.1 已踩坑记录

| # | 问题 | 触发条件 | 表现 | 根因 |
|---|------|----------|------|------|
| B1 | VS generator 不匹配 | 已有 CMakeCache.txt 记录 vs14，新调用指定 `-vs vs16` | `CMake Error: generator Visual Studio 16 2019 does not match Visual Studio 14 2015 Win64` | CMake 不允许同 build 目录混用 generator |
| B2 | BOOST_ROOT 未设置 | bash 环境中未 export BOOST_ROOT | `CMake Error: BOOST_ROOT has to be set in ENV for MSVC++ build` | `configure_boost.cmake` 检查 `ENV{BOOST_ROOT}` |
| B3 | python3 路径不对 | 用系统 python3 而非 selena_environment 的 | 依赖库找不到 / 版本不够 | selena_environment 自带 Python 3.8，系统可能是 3.13 |
| B4 | clean 时文件被占用 | VS 打开了 build 目录的项目 | `PermissionError: [WinError 32] file being used by another process` | VS 锁定 `.VC.db` 等文件 |
| B5 | PATH 不完整 | 缺少 boost/Qt/MATLAB 路径 | DLL 加载失败 / cmake 找不到依赖 | selena 依赖大量外部 DLL |

### 1.2 适配策略

```
编译前必须完成的环境准备：
  1. 检测 CMakeCache.txt 中记录的 generator → 自动匹配 -vs 参数
  2. export BOOST_ROOT (检测 .config 文件或默认路径)
  3. 使用 selena_environment 的 python3.exe
  4. PATH 组装: selena_env + boost + Qt + MATLAB + 系统 PATH
  5. 增量编译优先，clean 前先检测文件锁
```

### 1.3 环境检测顺序 (启动时执行)

```
1. 查找 selena_environment 目录
   - 默认: C:\TCC\Tools\selena_environment\*\MSYS\mingw64\bin\python3.exe
   - 可选: config.yaml 中 paths.selena_python
   
2. 查找 R2D2.py
   - 默认: {source_root}/ip_dc/dc_tools/R2D2.py
   - 可选: config.yaml 中 paths.r2d2_script

3. 查找 build_config (.config)
   - 默认: {source_root}/apl/byd/selena/cmake_build_cfg/*.config
   - 可选: --build-config 参数指定

4. 检测已有 build 目录的 CMakeCache.txt
   - 读取 CMAKE_GENERATOR 行 → 提取 "Visual Studio X" → 映射 vs 参数
   - 无 cache → 使用默认 vs14 (VS2015)

5. 检测 BOOST_ROOT
   - 环境变量已有 → 直接用
   - 无 → 扫描 C:\TCC\Tools\boost\* → 取最新版本
   - 仍无 → 报错，提示用户设置

6. 组装 PATH
   - boost/lib64-msvc-14.0
   - Qt/msvc2015_64/bin
   - Qt/msvc2015_64/lib
   - MATLAB/bin/win64
   - selena_environment/MSYS/mingw64/bin

7. 检测 clean 可行性
   - 用 subprocess 试 rm -rf，捕获 PermissionError
   - 失败时提示 "VS 可能打开了项目，请先关闭"
```

### 1.4 编译命令模板

```bash
# 环境变量
export PATH="{selena_mingw_bin}:{boost_lib}:{qt_bin}:{qt_lib}:{matlab_bin}:$PATH"
export BOOST_ROOT="{boost_root}"

# R2D2 调用
{python3} {r2d2} \
  -m "{build_config}" \
  -ghs_math -use_mat -notests -bm {build_mode} \
  {-vs_flag} \
  2>&1
```

### 1.5 编译时间基准

| 场景 | cmake | make | 总计 |
|------|-------|------|------|
| 首次全量 (2026-06-11 实测) | 3m11s | 7m28s | 10m39s |
| 增量编译 (估计) | 0m30s | 1-3m | 1-3m |

---

## 2. 仿真运行环境适配 (selena.exe)

### 2.1 已踩坑记录

| # | 问题 | 触发条件 | 表现 | 根因 |
|---|------|----------|------|------|
| R1 | --platformpluginpath 参数无效 | 命令行传 `--platformpluginpath` | `Unknown option: --platformpluginpath` | 该参数是 Qt 内部机制，VS 通过环境变量 QT_PLUGIN_PATH 注入，selena.exe 命令行不认识 |
| R2 | runtime XML task 不匹配 | runtime XML 与 MF4 数据来自不同版本 | `Mismatch: signal 0x133 task core0_t10 vs mC6E75A12` | Selena 运行时校验 task 映射，版本不一致则拒绝 |
| R3 | nogui 模式参数 | selena.exe 命令行不支持 --nogui | 需在 config 文件中设 `nogui=true` | nogui 是 config 参数，不是命令行参数 |
| R4 | 文件锁冲突 | VS 打开了 selena 项目 | Python clean 失败 WinError 32 | VS 的 .VC.db 文件锁定 |
| R5 | 可执行文件混淆 | PRD/文档写 daddy.exe | 实际产物是 selena.exe | daddy.vcxproj 生成 daddy.lib (静态库)，selena.vcxproj 生成 selena.exe |

### 2.2 适配策略

```
仿真运行前必须完成：
  1. 确定可执行文件: selena.exe (不是 daddy.exe)
  2. 位置: {build_output}/dc_tools/selena/core/{build_mode}/selena.exe
  3. 不传 --platformpluginpath
  4. 只传 --paramconfig <config.txt>
  5. PATH 必须包含: Qt bin + boost lib + MATLAB bin
  6. 仿真超时前检查进程是否还活着（避免挂起）
```

### 2.3 启动命令模板

```bash
# 环境变量
export PATH="{matlab_bin}:{qt_bin}:{boost_lib}:$PATH"

# 最小启动命令 (不需要 --platformpluginpath)
{selena_exe} --paramconfig "{config_txt}"
```

### 2.4 配置文件关键点

config.txt 中必须包含：
```
nogui=true              # 无头模式，命令行运行时必须
config={runtime_xml}    # runtime XML 路径
input={input_mf4}       # 输入数据
output={output_mf4}     # 输出数据
log={log_file}          # 日志文件
tolerant={true/false}   # 是否容忍 signal mismatch
```

---

## 3. MF4 数据适配

### 3.1 已知问题

| # | 问题 | 说明 |
|---|------|------|
| M1 | MF4 与 runtime 版本绑定 | MF4 中的信号 task 映射由录制时的 runtime 版本决定，换了 runtime 可能不匹配 |
| M2 | 大文件处理 | 典型 270MB，需要流式读取，不能一次性加载全部 |
| M3 | python-asammdf 兼容性 | 需实测项目 MF4 能否被 asammdf 正确解析 |

### 3.2 适配策略

```
MF4 读取：
  1. 优先使用 asammdf 的 block 读取模式 (只读需要的通道)
  2. 大文件 (>100MB) 使用 HDF5 后端 (if available)
  3. 解析失败时回退到 Vector CANdbase API (如果可安装)
```

---

## 4. 多项目/多 binding 适配

### 4.1 项目结构差异

不同 binding 目录结构可能不同：

| 路径变量 | BYD_OVS_CB (ovrs25) | BYD-SC6H-cr60light | 其他 binding |
|----------|---------------------|-------------------|-------------|
| source_root | C:\BYD_OVS_CB | D:\BYD-SC6H-cr60light\cr60_light | 自定义 |
| R2D2.py | ip_dc/dc_tools/R2D2.py | 可能不同位置 | 需配置 |
| build_config | apl/byd/selena/cmake_build_cfg/ | 可能不同 | 需配置 |
| selena exe | dc_tools/selena/core/{mode}/selena.exe | 可能不同 | 需配置 |

### 4.2 配置驱动的适配

所有路径必须通过 `config.yaml` 配置，硬编码只能作为默认值：

```yaml
project:
  name: "BYD_OVS_CB"          # 项目标识
  binding: "ovrs25"           # binding 名称

paths:
  source_root: "C:/BYD_OVS_CB"
  r2d2_script: null           # null = 自动检测 {source_root}/ip_dc/dc_tools/R2D2.py
  build_config: null          # null = 自动搜索 cmake_build_cfg/ 目录
  selena_python: null         # null = 自动搜索 C:\TCC\Tools\selena_environment\
  boost_root: null            # null = 自动搜索 C:\TCC\Tools\boost\
  
selena:
  executable_name: "selena.exe"  # 注意不是 daddy.exe
  executable_pattern: "**/dc_tools/selena/core/{build_mode}/selena.exe"
  runtime_xml: "C:/tools/Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml"
  config_template: "C:/tools/byd_CR_Selena_Config_ovrs.txt"
  # 重要: 不要传 --platformpluginpath 给 selena.exe
  no_platform_plugin_path: true

env:
  path_prefix:
    - "C:/Program Files/MATLAB/R2023b/bin/win64"
    - "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin"
    - "C:/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0"
  boost_root: null  # null = 自动检测
```

---

## 5. 运行时检测清单 (runtime check)

每次执行 `rsim` 命令前，自动运行以下检查：

```
[ ] 1. Python 版本 >= 3.8 (selena_environment 自带)
[ ] 2. R2D2.py 存在且可执行
[ ] 3. build_config (.config) 存在
[ ] 4. BOOST_ROOT 环境变量已设置
[ ] 5. selena.exe 存在 (编译后)
[ ] 6. selena_dll.dll 存在 (编译后)
[ ] 7. Runtime XML 文件存在
[ ] 8. config 模板文件存在
[ ] 9. MATLAB/Qt/Boost 在 PATH 中
[ ] 10. build 目录无文件锁 (如需 clean)
[ ] 11. CMakeCache.txt 的 generator 与命令行一致
[ ] 12. asammdf 库已安装 (信号提取)
```

检查失败时给出**具体修复建议**，不只是报错。

---

## 6. 错误处理策略

### 6.1 编译失败

```
R2D2 输出解析规则：
  - 搜索 "R2D2 execution finished successfully" → 成功
  - 搜索 "R2D2 execution failed" → 失败
  - 搜索 "error C" 行 → 提取编译错误
  - 搜索 "CMake Error" → 提取 CMake 错误
  - 搜索 "PermissionError" → 文件锁，提示关闭 VS
  - 搜索 "generator.*does not match" → generator 冲突，提示清除 CMakeCache.txt
  - 搜索 "BOOST_ROOT" → 环境变量缺失，提示设置
```

### 6.2 仿真失败

```
selena.exe 退出码分析：
  - 0 = 正常完成
  - 非 0 = 检查 CRlog.log
  - 超时 (600s) = 可能挂起，生成诊断快照

日志关键字检测：
  - "Mismatch" → runtime/task 不匹配
  - "Missing trigger" → 信号缺失
  - "error" → 运行时错误
  - "FATAL" → 严重错误
```

### 6.3 文件锁处理

```
Windows 文件锁检测与处理：
  1. 尝试操作前先用 ctypes + FindFirstFile 检查句柄
  2. 被锁时输出: "文件 X 被进程 Y (PID: Z) 占用"
  3. 常见锁定进程: devenv.exe (VS), code.exe (VSCode), explorer.exe
  4. 建议用户关闭对应进程后再试
```

---

## 7. 多代际适配原则

### 7.1 设计约束

当前项目以**五代雷达 (CR5CB)** 为唯一实现目标。但架构上必须满足：

- **五代和六代的编译/仿真/数据格式完全不同**
- 六代的具体环境未知，但扩展时必须做到**不改核心代码**
- 所有五代特有逻辑封装在 `platforms/gen5_selena/` 下
- 核心层 (`core/`) 只定义接口，不硬编码任何五代路径/命令

### 7.2 代际隔离规则

```
严格禁止：
  - core/ 或 cli/ 中出现 "gen5"/"CR5CB"/"ovrs25" 等代际标识
  - core/ 直接 import platforms/gen5_selena 下的模块
  - 在核心逻辑中使用 if platform == "gen5" 的分支判断

应当做到：
  - CLI 通过 --platform 或 config.yaml 加载对应 PlatformBackend
  - 所有代际差异通过 PlatformBackend 接口收敛
  - 信号列表、规则、环境路径全部配置化（按平台分文件）
  - 通用能力（AI 分析、规则引擎、HTML 报告）不依赖代际
```

### 7.3 六代扩展流程（预设计）

当获得六代环境后，扩展步骤：

1. 新建 `platforms/gen6_xxx/` 目录
2. 实现 `class Gen6Platform(PlatformBackend)` — 覆盖所有抽象方法
3. 在 `platforms/__init__.py` 中 `register(Gen6Platform)`
4. 新增 `config/platform_config/gen6.yaml`（环境路径、信号、规则）
5. 用户通过 `--platform gen6_xxx` 切换

**六代开发过程中不需要触碰五代代码，五代代码也不需要修改。**

### 7.4 环境差异维度备忘

未来适配六代时需要收集和记录的维度：

| 维度 | 收集内容 |
|------|---------|
| 编译系统 | 构建工具（SCons? CMake? 新工具?）、编译器版本、环境变量 |
| 仿真框架 | 可执行文件、启动参数、配置格式、日志格式 |
| 数据格式 | 输入/输出文件格式（MF4? MDF? 自定义?）、解析库 |
| 信号体系 | 信号命名规则、功能分类、TGU 输出模式 |
| 环境依赖 | Qt/Boost/MATLAB 版本、新增依赖 |
| 目录结构 | source_root 布局、build output 位置 |

### 7.5 代码审查要点

代码审查时检查：
- [ ] 新增代码是否违反了代际隔离规则
- [ ] 代际特有的路径/命令是否放在 `platforms/` 或 `config/` 下
- [ ] 新模块是否能被其他代际复用（如果可以，应该放在 `plugins/` 或 `core/`）

---

## 8. 变更记录

| 日期 | 变更 | 触发原因 |
|------|------|----------|
| 2026-06-11 | 初始版本 | 编译/仿真实测踩坑 |
| 2026-06-11 | B1-B5, R1-R5 坑点记录 | 自动化编译实测 |
| 2026-06-11 | 入口 exe 更正: daddy.exe → selena.exe | 编译产物实地探索 |
| 2026-06-11 | §7 多代际适配原则 | 六代雷达环境需求，架构调整为 platform-backend 模式 |
