# radar-sim V2 最终 Gap 收敛审计

> 审计日期：2026-08-13
> 审计范围：`PRD.md`、`docs/PRODUCT_CONTRACT.md`、`docs/V2_ARCHITECTURE.md`、`docs/handoffs/2026-08-11-business-convergence-master.md` 及当前工作区可见证据
> 文档状态：可追加（append-only）；本文件只记录审计结论，不改变产品合同、代码或历史 Job 记录。

## 1. 结论先行

当前 V2 已完成单轨配置、Web/SDK 共用调度合同、资源路由/传输内核、统一 Connector、结果 Manifest/ZIP 和 Linux 控制面收口；并且已经有一条真实本地端到端成功证据：`job_1ebbef262a89`。

但“V2 四条业务路径全部真实交付”尚未收敛。当前唯一已记录的 V2 Cluster 黑盒任务 `job_a6cd945004f9` 在 `environment_check` 阶段因外部 `SZHRADAR01:8123` Manager XML-RPC/Submit path 不可达而失败，未进入传输或 Selena 执行。因此本文件不把今天的 Cluster 记为通过，也不把自动化测试或旧版本历史记录替代当前 release 的 Cluster 证据。

截至本审计，准确状态是：

- `existing + local`：真实通过；
- `existing + cluster`：当前真实尝试被外部 Cluster Manager 阻塞，待恢复后重提；
- `build + local`、`build + cluster`：有合同/自动化证据，当前 master handoff 未记录最终版本的真实黑盒通过；
- no-auth：是用户延后令牌后的受信内网边界，不是企业级认证完成；
- `source_to_local`、Cluster 结果反向直传、可安装 radar-sim MCP/Skill、关机唤醒 Connector：明确延后/非首版能力，不得在收敛报告中伪装成已完成。

## 2. 证据等级和权威顺序

本审计使用三档证据，避免“代码存在”被误写成“真实交付”：

| 等级 | 含义 | 本文用语 |
|---|---|---|
| A | 当前 release 的目标环境真实 Job/部署/结果证据，含可复核的 Job、Stage、Manifest 或校验和 | **真实通过** |
| B | 当前仓库代码、合同测试、定向回归、编译/语法检查 | **实现/自动化通过，未等同黑盒** |
| C | 外部依赖、明确非目标或尚未实现的能力 | **待外部动作/明确缺口** |

文档冲突时沿用产品约定：`docs/PRODUCT_CONTRACT.md`（用户合同）→ `PRD.md`（需求）→ `docs/V2_ARCHITECTURE.md`（V2 架构/矩阵）→ 本审计与 `docs/handoffs/2026-08-11-business-convergence-master.md`（实施证据）。旧 `HANDOFF.md` 中的历史版本描述不能覆盖当前 V2 证据。

## 3. 目标逐项收敛矩阵

