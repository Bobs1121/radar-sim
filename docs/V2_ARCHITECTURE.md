# radar-sim V2 单轨架构与交付边界

> 状态：V2 唯一实施基线  
> 日期：2026-08-11  
> 上位产品合同：`docs/PRODUCT_CONTRACT.md`

## 1. 产品定义

radar-sim 是现有 Selena 编译和仿真工具外围的一层轻量自动化脚手架。它只做：

1. 接收一份业务 YAML；
2. 判断输入位于哪台设备、目标在本地还是 Cluster；
3. 下发源到目标的传输计划，Linux 不转发大文件；
4. 需要编译时，在用户 Windows 上执行用户指定的 Selena 编译脚本；
5. 生成通用 Selena `--paramconfig`，调用成熟的本地或 Cluster 仿真入口；
6. 汇总每条数据的状态、日志和结果，并把结果交付到用户指定位置或提供 ZIP。

Linux 是控制面，不编译、不执行本地仿真、不承载 MF4/Selena 目录的数据正文。Windows Connector 也是薄执行器，不重建用户已有的 VS、Selena 或本地仿真环境。

## 2. V2 唯一入口

公共入口只有 Web 与 Python SDK/REST API。Web 是同一 API 的可视化前台；两者提交同一份 `user-run-config/2.0`，得到同一种 Job、Stage、日志、诊断、结果清单与下载能力。

旧 YAML、旧配置入口、Agent 能力档位选择和项目注册不做兼容。产品尚未正式发布，V2 直接成为唯一主链。

所有公共 YAML 都必须使用 `schema_version: "2.0"` 并由 `UserRunConfig` 解析。该模型在顶层和每个嵌套对象都启用 Pydantic `extra="forbid"`：旧版本字段不会被静默迁移、忽略或猜测，提交旧 YAML 会直接返回校验错误，用户需要按 2.0 合同重新填写。

## 3. 用户最少配置

```yaml
schema_version: "2.0"

selena:
  source: existing                 # existing | build

  # source=existing 必填：目录内含 Selena.exe 及其同目录 DLL
  existing_path: "C:/path/to/RelWithDebInfo"

  # source=build 必填：只执行用户选择的脚本，不添加业务参数
  # code_path: "C:/path/to/workspace"
  # selena_build_script: "C:/path/to/build_selena.bat"

  branch: ""                      # 可选；仅提示与当前分支是否一致，不自动清仓/切换
  package_build_script: ""        # 可选；只用于依赖诊断，不改变编译命令
  runtime_xml: "C:/path/to/Runtime.xml"

data:
  path: "C:/path/to/file-or-folder"

simulation:
  target: auto                     # auto | local | cluster
  source: ""                      # 可选 RadarFL/RadarFR/RadarRL/RadarRR/RadarFC；空值自动推导
  mat_filter: ""                  # 可选；显式值优先，否则在代码/产物附近确定性推导
  adapter_file: ""                # 可选；没有业务需要时可空

result:
  path: ""                        # Connector/SDK 接收端保存根目录；空值使用接收端默认目录
```

`source=build` 时不需要 `existing_path`；`source=existing` 时不需要代码仓和任何编译脚本。用户不填写产品项目、运行档位、模板名称、Runtime Bundle 引用、Agent ID、服务令牌、共享盘类型或 Cluster 内部参数。

## 4. 唯一执行流水线

```text
V2 YAML
  -> resolve_spec（规范化路径、owner/device、资源可达性）
  -> environment_check（只检查脚手架前置条件）
  -> build Selena 或 import existing Selena
  -> prepare resources（零复制或源到目标直传）
  -> preflight（生成通用 paramconfig，不做产品白名单拦截）
  -> run local 或 submit Cluster
  -> collect each input independently
  -> manifest + result delivery + ZIP
```

旧数据库中的内部标识只允许承载由内容/工作区哈希生成的匿名 `execution_identity`，用于授权、幂等、缓存和追踪；它不得选择业务配置、模板、编译参数或仿真流程，也不得出现在用户 YAML/Web 表单中。

## 5. Selena 两种来源

### 5.1 已有 Selena

- 校验选择目录中唯一的 `Selena.exe`、同目录必要 DLL 和 Runtime XML；
- 执行身份由二进制、DLL、Runtime 内容生成，路径名称和代码仓名称不参与业务猜测；
- 不检查 VS，不要求代码仓，不读取任何产品目录索引；
- local 时本机原地使用；cluster 时由源端直传 Cluster 可访问存储或对共享路径零复制登记。

### 5.2 本地编译

- Windows Connector 在授权工作区中运行用户给定脚本，命令语义固定为 `cmd /c <script>`；
- 不注入 Xpeng/BYD/GAC 等产品参数；脚本拥有自身参数和环境；
- `branch` 仅做分支差异提示，默认编译当前工作区，不检查 diff、不清仓、不 reset；
- 优先从脚本推导输出目录并验证 `Selena.exe`，编译完成以实际产物、DLL 和 Runtime 形成 Bundle；
- `package_build_script` 只提供环境依赖诊断线索，不替代用户的 VS/本地仿真环境。

