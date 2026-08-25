# Radar-sim 配置引导规则

本文件只处理仿真语义、重复运行意图和当前代码环境发现。Connector、MCP/SDK 版本、能力探测、任务调度和传输由 Skill 按主流程自动处理，不要求用户理解或填写。首次确认后的重复运行不得再次询问同一配置。

## 分发与用户环境隔离

Skill 是面向不同用户和代码仓分发的公共接口，禁止在 Skill、references、脚本或示例中写死用户名、机器名、Agent ID、Token、服务器 IP/URL、UNC 路径、绝对工作区路径、结果路径或安装版本目录。

- 工作区、代码、数据和结果路径从当前 Agent 工作区、用户显式输入和本机标准目录发现；不能复用发布者的路径。
- 服务地址按以下顺序解析：安装器/本地 MCP 配置或环境变量 `RADAR_SIM_SERVICE_URL`、`RADAR_SIM_BASE_URL`，再到服务提供方生成的本机 profile。公开分发的 `service-profile.json` 必须为空地址，由安装过程在本机生成配置。
- 用户路由标签使用 `RADAR_SIM_USER`，未设置时才使用当前操作系统登录名；这不是身份认证，也不能在 Skill 中写入固定用户。
- `mcp-config.json`、credentials、Agent ID、服务 profile 和结果 catalog 都是本机安装状态，不能打包进公共 Skill，也不能作为其他用户的默认值。

如果用户已经给出完整 `UserRunConfig 2.0` YAML，或当前会话已有一份已确认 YAML，直接进入规范化/校验；不要为了重复确认而重新扫描整个代码仓。

如果用户只说“再仿一次”“用刚才配置重跑”或“我改了这里，帮我重新仿一下”，先查当前会话的最后一份已确认配置；如果会话没有该配置，再只读查找用户明确允许的保存配置（优先 `.radar-sim/simulation.yaml`）。找不到保存配置时才进入环境发现。不能把“当前目录存在 Selena.exe”当成保存配置，也不能把旧 Job 的内部 resolved spec 直接当成新的用户 YAML。

### 重复运行和编译意图

Skill 先判断用户意图，再填写 `selena.source`；它不在 YAML 中伪造 `incremental`、`full` 或 `clean` 字段。

| 证据 | 选择 | 说明 |
|---|---|---|
| 明确说改了代码、修复后验证、修改后重新仿真 | `build` | 保留之前的 data、Runtime、target 等语义字段；若旧配置是 `existing`，提示现有二进制不会包含修改，建议切换到 `build` |
| 只说重复运行且之前是 `build` | `build` | 后端根据分支、提交、工作区修改和 Selena 产物证据决定跳过/增量/全量 |
| 只说重复运行且之前是 `existing` | `existing` | 不编译，继续使用已确认的 Selena 目录 |
| 明确说不要编译、用现有产物 | `existing` | 优先级高于目录中是否发现代码仓 |
| 明确说编译当前代码 | `build` | 编译模式仍由后端证据策略决定 |

如果代码发生变化但无法检测 Git 变更，保持 `build`，让后端执行增量安全回退；不要因为检测失败而选择 `existing` 或清理产物。分支期望与产物 provenance 正向不一致时，后端才允许全量编译。Runtime XML 是仿真运行资产，不作为 Skill 单独推断全量编译的依据；除非服务端合同明确要求，否则不因 Runtime 路径变化自行改变编译意图。

## 配置确认后的自动运行

第一次配置时可以把多个业务歧义合并为一次确认。确认后，Skill 应把规范化 YAML 和用户选择记录为当前 Agent/task 的 active profile；再次收到“再仿一次”“我改了这里，重新验证”“继续跑”时直接复用，不重新询问 Selena 来源、Runtime、target、Radar source 或结果目录。

以下动作属于内部自动处理，不发送确认问题：MCP/SDK/Skill 首启或兼容更新、Connector 检查/启动/更新、能力和 readiness 检查、YAML 导入导出、幂等重试、等待超时后的继续轮询、可重试传输/Stage 恢复，以及根据分支和产物证据选择跳过/增量/全量。只有没有可用数据候选、显式业务字段冲突、服务端要求用户作业务选择，或宿主安全策略拒绝必要机器变更时，才允许中断并给出一个抽象的可行动提示。

### Copilot 无感运行规则

