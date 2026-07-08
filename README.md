# radar-sim — 雷达仿真辅助与数据分析工具

代码改动后，辅助完成 **编译 → VS 仿真 → 数据分析** 的完整工作流。

## 快速开始（两种使用模式）

| 模式 | 适用场景 | 仿真执行位置 | Windows 端依赖 |
|------|----------|--------------|----------------|
| **模式 A：Linux 服务** | 多用户共享、不想装繁重依赖 | SZHRADAR 集群节点 | 仅 Python + PyYAML（无 MATLAB/Qt/Boost/VS） |
| **模式 B：Windows 本机仓** | 本地编译 + 仿真，需要完整能力 | 本机 + 集群 | Python + VS2019 + MATLAB + Qt + Boost |

### 模式 B：Windows 本机仓一键部署

clone 本仓库到 Windows 后，一条命令完成 venv + 依赖 + 配置 + 自检：

```powershell
git clone <repo> radar-sim
cd radar-sim
.\scripts\bootstrap.ps1 -Project ovrs25
```

脚本会：① 检测 Python 3.9+ → ② 创建 `.venv` → ③ `pip install -r requirements.txt` + `pip install -e .` → ④ 从模板生成 `local.yaml` → ⑤ 跑 `rsim doctor`（系统级）+ `rsim check`（配置级）。

VS2019 / MATLAB / Qt / Boost / selena_environment 这些重依赖需自行安装（license 绑定，脚本不自动装），`rsim doctor` 会告诉你缺什么。完整依赖矩阵见 `docs/environment-setup.md`。

### 模式 A：Linux 服务（仅 cluster 仿真）

Linux 上起 control server（只接受 cluster.run），Windows 端起 agent 连上即可提交集群仿真，无需 clone 完整仓的工具链：

```bash
# Linux server（纯 stdlib，零 pip install）
rsim server serve --host 0.0.0.0 --port 8877 --allowed-task-types cluster.run
```

```bat
:: Windows agent（最小依赖：Python + PyYAML + 本仓库用于 cluster 链路）
rsim agent --server-url http://<linux-server>:8877
```

详见 `docs/cluster-only-quickstart.md`（模式 A 快速开始）、`docs/linux-server-deploy.md` 和 `docs/e2e-linux-windows-runbook.md`。

### 开发安装

```bash
# 开发安装
pip install -e .

# 或直接运行
python rsim.py --help
```

### 初始化项目

```bash
# 交互式向导（推荐）
rsim init myproject

# 已有项目：编辑 config/projects/<name>/config.yaml
```

### 典型工作流

```bash
# 1. 编译 HEX（长时间，可按 Ctrl+C 中断）
rsim --project ovrs25 build hex

# 2. 编译 Selena
rsim --project ovrs25 build selena

# 3. 在 VS 中打开 Selena 工程
rsim --project ovrs25 open-vs

# 4. 在 VS 中启动仿真，输入测试数据
#    仿真完成后得到 output.mf4

# 5. 分析仿真结果
rsim --project ovrs25 analyze D:/sim/output.mf4

# 6. 查看历史分析
rsim --project ovrs25 history

# 7. 对比两次结果
rsim diff results/ovrs25/20260614_1000/ results/ovrs25/20260613_1500/

# 8. 向 AI 提问
rsim --project ovrs25 ask "FCTA 为什么没有激活？"
```

## 命令一览

| 命令 | 说明 |
|------|------|
| `rsim build [hex\|selena\|all]` | 编译 HEX（可中断）和 Selena |
| `rsim analyze <mf4>` | 分析 MF4 仿真输出 |
| `rsim ask "问题"` | 基于分析结果向 AI 提问 |
| `rsim diff <base> <current>` | 对比两次分析结果 |
| `rsim history` | 查看历史分析记录 |
| `rsim init [project]` | 交互式项目配置向导 |
| `rsim open-vs` | 打开 VS 工程 |
| `rsim check` | 检查环境配置（平台、配置、构建脚本、环境变量、构建一致性） |
| `rsim doctor` | 系统级诊断：VS/MATLAB/Qt/Boost/selena_env 实际安装、Python 包、集群 UNC 可达性 |
| `rsim tcc` | TCC 工具链：bootstrap-itc2 / install / auto-repair / status |

### 控制平面（双模式）

控制平面把"调度入口"和"执行"解耦。两种部署模式共用同一份代码，靠 server 启动参数区分：

- **模式 A（Linux cluster-only）**：server 用 `--allowed-task-types cluster.run` 启动，拒绝 local task；agent 默认只认领 cluster.run。Windows 端无需繁重依赖。
- **模式 B（Windows 本机仓）**：server 不带白名单（全允许），agent 显式 `--capability local.*` 启用本机编译/仿真。`rsim web` 内置 server+agent，单机一条命令即用。

