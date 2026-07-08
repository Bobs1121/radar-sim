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
