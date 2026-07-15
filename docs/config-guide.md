---
title: radar-sim 配置手册
description: 多项目雷达仿真工具的配置指南
---

# radar-sim 配置手册

> 新用户只维护一份项目无关 `UserRunConfig 2.0`，可在 Web 导入/修改/导出，并由 SDK 直接提交。最小字段见 [`config/simulation.example.yaml`](../config/simulation.example.yaml)：代码路径/分支/编译脚本或 Runtime Bundle、数据路径、Adapter、MatFilter、执行目标与结果选项。用户不填写 project、recipe、manager、共享盘映射或 Agent。
>
> 下文 `config/projects/*` 是平台管理员维护的内部适配层和 legacy CLI 说明，不是业务用户配置。

## 1. 配置文件架构

```
config/
├── default.yaml                          # 全局默认配置（AI 设置、默认项目）
├── platforms/
│   └── gen5_selena.yaml                  # 平台默认配置
├── recipes/
│   └── <recipe>.yaml                     # recipe 级覆盖（按项目复用构建/仿真形态）
└── projects/
    └── <项目名>/
        ├── config.yaml                   # 项目级配置（编译、环境、仿真）
  ├── local.yaml                    # 本地覆盖（可选，不入库）
        ├── signals.yaml                  # 信号监控配置
        ├── rules.yaml                    # 规则检查配置
        └── assets/                       # 仿真资产（runtime.xml 等）
```

### 配置加载顺序

最终配置是五层 merge 的结果，优先级 **从高到低**：

1. `config/projects/<项目名>/local.yaml` — 本地覆盖（可选，最高）
2. `config/projects/<项目名>/config.yaml` — 项目配置
3. `config/recipes/<recipe>.yaml` — recipe 复用配置
4. `config/platforms/<平台名>.yaml` — 平台默认配置
5. `config/default.yaml` — 全局默认（最低）

说明：
- `project.recipe` 用于选择 recipe 层，例如 `g3n_fvg3_od25`
- 配置采用深度 merge；字典递归合并，列表默认整体覆盖

配置系统会自动做以下路径推导：
- `selena_build_script` 缺省推导为 `{project_root}/apl/byd/bindings/{binding}/selena/jenkins_selena_build.bat`
- `hex_build_script` 缺省推导为 `{project_root}/apl/byd/bindings/{binding}/buildscripts/testbuild_BaseC0S_SINGLE.bat`
- `build_config` 可以从 `paths.selena_config` 推导
- `assets.fixed_config_path` 可以从 `paths.selena_paramconfig` 推导

## 2. default.yaml — 全局默认

```yaml
# 默认项目名（决定 rsim 命令不带 --project 时使用哪个项目）
default_project: "ovrs25"

analysis:
  ai:
    enabled: true
    base_url: "http://bcsc-openai.apac.bosch.com:30001/llm/model/v1"
    model: "hermes"
    timeout: 120
    max_tokens: 4096
    temperature: 0.1
    api_key_env: "MODEL_FARM_API_KEY"  # 从环境变量读取
  default_plugins:
    - signal_summary     # 信号统计
    - rule_check         # 规则检查
    - default_report     # HTML 报告
    # - ai_qa            # AI 分析（按需开启）
```

## 3. platform/\*.yaml — 平台默认配置

目前仅有 `gen5_selena.yaml`，定义 Gen5 Selena 平台的默认行为：

```yaml
machine:
  platform: gen5_selena

build:
  build_mode: RelWithDebInfo
  build_config: ROS_PER_SIT_RPM_FCT_RECR     # R2D2 build config 名称
  vs_solution: dc_tools/selena/selena.sln    # 相对于 build_output 的 sln 路径

assets:
  runtime_xml: selena/runtime.xml
  config_template: selena/selena_config_tmpl.txt
  fixed_config_path: byd_CR_Selena_Config_ovrs.txt
  matfilefilter: selena/matfilefilter.txt

vs_debug:
  solution: dc_tools/selena/selena.sln
  target_project: selena

environment:
  path_prefix: []
```

