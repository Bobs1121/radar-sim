# radar-sim V2 产品需求文档

> 版本：2.0 单轨
> 状态：首版发布基线
> 日期：2026-08-11

## 1. 产品目标

radar-sim 把用户已经能手工完成的 Selena 编译、本地仿真和 Cluster 仿真连接成一个稳定的自动化流程。它不是新的仿真引擎，也不替代用户的 Visual Studio、Selena 或本地仿真环境。

用户只提供代码/脚本或已有 Selena、Runtime、数据及少量可选仿真文件；系统完成路径适配、源到目标传输、命令下发、任务状态和结果交付。

## 2. 用户入口

- Web：导入、编辑、导出 YAML，提交任务，查看过程、诊断和结果。
- Python SDK/REST API：具备与 Web 相同的提交、传输、任务管理和结果能力，供后端产品及未来 AI 工具集成。

Web 与 SDK 必须使用同一 `UserRunConfig 2.0`、同一 `/api/v1` 调度核心。CLI 仅供部署和维护，不是第三个产品入口。

## 3. 用户故事

1. 我有已有 Selena、Runtime 和数据，希望在本机仿真。
2. 我有已有 Selena、Runtime 和数据，希望在 Cluster 仿真。
3. 我有代码和 Selena 编译脚本，希望编译当前工作区后在本机仿真。
4. 我有代码和 Selena 编译脚本，希望编译后把产物和数据直传 Cluster 仿真。
5. 我的数据或 Selena 在共享存储，希望系统原地读取，不做无意义复制。
6. 我的输入在本机，希望文件从本机直接进入目标数据面，不经过 Linux Web 服务。
7. 我有一批 MF4，希望单条失败不取消其余数据，最后分别看到成功和失败清单。
8. 我是新用户，希望只安装一个 Windows Connector，一次安装后长期复用、自动重连和一键升级。
9. 我只使用 SDK，希望能提交同一 YAML、执行必要直传并把结果保存到本地目录。

## 4. 用户配置

唯一配置和字段语义见 [V2_ARCHITECTURE.md](docs/V2_ARCHITECTURE.md)。核心字段为：

- `selena.source`: `build | existing`
- build：`code_path`、`selena_build_script`，可选 `branch`、`package_build_script`
- existing：`existing_path`
- 两者：`runtime_xml`
- `data.path`
- `simulation.target`: `auto | local | cluster`
- 可选 `simulation.source`、`mat_filter`、`adapter_file`
- 可选 `result.path`

用户不填写 project、profile、recipe、Agent 模式、Runtime Bundle、Cluster 拓扑或共享盘类型。

## 5. 产品行为

### 5.1 编译

- 只能在 Windows Connector 上发生；
- 默认编译当前工作区，不检查 diff、不清仓、不 reset、不自动切换分支；
- 分支不一致仅警告；
- 命令只执行用户选定脚本，不注入项目参数；
- 从脚本优先推导输出，编译后在受控 build 根内确认实际 Selena.exe；
- 软件包编译脚本只用于依赖诊断。

### 5.2 仿真

- 本地仿真假设用户已有成熟环境，系统准备外围输入后下发通用 Selena 命令；
- Cluster 仿真复用现有 Cluster 提交模块；
- 不按产品名选择流程或模板；
- Runtime 与 Selena 由用户保证业务匹配，系统依赖仿真输出判断，不做过重前置拦截。

### 5.3 数据和文件

- Linux 只下发 TransferPlan，不中转大文件；
- 本地目标可读的资源原地使用；
- Cluster 共享资源零复制登记；
- 调用机本地资源由 Connector/SDK 直接写入 Cluster 可访问数据面；
- 尚无安全 `source_to_local` 时明确提示不支持，不通过 Linux 绕传。

### 5.4 MatFilter 和雷达源

- 用户显式路径/源永远优先；
- MatFilter 留空时从代码仓、已有 Selena 邻近受控位置做通用、确定性推导；
- 不使用项目白名单或历史 Job 默认；
- 雷达源支持 RadarFL/FR/RL/RR/FC，空值从 Runtime/MF4 元数据推导；存在多个源时按用户值选择，未指定则稳定选择并记录。

### 5.5 结果

- 本地结果写入 `<result.path>/<job_id>`，空值使用 `~/RadarSim/results/<job_id>`；
- Web 保留 ZIP 下载；SDK 提供 Job 级校验下载；
- 批量 Manifest 分别列出成功和失败输入；
- 仿真内部失败需要保留受限日志和结果信息，外围框架错误必须给出稳定错误码和操作建议；
- 不允许 Stage 执行成功但实际 Manifest 失败时把 Job 标为成功。

## 6. 多用户与稳定性

- owner、Job、Connector、路径绑定、传输、日志和结果隔离；
- Cluster 共享执行器有界并发，不无限创建线程；
- Connector 用户级持久运行、开机/登录自启、断线重连、单实例、无周期黑窗；
- 路径支持正斜杠、反斜杠、UNC 和共享路径规范化；
- Linux 服务重启后 Job/Stage/Event 可恢复，任务中心刷新后仍可见；
- 传输支持进度、校验、幂等重试，不重复上传已完成内容。

## 7. 首版非目标

- 不安装 Visual Studio 或用户仿真环境；
- 不修复 Selena 内部 runnable/算法问题；
- 不引入消息队列、工作流平台或对象存储作为必选依赖；
- 不实现独立 MCP/Skill 调度器；未来只能薄封装 SDK；
- 暂不承诺 Cluster 结果自动反向直传、任意远端到 Windows 本地缓存和关机唤醒。

## 8. 发布验收

详细矩阵见 [V2_ARCHITECTURE.md](docs/V2_ARCHITECTURE.md#11-发布验收矩阵)。首版必须至少通过：

- 四种 Selena 来源/目标组合；
- Web 与 SDK 同 YAML 同 DAG；
- 两个新用户并发与身份/结果隔离；
- Connector 首装、重启、重连、升级和单实例；
- 本地/共享/Cluster 可读路径路由；
- 单文件与批量部分成功；
- 结果目录与 ZIP；
- 一个未登记的新项目仅靠路径、脚本、Runtime 和数据完成编译/仿真，不增加项目配置。

## 9. 权威文档顺序

1. `docs/PRODUCT_CONTRACT.md`：用户确认的不可偏移合同；
2. `PRD.md`：产品需求；
3. `docs/V2_ARCHITECTURE.md`：V2 架构、删除清单和验收；
4. `docs/DETAILED_DESIGN.md`：代码级设计；
5. `docs/handoffs/2026-08-11-business-convergence-master.md`：当前实施证据和缺口。
