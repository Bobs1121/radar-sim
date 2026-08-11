# radar-sim 产品合同（开发不可偏移基线）

> 状态：权威、强制
> 最近确认：2026-08-05
> 适用范围：Web、Python SDK、REST API、Linux 控制面、统一 Windows Connector、Cluster 调度
>
> 当前 P0 实施边界（2026-08-11）：用户只安装一个统一 Connector；`full/light` 仅为历史内部能力标签，不是用户选择。Cluster 方向的 `shared_copy` 已实现。远端资源无法被本地仿真 Windows 原地读取时，真正的 `source_to_local` 仍缺少目标 Windows 受控缓存与目标 Agent 授权，因此当前必须返回稳定 `source_to_local_unavailable`，不得把 Cluster staging 冒充本地缓存。该限制不影响资源均可由同一 Windows 读取的本地仿真。

本文件记录产品经理（用户）最终确认的用户侧合同。若 `PRD.md`、`docs/DETAILED_DESIGN.md`、历史测试或旧实现与本文件冲突，以本文件为准；开发必须修正旧实现，不能要求用户迁就内部对象。

## 1. 产品入口和职责边界

1. 用户入口只有两种：Web 和后端 Python SDK / REST API。
2. Web 只是 SDK/API 的前台表达；两者提交完全相同的一份 YAML/JSON 配置，调用同一个调度核心。
3. Linux 服务器是唯一中央入口和控制面，当前目标服务器是 `10.190.171.44`，但部署参数必须外置，后续可迁移到其他 Linux 服务器。
4. Linux 只负责控制面：接收配置、解析意图、编排 Stage、分配执行节点、签发传输目标、登记逻辑引用、调度 Cluster、汇总状态和结果。Linux API 进程和 Linux 本地磁盘不得成为用户大文件的上传中转站。
5. Linux 不编译 Selena，也不执行本地仿真。Selena 编译和本地仿真只能发生在授权 Windows 电脑；Cluster 仿真由 Linux 调度 Cluster 执行面。
6. Cluster 任务在数据、Selena 目录及配置就绪后，不再依赖用户 Windows 电脑在线。
7. 数据面与控制面强制分离：用户本地 MF4、Selena.exe/DLL、Runtime、MatFilter、Adapter 等任务文件必须由文件所在电脑直接写入 Cluster 可访问存储，或由 Cluster 直接原地读取；传输字节不得经过 Linux Web/API 端口。Linux 只下发不含文件正文的传输计划并接收进度、校验摘要和逻辑引用。
8. `target=local` 时，本机已有或本机可直接读取的输入禁止无意义搬运；编译与仿真发生在同一 Windows full 环境。若数据或已有 Selena 只存在于远端且本机不能原地读取，则由远端数据面直接传到该 Windows 的受控缓存，仍不得经过 Linux。Linux 只收发配置、控制命令、状态、日志和结果摘要。

## 2. 用户唯一配置

用户只关心下面的业务信息，不关心项目、profile、recipe、内部产物目录、共享盘类型、Agent ID、Cluster manager、工具链或 Runtime Bundle。`result.path` 只表示接收端希望直接消费的解压结果位置，不是服务端工作区。

```yaml
schema_version: "2.0"

selena:
  source: build                 # build | existing

  # source=build 时填写
  code_path: "C:/path/to/repo"
  branch: ""                    # 可选期望分支；始终编译当前工作区
  selena_build_script: "C:/path/to/selena_build.bat"
  package_build_script: "C:/path/to/software_package_build.bat"  # 可选依赖线索

  # source=existing 时填写；目录必须包含 Selena.exe 及其依赖 DLL
  # existing_path: "X:/path/to/selena_folder"

  # 两种 source 都填写，与 Selena 分支/产物强绑定
  runtime_xml: "C:/path/to/Runtime.xml"

data:
  path: "D:/path/or/shared/path/to/data"

simulation:
  target: auto                  # auto | local | cluster
  source: ""                    # 可选：RadarFC | RadarFL | RadarFR | RadarRL | RadarRR
  adapter_file: ""             # 可选；仅在当前 Selena/数据链确实需要时填写
  # 可选；显式填写时严格使用用户值。留空时由 SDK/Connector 从代码仓
  # 推导唯一高置信候选，无法唯一确定时再提示用户选择。
  mat_filter: ""

result:
  # 接收端结果保存根目录；每个 Job 落在 <path>/<job_id>；留空表示 auto
  path: ""
```