## 4. project config.yaml — 项目级配置

以 `ovrs25` 为例：

```yaml
project:
  name: "BYD_OVS_CB"           # 项目名称
  platform: "gen5_selena"      # 关联的平台配置

paths:
  project_root: "C:/BYD_OVS_CB"
  binding: "ovrs25"            # BYD binding 目录名

  # R2D2 编译入口
  r2d2_script: "C:/BYD_OVS_CB/ip_dc/dc_tools/R2D2.py"

  # Selena 编译配置名（会自动在 cmake_build_cfg 目录下查找 .config 文件）
  selena_config: "ROS_PER_SIT_RPM_FCT_RECR"

  # Selena 仿真 paramconfig 文件（对应 VS debug 的 --paramconfig）
  selena_paramconfig: "C:/tools/byd_CR_Selena_Config_ovrs.txt"

  # 编译/仿真所需的环境路径
  environment:
    matlab_root: "C:/Program Files/MATLAB/R2023b"
    qt_path: "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64"
    boost_root: "C:/TCC/Tools/boost/1.63.0_WIN64"
    selena_env_path: "C:/TCC/Tools/selena_environment/0.1.7_WIN64"
    python3_path: "C:/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin/python3.exe"
    vs_version: "2019"

  # R2D2 编译输出目录
  build_output: "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR"

  # 仿真运行时配置（对应 VS debug 启动参数）
  simulation:
    paramconfig: "C:/tools/byd_CR_Selena_Config_ovrs.txt"
    extra_args:
      - "--tolerant"
      - "--enable-multibuffer-border"
      - "--enable-doorkeeper"
    datasets:
      - name: "CBNA_23-4-26"
        input_mf4: "D:/data/byd/FRGVBYDP-21536/23-4-26_CBNA/CBNA_23-4-26/Gen5_2009-01-01_06-05_0116.MF4"
        output_dir: "D:/data/byd/FRGVBYDP-21536/23-4-26_CBNA/CBNA_23-4-26"

  # 分析时关注的信号
  signals:
    - name: "FCTA_State"
      group: "fcta"
      description: "FCTA 状态机"
    - name: "FCTA_Obj_Distance"
      group: "fcta"
      description: "目标距离"
    # ...
```

### 字段说明

| 字段 | 说明 | 必填 |
|------|------|------|
| `project.name` | 项目显示名称 | 是 |
| `project.platform` | 平台名，对应 `config/platforms/<name>.yaml` | 是 |
| `project.recipe` | 复用的 recipe 名，对应 `config/recipes/<recipe>.yaml` | 否 |
| `paths.project_root` | 源码根目录 | 是 |
| `paths.binding` | BYD binding 名，用于推导构建脚本路径 | 是 |
| `paths.r2d2_script` | R2D2.py 完整路径 | 是 |
| `paths.selena_config` | Selena 编译配置名 | 是 |
| `paths.selena_paramconfig` | Selena paramconfig 文件路径 | 是 |
| `paths.build_output` | 编译产物目录 | 是 |
| `paths.environment` | 环境依赖路径 | 是 |
| `paths.simulation` | 仿真启动配置 | 否 |
| `paths.signals` | 关注的信号列表 | 否 |

### 关键扩展字段

| 字段 | 说明 | 常见用途 |
|------|------|---------|
| `repos.outer_repo_root` | 外层仓库根目录 | `check` 校验本地仓库是否存在 |
| `repos.inner_repo_root` | 内层 Selena 仓库根目录 | `build` 前检查或切换分支 |
| `build.selena_branch` | 期望的 Selena 分支 | 构建前自动比对当前分支 |
| `build.script_args_template` | build script 参数模板 | 覆盖默认的 `build_mode/build_config/binding` 传参形态 |
| `simulation.adapter_file` | Selena paramconfig 中的 adapter 文件 | G3N/FVG3 等项目必需 |
| `simulation.paramconfig_options` | 额外 paramconfig 键值对 | 例如 `distilled-mat: true` |

