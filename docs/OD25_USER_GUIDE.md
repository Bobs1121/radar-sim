# OD25 Cluster 仿真使用指南

> 适用版本：radar-sim V1，Linux 统一入口 + OD25 + Cluster
>
> 当前测试入口：`http://10.190.171.44:8877`
>
> 用户入口：Web 或 Python SDK；两者使用同一份 YAML

## 1. 用户最终需要准备什么

OD25 用户不需要选择 project、recipe、profile、输出目录、Runtime Bundle、Cluster manager 或共享盘类型。只准备以下业务文件或路径：

| 内容 | 编译后仿真 | 已有 Selena 仿真 |
|---|---:|---:|
| Windows 代码仓路径 | 必需 | 不需要 |
| Selena 编译脚本 | 必需 | 不需要 |
| 软件包编译脚本 | 必需 | 不需要 |
| 已有 Selena 文件夹 | 不需要 | 必需 |
| 与 Selena 强绑定的 Runtime XML | 必需 | 必需 |
| 数据路径 | 必需 | 必需 |
| OD25 Adapter | 必需 | 必需 |
| MatFilter | 必需 | 必需 |

系统只接收一个 `data.path`。它会递归查找 MF4；本地数据在需要时上传，共享数据由 Linux 部署映射到 Cluster 可访问位置。

## 2. OD25 如何被系统识别

用户不填写“OD25”项目名。系统使用用户给出的脚本相对位置和脚本内容识别；即使代码仓没有预先登记，只要脚本能唯一推导工作区和构建输出也可以继续：

- Selena 入口：`apl/byd/selena/jenkins_selena_build.bat`
- 软件包入口：`apl/byd/tools/builder/cmake_build.bat`
- Selena 脚本中的配置：`full_DSP`
- 内部适配结果：OD25 的 `g3n_fvg3_od25` 配方

代码仓可以位于任意盘符或任意父目录，只要两个脚本都在所填 `code_path` 内。系统从 Selena 脚本的 `-B` 和 `full_DSP.config` 推导真实产物目录为 `build/full_dsp`，再定位其中的 `Selena.exe` 和依赖 DLL。

软件包脚本不会作为本任务的交互式整包构建执行。Agent 静态读取它及其本仓库调用链，用于识别构建变体、发现/安装 TCC 依赖和处理明确支持的生成文件。Visual Studio 仍由用户自行安装。

## 3. 两份可直接修改的 YAML

### 3.1 Windows 编译 Selena，再到 Cluster 仿真

复制并修改 [`config/od25-build-cluster.example.yaml`](../config/od25-build-cluster.example.yaml)：

```yaml
schema_version: "2.0"
selena:
  source: build
  code_path: "D:/bydod25fr/byd"
  branch: ""
  selena_build_script: "D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat"
  package_build_script: "D:/bydod25fr/byd/apl/byd/tools/builder/cmake_build.bat"
  runtime_xml: "D:/path/to/OD25_Runtime.xml"
data:
  path: "D:/path/to/OD25_measurements"
simulation:
  target: cluster
  adapter_file: "D:/path/to/OD25_adapter.txt"
  mat_filter: "D:/path/to/OD25_mat.filter"
```

`branch` 可留空。系统始终编译用户当前工作区，包括未提交修改；不会自动切分支、清仓、reset 或 stash。填写了 `branch` 但与实际分支不一致时，任务会显示警告并继续。

### 3.2 使用已有 Selena，再到 Cluster 仿真

复制并修改 [`config/od25-existing-cluster.example.yaml`](../config/od25-existing-cluster.example.yaml)：

```yaml
schema_version: "2.0"
selena:
  source: existing
  existing_path: "D:/path/to/OD25_Selena/RelWithDebInfo"
  runtime_xml: "D:/path/to/OD25_Runtime.xml"
data:
  path: "//server/share/path/to/OD25_measurements"
simulation:
  target: cluster
  adapter_file: "D:/path/to/OD25_adapter.txt"
  mat_filter: "D:/path/to/OD25_mat.filter"
```