输入确认完成后，仿真流程必须作为一个连续的 hands-off 操作执行：

- 用户说“开始仿真/再仿一次/修改后重新验证”，即视为同意本 Skill 范围内的常规准备、启动、更新、提交、轮询、可重试恢复和结果下载；不得逐步询问“是否允许”“是否继续”“是否更新”“是否下载”。
- 不把 MCP、Connector、SDK、Stage、Transfer、readiness、自检或重试状态转成用户问题，也不要要求用户点击 `Allow`、`Confirm`、`Continue`、`Run`、`Trust` 或 `Approve`。
- 优先调用 MCP 的 readiness/bootstrap 能力；必须使用本地进程时，只启动一次稳定的非交互入口，隐藏运行并输出可读进度，禁止 REPL、`python -`、内联客户端和等待键盘输入的命令。
- 任何缺失的业务输入必须在开始 Connector 更新、数据传输、编译或 Job 提交前一次性收齐。闭环之后不得因为发现了内部技术字段而二次打断用户。
- 宿主强制的安全审批属于 Agent 平台边界，Skill 不得伪造点击或绕过安全策略；只有宿主确实拒绝必要操作时，才返回一次简短阻塞原因和所需的宿主级动作，不能重复触发同一请求。
- 该规则不授权修改用户代码仓、任意下载、清理文件或其他无关外部操作；自动变更只能发生在 radar-sim 官方安装、运行时和结果目录内。

正常完成时只输出仿真结果地址；中间状态保留在隐藏终端/日志中，除非用户明确要求实时进度或诊断。

### 宿主审批的一次性准备

如果当前 Agent 运行在 VS Code/GitHub Copilot 中，Skill 应在第一次副作用操作前检查当前会话是否仍为 `Default Approvals`。如果会导致 MCP、Connector 或终端逐步弹窗，先一次性指导用户把当前工作区切换到 `Bypass Approvals`，或按 [copilot-approvals.md](copilot-approvals.md) 配置工作区设置；不要已经运行若干阶段后才提示。用户完成一次工作区/会话级配置后，不再重复指导。

默认推荐 `Bypass Approvals`，因为它只解决工具执行确认，不会替用户回答仿真语义问题；不把 `Autopilot` 作为仿真默认值，因为它可能自动回答 Agent 的澄清问题。不得由 Skill 自动修改用户的审批设置，也不得默认开启全局 `chat.tools.global.autoApprove`。若企业管理策略隐藏或禁止 Bypass/Autopilot，Skill 不能绕过，只能在开始前返回一次宿主策略阻塞提示。

### 输入闭环闸门

Skill 必须先完成一次只读输入预检，再开始任何 Connector 更新、数据传输、
Selena 编译或 Job 提交。输入预检必须同时覆盖：

1. active profile/上一 Job 配置恢复；
2. 代码仓、Selena 子仓、编译脚本、Runtime XML、已有产物和数据候选发现；
3. `import_simulation_yaml` 返回的全部 `missing_fields`、冲突和候选歧义。

存在任意未决字段时，只发送一次合并问题；不能先运行若干 Stage、再单独
询问 Runtime XML 或 MatFilter。用户回答后合并配置并重新 import/validate，
只有完整且语义确认的 YAML 才能进入能力准备和提交。

## 最小决策顺序

先形成候选，再确认四件事：

1. Selena 来源：当前代码编译 `build`，还是已有产物 `existing`；
2. 数据：MF4 文件/目录或用户确认的共享/逻辑引用；
3. Runtime XML：与本次 Selena 来源匹配的 Runtime；
4. 执行目标：`auto`、`local` 或 `cluster`。

如果用户已经提供完整 YAML，不要重复询问已明确的字段；先规范化并校验，只有服务端返回缺失/冲突时才追问。

## 当前代码环境发现

所有必要发现都是只读操作，并且结果必须带证据：

