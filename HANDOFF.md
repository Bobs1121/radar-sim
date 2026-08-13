# radar-sim V2 当前交接

> 更新时间：2026-08-13
> 状态：V2 project-free 单轨首版已发布；本地与 Cluster 真实验收已通过
> 分支：`codex/new-branch`
> 当前生产基线：`8f8601c`，Linux release `/home/hoz2wx/radar-sim-8f8601c`
> 回滚基线：`3cd10ae`，Linux release `/home/hoz2wx/radar-sim-3cd10ae`

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

### 自动化证据

- V2 集成门禁：`246 passed, 1 skipped, 1 warning`。
- identity/API/SDK/control-data 合同复验：`112 passed, 1 warning`。
- 全仓最终回归：`1553 passed, 12 skipped, 1 warning`，零失败，耗时 `384.27 s`。
- 幂等修复后全仓最终回归：`1556 passed, 12 skipped, 1 warning`，零失败，耗时 `429.94 s`。
- project-free 输出推导修复后全仓回归：`1557 passed, 12 skipped, 1 warning`，零失败，耗时 `502.79 s`。
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

1. `8f8601c` 已推送至 `origin/codex/new-branch` 并部署为不可变 release `/home/hoz2wx/radar-sim-8f8601c`。
2. 正确的用户级 `radar-sim-v1.service` 为 `active/running`，`NRestarts=0`；健康接口返回 `ok=true`。系统级同名 unit 未启用，排查时不要查错作用域。
3. Windows Connector 包为 `8,336,982` bytes，SHA-256 `1e1daea6bcb8f0da1705b4377329959e94b704b7411747d2619e2d686207cf3f`；Range 下载返回 `206`，现有 Connector 在服务重启后自动恢复轮询。
4. 本次 Connector 执行合同未升级，普通用户不需要为了 TransferPlan 服务端幂等修复重新安装；Web 的一键连接/更新入口继续使用同一统一安装包。

### 明确边界，不得伪装成已完成

- 当前 Sprint 按产品决定关闭登录令牌。`X-Rsim-User` 只是受信内网 owner 路由标签，不是认证；可以验证并发和逻辑隔离，不能宣称可抵御恶意冒充。正式对不受信用户开放前必须启用 Bearer/反向代理认证并验收跨 owner 拒绝。
- `source_to_local`、Cluster 结果反向直传/解压到任意 Connector、独立 MCP Server/Skill 包、关机设备远程唤醒均未开放。本轮只保证 SDK/API 是未来 AI 工具的稳定底座。
- Shared zero-copy、Linux SDK POSIX 本地源和 MatFilter 留空推导已有自动化证据；生产真实 mount、Linux 调用机和 MatFilter→Cluster Config.cfg 仍需要环境级 smoke，不能用单元测试替代。

## 5. 后续独立 Sprint（不影响当前 project-free existing Selena 首版）

这些不是当前已知主链阻断，但在扩大批量和项目覆盖前应处理：

1. `build + local`、`build + cluster` 在代码和自动化层已具备，仍需要在最终 release 上补充真实脚本的黑盒证据；实现时不得引入项目识别，唯一执行依据是用户编译脚本及其实际产物。
2. 多 MF4 source 已使用有界元数据推导；显式 `simulation.source` 永远优先。后续可增强 mixed-source 的逐条呈现，但不得引入项目映射。
3. Cluster collect 当前存在 50/200/500 条展示或扫描上限；超大批量需增加明确 truncation 元数据。
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
