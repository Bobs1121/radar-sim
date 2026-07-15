# radar-sim — Selena 编译与雷达数据仿真的统一调度平台

radar-sim v5 的产品入口是 **Web** 和 **Python SDK / versioned REST API**。两者共用同一个后端调度器和同一份项目无关 `UserRunConfig 2.0` YAML；CLI 只用于安装、运维、调试和兼容。

## 文档权威顺序

1. `PRD.md`：v5 产品口径、场景边界和发布范围的唯一来源。
2. `docs/DETAILED_DESIGN.md`：v5 目标架构、现有代码迁移映射和技术边界。
3. `DEVELOPMENT_PLAN.md`：当前可执行发布计划和下一阶段编码 backlog。
4. `HANDOFF.md`：v5 唯一实时状态、WP 完成度和架构一致性检查记录。
5. `CHECKPOINT.md`、`docs/handoff.md` 以及旧 phase 文档：历史记录，仅用于追溯，不再作为当前 backlog 或产品口径。

## 当前实现状态

当前正在按 [`docs/PRODUCT_CONTRACT.md`](docs/PRODUCT_CONTRACT.md) 收敛统一 `serve-v1`、Web、SDK 和 Stage 调度。内部 Runtime Bundle、数据/配置传输、Windows full/light Agent 与 Cluster 执行器已有实现基础，但只有通过公开 `existing_path + runtime_xml` 和四种组合重新验证后才算交付。

发布边界：Linux 永不编译 Selena或执行本地仿真；light 只编译/上传后交给 Cluster；full 才能本地仿真；没有 Windows 时填写/上传已有 Selena 文件夹后由 Linux 调度 Cluster。用户不选择 Runtime Bundle；系统从目录校验 `Selena.exe + 同目录 DLL + Runtime XML` 并在内部打包。真实企业 Cluster 共享盘/manager 仍需在目标环境最终验收。

## V1 首版：一个 YAML、一个 SDK 方法

完整 PRD 不变，当前首版只交付“已有 Selena + Cluster”。用户 YAML：

```yaml
schema_version: "2.0"
selena:
  source: existing
  existing_path: "D:/path/to/Selena-folder"
  runtime_xml: "D:/path/to/Runtime.xml"
data:
  path: "D:/path/to/data"
simulation:
  target: cluster
  adapter_file: ""       # ovrs25 可空
  mat_filter: "D:/path/to/MatFilter.cfg"
```

SDK 调用：

```python
from radar_sim_sdk import RadarSimClient

with RadarSimClient("http://10.190.171.44:8878") as client:
    job = client.submit_cluster_yaml("simulation.yaml")
    print(job.id)
```

SDK/服务会根据路径在哪一侧可达，自动准备 Selena 目录、Runtime、数据和配置资产；用户不填写 project、Bundle、Cluster 参数或输出目录。详见 [`docs/V1_MVP_SCOPE.md`](docs/V1_MVP_SCOPE.md)。

## 现有兼容 CLI 快速开始

### Windows 一键部署

轻量模式只编译、上传，再由 Linux Cluster 仿真：

```powershell
.\scripts\bootstrap.ps1 -Mode light -ControlPlane linux `
  -ServerUrl http://linux-rsim:8878 -AgentId <agent-id> `
  -AgentToken <agent-token> -ApiToken <user-token> -Start
```

完整模式连接 Linux 时，同一 Web 可选本地或 Cluster：

```powershell
.\scripts\bootstrap.ps1 -Mode full -ControlPlane linux `
  -ServerUrl http://linux-rsim:8878 -AgentId <agent-id> `
  -AgentToken <agent-token> -ApiToken <user-token> -Start
```

`-Mode full -ControlPlane local` 提供离线本地 Web/编译/仿真，不启动 Cluster executor。详见 `docs/release-deployment.md`。

### Linux 统一入口

```bash
bash scripts/linux_deploy.sh --yes
bash scripts/linux_deploy.sh status
bash scripts/linux_deploy.sh test
```

Linux 发布只启动认证后的 `serve-v1`（默认 8878），同时提供 Web/API/SDK、Agent 接口和 Cluster 调度。无 Windows 用户直接使用该入口和已有 Runtime Bundle。

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

### 控制平面（legacy/历史兼容双模式）

控制平面把"调度入口"和"执行"解耦。下面的 Mode A/Mode B 仅描述当前 legacy CLI/control-plane 兼容用法，不是 v5 当前产品模式；v5 产品部署以 Windows full 和 Linux central + optional light Agent 为准。

- **legacy Mode A（Linux cluster-only）**：server 用 `--allowed-task-types cluster.run` 启动，拒绝 local task；agent 默认只认领 cluster.run。Windows 端无需繁重依赖。
- **legacy Mode B（Windows 本机仓）**：server 不带白名单（全允许），agent 显式 `--capability local.*` 启用本机编译/仿真。`rsim web` 内置 server+agent，单机一条命令即用。

```bash
# legacy Mode A：Linux 跑 server（cluster-only），Windows 跑 agent
rsim server serve --host 0.0.0.0 --port 8877 --allowed-task-types cluster.run
rsim agent --server-url http://<server>:8877   # 默认 capability: cluster.run

# legacy Mode B：单机 rsim web 内置 control server + agent，前端 build/sim/cluster 都能用
rsim --project ovrs25 web
rsim --project ovrs25 web --no-control        # 退回本地 BuildTaskRegistry
rsim --project ovrs25 web --control-port 8877 # 指定控制端口

# legacy Mode B：跨机全功能（server 不限 task_type）
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