| 信息 | 建议发现方式 | 可否直接写入 YAML |
|---|---|---|
| `code_path` | 当前工作目录、Git 根目录、用户显式路径 | 只有用户选择 `build` 后才能写入 |
| 当前分支 | `git branch --show-current` | 作为建议值；用户未要求分支约束时可留空 |
| `selena_build_script` | 在代码根下查找 `jenkins_selena_build.bat`、`build_selena.bat` 等 | 只有唯一候选或用户确认后写入 |
| `runtime_xml` | 代码根、脚本目录、已有 Selena 输出附近的 `*.xml` | 多个候选必须确认 |
| `existing_path` | 有界搜索包含唯一 `Selena.exe` 和 colocated DLL 的目录 | 多个候选必须确认 |
| `data.path` | 用户显式路径优先；否则只列出源 MF4 文件/目录候选，排除 `job_*`、`outputs`、`results` 和日志目录 | 多个候选必须确认 |
| `simulation.mat_filter` | 显式值优先；否则留空交给 SDK/Connector 的受控推导 | 不要根据文件名猜测 |
| `simulation.adapter_file` | 仅当用户明确需要或服务端要求 | 不要使用项目默认值 |

代码仓存在不等于用户选择编译；找到 Runtime XML 不等于它与产物匹配；找到一个 MF4 不等于可以忽略用户意图。候选数量、路径和必要的文件元数据可以展示，文件正文不得读入对话。

## Web 字段到 YAML

| Web/用户语义 | YAML 字段 | 规则 |
|---|---|---|
| Selena 使用方式 | `selena.source` | `build` 或 `existing`，不明确时必须询问 |
| 代码仓 | `selena.code_path` | `build` 必填；`existing` 只有交叉验证时才需要 |
| 期望分支 | `selena.branch` | 可选；不切换分支、不 reset，不把当前分支强制变成用户意图 |
| 编译脚本 | `selena.selena_build_script` | `build` 必填；候选不唯一时询问 |
| 软件包脚本 | `selena.package_build_script` | 可选，仅用于依赖诊断 |
| 已有 Selena | `selena.existing_path` | `existing` 必填；目录内应有 Selena.exe 与 DLL |
| Runtime | `selena.runtime_xml` | 两种来源都必填 |
| 数据 | `data.path` | 文件、目录、UNC/DFS、共享或逻辑引用 |
| 目标 | `simulation.target` | 未说明时建议 `auto`；不要求用户配置能力细节 |
| Radar source | `simulation.source` | 显式值优先；未知时留空自动推导 |
| Adapter | `simulation.adapter_file` | 只有本次仿真明确需要才填写 |
| MatFilter | `simulation.mat_filter` | 显式值优先；未知时留空自动推导 |
| 结果目录 | `result.path` | 可选；留空使用接收端默认目录 |

禁止添加 `project`、`profile`、`recipe`、Agent ID、Token、Cluster 内部路径、Runtime Bundle ID 或自定义运行档位。

## 推荐 YAML 草稿

```yaml
schema_version: "2.0"
selena:
  source: build
  code_path: "D:/workspace/current-repo"
  branch: ""
  selena_build_script: "D:/workspace/current-repo/path/to/build_selena.bat"
  package_build_script: ""
  existing_path: ""
  runtime_xml: "D:/workspace/current-repo/path/to/Runtime.xml"
data:
  path: "D:/measurements"
simulation:
  target: auto
  source: ""
  adapter_file: ""
  mat_filter: ""
result:
  path: ""
```

`existing` 草稿删除或置空 `code_path`、编译脚本和 `branch`，填写 `existing_path` 与匹配的 `runtime_xml`。不要为了让 YAML 看起来完整而填入猜测值。

## 必须确认的情况

用一次合并确认覆盖所有未决项，不要连续询问内部技术字段：

- 用户没有说明 `build`/`existing`；
- 用户没有说明数据路径，或存在多个 MF4/目录候选；
- 存在多个编译脚本、Runtime XML 或已有 Selena 目录；
- 用户要求的分支与当前工作区分支不同；
- 显式 Radar source 与 MF4 元数据冲突；
- 发现多个 Adapter 候选，且用户没有说明是否需要 Adapter；
- 用户要求保存 YAML 但没有给出保存路径。

确认语句应包含候选的完整路径、选择理由和将写入的字段。例如：

```text
我发现两个 Selena 编译脚本和一个 Runtime XML。建议使用：
- 编译脚本：D:/repo/apl/.../jenkins_selena_build.bat（位于当前 Selena 子仓）
- Runtime：D:/repo/runtime/Runtime.xml（与脚本同一代码环境）
- 数据：D:/data/one.MF4

是否按这个配置执行？执行目标默认使用 auto。
```

用户确认后再调用规范化/校验/提交工具。若用户只要求“生成配置”，到导出 YAML 为止，不自动创建 Job。
