# radar-sim V2 当前交接

> 更新时间：2026-08-13
> 状态：V2 project-free、Connector v9 与全链异常收口已部署；等待一台全新/升级 Windows 电脑完成真实安装验收
> 分支：`codex/new-branch`
> 当前生产基线：`7020321`，Linux release `/home/hoz2wx/radar-sim-7020321`
> 回滚基线：`8f8601c`，Linux release `/home/hoz2wx/radar-sim-8f8601c`

本文是下一位开发者的唯一实时入口。历史长篇实施日志已从根 handoff 删除；需要追溯时使用 Git 历史和 `docs/handoffs/` 中带日期的证据文件。产品和架构决策依次以 `docs/PRODUCT_CONTRACT.md`、`PRD.md`、`docs/V2_ARCHITECTURE.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md` 为准。

## 1. 当前产品合同

radar-sim 是外围自动化脚手架，不实现 Selena 内部仿真，也不安装 Visual Studio 或替用户维护本地仿真环境。

- 公共配置只有 `UserRunConfig 2.0`。Web、Python SDK 和 REST API 使用同一配置、同一调度核心和同一 Job/Stage/Manifest。
- 用户不配置业务项目、profile、recipe、Runtime Bundle、Agent ID、Cluster 拓扑或共享盘类型。
- V2 主链完全不识别、不登记、也不按任何业务项目分支。允许从用户给定文件和脚本推导具体参数，但禁止“先识别成项目，再套项目规则”。增加新 Selena 工程不允许增加 project adapter、project registry 或项目专用任务流程。
- Selena 来源只有 `build` 与 `existing`；仿真目标只有 `auto`、`local`、`cluster`。
- 编译只发生在 Windows，执行用户给定的 Selena 编译脚本；默认使用用户已切好的当前工作区和本地修改，不做 checkout/reset/clean/stash。
- 本地仿真由统一 Windows Connector 在用户电脑执行。用户默认已准备 Selena 仿真环境；Connector 只做外围检查、路径准备、指令下发和结果收集。
- Cluster 仿真由 Linux 控制面调度 Linux executor/Gateway。大文件从源设备直接写 Cluster 数据面，正文不经过 Linux Web/API。
- Windows 用户只安装一个统一 Connector。一次安装、持久运行、断线重连、单实例；不再区分轻量/完整 Agent。
- 批量输入逐条记录成功/失败；单条 Selena 内部失败不能取消其它输入和结果收集。
- 本地仿真可把结果物化到 `<result.path>/<job_id>` 并保留 ZIP。Cluster 当前保证 owner-scoped ZIP/引用；反向直传并解压到任意用户设备尚未开放。

## 2. 已发布的收口修改

### 已修复

