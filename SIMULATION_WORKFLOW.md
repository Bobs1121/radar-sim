# Selena 仿真流程实战记录

> 记录日期: 2026-06-23
> 环境: Windows 11, Python 3.12.10, Selena v1.18.0 Roberta, Qt 5.8.0, MATLAB R2023b
> 输入数据: 3 条 MF4 (CBNA_23-4-26 数据集, FL 前左雷达)
> 雷达角位: CFL (C-Front-Left) / source=RadarFL
> 结果: 3/3 全部成功 (Exit code: 0)

---

## 1. 仿真环境依赖

### 1.1 前置条件：已编译的 selena.exe

selena.exe 通过 `rsim build selena` 编译产出，路径为：
```
C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo\selena.exe
```

**验证存在**:
```powershell
Test-Path "C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo\selena.exe"
# True
```

### 1.2 运行时环境变量（PATH）

selena.exe 依赖以下 DLL/库路径，必须在 PATH 中按顺序排列：

| 组件 | 路径 | 必需文件 |
|------|------|---------|
| selena_environment | `C:/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin` | selena 运行时 DLL |
| MATLAB | `C:/Program Files/MATLAB/R2023b/bin/win64` | mwannocrt.dll 等 |
| Qt bin | `C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin` | Qt5Core.dll 等 |
| Qt lib | `C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/lib` | Qt 插件目录 |
| Boost | `C:/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0` | libboost_*.dll |

**配置来源**: `config/projects/ovrs25/config.yaml` → `paths.environment` 节

### 1.3 运行时 XML（可选）

selena.exe 通过 paramconfig 中的 `config=` 指向 Runtime XML：
```
C:\tools\Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml
```

该文件定义 34 个 runnable 和 316 条连接关系。

---

## 2. Paramconfig 参数字典

### 2.1 原始模板

基础 paramconfig 位于 `C:\tools\byd_CR_Selena_Config_ovrs.txt`：

```
config=C:\tools\Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml
input=D:\data\byd\FRGVBYDP-21536\23-4-26_CBNA\CBNA_23-4-26\Gen5_2009-01-01_06-05_0116.MF4
output=D:\data\byd\FRGVBYDP-21536\23-4-26_CBNA\CBNA_23-4-26\Gen5_2009-01-01_06-05_0116out.MF4

log=C:\tools\CRlog.log
source=RadarFL
 
matfilefilter=C:\BYD_OVS_CB\reco_fw\tools\selena\matlab_transport_cfg\matlab_swx_plotreco.mdf.mat.filter
 
nogui=true
write-mat=true
 
tolerant=false
userparam=mountingPosition=CFL
disable-sequence-check=false
enable-multibuffer-border=true
enable-doorkeeper=true
```

### 2.2 关键字段说明

| 字段 | 含义 | 仿真中影响 |
|------|------|-----------|
| `config` | Runtime XML 路径，定义 runnable + 连接 | 决定加载哪些功能模块 |
| `input` | 输入 MF4 数据文件 | 雷达原始数据源 |
| `output` | 输出 MF4 路径 | 仿真后处理后的结果 |
| `log` | 仿真日志 | 调试用 |
| `source` | MF4 源名称 ("RadarFL") | 匹配 MF4 内部的 channel group |
| `matfilefilter` | MATLAB 过滤器 | .mat 输出过滤规则 |
| `nogui=true` | 无 GUI 模式 | 必须设置，否则弹出 GUI 窗口 |
| `write-mat=true` | 同时输出 .mat 文件 | MATLAB 后处理用 |
| `userparam=mountingPosition=CFL` | 传感器安装位置 | CFL=左前角 (C-Front-Left) |
| `enable-multibuffer-border=true` | 启用多缓冲区边界处理 | 处理输入序列号不连续情况 |
| `enable-doorkeeper=true` | 启用守门员机制 | 保证数据时序一致性 |

### 2.3 为多条数据生成参数字典

3 条输入数据需要各自的 paramconfig，区别仅在 `input` 和 `output` 路径：