`build.script_args_template` 示例：

```yaml
build:
  script_args_template:
    - "--mode"
    - "{build_mode}"
    - "--cfg"
    - "{build_config_name}"
```

`simulation.paramconfig_options` 示例：

```yaml
simulation:
  adapter_file: "D:/shared/adapter_byd.txt"
  paramconfig_options:
    distilled-mat: true
```

### VS 环境变量组装（`_build_env_full`）

编译和仿真时会自动组装 `PATH`，顺序如下：
1. `python3_path` 所在目录
2. `selena_env_path/MSYS/mingw64/bin`
3. `qt_path/bin` + `qt_path/lib`
4. `matlab_root/bin/win64`
5. `boost_root/lib64-msvc-14.0`
6. 系统原有 `PATH`

同时设置 `BOOST_ROOT` 环境变量。

### VS version 自动检测（`_detect_vs_postfix`）

根据 Visual Studio 安装目录自动推导 R2D2 参数：
- `C:\Program Files (x86)\Microsoft Visual Studio\2019` → `-vs vs16`
- `C:\Program Files (x86)\Microsoft Visual Studio\2022` → `-vs vs17`
- `C:\Program Files (x86)\Microsoft Visual Studio\2017` → `-vs vs15`

### 4.6 profiles — 仿真 profile（本地/云端统一模型）

一个项目下可定义多个 `profiles`，每个 profile 固定一组（Selena 来源 + 数据策略 + 后端），用 `--profile <name>` 切换。无 `--profile` 时用隐式 `default` profile（从 `simulation`/`cluster`/`assets` 派生，本地编译 + 原地引用）。

```yaml
profiles:
  - name: local-build
    description: "本地编译 Selena + 数据原地引用"
    backend: local              # local | cluster
    selena:
      source: build             # build=从 build.build_output 派生; path=用 exe 字段
      exe: ""                   # source=path 时必填
    data:
      copy: false               # true=把数据复制到本地临时区/共享工作区; false=原地引用
      required_signals: []      # 可选，--select 扫描时校验信号存在

  - name: byd-ovrs-bl01v7-er-shared
    description: "云端：历史共享 Selena + 公盘数据"
    backend: cluster
    selena:
      source: path
      exe: "\\\\share\\BYD_OVRS_Selena_Master\\selena.exe"
    data:
      copy: false
    cluster:                    # 仅 backend=cluster 需要
      group: Radar
      subgroup: PSS1
      simulation_prio: 1
    # 可选 asset/sim 覆盖（两个后端都生效）：
    runtime_xml: "\\\\share\\runtime_1r1v.xml"
    source: RadarFC
```

字段说明：

| 字段 | 说明 |
|------|------|
| `backend` | `local`（`rsim run`）或 `cluster`（`rsim cluster run`） |
| `selena.source` | `build`：从本地编译产物派生 selena.exe（集群后端会自动打包 trimmed runtime ≈90MB 推送到 job 目录）；`path`：用 `selena.exe` 指向已有产物 |
| `data.copy` | 数据是否复制。本地后端：公盘 UNC 数据默认原地引用（不下载），`copy=true` 才下载到本地临时区。集群后端：本地盘数据 `copy=true` 迁移到共享工作区，`copy=false` 则报错（不静默提交 worker 看不见的路径） |
| `data.required_signals` | `--select` 扫描时校验每个 MF4 是否含这些信号（bounded byte 扫描，不开 asammdf） |
| `cluster.*` | 集群调度参数（group/subgroup/simulation_prio/timeout_min 等） |

Cluster 提交凭据不得写入项目 YAML 或用户 `SimulationSpec`。部署服务通过环境变量 `RSIM_CLUSTER_KILL_PASSWORD` 注入；日志、任务事件和 dry-run command 只显示 `<redacted>`。