| 目标/验收项 | 当前状态 | 当前证据 | 剩余 Gap / 完成动作 | 等级 |
|---|---|---|---|---|
| V2 单轨入口与 `UserRunConfig 2.0` 严格校验 | 已完成 | `PRD.md` §2/§4；`docs/V2_ARCHITECTURE.md` §2/§3；`UserRunConfig` 对未知字段拒绝；旧 project/profile/recipe 用户字段已移除 | 发布前再做一次 Web 导入→导出→SDK 往返样例留档；不得恢复旧入口 | B |
| Web 与 SDK 同一 YAML、同一 Job/Stage/DAG/Manifest 合同 | 合同与自动化完成；最终 Web 黑盒仍需补强 | V2 API/SDK/Web/transfer 定向组：`308 passed, 1 skipped, 1 warning`、`224 passed, 5 skipped, 1 warning`；OpenAPI：`1 passed, 1 warning` | 用当前最终 release 的新鲜 Web owner 做一次“一键连接→提交同 YAML→结果下载”纵向记录；没有该记录时只称合同一致 | B |
| `existing + local` | 真实通过 | 真实 Job `job_1ebbef262a89`：已有 Selena、Runtime XML、确定性 MatFilter、单条 MF4；所有执行 Stage 成功，`run_simulation=0`；Manifest `succeeded`、delivery `delivered`；输出 `239,051,624` bytes，SHA-256 `1a75992f5a87e543606b4d7831683f198d930d6e2e8cec412f242ebd42fbd440`；SDK ZIP/目录落在 `D:/RadarSim/v2-results/job_1ebbef262a89/` | 如发布门禁要求四组合均为当前版本黑盒，仍需独立记录 `build + local`；不重复宣称此 Job 覆盖编译路径 | A |
| `build + local` | 实现/自动化完成，真实未闭环 | V2 build script、输出推导、依赖环境恢复、通用 ParamConfig 已有代码和定向测试；master 只记录了 existing 本地真实成功 | 在更新后的 Connector v8 和最终 Linux release 上，使用真实用户工作区/脚本跑一次：编译产物（exe+DLL）→本地仿真→Manifest/结果目录；记录脚本、产物、Job 和退出码 | B→A 待补 |
| `existing + cluster` | 当前真实尝试被外部阻塞 | 真实 Job `job_a6cd945004f9`：V2 `valid/ready`、显式 `target=cluster`、MatFilter 已推导、Connector 在线且为当前合同；在 `environment_check` 失败，错误码 `CLUSTER_ENVIRONMENT_UNAVAILABLE`，原因 `Manager XML-RPC port: unavailable; Submit path: unavailable`；Linux 与 Windows 均探测 `SZHRADAR01 (10.54.5.71):8123` closed；未发生 transfer/Selena execution | 恢复标准 Cluster Manager XML-RPC/Submit path 后，按同一配置重提；必须见四角色 transfer completed、Cluster run、逐条 Manifest、结果 ZIP/校验和。当前不能写“Cluster 已通过” | C→A 待外部 |
| `build + cluster` | 实现/合同测试完成，真实未闭环 | V2 编译后直传 Cluster 的 route/TransferPlan/Cluster submit 逻辑已有代码与测试 | 依赖上行 Manager 恢复；先真实 `build + cluster` 或按发布矩阵顺序补齐，保留构建产物、四角色传输、Cluster run、结果证据；不能用 `existing + cluster` 代替 | B→A 待补 |
| Linux 不接收大文件正文；源到目标直传/共享零复制 | 内核和合同已实现；本轮 Cluster Job 未因外部阻塞而产生传输证据 | `docs/V2_ARCHITECTURE.md` §6/§11；master 记录 direct-transfer barrier、只保存签名计划/进度/path-free Manifest；`job_a6cd945004f9` 证明 Manager 不可用时 `TransferPlan=0`，不会盲传 | Cluster 恢复后至少抓取/审计一次真实四角色传输：目标、进度、校验、幂等重试、Linux API 无正文；当前不把“未启动传输”误写为“本轮直传成功” | B；A 待补 |
| 单条/批量、失败隔离、部分成功 Manifest | 合同/测试有覆盖；最终 V2 真实批量证据未在 master 中闭环 | `PRD.md` §5.5、`docs/V2_ARCHITECTURE.md` §6/§11 要求逐条独立；测试组覆盖 Manifest/错误归一化 | 用至少两条 MF4 的真实批次验证一条成功、一条失败仍保留两条结果和稳定诊断；明确记录不是 Selenium/引擎内部问题时的外围错误 | B→A 待补 |
| 统一 Connector 首装、持久、自启、重连、更新、单实例 | 当前用户/当前设备已验证；跨设备最终黑盒仍需留档 | master 记录 contract v8、one-click update、持久 owner/device、自注册恢复；`f7e0bc5` 发布后 Connector 自动重注册；本地 Job 使用更新后的 Connector | 若发布门禁要求“新用户”证据，选全新 Windows owner 做首装/重启/断线/更新/单实例实测；否则保留为已实现 + 当前设备实证 | B→A 待补 |
| owner/job/path/log/result 隔离与 Cluster 有界并发 | 受信内网下逻辑隔离/自动化完成 | 稳定 `user-<lowercase NTID/OS login>`；owner/device/root 授权、bounded pools、heartbeat/stale recovery、owner-fair ordering；master 定向组 `211 passed, 1 warning` 等 | no-auth 未形成不可伪造的企业身份；若开放不可信网络，必须先完成 Bearer/SSO、owner 由服务端认证派生、配额/审计/反伪造 | B；安全边界 C |
| 结果目录、ZIP、Manifest 真值 | 本地真实通过；Cluster 当前待复验 | `job_1ebbef262a89` 有 path-free Manifest、`delivered`、ZIP 和可消费 MF4；结果 ZIP 使用唯一临时文件原子发布 | Cluster Job 成功后再记录 ResultCatalog/ZIP/校验；Cluster 反向落用户 `result.path` 仍未开放，不能从 ZIP 通过推导出本地物化完成 | A（local）；C（Cluster reverse） |
| Linux release/服务本身 | 当前 release 已部署且稳定 | master：`f7e0bc5` immutable release，systemd `active/running`、`NRestarts=0`；post-fix server focused suite `84 passed`；此前 release-gate 组和 compile/node/git diff 检查通过 | 外部 Manager 不属于 radar-sim 进程；Manager 恢复后只需重跑上述真实 Cluster 门，不绕过 `environment_check` | B（服务）；C（外部 Manager） |
| 新项目不增加 project 配置 | V2 设计与自动化已完成；编译真实泛化边界仍是用户脚本/工具链 | `docs/V2_ARCHITECTURE.md` §5/§10；匿名 `execution_identity` 仅授权/缓存/追踪；项目名不得选择参数 | 新项目的 build 仍须脚本能表达输出且 Windows 依赖满足；不能承诺“任意项目无需脚本/环境即可编译” | B |
| 纯浏览器 Linux 本地文件、远端→本地 `source_to_local` | 明确不支持/未发布 | 架构 §6/§10、产品合同 §3.1：不可经 Linux 大文件中转；无目标 Windows 受控缓存和目标 Agent 授权时返回 `source_to_local_unavailable` | 保持稳定 unsupported/needs-input 诊断；未来若重新纳入范围，补目标设备授权、TransferPlan、缓存清理、恢复和黑盒测试 | C |
| Cluster→用户设备结果反向直传 | 明确未发布 | `docs/V2_ARCHITECTURE.md` §9/§10；当前只保证 owner-scoped ResultCatalog/ZIP | 未来补目标设备授权和反向 TransferPlan；禁止用 Linux API 搬运结果正文或把 Cluster staging 当本地缓存 | C |
| 可安装 radar-sim MCP/Skill | 明确未发布 | `PRD.md` §7、master/现状说明：只有 Python SDK 合同，仓库无可安装 MCP Server/Skill | 未来只薄封装 `RadarSimClient` 校验/提交/查询/诊断/重试/取消/下载，不复制调度或传输 | C |
| 关机/睡眠状态远程唤醒 Connector | 首版非目标 | `PRD.md` §7、`docs/V2_ARCHITECTURE.md` §10 | 保持等待/重连语义；不把离线电脑写成外部故障 | C |
| legacy 公共命令/API 清理 | 非主链阻塞项，需单独收尾 | master 将 unreachable legacy module quarantine/public-route inventory 列为后续工作，不应阻塞 V2 主链验证 | 完成公开路由库存后再删除/隔离；不在本审计中修改实现或删除历史兼容代码 | B→后续 |