```powershell
$base = "D:\data\byd\FRGVBYDP-21536\23-4-26_CBNA\CBNA_23-4-26"
$files = @(
    "Gen5_2009-01-01_05-56_0114.MF4",
    "Gen5_2009-01-01_06-02_0115.MF4",
    "Gen5_2009-01-01_06-05_0116.MF4"
)

foreach ($f in $files) {
    $in = "$base\$f"
    $out = "$base\$($f -replace '\.(MF4)$','out.MF4')"
    # 复制模板，替换 input 和 output 行
    Get-Content "C:\tools\byd_CR_Selena_Config_ovrs.txt" |
        ForEach-Object {
            $_ -replace '^input=.*', "input=$in" |
            ForEach-Object { $_ -replace '^output=.*', "output=$out" }
        } |
        Out-File "C:\tools\byd_CR_Selena_Config_ovrs_$($f -replace '\.(MF4)$','').txt" -Encoding ASCII
}
```

产出 3 个文件：
- `byd_CR_Selena_Config_ovrs_Gen5_2009-01-01_05-56_0114.txt`
- `byd_CR_Selena_Config_ovrs_Gen5_2009-01-01_06-02_0115.txt`
- `byd_CR_Selena_Config_ovrs_Gen5_2009-01-01_06-05_0116.txt`

### 2.4 参数字典验证

每个 paramconfig 必须满足：
1. `input` 路径对应的 MF4 文件存在
2. `config` 指向的 Runtime XML 存在
3. `matfilefilter` 指向的 filter 文件存在（MATLAB 输出需要）

---

## 3. selena.exe 调用方式

### 3.1 命令行格式

```
selena.exe --paramconfig <paramconfig_file> [additional_flags]
```

### 3.2 实际调用的参数

```powershell
selena.exe `
    "--paramconfig" "C:\tools\byd_CR_Selena_Config_ovrs_Gen5_2009-01-01_05-56_0114.txt" `
    "--tolerant" `
    "--enable-multibuffer-border" `
    "--enable-doorkeeper"
```

CLI 参数（`--tolerant` 等）会**覆盖** paramconfig 中的同名设置：
- `--tolerant` 覆盖 paramconfig 中的 `tolerant=false` → 容忍部分信号缺失
- `--enable-multibuffer-border` 覆盖 `enable-multibuffer-border=true`
- `--enable-doorkeeper` 覆盖 `enable-doorkeeper=true`

**来源**: `config/projects/ovrs25/config.yaml` → `paths.simulation.extra_args`

### 3.3 必需的环境设置

```powershell
# PATH 必须包含上述 5 个组件路径
$env:PATH = (
    "C:/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin;",
    "C:/Program Files/MATLAB/R2023b/bin/win64;",
    "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin;",
    "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/lib;",
    "C:/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0;"
) -join "" + $env:PATH

# Boost 需要额外环境变量
$env:BOOST_ROOT = "C:/TCC/Tools/boost/1.63.0_WIN64"
```

### 3.4 Working Directory

必须设置 selena.exe 所在目录为工作目录，以便插件加载器能找到 `plugins/` 子目录：
```
C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo\
```

---

## 4. 仿真执行过程

### 4.1 启动输出

selena.exe 启动后输出版本信息和参数回显：

```
Selena Version: 1.18.0 Roberta
Build date: Jun 18 2026

Parameter file =                    (命令行 --paramconfig 指定)
Input file mdfplayer = <input.MF4>
Source name mdfplayer = RadarFL
Output file mdfrecorder = <output.MF4>
Config = C:\tools\Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml

Selena will simulate the forensic use case:
 - Prefill output ports
 - Set internal states
User parameters: 
  mountingPosition = CFL
```

### 4.2 插件加载阶段

```
Loading plugins from path ".../RelWithDebInfo/plugins"
  DataPlayerModules:  mdfplayer (DataPlayerPluginMDF)
  DataRecorder:       mdfrecorder (DataRecorderPluginMDF)
  Scheduler:          mdfscheduler (SelenaSchedulerPluginMDF)
```