```bash
# 模式 A：Linux 跑 server（cluster-only），Windows 跑 agent
rsim server serve --host 0.0.0.0 --port 8877 --allowed-task-types cluster.run
rsim agent --server-url http://<server>:8877   # 默认 capability: cluster.run

# 模式 B：单机 rsim web 内置 control server + agent，前端 build/sim/cluster 都能用
rsim --project ovrs25 web
rsim --project ovrs25 web --no-control        # 退回本地 BuildTaskRegistry
rsim --project ovrs25 web --control-port 8877 # 指定控制端口

# 模式 B：跨机全功能（server 不限 task_type）
rsim server serve --host 0.0.0.0 --port 8877
rsim server create-job local.build_selena --project ovrs25 --mode RelWithDebInfo
rsim server create-job cluster.run      --project ovrs25 --dataset smoke --max-minutes 5
rsim agent --server-url http://<server>:8877 --capability local.check --capability local.build_selena --capability local.run_sim --capability cluster.run

# TCC 工具链（agent 可调度的子命令）
rsim tcc bootstrap-itc2                 # 装 itc2.exe（从 ITO 镜像）
rsim tcc install IF:BTC-7.0.0           # 装工具集
rsim tcc auto-repair                    # 一键：itc2 + 推导 + 安装
rsim tcc status                         # 只读检测
```

详见 `SIMULATION_WORKFLOW.md` §10。


## 项目结构

```
config/
  default.yaml                    # 全局默认配置
  projects/
    ovrs25/                       # 每个项目一个目录
      config.yaml                 # 项目配置
      signals.yaml                # 信号配置
      rules.yaml                  # 规则配置
      assets/                     # 仿真资产（runtime XML 等）
core/
  config.py                       # 多项目配置加载
  models.py                       # 数据模型
  platform.py                     # 平台抽象接口
  analysis_runner.py              # 分析插件运行器
  tui.py                          # 终端 UI
cli/
  build.py / analyze.py / diff.py / ...
plugins/analysis/
  signal_summary.py               # 信号统计插件
  rule_check.py                   # 规则检查插件
  default_report.py               # HTML 报告插件
  ai_qa.py                        # AI 问答插件
platforms/
  gen5_selena/                    # Gen5 雷达平台支持
```

## 插件开发

```python
from core.analysis_runner import AnalysisPlugin, AnalysisContext
from core.models import PluginResult

class MyPlugin(AnalysisPlugin):
    @property
    def name(self):
        return "my_plugin"

    def analyze(self, signals, context):
        # signals: dict[str, SignalData]
        # 分析逻辑...
        return PluginResult(
            plugin_name=self.name,
            success=True,
            data={"key": "value"},
            summary="简要说明",
        )
```

将插件放在 `plugins/analysis/` 目录下，自动发现加载。

## 用户配置指南（多用户/多分支/多数据）

不同用户的不同需求（Selena 分支、代码仓、数据、profile）通过 **`local.yaml`** 覆盖，不改动共享的 `config.yaml`。`local.yaml` 在 `.gitignore` 中，每个用户的本机私有配置。

### 配置分层

```
default.yaml → platforms/gen5_selena.yaml → recipes/<recipe>.yaml → projects/<name>/config.yaml → projects/<name>/local.yaml
                                                                                                   └─ 你在这里覆盖
```

### 三步上手

```bash
# 1. 从模板生成本机 local.yaml
rsim --project ovrs25 config init

# 2. 编辑 local.yaml，覆盖你需要的部分（分支/数据/profile）
#    编辑后用 show 确认生效
rsim --project ovrs25 config show
rsim --project ovrs25 config diff   # 显示 local.yaml 覆盖了哪些键

# 3. 跑前先校验环境
rsim --project ovrs25 check --backend local --profile local-build
```

### 常见场景（local.yaml 片段）

**用 feature 分支 + 本地数据**：
```yaml
repos:
  inner_repo_root: "C:/BYD_OVS_CB/apl/byd"
build:
  selena_branch: "feature/my-change"   # rsim build 会切到此分支编译
simulation:
  datasets:
    - name: "CBNA_local"
      input_dir: "D:/data/byd/.../CBNA_23-4-26"
profiles:
  - name: "local-build"
    selena:
      selena_branch: "feature/my-change"  # check 会校验 exe 与分支匹配
```

**云端仿真（打包本地 Selena + 公盘数据）**：已在 `config.yaml` 的 `profiles` 里配好 `cloud-build`，直接 `rsim cluster run --profile cloud-build <MF4> --execute`。本机路径差异在 `local.yaml` 覆盖 `repos`/`build`/`environment` 即可。

### 环境校验

多分支/多仓/多数据场景，一条命令校验全部（repo 分支/submodule、Selena exe 与分支匹配、runtime/adapter、数据可达、worker 依赖）：

```bash
rsim --project ovrs25 check --backend local --profile <name>     # 本地
rsim --project ovrs25 check --backend cluster --profile <name>  # 云端
```

输出按 severity 分级：`OK`（info 通过）、`W`（warning，建议修但不阻塞）、`!!`（error，必须修）。

## 配置示例

`config/projects/ovrs25/config.yaml`:
```yaml
project:
  name: "BYD_OVS_CB"
  platform: "gen5_selena"

paths:
  project_root: "C:/BYD_OVS_CB"
  binding: "ovrs25"

analysis:
  default_signals:
    - name: "FCTA_State"
      group: "fcta"
```

## 依赖

- Python 3.9+
- asammdf（MF4 解析）
- PyYAML（配置管理）
- openai（AI 问答，可选）

## License

Internal use only.