`existing_path` 应指向能唯一找到 `Selena.exe` 的产物文件夹。系统会把 `Selena.exe`、同目录全部 DLL 和 Runtime XML 作为一个内部整体校验和传输，但 YAML 中不会出现 Bundle 概念。

## 4. Windows 用户的一键连接

### 4.1 哪些用户需要安装

- `source: build`：任务需要连接代码仓所在的 Windows 电脑；编译和上传完成前保持电脑在线。
- `source: existing` 且 Selena、Runtime、数据或配置文件只在 Windows 本地：任务需要连接保存这些文件的 Windows 电脑，或直接在该电脑运行 SDK。
- 所有输入都已在 Linux/Cluster 可访问共享存储：不需要连接 Windows 电脑。

OD25 只做 Cluster 仿真时使用 `light` 即可。`light` 负责本地编译、依赖检查、产物/本地数据上传，不支持本地仿真。

### 4.2 前置条件

- Windows 10/11；
- git；
- 用户自行安装带 C++ 编译器的 Visual Studio。当前脚本兼容性检查会识别 VS2015/v140 以及受支持的新版本；
- 能访问 `10.190.171.44:8877`、代码仓和所填本地路径。

### 4.3 当前测试服务器的一键连接

1. 打开 `http://10.190.171.44:8877` 并提交或查看任务。
2. 当页面显示“任务正在等待连接本机”时，点击“一键连接本机”。
3. 双击下载的 `RadarSim-连接本机.cmd`。
4. 看到“本机已经连接”后返回网页；原任务自动继续，无需重新提交。

系统会自动绑定当前 Linux 服务、下载匹配版本、创建隔离 Python 环境、检查 VS、注册登录自启并在断线后重连。缺少 Python 时会先尝试当前用户静默安装；Visual Studio 仍由用户自行安装。当前可信内网测试服务关闭认证，因此不要求令牌，也不会把任何连接参数写入业务 YAML。

## 5. Web 使用方式

1. 打开 `http://10.190.171.44:8877`，进入“新建任务”。
2. 点击“导入 YAML”，选择上述任一 YAML；也可以直接在页面修改后“导出 YAML”复用。
3. 执行目标选择“Cluster”。
4. 本机 Adapter 或 MatFilter 请使用对应的“选择并上传”按钮；页面会把本地文件换成可复用引用。数据可直接填路径，也可以使用数据文件夹选择器上传。
5. 点击“检查配置”，确认执行预览包含识别、环境检查、准备 Selena/数据、Cluster 仿真和结果归档。
6. 点击“提交任务”，在“任务中心”查看阶段、进度、日志和失败建议。刷新网页后任务仍由 Linux 数据库保存，可继续查看。
7. 成功后点击“下载结果 ZIP”。

如果 YAML 中是 Windows 本地路径，必须让同一用户的 Windows Agent 在线；Linux 不会尝试直接读取另一台电脑的 `C:/` 或 `D:/`。编译、Runtime Bundle 和数据上传完成后，Cluster 阶段不再依赖该 Windows 电脑在线。

## 6. Linux 后端 SDK 使用方式

### 6.1 安装

在集成产品的 Python 环境中，从 Git 仓库安装 SDK：

```bash
git clone <radar-sim-git-url>
cd radar-sim
python -m pip install ".[sdk]"
```

在仓库检出目录开发时可用：

```bash
python -m pip install -e ".[sdk]"
```

### 6.2 一个方法提交、等待并下载结果

```python
from pathlib import Path
from radar_sim_sdk import RadarSimClient

with RadarSimClient(
    "http://10.190.171.44:8877",
    user="alice",
) as client:
    job = client.submit_yaml(
        "od25.simulation.yaml",
        idempotency_key="od25-issue-123-run-1",
    )
    print("job:", job.id)

    terminal = client.wait(job.id, timeout=4 * 60 * 60)
    if terminal.status != "succeeded":
        raise RuntimeError(f"simulation failed: {terminal.id}")

    manifest = client.manifest(job.id).manifest
    archive = client.download_result(manifest["result_ref"], Path("downloads"))
    print("result:", archive)
```