然后逐一定位 34 个 runnable：`g_Golf_Fct_Hmi_RunnableHmi`, `g_Golf_Fct_Spp_RunnableSpp`, ...

每个 runnable 的 float 异常处理和内部状态模式初始化为 `INIT`。

### 4.3 连接校验

启动后输出配置摘要：
```
runnables: 34
connections: 316
config warnings: 0
config errors: 0
connection errors: 7   ← 7 个 receiverport 在 MF4 中不存在
```

这些连接错误是**非致命**的 — 运行时会在 `tolerant` 模式下跳过对应通道。

### 4.4 执行调度

```
found startpoint - 796 trigger with 291 runnables skipped...
start simulation...
MDF-Scheduler running: | 96142/96142 100.00%
```

关键数字（以 #1 为例）：
- 输入 MF4 ~260MB
- 总触发信号: 96,142 条
- 跳过: 291 条 (startpoint 之前)
- 处理: 796 条 trigger
- 仿真时长: ~30s 数据量, ~37s 实际耗时, 0.83x 实时因子

### 4.5 Doorkeeper 警告

仿真过程中会输出大量 doorkeeper 警告，例如：
```
Doorkeeper exact sequence number not found for m_escStatePort_in
  (requested: 40127; storage: [40102, 40103, ..., 40125])
```

**原因**: 某些 CAN 信号在输入 MF4 中的 sequence number 间断（40126 缺失，跳到 40127）。
启用 `--enable-doorkeeper` 后会使用最近可用数据进行插值，而不是中止执行。这是**预期行为**。

### 4.6 完成输出

```
MDF-Scheduler finished: file duration: 30.011655sec simulation duration: 36.335230sec factor: 0.825966
Total number of signals not found: 23120   ← 部分信号在输入 MF4 中不存在

Simulation finished [2026-06-23 14:24:51].
Duration init: 54544ms    (环境初始化)
Duration simulation: 37465ms  (实际执行)
```

Exit code: 0 → 仿真成功。

---

## 5. 验证仿真结果

### 5.1 输出文件存在性

```
D:\data\byd\FRGVBYDP-21536\23-4-26_CBNA\CBNA_23-4-26\
├── Gen5_2009-01-01_05-56_0114.MF4          (输入,  260 MB)
├── Gen5_2009-01-01_05-56_0114out.MF4       (输出,  364 MB) ← 新生成
├── Gen5_2009-01-01_06-02_0115.MF4          (输入,  260 MB)
├── Gen5_2009-01-01_06-02_0115out.MF4       (输出,  363 MB) ← 新生成
├── Gen5_2009-01-01_06-05_0116.MF4          (输入,  260 MB)
└── Gen5_2009-01-01_06-05_0116out.MF4       (输出,  363 MB) ← 新生成
```

输出文件比输入文件大 ~104 MB，因为包含了经过所有 runnable 处理后的内部信号（雷达感知输出、FCTA 状态、BSD 报警等）。

### 5.2 成功判断标准

| 检查项 | 结果 | 含义 |
|--------|------|------|
| selena.exe exit code = 0 | | 正常退出 |
| `<input>out.MF4` 文件存在且 > 0 | | 输出了仿真数据 |
| 输出文件比输入大 | | 经过 runnable 处理，附加了输出信号 |
| 无 fatal/crash 日志 | | 无运行时崩溃 |
| `Total number of signals not found` | 23120 | 部分输入信号不存在，可接受 |
| connection errors: 7 | | 7 个端口无映射，可接受 |

---

## 6. 完整批次执行命令（可复现）