1. `core/stage_binder.py` 分离 `workspace_binding_id` 与 data `advertised_binding_id`，防止数据 binding 覆盖工作区 binding；回归同时断言 environment 使用 workspace、prepare_data 使用 data。
2. SDK 显式 `user` 和 `X-Rsim-User` 统一到稳定 `user-<lowercase>`；Web、SDK、Connector 新请求使用相同 owner。服务端继续只为历史记录兼容旧任意标签；Bearer 部署始终以认证 principal 为 owner。
3. 删除公开的 V1 `submit_cluster_yaml()`；V2 只保留 `submit_yaml()`/`submit_run()`。
4. artifact、Runtime Bundle、result、dataset、已有 Selena 和配置资产上传都使用有界流式读取；在追加超限块前拒绝，避免无 `Content-Length` 时先分配超大内存。
5. Web/SDK 本地零传输合同测试改用真实稳定 owner，消除旧测试身份与当前 Connector 合同不一致。
6. SDK 已有 Selena 直传与 Connector 使用同一白名单，只传 `Selena.exe + DLL`，排除 PDB/ILK/LIB/EXP 和调试目录；缺少 `Selena.exe` 稳定报错。数据源探测最多读取 16 条 MF4 的轻量元数据，显式 `simulation.source` 不读 MF4；多源按产品规则稳定选择一个并保留 mixed 候选证据。
7. 全仓回归发现并清除 4 个过时断言：旧 Web 产品识别文案、已删除的 SDK `upload_run_data` monkeypatch、项目模板缺失必须失败。它们与当前 project-free、源到源传输和公共模板回退合同冲突，不是运行代码回归。
8. README、PRD、开发计划、产品合同、V2 架构和详细设计已统一到 2026-08-13 V2 单轨口径；根 handoff 已精简，旧 V1/项目化部署、配置、环境和实战文档只保留归档跳转，不再暴露冲突步骤。
9. TransferPlan 已实现通用幂等：同一 owner/job/stage/role、同一输入元数据与目标根的并发或网络重试复用同一计划；失败、取消、过期或输入改变才签发新计划。SQLite 使用短事务串行签发，避免一个 Job 因 SDK 超时重试产生多个大文件传输。
10. V2 编译输出推导已彻底断开历史项目上下文解析。只读取用户 Selena 脚本中的通用 build/output 开关；无法静态解析时回退工作区内受限 `ip_dc/build`/`build` 并在编译后查找实际 Selena.exe。`/apl/`、`byd`、R2D2、hex 或产品名不再影响 V2 输出路径。
11. 新用户 Connector 安装误失败已定位为身份双重规范化：Web/安装配置使用旧 `web-*`，运行进程又改写为 `user-web-*`，导致进程实际已注册轮询但安装器在另一 owner 下等待。v9 将安装 owner 视为服务端绑定的 opaque identity，注册、轮询、传输和精确设备状态使用同一值；旧随机 owner 只能在旧合同升级时一次性迁移到稳定 `user-<NTID>`，之后禁止静默换 owner 或服务器。
12. 安装完成按本次 `agent_id + owner + contract` 精确确认本机；同一稳定设备 ID 的 owner 检查位于 SQLite 原子 UPSERT 临界区。Poll、heartbeat、日志、进度和结果回调也校验 Job/Connector owner，避免共享控制库中的偶然串线。
13. Web 停用旧浏览器随机身份，必须输入稳定 NTID 后才能继续；同一 YAML 的提交幂等键跨页面刷新保留。SDK/Web 对下载断流、结果引用缺失、重复下载、重复 Transfer role、路径 token 和大文件超时给出稳定错误，失败临时文件自动清理。
14. 编译增加框架内部安全超时（默认 4 小时，可由部署环境调整，非用户/项目配置），超时/取消终止 Windows 子进程树并返回 `BUILD_TIMEOUT`。本地仿真 Lease 增加执行 token/PID 锁：活进程存在时重启后的 Connector 只观察，不重复启动 Selena；旧进程已死时允许接管。
15. 批量本地任务只有 `selena_failed` 这类 Selena 内部逐输入失败可以形成 partial；paramconfig、loader、依赖、timeout 和 runner contract 等外围失败必须使 Stage 失败。Cluster 状态网关中断可重试 collect 且不重复提交；非空 MF4 没有任何 `result.ini` 不再判成功；大批量扫描截断会有界复扫；共享路径不可达返回 `CLUSTER_SHARED_DATA_UNAVAILABLE`。
16. 数据传输重试跳过已经持久化完成的 role，防止重复复制/重复仿真；多 MF4 mixed source 的候选和选择依据贯穿 Transfer/Cluster 证据，同时仍按用户合同自动选择一个继续运行。结果 ZIP/Manifest 缺失不能形成业务成功。
17. `serve-v1` 增加服务端维护循环：启动即回收一次、之后默认每 30 秒检查失联或失去执行归属的任务。只有新鲜心跳明确报告 `current_task_id`（兼容旧 Connector 的 `busy + empty task`）才证明 Stage 仍在执行；在线但 idle/已切换其它任务的孤儿 Stage 在 30 秒交接保护后同样回收。已请求取消的 Stage 最终落为 `cancelled`；普通 Stage 仍按最多 3 次的既有策略重排/失败，避免任务永久停在 `running/cancelling`。周期、阈值、交接保护和重试上限只由部署环境变量控制，不进入用户 YAML。
18. `existing + cluster` 在 Selena、runtime、数据和配置均位于 Cluster 可见共享/central namespace 时，完全跳过 Windows resolver/build/register，直接由 Linux/Cluster 环境检查、预检和提交；无需 Connector，也不加载项目规则。逻辑 `config-asset:*` 仍通过成熟的小配置复制链处理，不把 Linux 内部引用冒充 worker 可见路径。

### 自动化证据