`submit_yaml()` 与 Web 调用同一个 `/api/v1/run-jobs` 调度核心。`idempotency_key` 用于避免集成产品因网络重试重复创建任务；同一次业务请求保持不变，新一次运行换一个值。

### 6.3 SDK 进程所在位置决定谁读取本地文件

- SDK 在 Windows 电脑运行：它能读取的已有 Selena、Runtime、数据、Adapter、MatFilter 会自动校验和上传。
- SDK 在 Linux 后端运行：共享路径或 Linux 本机可读路径可直接处理；YAML 中的 Windows 代码仓/已有 Selena/Runtime/数据由在线 Windows Agent处理。
- Linux 后端不能直接读取 Windows 本地 Adapter/MatFilter。此时先通过 Web 文件按钮生成可复用 `config-asset://` 引用，或把文件放到 Linux 可访问共享路径，再把引用/路径写入同一 YAML。

当前测试服务关闭认证，`token` 可省略；正式服务开启认证后使用 `RadarSimClient(..., token="...")`，令牌仍不写入 YAML。

## 7. Linux 服务的启动与迁移

当前 `10.190.171.44:8877` 使用用户级 systemd 服务：

```bash
systemctl --user start radar-sim-v1.service
systemctl --user status radar-sim-v1.service
systemctl --user restart radar-sim-v1.service
curl http://127.0.0.1:8877/api/v1/health
```

部署到其他 Linux 服务器：

```bash
bash scripts/linux_deploy.sh --yes
bash scripts/linux_deploy.sh status
bash scripts/linux_deploy.sh test
```

Linux 只负责接收 YAML、调度 Stage、转换共享路径、上传/登记资产、调用 Cluster、保存状态和归档结果；不会编译 Selena，也不会执行本地仿真。新的共享盘根目录需要管理员在部署级 `deployment.yaml` 中配置一次挂载映射，用户 YAML 不增加字段。

## 8. 成功标准与常见失败

成功必须同时满足：所有 Cluster 子任务完成、至少产生一个非空输出 MF4、结果 Manifest 为 `succeeded`，并且结果 ZIP 可下载。仅看到 Cluster 提交成功或 `result.ini` 不代表仿真成功。

| 现象 | 用户先检查什么 |
|---|---|
| 显示“等待连接本机” | 点击“一键连接本机”并双击下载程序；连接成功后原任务自动继续 |
| 识别配置失败 | `code_path` 是否包含两个脚本；脚本是否能推导唯一构建输出；Runtime 是否在所填电脑可访问 |
| VS 检查失败 | 是否安装了 C++ workload；VS 由用户安装，Agent只检测和适配脚本参数 |
| 环境依赖失败 | 查看环境检查给出的 TCC/生成文件动作；修复后只重试失败 Stage |
| 数据准备失败 | 共享路径是否已在 Linux 挂载映射内；本地数据时 Agent/SDK 调用机是否能读取 |
| OD25 preflight 提示 Adapter 缺失 | OD25 Adapter 是必需输入，请填写或上传正确文件 |
| 仿真结束但任务失败 | 查看首条真实 Cluster 错误、各子任务状态和非空 MF4 数量，不以耗时长短判断 |

## 9. 当前验收边界

- OD25 已有 Selena + Cluster 已完成真实 12 文件任务：12/12 成功、0 失败，并完成结果归档和下载验证。
- OD25 代码仓的双脚本识别、任意盘符重定位、`build/full_dsp` 产物推导和本机现有 `Selena.exe` 定位已完成自动测试与实物检查。
- Windows `source: build -> Cluster` 的编译、上传和 Cluster 组件都已有实现；但仍建议发布给首位新用户时保留一次陪跑验收，重点确认该用户的 VS/TCC 环境和 Runtime/Adapter 版本匹配。

不要把上述最后一项理解为“任意 OD25 分支和任意 Runtime 都天然兼容”：Runtime、Adapter 和 Selena 产物本身仍必须属于同一业务版本。