```powershell
# ====== 第 1 步: 环境准备 ======
$env:PATH = (
    "C:/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin;",
    "C:/Program Files/MATLAB/R2023b/bin/win64;",
    "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/bin;",
    "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64/lib;",
    "C:/TCC/Tools/boost/1.63.0_WIN64/lib64-msvc-14.0;"
) -join "" + $env:PATH
$env:BOOST_ROOT = "C:/TCC/Tools/boost/1.63.0_WIN64"

$exe = "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/dc_tools/selena/core/RelWithDebInfo/selena.exe"

# ====== 第 2 步: 为每条数据生成 paramconfig ======
$base = "D:\data\byd\FRGVBYDP-21536\23-4-26_CBNA\CBNA_23-4-26"
$files = @(
    "Gen5_2009-01-01_05-56_0114.MF4",
    "Gen5_2009-01-01_06-02_0115.MF4",
    "Gen5_2009-01-01_06-05_0116.MF4"
)

$template = "C:\tools\byd_CR_Selena_Config_ovrs.txt"

foreach ($f in $files) {
    $in = "$base\$f"
    $out = "$base\$($f -replace '\.(MF4)$','out.MF4')"
    $cfg = Get-Content $template
    $cfg = $cfg | ForEach-Object { $_ -replace '^input=.*', "input=$in" }
    $cfg = $cfg | ForEach-Object { $_ -replace '^output=.*', "output=$out" }
    $cfgFile = "C:\tools\byd_CR_Selena_Config_ovrs_$($f -replace '\.(MF4)$','').txt"
    $cfg | Out-File $cfgFile -Encoding ASCII
}

# ====== 第 3 步: 逐条执行仿真 ======
foreach ($f in $files) {
    $cfg = "C:\tools\byd_CR_Selena_Config_ovrs_$($f -replace '\.(MF4)$','').txt"
    Write-Host "=== Running: $f ===" -ForegroundColor Yellow
    & $exe "--paramconfig" $cfg "--tolerant" "--enable-multibuffer-border" "--enable-doorkeeper"
    Write-Host "Exit code: $LASTEXITCODE" -ForegroundColor Cyan
    Write-Host ""
}

# ====== 第 4 步: 验证结果 ======
Get-ChildItem "$base\*.MF4" |
    Select-Object Name, @{N='MB';E={[math]::Round($_.Length/1MB,1)}}, LastWriteTime
```

---

## 7. rsim run 命令集成

### 7.1 rsim 工具链定位

```
rsim build selena    ← 编译出 selena.exe
rsim prepare-sim     ← 准备 paramconfig（同步到 C:/tools/）
<用户手动或使用 rsim run> ← 执行仿真
rsim analyze         ← 分析输出 MF4
```

### 7.2 通过 rsim run（待改进）

当前 `rsim run` 命令 (`cli/run.py`) 实现了自动化调用 selena.exe：
```
rsim run <input.mf4> [--output-mf4 <out.mf4>] [--extra-args ...]
```

但实际运行时，本次批处理采用的是**直接 PowerShell 调用**，因为：
1. `render_selena_config` 的 `assets.config_template` 指向需要 MF4 路径占位符，但模板中是固定路径
2. paramconfig 的 `nogui=true` + `write-mat=true` 是模板内参数，CLI 参数 `--tolerant` 覆盖 `tolerant=false`

### 7.3 改进方向

后续 `rsim run` 需要支持：
1. **批量执行**：给定数据集目录，遍历所有 MF4 执行仿真
2. **参数字典自动生成**：读取模板，自动替换 `input`/`output` 路径
3. **错误容错**：selena.exe 的 "signals not found" 和 "connection errors" 应视为 WARNING 而非 ERROR
4. **状态回写**：仿真结果记录到 `results/<project>/.run_history.json`

---

## 8. Cluster 批量仿真环境（待接入）

用户提供了可用于批量仿真的内部 Cluster V2.0 环境。

合规室里面 / VDI：