强制约束：

- `data` 只有一个 `path`。用户不区分本地、公盘或上传数据；系统自动识别、检索 MF4。Cluster 目标不可直接访问时，由数据所在电脑直传 Cluster 可访问存储；本地仿真时不传输。
- `simulation.source` 是可选的用户意图：显式填写 `RadarFC/RadarFL/RadarFR/RadarRL/RadarRR` 时必须严格采用，并跳过自动选择；`RadarFC` 的稳定 mounting 为 `front`。留空时从本次 MF4 的 acquisition source 推导。单一源直接采用；多源按 MF4 acquisition group 的稳定顺序选择第一个，不阻断任务，同时记录候选、选择结果和依据。项目名、项目 adapter 或历史 profile 不得覆盖用户选择或本次数据推导。
- `source=build` 时，系统从用户给出的 Selena 编译脚本确认真实输出位置，并在编译后验证 `Selena.exe` 与同目录 DLL；软件包编译脚本为可选项，只用于补充内部项目识别、环境依赖发现/处理及其明确声明的代码生成步骤。未提供软件包脚本不得单独阻止编译。
- `source=build` 始终编译用户当前工作区及其未提交修改，默认认为用户已自行切好分支。系统不得自动执行 checkout、reset、clean 或 stash。`branch` 仅是可选的期望分支；与实际分支不一致时，Web、SDK Job 结果和任务日志必须明确警告，但允许用户继续执行。
- 清仓只属于用户明确选择并二次确认的可选动作，默认流程不执行。`git clean -xfd`、`git reset --hard`、递归 submodule reset/clean 等破坏性命令绝不能静默作用于用户工作区。
- `source=existing` 时，用户只填写 Selena 文件夹路径和 Runtime XML。系统必须使用该目录中的 `Selena.exe` 和所需 DLL，不能只复制一个 exe。
- Runtime Bundle、artifact id、bundle ref 等可以作为内部传输/缓存实现，但绝不出现在用户配置和 Web 表单中。
- 仿真执行必须项目无关：Runtime、Adapter、MatFilter 只取本次任务输入，显式空值不得回退到项目资产；Cluster 使用框架统一 ParamConfig，不套用项目模板。项目识别只可用于本地编译脚本、工具链依赖和产物路径推导。
- “已有 Selena”以及编译完成后的本地仿真不得要求项目预登记。已知内部适配器可提供环境提示；未知项目必须使用通用 Gen5 Selena 参数适配器，内部匿名身份只用于授权、缓存和追踪，不能选择业务默认值或阻断执行。
- `result.path` 是可选的接收端结果保存根目录。显式填写时，执行端/连接端在完成 Manifest 后把可直接消费的结果文件和 Manifest 写入 `<result.path>/<job_id>`；留空保持 `auto` 语义，由接收端选择默认根目录，不得被 Linux 服务解析成用户可见的绝对路径。ZIP 归档作为并行保留能力继续通过逻辑 `result_ref` 提供；公共 Job/Manifest 不回写物理保存路径。
- Web 必须支持同一 YAML 的导入、修改和导出；SDK 直接使用同一 YAML/JSON。
- 路径输入在 Web 中应提供文件/文件夹选择器；选择只改善体验，不改变配置字段。

## 3. 四条必须真实跑通的业务路径

| Selena 来源 | 仿真目标 | 执行位置与系统行为 |
|---|---|---|
| 本地编译 | 本地仿真 | Windows full 编译，校验 exe + DLL + Runtime，并在同一 Windows full 执行仿真；输入文件不上传、不复制到 Linux/Cluster |
| 已有 Selena 文件夹 | 本地仿真 | Windows full 直接使用用户目录中的 exe + DLL + Runtime；输入文件不上传、不复制到 Linux/Cluster |
| 本地编译 | Cluster 仿真 | Windows full/light 编译并校验完整 Selena 目录；该 Windows 电脑将 Selena、数据和必要配置直接写入 Cluster 可访问存储；Linux 只登记引用并调度 Cluster |
| 已有 Selena 文件夹 | Cluster 仿真 | 文件所在 Windows/Linux SDK 调用机、连接组件或已有共享路径将完整 Selena、数据和必要配置直接提供给 Cluster；Linux 只登记引用并调度 Cluster |

