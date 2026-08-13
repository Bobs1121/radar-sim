# radar-sim V2 当前交接

> 更新时间：2026-08-13
> 状态：V2 单轨收敛与真实发布验收进行中
> 分支：`codex/new-branch`
> 当前生产基线：`f7e0bc5`，Linux release `/home/hoz2wx/radar-sim-f7e0bc5`
> 当前工作区：含尚未提交、尚未部署的 V2 收口修改；不得把本文件中的“自动化通过”解释为生产已更新

本文是下一位开发者的唯一实时入口。历史长篇实施日志已从根 handoff 删除；需要追溯时使用 Git 历史和 `docs/handoffs/` 中带日期的证据文件。产品和架构决策依次以 `docs/PRODUCT_CONTRACT.md`、`PRD.md`、`docs/V2_ARCHITECTURE.md`、`docs/DETAILED_DESIGN.md`、`DEVELOPMENT_PLAN.md` 为准。

## 1. 当前产品合同

radar-sim 是外围自动化脚手架，不实现 Selena 内部仿真，也不安装 Visual Studio 或替用户维护本地仿真环境。

- 公共配置只有 `UserRunConfig 2.0`。Web、Python SDK 和 REST API 使用同一配置、同一调度核心和同一 Job/Stage/Manifest。
- 用户不配置业务项目、profile、recipe、Runtime Bundle、Agent ID、Cluster 拓扑或共享盘类型。
- Selena 来源只有 `build` 与 `existing`；仿真目标只有 `auto`、`local`、`cluster`。
- 编译只发生在 Windows，执行用户给定的 Selena 编译脚本；默认使用用户已切好的当前工作区和本地修改，不做 checkout/reset/clean/stash。
- 本地仿真由统一 Windows Connector 在用户电脑执行。用户默认已准备 Selena 仿真环境；Connector 只做外围检查、路径准备、指令下发和结果收集。
- Cluster 仿真由 Linux 控制面调度 Linux executor/Gateway。大文件从源设备直接写 Cluster 数据面，正文不经过 Linux Web/API。
- Windows 用户只安装一个统一 Connector。一次安装、持久运行、断线重连、单实例；不再区分轻量/完整 Agent。
- 批量输入逐条记录成功/失败；单条 Selena 内部失败不能取消其它输入和结果收集。
- 本地仿真可把结果物化到 `<result.path>/<job_id>` 并保留 ZIP。Cluster 当前保证 owner-scoped ZIP/引用；反向直传并解压到任意用户设备尚未开放。

## 2. 本轮尚未提交的收口修改

### 已修复

1. `core/stage_binder.py` 分离 `workspace_binding_id` 与 data `advertised_binding_id`，防止数据 binding 覆盖工作区 binding；回归同时断言 environment 使用 workspace、prepare_data 使用 data。
2. SDK 显式 `user` 和 `X-Rsim-User` 统一到稳定 `user-<lowercase>`；Web、SDK、Connector 新请求使用相同 owner。服务端继续只为历史记录兼容旧任意标签；Bearer 部署始终以认证 principal 为 owner。
3. 删除公开的 V1 `submit_cluster_yaml()`；V2 只保留 `submit_yaml()`/`submit_run()`。
4. artifact、Runtime Bundle、result、dataset、已有 Selena 和配置资产上传都使用有界流式读取；在追加超限块前拒绝，避免无 `Content-Length` 时先分配超大内存。
5. Web/SDK 本地零传输合同测试改用真实稳定 owner，消除旧测试身份与当前 Connector 合同不一致。
6. SDK 已有 Selena 直传与 Connector 使用同一白名单，只传 `Selena.exe + DLL`，排除 PDB/ILK/LIB/EXP 和调试目录；缺少 `Selena.exe` 稳定报错。数据源探测最多读取 16 条 MF4 的轻量元数据，显式 `simulation.source` 不读 MF4；多源按产品规则稳定选择一个并保留 mixed 候选证据。
7. 全仓回归发现并清除 4 个过时断言：旧 Web 产品识别文案、已删除的 SDK `upload_run_data` monkeypatch、项目模板缺失必须失败。它们与当前 project-free、源到源传输和公共模板回退合同冲突，不是运行代码回归。
8. README、PRD、开发计划、产品合同、V2 架构和详细设计已统一到 2026-08-13 V2 单轨口径；根 handoff 已精简，旧 V1/项目化部署、配置、环境和实战文档只保留归档跳转，不再暴露冲突步骤。