- `Remote(VDI) - XC-CN Data Compliance Solution - Docupedia`
- `01_Cluster+KPI+VDI - XC-DA/EDY-CN - Docupedia (bosch.com)`
- VDI Cluster Path：`\\selena01\_cluster_software\`

合规室外面：

- `Submit Gen5 Cluster Simulation Task Online - XC-AS/EDY-CN - Docupedia`
- 本地 job 页面：[http://szhradar01/cluster/?page=jobs](http://szhradar01/cluster/?page=jobs)
- Cluster Path：`\\szhradar01\_cluster_software\`
- 工具/项目共享路径：`\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster`

### 8.1 已确认的 Cluster V2.0 提交流程

`\\szhradar01\_cluster_software\client.py` 是 Cluster Software 2.0 的提交客户端，Python 2 语法。典型调用来自 BYD_OVRS 样例：

```bat
C:\Python27\python.exe client.py <Config.cfg> 1234 <username>
```

样例位置：

```text
\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER
```

其中：

- `Config.cfg` 定义 job：输入数据路径、仿真脚本、优先级、worker group/subgroup、timeout 等
- `SIMULATION_SELENA.py` 定义 worker 上实际执行的仿真命令
- `datafile_path` 可以直接指向单个 MF4 文件；manager 会将其作为单任务 job
- `extension` 支持 `*.MF4,*MF4.zip`
- 输出目录由 manager 在 config 文件所在目录下自动创建，形如 `OUT_<timestamp>...`

Cluster config 必需字段包括：

```text
simulation
simulation_prio
python_version
datafile_path
extension
skip_dir
skip_filename
finalstep
send_email
send_netsend
group
subgroup
```

BYD_OVRS 旧样例中 `SIMULATION_SELENA.py` 使用命令行方式调用 Selena：

```text
selena.exe --nogui --i_mdfplayer <inputfile> -c <runtime.xml> -o <output.MF4> -l <log.log> -s <RadarFC/RadarFL> ...
```

当前 `radar-sim` 项目更适合生成 worker 脚本，在 worker 的 `outputpath` 下临时写 paramconfig，然后执行：

```text
selena.exe --paramconfig <generated_paramconfig> --tolerant --enable-multibuffer-border --enable-doorkeeper
```

### 8.2 当前访问结果（2026-06-26）

从当前 Codex 会话实测：

- `\\szhradar01\_cluster_software\` 可读
- `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster` 可读
- `\\selena01\_cluster_software\` 当前 shell 无法解析；用户反馈 Windows 文件资源管理器可直接打开
- `http://szhradar01/cluster/?page=jobs` 超时
- XML-RPC manager `szhradar01.apac.bosch.com:8123` 超时
- 本机缺少 `C:\Python27\python.exe`，无法直接运行 Python 2 `client.py`

补充体检结果：

- `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster` 支持当前会话读写
- 已在 `...\Cluster\radar-sim_probe\...` 下创建小文件并读回成功
- 从 UNC 路径执行一个 Python 探针脚本成功
- 1 MB 文件写入/读取往返成功，但速度偏慢；大批量 MF4 最好原本就在共享路径上，避免每次从本地 `D:\data` 搬运
- `\\szhradar01\_cluster_software\` 包含 Cluster V2.0 的 `client.py`、`manager.py`、`worker.py`、`simulation_runtime.py`、`python27_deprecated_modules` 和 Python 2.7 安装包
- MySQL `szhradar01:3306` 可连；只读查询显示 cluster 在线：
  - `state=1`
  - `state_message=Online`
  - `manager_host=SZHRADAR01`
  - `manager_port=8123`
  - `http_host=https://szhradar01`
- 可用 group/subgroup 包括：
  - `Radar/PSS1`
  - `Radar/PSS2`
  - `Radar/ACC`
  - `Radar/Jenkins`
  - `Radar/RA6`
- 当前会话访问 `80/443/8123` 均超时，因此还不能通过官方 web/manager 入口提交任务

### 8.3 ovrs25 云端第二通路判断

本地编译 Selena 后，理论上可以通过共享盘发布到云端 worker 使用。当前本地 Selena 运行目录：

```text
C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo
```

全量约 640 MB；去掉 `.pdb`、`.ilk`、log、missing-signal 文本后，运行必需文件约 90 MB：

```text
selena.exe
selena_dll.dll
selena_core.dll
selena_gui.dll
Mdf4Lib_x64.dll
MdfLibSort_x64.dll
MDFSort_x64.dll
Qt5Core.dll
Qt5Xml.dll
XmlParser_x64.dll
```

