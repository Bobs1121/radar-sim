# radar-sim V2 详细设计

> 状态：V2 唯一代码设计基线
> 日期：2026-08-11
> 产品合同：`docs/PRODUCT_CONTRACT.md`
> 架构索引：`docs/V2_ARCHITECTURE.md`

## 1. 设计原则

1. 薄层：不实现仿真算法，只适配路径、资源、命令、状态和结果。
2. 单轨：公共配置只有 `UserRunConfig 2.0`，旧 `SimulationSpec` 不再有 HTTP/SDK 入口。
3. 去项目化：不根据 Xpeng/BYD/GAC/OD25 等产品名选择参数、模板或流程。
4. 控制/数据面分离：Linux API 不收发 MF4、Selena 目录和大型结果正文。
5. 源到目标：文件由可读取它的 Connector/SDK 直接写入执行目标可访问存储。
6. 真实状态：外围失败、仿真内部失败和部分成功分别表达，不做假成功。
7. 多用户：所有用户资源以 owner 为第一隔离维度，内部匿名执行身份不是授权主体。

## 2. 组件

| 组件 | 职责 | 不负责 |
|---|---|---|
| Web | V2 YAML 导入/编辑/导出、任务和结果 UI | 调度、路径转换、文件中转 |
| Python SDK | 同 API 的编程入口、调用机直传、Job 等待与结果下载 | 第二套 DAG、项目适配 |
| FastAPI adapter | HTTP、认证、错误 envelope、SSE、文件响应 | 业务编排 |
| `ApiV1Service` | V2 校验、路由决策、Job/Stage 创建、公共状态 | 运行 Selena、搬运正文 |
| `ControlService` | 持久 Job/Stage/Event、原子 claim、重试/取消、恢复 | 产品识别 |
| Windows Connector | 读取本机路径、编译、local 仿真、Cluster 直传、结果落盘 | 安装 VS/仿真环境 |
| Linux executor | Cluster 环境检查、准备配置、收集清单 | Windows 编译、本地仿真 |
| Cluster gateway | 提交/查询成熟 Cluster 任务 | 用户路径推导 |

## 3. 公共 API