- V2 集成门禁：`246 passed, 1 skipped, 1 warning`。
- identity/API/SDK/control-data 合同复验：`112 passed, 1 warning`。
- 全仓最终回归：`1553 passed, 12 skipped, 1 warning`，零失败，耗时 `384.27 s`。
- 幂等修复后全仓最终回归：`1556 passed, 12 skipped, 1 warning`，零失败，耗时 `429.94 s`。
- project-free 输出推导修复后全仓回归：`1557 passed, 12 skipped, 1 warning`，零失败，耗时 `502.79 s`。
- Connector v9/全链异常矩阵聚焦回归：`309 passed, 4 skipped`、`203 passed, 1 skipped`、身份安装 `103 passed`，均零失败。
- v9 第一次全仓门禁：`1581 passed, 12 skipped, 1 warning`，仅 1 个旧测试夹具因直接注册 Windows Agent 未写 owner 被新的生产隔离合同拒绝；夹具已改为真实注册形态，相关隔离回归 `82 passed`。
- Connector v9/全链收口最终全仓门禁：`1582 passed, 12 skipped, 1 warning`，零失败，耗时 `543.12 s`。唯一 warning 仍是 Starlette/httpx 弃用提示。
- 最终复审补充本地 Lease 过期接管保护后再次全仓回归：`1583 passed, 12 skipped, 1 warning`，零失败，耗时 `534.36 s`。
- 服务端自动维护、共享 existing Selena 无 Connector 闭环及全部叠加修改最终全仓门禁：`1591 passed, 12 skipped, 1 warning`，零失败，耗时 `405.28 s`；共享路径/逻辑资产扩大回归 `142 passed, 1 warning`，在线孤儿 Stage 归属修复后的服务维护/回收回归 `67 passed`。
- Linux 候选 release 平台无关门禁：`78 passed`；其中 TransferPlan 幂等、API、Cluster Stage 均通过。
- 唯一 warning 是 Starlette/httpx 弃用提示，不是业务失败。

## 3. 已有真实验收证据

### 本地仿真已通过

- Job：`job_1ebbef262a89`
- 输入：
  - `D:\data\byd\CRGVBYDPF-13086\0729\Gen5_2026-07-28_17-22_0118.MF4`
  - `C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo`
  - `C:\tools\Runtime_For_byd_ovrs25_bl16rc71_al2.xml`
- 结果：Job/Manifest succeeded，1/1 输入成功；输出 MF4 `239,051,624` bytes，SHA-256 `1a75992f5a87e543606b4d7831683f198d930d6e2e8cec412f242ebd42fbd440`。
- SDK ZIP 与本地结果目录已交付到 `D:\RadarSim\v2-results\job_1ebbef262a89`。

### Cluster 仿真已通过

- 旧阻塞 Job `job_a6cd945004f9` 仍保留：当时 `SZHRADAR01:8123` 不可达，正确停在 `environment_check`，没有误传文件或伪装成功。
- 恢复后真实 Job `job_bcf8bd2f1dbe` 已在 release `f20df78` 完成 `existing + cluster`：1/1 输入成功，9 个结果文件，Cluster run `cluster-run:5409fd5266f84876acbcd22200484299`。
- 数据集由 Windows Connector 直接写 Cluster 数据面：`443,266,984` bytes，SHA-256 `1c7bbbe1703da67e16ee7299181613333df4abbcba8337e6c81eb3462f86d23b`；Runtime bundle、Runtime XML、MatFilter 也分别完成直传。Linux 只保存计划、进度和引用，没有接收大文件正文。
- ResultCatalog 引用 `result:sha256:87db2f82a54b3811411b212725984065134d988e8a9653192c7ef93e17467fb1`；SDK 下载 ZIP `12,173,015` bytes，SHA-256 `4f59686ad2e767d918d4635768ea7ce57df1a787491561f3251efe68b7ba9e8e`。
- 验收中发现 SDK 请求被中止后会为同一 Job 创建重复 TransferPlan；未影响成功任务，两个孤立计划已取消。通用修复已在 `3cd10ae` 发布并由 6 线程并发测试验证，不是针对本 Job 的点修。

## 4. 当前发布状态与边界

### 当前线上事实