当前项目已有云端可复用的仿真资产：

```text
config/projects/ovrs25/assets/selena/runtime.xml
config/projects/ovrs25/assets/selena/matfilefilter.txt
config/projects/ovrs25/assets/selena/selena_config_tmpl.txt
```

需要注意：

- worker 不能访问本机 `D:\data\...`，输入 MF4 必须放到 worker 可访问共享路径
- worker 不能依赖本机 `C:\BYD_OVS_CB\...`，Selena 运行包必须放到共享路径，或使用 cluster 上统一安装/发布的 Selena 包
- `rsim cluster prepare` 可以先生成 job 包而不提交：
  - `Config.cfg`
  - `SIMULATION_RADAR_SIM.py`
  - runtime/filter/paramconfig template
  - 指向共享路径上的 Selena 运行包和输入 MF4
- 官方提交仍需要 `client.py` 能连到 manager `8123`
- 因为数据库 `3306` 可达，理论上可以直接写 `cluster_jobs/cluster_tasks` 入队；但这会绕过 manager 校验，必须等用户明确批准后再考虑，而且只应先做单 MF4 smoke test

当前只记录入口信息，尚未验证提交协议。后续接入时建议先调研：

1. 在线提交页面和 `client.py` 的推荐使用边界
2. 单个 job 需要携带哪些文件：MF4、paramconfig、runtime XML、adapter/filter、selena.exe 或编译产物
3. 输出 MF4、log、状态文件的回收路径
4. job 页面是否可用于查询状态，是否有可脚本化接口
5. 是否支持并发数、优先级、项目/用户隔离等参数

建议设计方向：

- 保留本地 `rsim run` 作为 baseline
- 新增 cluster backend，而不是把 cluster 逻辑写死进 `core/simulation.py`
- 可能的命令形态：

```powershell
rsim cluster submit --dataset <dataset_dir>
rsim cluster status <job_id>
rsim cluster fetch <job_id>
```

---

## 9. 排错清单

| 症状 | 原因 | 解决 |
|------|------|------|
| `selena.exe` 无法启动 | PATH 缺少 DLL | 检查 5 个组件路径全部存在 |
| `selena.exe not found` | 未编译或路径错误 | `rsim build selena`，检查 `build_output` |
| `nogui=true` 但仍然弹出 GUI | paramconfig 中 nogui 未设置 | 确认 `nogui=true` 在文件中 |
| connection errors: 7 | 运行时 XML 中的端口在 MF4 中不存在 | `--tolerant` 可忽略 |
| signals not found: 23120 | 输入 MF4 缺少部分信号 | 正常，这些是 CAN 总线非核心信号 |
| Doorkeeper sequence not found | 序列号间断 | `--enable-doorkeeper` 已启用插值 |
| 仿真卡住不结束 | 输入 MF4 损坏或路径错误 | 检查文件大小，用 CANalyzer 验证 |
| 输出 MF4 为空 | selena.exe 异常退出 | 检查 Exit code，查看 CRlog.log 日志 |

---

## 10. 控制平面（Linux 迁移雏形）

> 记录日期: 2026-07-03
> 状态: 已实现并验证（297 测试通过，含 21 个 control 测试），作为本地 web 控制台的并行通路

### 10.1 设计定位

真正的 Selena 编译/仿真只能在 Windows 本机执行（依赖 MATLAB/Qt/Boost/TCC 环境）。
控制平面解决的是"调度入口可迁移到 Linux"的问题：

- **Linux 侧**：跑轻量 control server（纯 stdlib HTTP + SQLite），管 job/task/agent 状态与日志
- **Windows 用户机**：跑 polling agent，主动轮询 server 认领任务，调用本机 `rsim` 执行
- **本机 web 控制台**（`rsim web`，127.0.0.1:8765）：继续走 `BuildTaskRegistry`，不依赖 server

两条通路并行不冲突。本地调试用 web；跨机器/集中调度用 server+agent。