`target=auto` 不是第五种业务：调度器根据 Selena/data 路径可达性、在线能力和执行环境，在上述本地或 Cluster 路径中做选择，并把选择原因展示给用户。

补充边界：`source=existing + target=cluster` 不进入编译环境依赖链，不要求用户安装 Visual Studio、CMake 或项目软件包依赖。若 Selena、Runtime、MatFilter 和数据已经位于 Cluster 可访问位置，完全不需要 Windows 组件；若其中任一路径只在用户 Windows 本地，则只需要轻量 Windows 连接组件完成读取并直传 Cluster，不执行编译，也不检查编译依赖。Linux 能挂载某个共享路径只是控制面校验能力，不代表允许先把用户文件传到 Linux。

### 3.1 文件传输产品合同

“直接传到 Cluster”在当前成熟仿真体系中定义为：写入 Cluster 工作节点可见的 UNC/共享任务工作区，或调用部署方提供的 Cluster 上传网关。计算节点通常在任务提交后才确定，因此不要求客户端把文件发送到某一台临时工作机。

| 输入位置与入口 | Cluster 仿真时的数据面行为 | Linux 服务响应 |
|---|---|---|
| Cluster 已可访问的 UNC/共享路径 | 原地引用，零复制 | `transfer_skipped`，登记受控引用后继续 |
| Windows 本地路径 + Web | 一键连接组件按 Linux 签发的目标直写 Cluster 共享工作区 | Job 进入 `waiting_for_local_connector`；连接后自动继续，不要求重新提交 |
| Windows 本地路径 + SDK | SDK 可直接执行受控传输；需要编译或本地仿真时复用一次安装、持久在线的统一 Connector | 返回传输计划并展示进度；文件正文不经过 API |
| Linux 工作站本地路径 + SDK | SDK 使用该工作站已挂载的 Cluster 共享目录或 Cluster 上传适配器直传 | 无可用直传适配器时在传输前返回明确处理动作，不提示安装 Windows 组件 |
| Linux/Windows 本地路径 + 纯浏览器 Web | 浏览器不能凭路径读取任意本机文件 | 提示连接本机组件或改用本机 SDK；不得回退为经 Linux Web 端口上传大文件 |
| 已登记的 `dataset://` / Bundle / Asset 引用 | 引用的物理位置必须已对 Cluster 可达，直接复用 | 校验所有权、完整性和可达性后继续 |
| 本机已有/本机可读共享输入 + 本地仿真 | 原地读取，不搬运 | `transfer_skipped_local_execution`，直接进入本机预检/仿真 |
| 远端数据或远端 Selena + 本地仿真 | 优先由执行仿真的 Windows Connector 原地读取共享路径；当前 P0 若不可原地读取则在仿真前返回 `source_to_local_unavailable` | Linux 不接收文件正文；后续实现目标 Windows 受控缓存后再开放跨设备直传 |

直传过程必须具备：按用户和 Job 隔离的不可猜测目标、路径越界保护、断点续传、内容指纹去重、取消、进度上报、重试幂等和完成 Manifest。目标 UNC、挂载点、凭据、上传网关和保留期属于部署配置，不进入用户 YAML。Linux 只能接收文件清单、大小、校验值、进度和逻辑引用，不能接收 MF4/Bundle 文件正文。

### 3.2 通用输入来源与执行位置解析

产品不能把内部适配写成固定项目或四条特例。调度器对每项输入独立解析：

