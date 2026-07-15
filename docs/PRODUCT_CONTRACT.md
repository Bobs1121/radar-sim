# radar-sim 产品合同（开发不可偏移基线）

> 状态：权威、强制
> 最近确认：2026-07-15
> 适用范围：Web、Python SDK、REST API、Linux 控制面、Windows full/light Agent、Cluster 调度

本文件记录产品经理（用户）最终确认的用户侧合同。若 `PRD.md`、`docs/DETAILED_DESIGN.md`、历史测试或旧实现与本文件冲突，以本文件为准；开发必须修正旧实现，不能要求用户迁就内部对象。

## 1. 产品入口和职责边界

1. 用户入口只有两种：Web 和后端 Python SDK / REST API。
2. Web 只是 SDK/API 的前台表达；两者提交完全相同的一份 YAML/JSON 配置，调用同一个调度核心。
3. Linux 服务器是唯一中央入口和控制面，当前目标服务器是 `10.190.171.44`，但部署参数必须外置，后续可迁移到其他 Linux 服务器。
4. Linux 只负责：接收配置、解析意图、编排 Stage、分配执行节点、传输/登记文件、调度 Cluster、汇总状态和结果。
5. Linux 不编译 Selena，也不执行本地仿真。Selena 编译和本地仿真只能发生在授权 Windows 电脑；Cluster 仿真由 Linux 调度 Cluster 执行面。
6. Cluster 任务在数据、Selena 目录及配置就绪后，不再依赖用户 Windows 电脑在线。

## 2. 用户唯一配置

用户只关心下面的业务信息，不关心项目、profile、recipe、输出目录、共享盘类型、Agent ID、Cluster manager、工具链或 Runtime Bundle。

```yaml
schema_version: "2.0"

selena:
  source: build                 # build | existing

  # source=build 时填写
  code_path: "C:/path/to/repo"
  branch: ""                    # 空 = 编译当前工作区（包含未提交修改）
  selena_build_script: "C:/path/to/selena_build.bat"
  package_build_script: "C:/path/to/software_package_build.bat"

  # source=existing 时填写；目录必须包含 Selena.exe 及其依赖 DLL
  # existing_path: "X:/path/to/selena_folder"

  # 两种 source 都填写，与 Selena 分支/产物强绑定
  runtime_xml: "C:/path/to/Runtime.xml"

data:
  path: "D:/path/or/shared/path/to/data"

simulation:
  target: auto                  # auto | local | cluster
  adapter_file: ""             # ovrs25 可选；其他内部 recipe 按规则校验
  mat_filter: "C:/path/to/MatFilter.cfg"
```

强制约束：

- `data` 只有一个 `path`。用户不区分本地、公盘或上传数据；系统自动识别、检索 MF4，并在目标不可访问时传输到 Cluster 可访问存储。
- `source=build` 时，系统从用户给出的 Selena 编译脚本确认真实输出位置，并在编译后验证 `Selena.exe` 与同目录 DLL；软件包编译脚本只用于内部项目识别和环境依赖发现/处理。
- `branch` 非空时使用隔离 worktree 自动切分支并编译；为空时编译用户当前工作区及其未提交修改，不能破坏用户工作区。
- `source=existing` 时，用户只填写 Selena 文件夹路径和 Runtime XML。系统必须使用该目录中的 `Selena.exe` 和所需 DLL，不能只复制一个 exe。
- Runtime Bundle、artifact id、bundle ref 等可以作为内部传输/缓存实现，但绝不出现在用户配置和 Web 表单中。
- Web 必须支持同一 YAML 的导入、修改和导出；SDK 直接使用同一 YAML/JSON。
- 路径输入在 Web 中应提供文件/文件夹选择器；选择只改善体验，不改变配置字段。

## 3. 四条必须真实跑通的业务路径

| Selena 来源 | 仿真目标 | 执行位置与系统行为 |
|---|---|---|
| 本地编译 | 本地仿真 | Windows full 编译，校验 exe + DLL + Runtime，再在同一 Windows full 执行仿真 |
| 已有 Selena 文件夹 | 本地仿真 | Windows full 校验用户目录中的 exe + DLL + Runtime，再执行本地仿真 |
| 本地编译 | Cluster 仿真 | Windows full/light 编译并上传完整 Selena 目录所需内容；Linux 接管并调度 Cluster |
| 已有 Selena 文件夹 | Cluster 仿真 | 系统从 Windows、SDK 调用机或共享路径获取并登记完整 Selena 目录；Linux 接管并调度 Cluster |

`target=auto` 不是第五种业务：调度器根据 Selena/data 路径可达性、在线能力和执行环境，在上述本地或 Cluster 路径中做选择，并把选择原因展示给用户。

## 4. 部署形态

| 部署形态 | 能力 | 明确不支持 |
|---|---|---|
| Windows full | 本地编译、已有 Selena、本地仿真、上传后 Cluster 仿真 | 不充当中央 Linux 控制面 |
| Linux + Windows light Agent | Windows 本地编译、完整产物/必要数据上传；Linux 后续调度 Cluster | light 首版不支持本地仿真，不承担 Cluster 运行期 |
| 完全不部署 / 没有 Windows | 在 Web/SDK 填写 Linux/共享存储可达的已有 Selena 文件夹，或从 Web/SDK 选择并上传；Linux 调度 Cluster | 不支持 Selena 编译，不支持本地仿真 |

Windows Agent 安装必须一键完成。可自动发现且安全的环境在第一次任务中自动配置并持久复用；环境缺失必须在任务执行前给出明确检查结果和处理动作。

## 5. 任务编排与可视化验收

调度器根据配置动态生成必要 Stage，不为不需要的工作制造步骤：

1. 配置解析与业务识别；
2. 执行节点/路径可达性解析；
3. 环境和依赖检查，必要时执行允许的自动处理；
4. 可选 Selena 编译；
5. Selena 文件夹校验与内部登记/传输；
6. 数据检索与按需传输；
7. Runtime、Adapter、MatFilter 下发和预检；
8. 本地或 Cluster 仿真提交/运行；
9. 结果收集与 Manifest。

Web 和 SDK 必须能读取同样的 Job/Stage/Event。Web 至少展示：当前 Stage、执行节点、自动路由原因、进度、日志、失败字段、修复建议、重试/取消动作和最终结果。

## 6. 发布门禁

以下证据全部存在前，不得再宣称“已经交付”：

- 同一个示例 YAML 可被 Web 导入/导出并被 SDK 提交，往返后不出现内部字段；
- 四条业务路径都有合同测试和纵向执行测试；
- “已有 Selena”测试证明 DLL 随目录被校验、打包/传输和实际使用；
- 本地路径、共享路径和上传路径对用户仍表现为唯一 `data.path` / `existing_path`；
- Linux 节点无法领取 Selena 编译或本地仿真 Stage；light Agent 无法领取本地仿真 Stage；
- 真实失败能停在正确 Stage，不能再出现由内部默认值造成的 `output_root must be narrower than workspace_root`；
- 在 `10.190.171.44` 完成 Linux 部署、Web/SDK 健康检查和至少一条目标环境 Cluster 烟测；
- `HANDOFF.md` 如实记录代码证据、测试证据、外部环境未验收项和已知限制。

## 7. 防漂移规则

每个开发任务开始前必须写明触碰本文件的哪一条；结束时在 `HANDOFF.md` 记录：用户路径是否变简单、是否新增了用户字段、四种组合受影响情况、测试证据和未验证项。任何新增用户字段或部署职责变化必须先获得用户确认。