## 4. 真实证据台账（不可互相替代）

### 4.1 本地真实通过：`job_1ebbef262a89`

- 版本上下文：master 记录的 runtime-environment 修复、Connector contract v8 和 immutable Linux release；任务使用当前用户已有 Selena、Runtime XML 和本地数据。
- 输入：`Gen5_2026-07-28_17-22_0118.MF4`；MatFilter 从受控 repository-adjacent 根确定性推导；没有项目名/注册表适配器参与选择。
- 执行：所有实际执行 Stage 成功；`build`/`register` 因 `source=existing` 如实跳过；`run_simulation` 返回 0。
- 输出：`outputs/0001-Gen5_2026-07-28_17-22_0118--out.MF4`，`239,051,624` bytes，SHA-256 `1a75992f5a87e543606b4d7831683f198d930d6e2e8cec412f242ebd42fbd440`。
- 交付：Manifest `succeeded`，delivery `delivered`；SDK ZIP 及直接可消费输出写入 `D:/RadarSim/v2-results/job_1ebbef262a89/`，Manifest 不含物理路径。
- 期间一次幂等 `GET` 出现 `httpx.RemoteProtocolError: Server disconnected without sending a response`；服务未重启（同 MainPID、`NRestarts=0`），SDK 后续仅对 `GET/HEAD` 做最多三次有界重试，状态变更请求保持单次，避免重复建 Job。该异常不改变本 Job 成功结论。

该证据只覆盖 `existing + local`，不能外推到 build 或 Cluster。

### 4.2 Cluster 阻塞：`job_a6cd945004f9`

- V2 校验 `valid/ready`，显式选择 `target=cluster`；同一 MatFilter 已推导；统一 Windows Connector 在线且合同为最新。
- 失败 Stage：`environment_check`；错误码 `CLUSTER_ENVIRONMENT_UNAVAILABLE`；公开原因：`Manager XML-RPC port: unavailable; Submit path: unavailable`。
- 外部探针：Linux 控制主机与 Windows submitter 均确认 `SZHRADAR01 (10.54.5.71):8123` closed；SMB 软件/数据挂载仍可读写；Linux 服务未重启。
- 影响范围：任务在任何大文件传输和 Selena 执行前结束；下游被取消，保留稳定 retry action；因此该 Job 不是 Cluster 仿真失败，也不是 Cluster 成功，而是 Cluster Manager 外部依赖不可用。

在标准 Manager XML-RPC 恢复前，任何新 Cluster 任务都应先停在同一环境门禁；不能绕过检查、手工伪造 transfer completed 或把旧版本/旧 Job 当作当前 release 的 Cluster 通过。

## 5. no-auth 的明确边界（用户延后令牌）

当前部署的 `authentication_required=false` / `--insecure-no-auth` 是用户明确选择“令牌后延”后的**受信内网试用边界**。它的含义必须固定为：