**向后兼容**：旧 `cluster.profiles`（扁平格式，无 `backend`/`selena`/`data` 嵌套）自动转换为统一 profile（backend=cluster）。顶层 `profiles` 与旧 `cluster.profiles` 合并，同名时顶层优先。

**相关命令**：

```bash
rsim run --profile local-build --dataset <name> --select    # 本地仿真
rsim cluster run --profile <cluster-profile> --select --execute  # 云端一键
rsim check --backend local|cluster --profile <name>          # 后端定向环境检查
```

## 5. signals.yaml — 信号监控配置

```yaml
signals:
  - name: "FCTA_State"
    group: "fcta"
    description: "FCTA 状态机"

  - name: "TGU_OUT_ObjectList"
    group: "tgu"
    description: "TGU 输出目标列表"

groups:
  fcta:
    - FCTA_State
  tgu:
    - TGU_OUT_ObjectList
```

信号名称必须与 MF4 文件中实际的 signal name 完全匹配（大小写敏感）。

## 6. rules.yaml — 规则检查配置

```yaml
rules:
  - name: "fcta_activates"
    signal: "FCTA_State"
    condition: "reaches value 1"
    severity: "P0"
    description: "FCTA should enter ACTIVE state"

  - name: "no_critical_error"
    source: "log"
    condition: "no [ERROR] entries"
    severity: "P0"
    description: "No critical errors in simulation log"
```

### 支持的 source 类型

| source 类型 | 说明 | 支持的 condition |
|------------|------|-----------------|
| `signal`（默认） | 基于信号数据 | `reaches value X`, `always > X`, `always < X` |
| `log` | 基于仿真 log 文件 | `no [ERROR] entries` |
| `file` | 基于文件存在性 | `output_mf4 exists and size > 0` |

### severity 级别

- `P0` — 阻断性问题
- `P1` — 需要关注
- `P2` — 仅供参考

## 7. Selena paramconfig 文件

`C:\tools\byd_CR_Selena_Config_ovrs.txt` 是 Selena 仿真的启动配置：

```
config=C:\tools\Runtime_BYD_OVRS25_CR5CB_BL16_RC36.xml   # Runtime XML
input=D:\data\byd\...\Gen5_2009-01-01_06-05_0116.MF4       # 输入 MF4
output=D:\data\byd\...\Gen5_2009-01-01_06-05_0116out.MF4   # 输出 MF4
log=C:\tools\CRlog.log                                       # Log 文件
source=RadarFL                                               # 数据源标识
matfilefilter=C:\BYD_OVS_CB\...\matlab_swx_plotreco.mdf.mat.filter
nogui=true
write-mat=true
tolerant=false   # VS 中通常改为 true，命令行用 --tolerant 覆盖
userparam=mountingPosition=CFL
disable-sequence-check=false
enable-multibuffer-border=true
enable-doorkeeper=true
```

### VS debug 启动参数

在 Visual Studio 中调试 Selena 时使用的参数：
```
--paramconfig "C:\tools\byd_CR_Selena_Config_ovrs.txt" --tolerant --enable-multibuffer-border --enable-doorkeeper
```

对应的 `PATH` 环境变量必须包含 MATLAB、Qt、Boost 和 selena_environment 的路径。

## 8. 新增项目

1. 创建 `config/projects/<项目名>/` 目录
2. 编写 `config.yaml`、`signals.yaml`、`rules.yaml`
3. 在 `default.yaml` 中修改 `default_project`（可选）
4. 运行 `rsim --project <项目名> check` 验证配置

```bash
# 也可使用交互式向导
rsim init <项目名>
```

## 9. 平台扩展

新增平台需在 `platforms/` 下创建目录，实现：
- `builder.py` — 构建逻辑
- `__init__.py` — 注册平台类和 MF4 解析方法

配置加载时会根据 `project.platform` 自动查找 `config/platforms/<name>.yaml`。