### 自动化证据

- V2 集成门禁：`246 passed, 1 skipped, 1 warning`。
- identity/API/SDK/control-data 合同复验：`112 passed, 1 warning`。
- 全仓最终回归：`1553 passed, 12 skipped, 1 warning`，零失败，耗时 `384.27 s`。
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

### Cluster 旧阻塞与当前待验

- 旧 Job：`job_a6cd945004f9`。
- 当时在 `environment_check` 失败，原因是 `SZHRADAR01:8123` 不可达；没有开始传输或 Selena 仿真，不能称为 Cluster 成功。
- 2026-08-13 本机重新探测 `SZHRADAR01:8123` 已可连接。必须在本轮代码提交、不可变 Linux 部署后，用同一真实输入重新提交 Cluster 任务，记录 TransferPlan、传输 Manifest、外部 Cluster Job ID、逐输入 Manifest、ZIP checksum 和 Linux 未接收文件正文的证据。

## 4. 发布前剩余门禁

### 必须完成

1. 再跑全仓测试，零失败。
2. 审核并提交当前 diff，推送 `origin/codex/new-branch`。
3. 在 `10.190.171.44` 创建新的不可变 release，切换 `radar-sim-v1.service`；确认 health、Web、SDK、systemd `active/running`、`NRestarts=0`。
4. 用上节真实输入完成已有 Selena + Cluster 的黑盒任务；确认 direct transfer 不经过 Linux body route，并取得可下载结果。
5. 复查统一 Connector contract；若本轮未改变 Connector 执行文件，无需强制用户更新。若 bundle 内容或 contract 改变，必须从 Web 一键更新并验证旧实例不领新任务。
6. 把提交、部署目录、服务状态、真实 Job ID、外部 Job ID、Manifest、ZIP checksum 追加到本文件，不覆盖失败历史。

### 明确边界，不得伪装成已完成

- 当前 Sprint 按产品决定关闭登录令牌。`X-Rsim-User` 只是受信内网 owner 路由标签，不是认证；可以验证并发和逻辑隔离，不能宣称可抵御恶意冒充。正式对不受信用户开放前必须启用 Bearer/反向代理认证并验收跨 owner 拒绝。
- `source_to_local`、Cluster 结果反向直传/解压到任意 Connector、独立 MCP Server/Skill 包、关机设备远程唤醒均未开放。本轮只保证 SDK/API 是未来 AI 工具的稳定底座。
- Shared zero-copy、Linux SDK POSIX 本地源和 MatFilter 留空推导已有自动化证据；生产真实 mount、Linux 调用机和 MatFilter→Cluster Config.cfg 仍需要环境级 smoke，不能用单元测试替代。

## 5. 后续鲁棒性 backlog

这些不是当前已知主链阻断，但在扩大批量和项目覆盖前应处理：

1. 多 MF4 且 acquisition source 不同：当前 source 自动推导主要使用第一条输入证据；需定义并测试 mixed-source 行为，不能静默误选。
2. SDK 的已有 Selena 目录扫描与 Connector 扫描规则需持续保持一致，只传 `Selena.exe + DLL + Runtime/显式配置`，不得带 PDB/ILK/LIB/调试目录。
3. Cluster collect 当前存在 50/200/500 条展示或扫描上限；大批量必须返回明确 truncation 元数据，不能让 Manifest 看似完整。
4. 增加同一 Job 的本地结果目录与 ZIP 并存、Range/并发下载、损坏 `result.ini` 和超大批量的稳定诊断测试。

## 6. 下一位开发者启动步骤

1. 阅读本文件和文首五份权威文档。
2. 运行 `git status --short`，保护用户和其他 Agent 的未提交修改。
3. 对照生产 health 与 `git rev-parse HEAD`，不要假设工作区已部署。
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