V2 创建入口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/schema/run-config` | 唯一 YAML/JSON schema |
| POST | `/api/v1/run-configs/import` | YAML -> canonical V2 |
| POST | `/api/v1/run-configs/export` | canonical V2 -> YAML |
| POST | `/api/v1/run-configs/validate` | 路由和 readiness 预览 |
| POST | `/api/v1/run-jobs` | 创建 V2 Job |
| GET | `/api/v1/capabilities` | `windows`、`cluster`、Connector 版本 |

任务管理继续使用 `/api/v1/jobs/{id}`、events、cancel、retry、manifest、diagnosis、transfers 和 results。`POST /api/v1/jobs`、`POST /api/v1/validate`、`/api/v1/specs/*`、`/api/v1/projects` 和 SimulationSpec schema 已从公共路由删除。

SDK 只提供 `validate_run()`、`submit_run()`、`submit_yaml()` 及任务/传输/结果方法；旧 `validate(SimulationSpec)` 和 `submit(SimulationSpec)` 已删除。

## 4. V2 模型

`core.user_config.UserRunConfig` 是 Web、SDK 和 API 的唯一模型。canonical 导出始终包含：

- `schema_version: "2.0"`
- `selena`
- `data`
- `simulation`
- `result.path`

严格拒绝未知业务字段，不迁移旧 project/profile/recipe。路径只在能够读取它的执行设备上解析；Linux 公共对象只保留脱敏逻辑引用和摘要。

## 5. 执行身份和授权

### 5.1 owner

- 无认证内网试用：稳定 `user-<lowercase NTID>`；
- 正式模式：owner 只来自 Bearer token；
- Web、SDK、Connector 必须使用同一 owner；
- Job、Agent、Transfer、Result 和动作按 owner 隔离。

### 5.2 execution identity

数据库中历史字段 `project/internal_project` 暂作为匿名 `execution_identity` 存储：

- build：由规范化工作区和 Selena 编译脚本生成 `workspace-<digest>`；
- existing：由 Selena.exe、DLL 和 Runtime 内容指纹生成 `workspace-<digest>`；
- 只用于 binding、缓存、幂等和追踪；
- 不读取 `config/projects/*`，不选择 recipe、模板或参数；
- 公共 YAML 和 Web 不显示。

### 5.3 本地授权

- workspace binding：owner/设备上的工作区与较窄 output root；
- asset binding：Runtime/MatFilter/Adapter/已有 Selena 的受控根；
- data binding：`owner + device_id + normalized root`；
- 公共 Stage 只携带 opaque binding ID，相对脚本 ref 和逻辑引用；
- Agent claim 后才恢复物理路径。

## 6. DAG

固定十阶段：

1. `resolve_spec`
2. `environment_check`
3. `prepare_source`
4. `prepare_data`
5. `build_selena`
6. `register_artifact`
7. `preflight`
8. `run_simulation`
9. `collect_results`
10. `finalize_manifest`

不需要的阶段标记 `skipped`，不创建另一套项目 DAG。Cluster 环境轻量检查先于本地大文件传输；环境不可用时不浪费数据搬运。

## 7. 编译设计

### 7.1 解析

`WorkspaceRecognizer.recognize(..., generic_only=True)`：

- 验证 code path 和脚本位于同一授权工作区；
- 生成匿名 execution identity；
- 从 Selena 脚本内容优先推导 build config/output；
- 不加载项目 adapter 表；
- 无静态 output 时选择较窄通用 build 根（`ip_dc/build` 或 `build`）。

### 7.2 执行

`prepare_selena_build()` 对 V2：

- 直接构造 V2 local bindings，不调用 legacy adapter；
- 命令为 `cmd /c <authorized script>`；
- injected args 为空；
- package script 只做存在性/依赖诊断；
- branch mismatch 写 warning，不切换、不检查 diff。

### 7.3 产物确认

1. 静态推导出的 Selena.exe 存在则优先；
2. 否则在 authorized output roots 内有界搜索；
3. 优先父目录匹配 build mode；
4. 再选择最新修改产物，稳定路径作 tie-breaker；
5. 校验文件为普通非 symlink、位于授权根、hash/size 有效；
6. 以 exe、同目录 DLL、Runtime 形成 Runtime Bundle。

## 8. 已有 Selena

`import_existing_selena()`：

- 目录内必须唯一定位 Selena.exe；
- 至少有同目录 DLL；
- Runtime XML 必须存在且为有效 XML；
- code path、脚本路径和文件夹名不参与产品推导；
- 目录重定位但内容相同应得到同一 execution identity；
- local 原地使用，cluster 形成直传/零复制资源清单。

## 9. 通用仿真适配

`load_local_execution_config()` 永远加载平台公共层和共享 `selena_paramconfig_v1.txt`，不加载项目配置。

本地 Runner：

1. 在 run lease 受控目录生成 paramconfig、日志和输出；
2. 注入 input/output、Runtime、MatFilter、Adapter、source 和 mounting；
3. 不调用 recipe handler；
4. 执行 `<Selena.exe> --paramconfig <file> [safe extra args]`；
5. 使用 Windows Job Object 终止子进程树；
6. 只上传受限日志尾和结构化结果，完整日志留在执行端。

Cluster Runner 使用相同业务字段生成 Cluster `Config.cfg`/任务输入，再调用现有 gateway；Linux 不解释产品。

## 10. MatFilter、Adapter 和 source

- MatFilter 显式路径最高优先；
- 自动搜索顺序：code path、已有 Selena 邻近受控目录、脚本/Runtime 邻近受控范围；
- 候选按稳定优先级选择并记录，不因多个候选前置失败；
- Adapter 显式可选，不由 recipe 强制；
- source 显式值优先，允许 RadarFL/FR/RL/RR/FC；
- 空值读取 Runtime DataPlayer 与 MF4 acquisition 元数据；
- 多源且未显式选择时稳定选择一个，并在日志/Manifest 中记录推导证据。

## 11. 数据路由

资源逐项判断：dataset、runtime_bundle、runtime_xml、mat_filter、adapter。

- `original_read`：执行端可原地读取；
- `shared_zero_copy`：Cluster 可读共享路径，登记逻辑引用；
- `shared_copy`：Connector/SDK 执行签名 TransferPlan，源到 Cluster 数据面；
- `source_to_local`：未来远端到目标 Windows 缓存；当前没有安全目标授权时稳定 unavailable；
- 禁止 gateway upload 或 Linux request body 作为大文件兜底。

TransferPlan 具备 owner/job/stage/role、隔离相对路径、checksum、进度、完成 Manifest、取消与幂等重试。一个资源完成不能误标其他资源完成。

## 12. Connector

- 公共 mode 固定 `unified`；HTTP query 和 SDK 拒绝 light/full；
- capability API 只返回统一 `windows`，不返回 light/full；
- 一次安装，保存 server/owner/agent identity 和 bindings；
- 用户登录自启、监督重启、指数退避、单实例；
- 使用隐藏窗口/`CREATE_NO_WINDOW`，不周期弹终端；
- contract version 不匹配时旧实例不领任务，Web 提供一键更新；
- 更新保留 identity、bindings 和自启配置。

内部数据库的 `windows_agent/windows_full` node kind 可在迁移清理前存在，但不能进入用户 API、安装选项或 YAML。

## 13. 批量与结果

- 数据目录解析为独立 input item；
- 单条失败记录 error code、日志尾、输入相对路径，继续其余输入；
- Job summary 给出 total/succeeded/failed；
- 有部分成功时状态/Manifest 表达 partial，不取消已成功项；
- local 结果原子物化到 `<result root>/<job_id>`；
- Web ZIP 和 SDK `download_job_result()` 校验 checksum；
- `finalize_manifest` 根据业务 summary 决定 Job 最终状态，不能仅凭进程 exit 0 假成功。

## 14. 并发和恢复

- Control DB 事务化 claim，Stage at-least-once；
- Windows Connector 单 `current_task_id`，不在同一实例无限并发；
- Linux/Gateway 使用有界 worker pool，默认每角色 2，范围 1..16；
- Cluster worker identity 为服务端注册，普通 Agent 不能伪造；
- owner 公平排序，不设置硬配额；
- heartbeat/stale reclaim/幂等完成处理服务重启和短暂断线。

## 15. 错误分类

| 类别 | 示例 | 行为 |
|---|---|---|
| control plane | Linux/DB/调度组件不可用 | 不搬大文件，稳定错误码，允许恢复/重试 |
| connector | 未安装、离线、版本旧、路径不在该设备 | 引导一次连接/自动重连/更新或连接正确设备 |
| routing | shared 未挂载、source_to_local 不可用 | needs-input/unavailable，不伪造成功 |
| build | 脚本缺失、返回非零、无 Selena.exe | 保留编译日志和依赖提示 |
| simulation internal | Selena 返回非零、单条结果失败 | 不由框架修复；保留日志/Manifest，继续批量 |
| result | Manifest 矛盾、归档不可用 | Job 不标成功，给出结果诊断 |

## 16. 测试与发布门禁

代码门禁至少覆盖：

- UserRunConfig round trip、Web/SDK hash/DAG 一致；
- V2 不进入 legacy adapter、recipe 或 project config；
- 已有 Selena 内容身份与路径无关；
- 静态/动态 build output；
- 显式/自动 MatFilter 和 RadarFC；
- 四组合、路径矩阵和单条/批量部分成功；
- owner/device 绑定隔离和两用户并发；
- direct transfer 无 Linux 正文；
- Connector unified 首装/升级/重连/单实例；
- result.path、ZIP、SDK 下载；
- API OpenAPI 不包含 legacy 创建路由。

自动测试不是实机验收。发布前仍需在目标 Linux 与新 Windows 用户上完成真实 existing/build + local/cluster 验收，并记录 Job ID、Manifest 和结果位置。