## 6. 数据面路由

| 输入位置 | 仿真目标 | 行为 |
|---|---|---|
| 同一 Windows 可读 | local | 原地读取，不传输 |
| Windows/Linux 调用机本地 | cluster | Connector/SDK 按 TransferPlan 直写 Cluster 数据面，Linux 不接收正文 |
| Cluster 可读共享路径 | cluster | 零复制登记，Linux 直接调度 |
| 远端资源 | local | 需要源端到目标 Windows 的 `source_to_local`；未具备安全目标缓存时明确返回 unavailable，不经 Linux 绕传 |

MatFilter 采用“用户显式值最高优先；否则在 `code_path`、已有 Selena 邻近目录及受控范围内按稳定规则推导”。多个候选不得因为名称猜测；系统应给出最终采用文件及候选诊断。数据目录中有多条 MF4 时逐条独立运行，单条失败不取消其余数据，最终 Manifest 分列成功与失败项。

## 7. 目标选择

- `local`：要求同 owner 的统一 Windows Connector 在线，并假设用户本地仿真环境已建立；外围准备完成后只下发 Selena 仿真指令。
- `cluster`：要求 Cluster 调度组件健康；输入全部进入 Cluster 可访问数据面后提交任务。共享输入无需 Connector，本地输入需要源设备 Connector 或 SDK 直传能力。
- `auto`：若任务需要 Windows 本地编译则先绑定 Windows；仿真目标按可用资源和显式输入决定。任何自动决策都写入 Stage 日志，不向用户暴露内部实现选择。

## 8. 统一 Connector

用户只安装一个统一 Connector。安装入口由 Web/SDK 下载，一次执行后以隐藏的用户级自启/监督方式持久运行，断线重连且不得周期弹出黑色终端窗口；能力来自本机实际可读路径与已有环境。升级由 Web 显示版本状态并提供同一“一键更新”入口，不要求用户重新输入 Agent ID、服务地址或令牌。

## 9. 结果合同

- `result.path` 只表示 Connector/SDK 接收端的结果保存根。对于本地仿真，接收端可将结果物化到 `<result.path>/<job_id>`；空值使用接收端默认的 `~/RadarSim/results/<job_id>`。纯 Web/Cluster 提交若没有反向 Connector，不承诺写入该路径，只提供 owner 隔离的 ZIP；
- Web 保留按 Job 下载 ZIP；SDK 提供 Job -> Manifest -> 校验下载的高层方法；
- 本地仿真可由 Connector/SDK 接收并物化到目标目录；
- 每条输入记录 `success/failed`、结果文件和受限日志尾；Job 只有在所有必要外围 Stage 正常结束且结果 Manifest 可解释时才成功；
- Cluster 到用户设备的自动反向直传尚未完成时，只能如实提供受 owner 隔离的 ZIP/引用，不得宣称已落到本机目录。

## 10. 明确删除与暂不承诺

### 从 V2 主链删除

- 用户可见的产品项目、运行档位、模板选择、项目注册和固定产品白名单；
- `ovrs25/bydod25/xpeng/...` 对编译参数、产物目录或 Selena 模板的选择；
- Agent 能力档位安装选择；
- legacy YAML 迁移、旧 Web 表单与旧 SDK 参数入口；
- Runtime Bundle 引用等内部对象作为用户配置；
- 通过 Linux API 上传/下载大文件正文。

### 尚未完成，不得对外宣称

- 可安装的 radar-sim MCP Server/Skill；未来只能薄封装当前 SDK；
- Cluster 结果自动反向直传并解压到任意用户设备；
- 远端资源到本地 Windows 的通用 `source_to_local`；
- 操作系统关机状态下远程唤醒 Connector。

## 11. 发布验收矩阵

每次发布至少验证：

1. Web 和 SDK 对同一 YAML 生成相同 Stage DAG；
2. 两个 owner 并发提交时，Connector、路径绑定、日志、结果和取消/重试互不串扰；
3. `existing + local`、`existing + cluster`、`build + local`、`build + cluster` 四组合；
4. 本地路径、正反斜杠、UNC/共享路径和 Cluster 可读路径；
5. 显式/自动 MatFilter、空/显式 source、单文件/目录批量；
6. 单条仿真失败后其余数据继续，Manifest 明确列出成功和失败；
7. Linux 不出现用户大文件正文，源到目标传输有进度、校验与幂等重试；
8. Connector 首装、重启自启、断线重连、版本更新、无重复进程和无可见终端弹窗；
9. 结果目录与 ZIP 都能按 owner 获取，假成功必须被外围 Manifest 阻断；
10. 新项目仅更换用户路径/脚本/Runtime/数据即可进入同一通用流程，不新增项目配置文件。