### 10.2 架构

```
┌─────────────┐   HTTP/JSON    ┌──────────────────┐   轮询认领    ┌─────────────────┐
│  调度方       │ ────────────→ │  control server   │ ←───────────→ │  Windows agent   │
│ (CLI/web/    │   create-job  │  (Linux, stdlib   │   poll/heartbeat│ (polling, 调本机 │
│  外部系统)    │               │   + SQLite WAL)   │               │  rsim.exe)       │
└─────────────┘               └──────────────────┘               └─────────────────┘
                                     │                                      │
                                     │ SQLite                               │ subprocess
                                     │ results/_control.db                  │ rsim.py <cmd>
                                     ▼                                      ▼
                              jobs/tasks/agents/logs              check/build/run/cluster
```

数据模型：`jobs`（1）→ `tasks`（N，按 `order_index` 顺序依赖）→ `task_logs`（按 `log_id` 增量）。
`agents` 注册能力（capability），按 task_type 匹配认领。

### 10.3 命令

```bash
# 1. Linux 侧启动 control server
#    模式 A（cluster-only，推荐）：--allowed-task-types cluster.run
#      拒绝 local.check / local.build_selena / local.run_sim（HTTP 400）
#      Windows 端无需 MATLAB/Qt/Boost/VS，仿真在集群节点跑
#    模式 B（本机仓，全允许）：省略 --allowed-task-types
#      需 Windows 本机完整工具链，local + cluster 都能用
rsim server serve --host 0.0.0.0 --port 8877 --allowed-task-types cluster.run
# Control DB: results/_control.db（SQLite，自动创建）

# 2. 创建 job（写控制 DB，等 agent 认领）
#    模式 A 只能投 cluster.run；模式 B 四种都可
rsim server create-job local.check      --project ovrs25 --backend local        # 仅模式 B
rsim server create-job local.build_selena --project ovrs25 --mode RelWithDebInfo --clean  # 仅模式 B
rsim server create-job local.run_sim    --project ovrs25 --input-mf4 D:/data/case.MF4 --dry-run  # 仅模式 B
rsim server create-job cluster.run      --project ovrs25 --dataset smoke --max-minutes 5 --execute  # 模式 A/B

# 3. Windows 用户机启动 agent（轮询 server，调本机 rsim）
rsim agent --server-url http://<linux-host>:8877 --agent-id <user-pc>
# 模式 A 默认能力: cluster.run（+ tcc.*）
# 模式 B 显式启用 local: --capability local.check --capability local.build_selena --capability local.run_sim --capability cluster.run

# 4. 查询 / 取消
rsim server get-job  <job_id>
rsim server get-logs <job_id> [--task-id T] [--since N] [--limit 200]
rsim server cancel   <job_id>
```

### 10.4 HTTP 端点（control server）

| 方法 | 路径 | 作用 |
|------|------|------|
| GET  | `/health` | 存活检查 |
| POST | `/api/agents/register` | agent 注册（name/capabilities/metadata） |
| POST | `/api/agents/poll` | 认领下一个可执行 task |
| POST | `/api/agents/heartbeat` | 心跳 + 查 cancel_requested |
| POST | `/api/tasks/logs` | agent 回传增量日志（stdout） |
| POST | `/api/tasks/result` | agent 提交 task 结果（status/returncode） |
| POST | `/api/jobs` | 创建 job（job_type/payload/tasks/metadata） |
| GET  | `/api/jobs/<id>` | 查 job 状态（含所有 task） |
| GET  | `/api/jobs/<id>/logs?since=N&limit=L` | 增量拉日志 |
| POST | `/api/jobs/cancel` | 取消 job（级联 cancel 子 task） |

### 10.5 任务类型（task_type → rsim 子命令映射）

agent 把 task payload 翻译成本机 `rsim` 命令执行：