1. `X-Rsim-User` 和稳定 `user-<lowercase NTID/OS login>` 只负责 owner 路由、逻辑隔离和任务归属；它不是不可伪造的身份认证，也不是授权令牌。
2. 当前实现可在受信 Bosch 内网中支持 owner/job/device/root 的逻辑隔离、Connector 领取约束和有界并发；不能把它表述为对不可信用户开放的企业级多租户安全边界。
3. 服务不得暴露到公网或不受信网络；审计、配额、公平性和恶意调用防护仍不等同于已完成的 SSO/Bearer 鉴权。
4. 若用户重新启用令牌/SSO 要求，收敛动作是：由服务端从有效 Bearer/SSO 身份派生 owner，加入令牌过期/撤销、跨接口权限检查、审计和反伪造测试；不能继续把 header 自报值当认证。

因此，no-auth 是当前**有意保留的边界**，不是本轮“漏做后被误标完成”的缺陷；但它是发布到不受信网络前的硬阻塞条件。

## 6. 完全收敛的最小动作清单

以下动作完成后，才可以把“当前 V2 四路径真实发布”标记为收敛；每项都要附 Job/Stage/Manifest/校验或环境探针，不以代码存在替代：

1. **恢复外部 Cluster Manager**：确认 `SZHRADAR01:8123` XML-RPC/Submit path 从 Linux 与 Windows 均可达；保留时间戳和探针结果。
2. **重提 `existing + cluster`**：沿用 `job_a6cd945004f9` 的同一份 V2 配置或等价配置；确认 `environment_check` 通过、四角色（`dataset`、`runtime_bundle`、`runtime_xml`、`mat_filter`，以及实际需要的 `adapter`）传输/登记完成、Cluster run 终态、逐条 Manifest、ZIP/校验和。
3. **真实补齐 `build + local`、`build + cluster`**：使用真实用户脚本和工作区，记录编译产物、Connector contract、任务日志和结果。Cluster 组合必须再次证明正文不经过 Linux API。
4. **真实批量部分成功**：至少两条输入，一条成功、一条失败；确认其余输入继续运行，Manifest/诊断与结果目录分别可解释。
5. **如发布需要 Web 强证据，补一个全新 Web owner**：一键连接、提交同一 YAML、等待任务、下载结果；记录 owner/device 隔离及不重复建 Job。
6. **重新执行发布门禁**：定向回归、`py_compile`、前端 `node --check`、`git diff --check`；全量测试若超时/无终态，必须原样记录，不得写成全量通过。
7. **复核 no-auth 运行范围**：继续保持受信内网说明；在令牌未启用前不得公开部署。令牌/SSO、`source_to_local`、Cluster 反向结果、MCP/Skill 等属于独立后续发布，不应和当前 Cluster 外部阻塞混成一个失败原因。

## 7. 今日 Cluster 验收追加区（append-only）

本节专门留给 2026-08-13 及之后的真实 Cluster 验收。追加新记录时不要覆盖 §4.2 的 `job_a6cd945004f9` 阻塞事实；若 Manager 仍不可用，追加探针和“仍阻塞”即可，不能改写为通过。

### 追加模板

```text
### <YYYY-MM-DD HH:MM Asia/Shanghai> — <通过/仍阻塞/部分通过>

- release/commit：<immutable release path + commit>
- Linux health/systemd：<health、MainPID、NRestarts>
- Cluster probe：<SZHRADAR01:8123 from Linux + Windows; open/closed; timestamp>
- V2 config fingerprint：<不写用户文件正文；记录 schema/source/target 与安全摘要>
- Job：<job_id>
- environment_check：<succeeded / CLUSTER_ENVIRONMENT_UNAVAILABLE / ...>
- Transfer：<各 role 的 plan/status/bytes/checksum；是否有 Linux 正文>
- Cluster run：<cluster-run id、submit/status/log 摘要>
- Manifest：<status、success_count、failed_count、file_count、diagnosis>
- Result：<result_ref、ZIP bytes/SHA-256、可下载/可物化路径>
- 结论：<只写本条证据覆盖的路径；不外推到其他组合>
- 未完成动作：<如有>
```

追加记录的最低证据要求：环境探针必须来自 Linux 和实际 Windows submitter；成功必须同时具备任务终态、逐条 Manifest、结果引用/校验；若只到 `environment_check` 或只生成 TransferPlan，结论只能是“未进入仿真/仍阻塞”。

## 8. 审计结论

在 `job_1ebbef262a89` 的真实本地成功和 `job_a6cd945004f9` 的可解释外部阻塞之间，V2 的代码/合同边界已经足够明确，剩余工作不是继续扩展产品概念，而是补齐当前 release 的目标环境证据并保持未发布能力的诚实边界。没有新的 Cluster Manager 恢复与真实 Job/Manifest 证据前，本项目应标注为：

> **V2 implementation converged; local black-box passed; Cluster black-box externally gated; no-auth trusted-intranet trial only.**
