# radar-sim 环境配置手册

本文面向 `ovrs25 / BYD_OVS_CB` 项目，目标是让新同事拿到仓库后，知道：

- 哪些依赖可以随项目一起交付
- 哪些依赖必须单独安装
- 需要配置哪些环境变量
- 建议把软件装到哪里
- `local.yaml` 应该怎么写

## 1. 总原则

`radar-sim` 的依赖分成两类：

1. Python 依赖和项目资产
   这类可以跟项目一起交付，甚至可以放进仓库或离线包。

2. 系统级工具链和商业软件
   这类通常不建议直接复制进仓库，需要单独安装或使用公司统一安装路径。

对于 `ovrs25` 来说，推荐的最小思路是：

- 项目配置只保留项目级信息，例如 `selena_build_script`、数据集目录、仿真策略
- 机器差异只放在 `config/projects/ovrs25/local.yaml`
- 运行时 PATH 由 `radar-sim` 根据配置自动拼接，不要求用户长期手工改系统环境变量

## 2. 依赖项分类

### 2.1 可以随项目一起交付的内容

- Python 代码本身
- `requirements.txt`
- 项目配置文件
- `config/projects/<project>/assets/` 下的项目资产
  - `runtime.xml`
  - `matfilefilter.txt`
  - `selena_config_tmpl.txt`
- 离线 Python wheel 包
  - 推荐单独做成 `third_party/python-wheels/`

### 2.2 不建议直接复制进仓库的内容

这些通常体积大、依赖注册表、涉及许可证，或者和机器 ABI 强相关：

- Visual Studio 2019
- MATLAB
- Qt 5.8 MSVC 版本
- Boost 1.63.0 WIN64
- `selena_environment`
- 项目源码树 `C:\BYD_OVS_CB`

### 2.3 可以复制，但更建议“统一安装目录”

如果公司内部允许，也可以通过共享盘或工具包统一发放，但不建议直接塞进 Git 仓库：

- `selena_environment`
- Qt
- Boost
- Python runtime

建议做法：

- 仓库中只保留“路径配置”
- 二进制包放在共享目录，例如 `D:\toolchains\...` 或公司软件仓库
- 每台机器通过 `local.yaml` 指向实际安装路径

## 3. ovrs25 项目当前依赖矩阵

### 3.1 必需软件

| 软件 | 是否必须 | 用途 | 当前推荐版本/路径 |
|------|------|------|------|
| Python | 必须 | 运行 `rsim` | 3.9+ |
| Visual Studio | 必须 | 编译/调试 Selena | VS2019 |
| MATLAB | 建议必须 | Selena / MATLAB 相关运行时 | `C:\Program Files\MATLAB\R2023b` |
| Qt | 必须 | Selena 运行时依赖 | `C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64` |
| Boost | 必须 | Selena / 构建依赖 | `C:\TCC\Tools\boost\1.63.0_WIN64` |
| selena_environment | 必须 | Python3 + MSYS runtime | `C:\TCC\Tools\selena_environment\0.1.7_WIN64` |
| BYD_OVS_CB 源码树 | 必须 | R2D2、编译、VS 工程、selena.exe | `C:\BYD_OVS_CB` |

### 3.2 Python 依赖

来自 [requirements.txt](/D:/RamboStar/idea/radar-sim/requirements.txt:1)：

- `PyYAML`
- `asammdf`
- `rich`
- `openai`
- `pytest`

推荐安装方式：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
pip install -e .
```

## 4. 项目配置与机器配置分工

### 4.1 项目配置

项目共享配置放在：
[config/projects/ovrs25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/ovrs25/config.yaml:1)

当前只保留项目级入口：

```yaml
project:
  name: "BYD_OVS_CB"
  platform: "gen5_selena"

build:
  selena_build_script: "C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/selena/jenkins_selena_build.bat"
```

系统会据此自动推导：

- `project_root`
- `binding`
- `build_config`
- `build_mode`
- `r2d2_script`
- `hex_build_script`
- `build_output`
- 部分工具链路径

### 4.2 机器本地配置

机器差异配置放在：
`config/projects/ovrs25/local.yaml`

模板见：
[local.example.yaml](/D:/RamboStar/idea/radar-sim/config/projects/ovrs25/local.example.yaml:1)

推荐最小写法：

```yaml
environment:
  matlab_root: "C:/Program Files/MATLAB/R2023b"
  qt_path: "C:/TCC/Tools/qt/5.8.0_WIN64/5.8/msvc2015_64"
  boost_root: "C:/TCC/Tools/boost/1.63.0_WIN64"
  selena_env_path: "C:/TCC/Tools/selena_environment/0.1.7_WIN64"
  python3_path: "C:/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin/python3.exe"
  vs_version: "2019"