| task_type | rsim 命令 | 关键 payload 字段 | 模式 A | 模式 B |
|-----------|-----------|-------------------|:------:|:------:|
| `local.check` | `rsim check` | backend, profile, deps | ❌ | ✅ |
| `local.build_selena` | `rsim build selena` | mode, clean, no_progress | ❌ | ✅ |
| `local.run_sim` | `rsim run` | input_mf4/input_path, dataset, profile, output_mf4, timeout, dry_run | ❌ | ✅ |
| `cluster.run` | `rsim cluster run` | input_mf4, dataset, run_id, copy_data, copy_selena, execute | ✅ | ✅ |
| `tcc.bootstrap_itc2` | `rsim tcc bootstrap-itc2` | — | ✅ | ✅ |
| `tcc.install_toolcollection` | `rsim tcc install` | toolcollection | ✅ | ✅ |
| `tcc.auto_repair_all` | `rsim tcc auto-repair` | — | ✅ | ✅ |

> 模式 A：Linux server 用 `--allowed-task-types cluster.run` 启动，拒绝 local.* task；agent 默认 capability 为 `cluster.run`（+ tcc.*）。local.* 分支在 `cli/agent.py:_build_task_command` 中保留，模式 B 显式 `--capability local.*` 即可启用。

### 10.6 端到端验证（2026-07-03）

```
server serve → create-job local.check → agent --once
→ agent 认领 task → 构建 "rsim check" 命令 → 执行 → 流式回传日志
→ submit_result → job 状态聚合
```
全链路通。job 最终 `failed` 仅因 ovrs25 环境检查本就返回 1，非 agent bug。

> 2026-07-07 更新：双模式架构确立。模式 A（Linux cluster-only）下 server 用 `--allowed-task-types cluster.run` 限制，投 local task 返回 400；模式 B（Windows 本机仓）保留全部 task_type。

### 10.7 已知点

- **子进程编码**：agent 读 rsim 子进程输出用 `encoding=utf-8, errors=replace`，避免 Windows charmap 遇中文炸
- **web 控制台已接入控制平面**：`rsim web` 启动时内置 control server + polling agent（同进程线程），前端投递的 build/sim/tcc 任务走控制平面（create_job → agent 调本机 rsim → 日志回传）。`--no-control` 可退回本地 `BuildTaskRegistry`
- **任务历史统一**：`/api/tasks` 合并 control DB 的 jobs + 旧 registry 历史
- **顺序依赖**：同 job 内 task 按 `order_index` 串行，前序未 `succeeded` 则后序阻塞
- **取消语义**：`cancel_job` 对 queued task 直接标 cancelled，对 running task 置 cancel_requested，agent 心跳时发现即 terminate 子进程

### 10.8 web 接入控制平面（2026-07-03 完成）

`rsim web` 现在是控制平面的入口：

```
rsim web（一条命令）
  ├─ 内置 control server（线程，127.0.0.1:8877，results/_control.db）
  ├─ 内置 agent（线程，poll 本机 server，调 rsim 子命令）
  ├─ web HTTP server（127.0.0.1:8765，前端）
  └─ 前端不变（适配层 core/web_control.py 保持 tail() 11 字段 shape）
```

- **新增 `rsim tcc` CLI**：`bootstrap-itc2` / `install <tc>` / `auto-repair` / `status`，把 `core/tcc.py` 暴露成子命令供 agent 调度
- **agent 加 tcc task_type**：`tcc.bootstrap_itc2` / `tcc.install_toolcollection` / `tcc.auto_repair_all` → `rsim tcc <cmd>`
- **适配层 `core/web_control.py`**：状态映射（`succeeded→success`）、log_id 当 `total_lines` 游标、缺字段（exe_path/files_done 等）从 job.result 取
- **端到端验证**：前端点编译 → job 进 control DB → 内置 agent 认领 → 跑 `rsim build selena` → 122 行日志回传 → 前端轮询看到；tcc bootstrap_itc2 → `rsim tcc bootstrap-itc2` → success
- **未来 Linux 迁移**：把内置 server 拆成独立 `rsim server serve`（Linux），web 端加 `--server-url` 指向远程 server，agent 留在 Windows。代码路径已统一，迁移零特例