- Selena：当前 Windows 工作区自动编译、本机已有目录、共享/远端已有目录、已登记逻辑引用；
- 数据：执行机本地目录、共享/远端路径、已登记逻辑引用；
- Runtime/Adapter：跟随用户显式路径，按相同可达性规则解析；MatFilter 显式路径优先，留空时仅依据 `code_path`、已有 Selena 文件夹、Selena 编译脚本、Runtime 的路径证据顺序做项目无关推导；多个同分高置信候选按相对路径稳定选择第一个，不因候选过多提前拦截仿真，禁止从项目名或历史任务静默补值；
- 执行位置：统一 Windows Connector 下发本地仿真，或 Cluster 仿真。

统一决策顺序为：`选择执行位置 -> 解析每项输入的可达执行节点 -> 原地读取优先 -> 内容指纹复用 -> 源端直传执行端 -> 登记逻辑引用 -> 启动仿真`。不同输入可以来自不同位置，不能要求它们预先位于同一目录或同一电脑。

| 典型组合 | 预期行为 |
|---|---|
| 本地数据 + 本地编译 Selena + 本地仿真 | 同一 Windows 编译并原地仿真，零传输 |
| 远端/共享数据 + 本地编译 Selena + 本地仿真 | Windows 编译；数据可读则原地使用，否则源端直传 Windows；Linux 只调度 |
| 本地数据 + 远端已有 Selena + 本地仿真 | 数据不动；Selena 可读则原地使用，否则直传 Windows 受控缓存 |
| 远端数据 + 远端已有 Selena + 本地仿真 | Windows full 直接读取可达共享输入；不可达项分别直传 Windows |
| 本地数据 + 本地编译 Selena + Cluster 仿真 | Windows 编译；数据与 Selena 分别直传 Cluster 数据面，Linux 随后提交 |
| 远端/共享数据 + 本地编译 Selena + Cluster 仿真 | 数据若已被 Cluster 访问则零复制；只把编译产物直传 Cluster |
| 本地数据 + 远端/共享 Selena + Cluster 仿真 | Selena 若已被 Cluster 访问则零复制；只把本地数据直传 Cluster |
| 远端/共享数据 + 远端/共享 Selena + Cluster 仿真 | 所有输入原地引用，Linux 仅生成配置并提交 Cluster |

`target=auto` 应在满足能力和用户意图的前提下优先选择总搬运量最小、已连接执行能力可用的路径，并在 Web/SDK 中展示选择原因；不得仅按项目名、路径盘符或某个历史 profile 决策。

## 4. 部署形态

| 部署形态 | 能力 | 明确不支持 |
|---|---|---|
| 安装统一 Windows Connector | 本地编译、已有 Selena、本地仿真、直传 Cluster 后云端仿真；实际能力由该电脑既有环境决定 | 不充当中央 Linux 控制面；本地仿真不上传输入；Connector 不代替用户安装仿真环境 |
| Linux 工作站使用 SDK | 本地可读文件由 SDK 直接写 Cluster 数据面；共享输入零复制；Linux 中央服务继续调度 | 不支持 Selena 编译和 Windows 本地仿真；纯浏览器不能读取任意 Linux 本地路径 |
| 完全不安装 Connector | 在 Web/SDK 填写 Cluster 可达的已有 Selena/数据路径；Linux 调度 Cluster | 不支持 Windows 本地编译或本地仿真；不允许经 Linux 控制面中转大文件 |

Windows Connector 安装必须一键完成，并且是当前 Windows 用户的一次性持久连接：服务地址、稳定用户身份和受限凭证写入本机安装目录，登录自启、断线重连和后续任务复用都由组件完成，不要求用户每次任务重新配置。只有换电脑、换 Linux 服务地址、切换服务身份或主动卸载时才重新安装。Visual Studio 由用户自行安装，Connector 负责识别可用 C++ toolset、校验并对 Selena 脚本的 VS 参数做最小适配，不代替用户安装 VS。其余可自动发现且安全的外围环境在第一次任务中自动配置并持久复用；仿真环境由用户维护，缺失时只给出明确检查结果和处理动作。