```

如果自动检测已经正确，可以省略其中一部分。

## 5. 环境变量策略

## 5.1 推荐策略

推荐不要让用户永久修改系统级 `PATH`。

原因：

- 容易污染其他项目
- 多版本 Qt / Boost / Python 共存时容易冲突
- `radar-sim` 已经支持运行前按配置动态拼接 PATH

当前 `radar-sim` 会根据 `local.yaml` 生成运行期 PATH，主要包括：

- `selena_env_path\MSYS\mingw64\bin`
- `matlab_root\bin\win64`
- `qt_path\bin`
- `qt_path\lib`
- `boost_root\lib64-msvc-14.0`

## 5.2 必须关注的环境变量

通常只需要理解，不一定要永久写入系统：

- `PATH`
  - Selena 运行时 DLL 搜索路径
- `BOOST_ROOT`
  - 某些构建/运行场景会读取

如果必须手工临时设置，可以用：

```powershell
$env:BOOST_ROOT = "C:\TCC\Tools\boost\1.63.0_WIN64"
$env:PATH = @(
  "C:\TCC\Tools\selena_environment\0.1.7_WIN64\MSYS\mingw64\bin",
  "C:\Program Files\MATLAB\R2023b\bin\win64",
  "C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\bin",
  "C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\lib",
  "C:\TCC\Tools\boost\1.63.0_WIN64\lib64-msvc-14.0",
  $env:PATH
) -join ";"
```

但更推荐通过 `local.yaml + rsim check` 管理。

## 6. 当前项目建议安装目录

为了减少路径差异，推荐统一成下面这套：

```text
C:\BYD_OVS_CB
C:\TCC\Tools\boost\1.63.0_WIN64
C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64
C:\TCC\Tools\selena_environment\0.1.7_WIN64
C:\Program Files\MATLAB\R2023b
```

如果无法统一安装目录，也没关系，只要在 `local.yaml` 里写明实际路径即可。

## 7. 新机器落地步骤

### 7.1 安装软件

按顺序准备：

1. 安装 Python 3.9+
2. 安装 Visual Studio 2019
3. 准备 `C:\BYD_OVS_CB`
4. 安装 MATLAB
5. 安装 Qt 5.8 MSVC
6. 安装 Boost 1.63.0 WIN64
7. 安装 `selena_environment`

### 7.2 安装 Python 环境

```powershell
cd D:\RamboStar\idea\radar-sim
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
pip install -e .
```

### 7.3 复制本地配置模板

```powershell
Copy-Item `
  config\projects\ovrs25\local.example.yaml `
  config\projects\ovrs25\local.yaml
```

然后把里面的实际路径改成当前机器的路径。

### 7.4 验证配置

```powershell
python rsim.py --project ovrs25 check
python rsim.py --project ovrs25 prepare-sim
python rsim.py --project ovrs25 run --dataset CBNA_23-4-26 --dry-run
```

## 8. 是否可以把依赖复制进项目

结论分三档：

### 8.1 推荐复制进项目的

- Python 依赖离线包
- 项目级 runtime / template / filter 资产
- 配置模板
- 文档

### 8.2 可以做“项目旁路交付包”的

不进 Git，但可以和项目一起打包发给别人：

- `selena_environment`
- Qt
- Boost
- Python 安装包

推荐目录：

```text
delivery/
  radar-sim/
  third_party/
    python-wheels/
    boost/
    qt/
    selena_environment/
```

### 8.3 不推荐复制到项目仓库的

- Visual Studio
- MATLAB
- 整个 `C:\BYD_OVS_CB`

原因：

- 体积太大
- 许可证和安装机制复杂
- 升级维护困难
- Git 仓库会失控

## 9. 推荐交付方案

对于“后面准备移植给别人使用”的场景，推荐下面这个方案：

### 方案 A：最稳

- Git 仓库里保留代码、配置、项目资产、文档
- 机器级软件走统一安装
- 用户只改 `local.yaml`

优点：

- 最稳
- 版本边界清晰
- 仓库干净

### 方案 B：半离线交付

- Git 仓库 + 一个 `third_party` 离线包
- 离线包里放 Python wheel、Qt、Boost、selena_environment
- MATLAB / VS 仍然单独安装

优点：

- 适合内网机器
- 能减少装环境时间

### 方案 C：全复制进仓库

不推荐。

只有在非常小、完全内部、短期试验场景下才勉强可用。

## 10. 一键部署与诊断（已实现）

模式 B（Windows 本机仓）现已提供两条命令，把"看文档手配"变成"脚本半自动落地"：

1. `scripts/bootstrap.ps1` — 已实现
   - 自动检测 Python 3.9+
   - 自动创建 `.venv` 并升级 pip
   - 自动 `pip install -r requirements.txt` + `pip install -e .`（若 `third_party/python-wheels/` 有 wheel 则离线装）
   - 自动从模板生成 `local.yaml`
   - 跑 `rsim doctor` + `rsim check` 自检
   - 用法：`.\scripts\bootstrap.ps1 -Project ovrs25`

2. `rsim doctor` — 已实现
   - 检测 VS2017/2019/2022 实际安装
   - 检测 MATLAB / Qt / Boost / selena_environment / python3 路径是否存在
   - 检测 Python 包（PyYAML/asammdf/rich）可导入性
   - 检测集群 UNC 共享路径可达性
   - 输出分级（ok/warning/error）+ 修复建议
   - 用法：`rsim --project ovrs25 doctor`（或 `--backend local`/`--json`）

### 仍待补（后续）

- `rsim config init --auto-detect`：扫描本机路径自动填 `local.yaml` 的 `environment.*`（当前 bootstrap 只复制模板 + doctor 诊断，路径仍需手填）。
- `third_party/python-wheels/` 离线 wheel 目录的预置与文档（内网 asammdf 等 C 扩展包）。
- `scripts/build_agent_pyz.py`：把 agent + cluster 链路打成单文件，让模式 A 的 Windows 端无需 clone 完整仓。