1. `7020321` 已推送至 `origin/codex/new-branch` 并部署为不可变 release `/home/hoz2wx/radar-sim-7020321`；前一不可变 release `8f8601c` 保留用于回滚。
2. 正确的用户级 `radar-sim-v1.service` 为 `active/running`，`NRestarts=0`；健康接口返回 `ok=true`。系统级同名 unit 未启用，排查时不要查错作用域。
3. Windows Connector v9 包为 `8,350,502` bytes，SHA-256 `0f9e299c8e8a5f98bd582dfe79f436708037b660f6aab1759e391950bd4bcf12`；Range 下载实测返回 `206` 与 `bytes 0-1023/8350502`。候选 release Linux 门禁 `77 passed, 1 skipped`；服务切换后 `active`、`NRestarts=0`、health `ok=true`，Cluster 四个角色 worker 均在线。
4. Connector 执行合同已升级为 9。旧 Connector 会被服务端阻止领取任务；用户再次运行 Web 的“一键连接/更新本机”会保留 Agent ID、路径绑定和自启方式，并把旧 `web-*` 随机 owner 一次性迁移到稳定 `user-<NTID>`。代码和自动化已通过，真实新用户电脑仍需再运行一次安装包作为最终外部验收，不能在此之前写成真实安装已通过。
5. 历史孤儿任务 `job_81f44ccae6c4` 曾因旧 Connector 在线但已不持有该 task 而永久停在 cancelling；`7020321` 上线后维护循环按精确 `current_task_id` 识别并自动收口为 `cancelled`，未手改数据库。

### 明确边界，不得伪装成已完成

- 当前 Sprint 按产品决定关闭登录令牌。`X-Rsim-User` 只是受信内网 owner 路由标签，不是认证；可以验证并发和逻辑隔离，不能宣称可抵御恶意冒充。正式对不受信用户开放前必须启用 Bearer/反向代理认证并验收跨 owner 拒绝。
- `source_to_local`、Cluster 结果反向直传/解压到任意 Connector、独立 MCP Server/Skill 包、关机设备远程唤醒均未开放。本轮只保证 SDK/API 是未来 AI 工具的稳定底座。
- Shared zero-copy、Linux SDK POSIX 本地源和 MatFilter 留空推导已有自动化证据；生产真实 mount、Linux 调用机和 MatFilter→Cluster Config.cfg 仍需要环境级 smoke，不能用单元测试替代。

## 5. 后续独立 Sprint（不影响当前 project-free existing Selena 首版）

这些不是当前已知主链阻断，但在扩大批量和项目覆盖前应处理：

1. `build + local`、`build + cluster` 在代码和自动化层已具备，仍需要在最终 release 上补充真实脚本的黑盒证据；实现时不得引入项目识别，唯一执行依据是用户编译脚本及其实际产物。
2. 多 MF4 source 已使用有界元数据推导；显式 `simulation.source` 永远优先。后续可增强 mixed-source 的逐条呈现，但不得引入项目映射。
3. Cluster collect 对首次扫描截断且未发现 `result.ini` 的情况已增加一次最多 10000 文件的有界复扫；Web/诊断的超大批量展示仍应增加显式 truncation 元数据。
4. 登录鉴权、Cluster 结果反向直传/解压、独立 MCP/Skill 包和关机唤醒属于独立版本，不得塞回当前薄层主链。

## 6. 下一位开发者启动步骤

1. 阅读本文件和文首五份权威文档。
2. 运行 `git status --short`，保护用户和其他 Agent 的未提交修改；正常起点应为干净工作区。
3. 对照生产 health、用户级 systemd unit 与 `git rev-parse HEAD`，不要误查系统级 unit，也不要假设工作区已部署。
4. 先执行与改动范围相关的 V2 合同测试；完成前执行全仓测试。
5. 真实任务必须区分：外围框架失败、Cluster 基础设施失败、Selena 内部逐输入失败。只修外围框架，不前置否决用户选择的 Runtime、分支或 runnable。
6. 每次部署使用不可变 release，切换前确认无 running/queued 任务；保留上一版回滚目录和 unit 备份。

## 7. 详细证据索引

- `docs/handoffs/2026-08-13-v2-final-gap-audit.md`：本轮目标矩阵、缺口和验收模板。
- `docs/handoffs/2026-08-11-business-convergence-master.md`：统一 Connector、资源路由、真实本地任务及此前 Cluster 阻塞的详细记录。
- `docs/handoffs/2026-08-11-result-delivery-mcp-acceptance.md`：结果目录、SDK 取件及 MCP/Skill 边界。
- `docs/handoffs/2026-08-11-multiuser-connection-audit.md`：无认证部署的安全边界，仅作审计证据。
- `docs/handoffs/identity-unification.md`、`cluster-concurrency.md`、`resource-routing.md`：身份、并发和资源路由切片。

Git 历史保留了本文件被精简前的全部旧实施日志；旧日志不再作为当前产品状态。