连接的恢复边界：电脑重启后，用户登录 Windows 即由计划任务或 Startup 回退自动拉起连接；电脑关机、睡眠、尚未登录或网络隔离时，Linux/Web/SDK 不能远程唤醒电源或启动本机进程，只能保持等待/重连，恢复后继续原任务。用户路径在 Web、SDK 和 Agent 绑定层统一规范化，接受正斜杠、反斜杠、重复分隔符、`.`/`..` 和 Windows UNC 等价写法；`shared://`、`dataset://` 等逻辑 URI 不按本地文件系统规则改写。

Connector 不是所有用户的必需组件：`source=existing + target=cluster` 且 Selena、Runtime、MatFilter、数据均位于 Cluster 可访问位置时，Linux Web 和 SDK 均可直接提交；只有输入仍在 Windows 本地，或用户要求 Windows 编译/本地仿真时，才需要统一 Connector。SDK 调用方可在本机具备 Cluster 直达能力时自行执行受控直传；Linux SDK 调用机不能读取 Windows 本地盘，应改用共享路径或连接实际存放文件的 Windows 电脑。

## 5. 任务编排与可视化验收

调度器根据配置动态生成必要 Stage，不为不需要的工作制造步骤：

1. 配置解析与业务识别；
2. 执行节点/路径可达性解析；
3. 环境和依赖检查，必要时执行允许的自动处理；
4. 可选 Selena 编译；
5. Selena 文件夹校验，以及本机原地使用或直传 Cluster 后内部登记；
6. 数据检索，以及本机原地使用、Cluster 原地引用或客户端直传；
7. Runtime、Adapter、MatFilter 下发和预检；
8. 本地或 Cluster 仿真提交/运行；
9. 结果收集与 Manifest。

Web 和 SDK 必须能读取同样的 Job/Stage/Event。Web 至少展示：当前 Stage、执行节点、自动路由原因、进度、日志、失败字段、修复建议、重试/取消动作和最终结果。

结果下载继续保留 ZIP 归档。SDK 的 `download_job_result()` 是手动下载便捷方法，按
`Job -> Manifest -> result_ref` 顺序获取并校验归档；未显式传入 `destination` 时，接收设备默认保存到
`<result.path>/<job_id>`；空值时使用 `Path.home()/RadarSim/results/<job_id>`。执行端解压结果和手动下载 ZIP 并行放在同一 Job 目录；浏览器受下载权限模型约束，不能保证写入 `result.path`，但仍保留 ZIP 下载。

## 6. 发布门禁

以下证据全部存在前，不得再宣称“已经交付”：

- 同一个示例 YAML 可被 Web 导入/导出并被 SDK 提交，往返后不出现内部字段；
- 四条业务路径都有合同测试和纵向执行测试；
- “已有 Selena”测试证明 DLL 随目录被校验、打包/传输和实际使用；
- 本地路径、共享路径和上传路径对用户仍表现为唯一 `data.path` / `existing_path`；
- Cluster 路径的 MF4、Selena Bundle 和配置资产传输抓包/指标证明文件正文不经过 Linux API 端口，Linux 服务在大文件传输期间仍能稳定响应 Web、SDK 和 Agent 心跳；
- 两条输入均本机可达的本地仿真路径证明不会创建上传会话、Cluster staging 目录或数据传输 Stage；远端输入到本地仿真则只允许源端到 Windows full 的直接传输；
- Windows Web、Windows SDK、Linux SDK 的本地文件直传，以及共享路径零复制均有合同测试；
- Linux 节点无法领取 Selena 编译或本地仿真 Stage；统一 Connector 只领取其真实声明且服务端允许的本机能力；
- 真实失败能停在正确 Stage，不能再出现由内部默认值造成的 `output_root must be narrower than workspace_root`；
- 在 `10.190.171.44` 完成 Linux 部署、Web/SDK 健康检查和至少一条目标环境 Cluster 烟测；
- `HANDOFF.md` 如实记录代码证据、测试证据、外部环境未验收项和已知限制。

## 7. 防漂移规则

每个开发任务开始前必须写明触碰本文件的哪一条；结束时在 `HANDOFF.md` 记录：用户路径是否变简单、是否新增了用户字段、四种组合受影响情况、测试证据和未验证项。任何新增用户字段或部署职责变化必须先获得用户确认。
