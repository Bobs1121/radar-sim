# radar-sim 另一 AI 审查复核与新用户真实稳定性测试报告

日期：2026-08-17  
范围：复核另一 AI 在 `785fce4`、`0a06c01`、`9e18d70` 上的审查/加固，并用真实 Web、SDK、Windows Connector、Linux 服务和既有 Job 做新用户与稳定性测试。  
结论性质：这是当前证据下的上线判断，不等同于所有 Cluster、双认证用户、250+ 文件和全新 Windows 机器场景都已通过。

## 1. 结论先行

当前结论：**核心单用户本地链路可用；正式多用户和完整新用户生产链路仍不能判定通过。**

- 另一 AI 的代码审计和 P1/P2 加固总体没有把 P0 认证问题掩盖掉，审计主报告仍正确写明：当前无认证部署只能是受信内网试用。
- 最新代码全量回归最终通过：`1651 passed, 12 skipped, 1 warning`。
- 最新代码改动范围定向回归：`213 passed, 1 skipped, 1 warning`。
- 候选 release `/home/hoz2wx/radar-sim-9e18d70` 已切换到 live service，服务 `active/running`，`NRestarts=0`；Connector 已更新，contract 15，Windows/Cluster 能力正常。
- Web 新用户、SDK 新用户、已有用户 SDK 等待和结果下载均做了真实测试。
- 发现一个真实 Web 新用户问题：新 owner 没有任务时，任务中心不断请求 `GET /api/v1/jobs?limit=20`，页面持续显示“正在加载任务”，不落到“当前没有任务”。
- 发现一个真实 Web 路由文案问题：用户填写 Windows 本地代码/脚本、没有连接 Windows Agent、目标为自动时，页面显示“配置有效、当前将使用 Cluster”，同时又提示需要 Windows；后端 readiness 实际是 `blocked/can_submit=false`，没有偷偷执行，但用户容易误解为 Cluster 能直接读取本地 C 盘并编译。
- 发现一个真实结果正确性风险：此前成功 Job 的每个 Selena 日志都报告 `Total number of signals not found: 22830`，但命令带 `--tolerant` 且进程返回 0，框架仍判定 Job succeeded。对于点云错乱问题，这不能被视为可信成功。
- 当前正式多用户仍被 P0 阻断：`authentication_required=false` 时 `X-Rsim-User` 可伪造。

## 2. 另一 AI 审查结论的复核

### 2.1 结论成立的部分

以下结论与当前代码和测试一致：

- Web、SDK、REST 使用同一 `UserRunConfig 2.0`、canonical fingerprint 和十阶段 DAG；
- `RadarSimClient.wait_job()` 已补充，事件 cursor 优先，观察超时不会取消 Job；
- SDK 结果下载有临时文件、SHA-256 校验和原子替换；
- Result Catalog 已增加过期回收和 free-space watermark 代码；
- Connector 配置文件损坏时增加 recovery copy 恢复逻辑；
- build provenance 新增 `fresh/incremental/full` 识别字段；
- cancel/success 竞态、Stage handoff 重放、Cluster receipt/Config 路径反查已有回归覆盖；
- partial 只能由真实 Selena per-input 混合结果产生，框架错误不能伪装成 partial；
- 另一 AI 已明确承认真实 SDK、双 owner、全新 Windows、真实 Cluster 长队列和 250+ 传输仍需要部署验收。

### 2.2 不能直接接受的“已完成”表述

代码级测试通过不代表以下能力在新用户环境已验证：

- `0a06c01/9e18d70` 在本次复核前没有运行在 live service；之前 live service 是 `d3de370`；
- 认证模式单测不等于正式双 owner live 验收；
- TestClient/MockTransport 不等于 SDK 真实提交、长等待、结果下载和断网恢复；
- 静态检查 Connector 安装脚本不等于全新 Windows 用户首装；
- 1000+ 单元测试不等于真实 Cluster、250+ 文件、磁盘满、UNC、杀毒软件和服务重启窗口；
- `partial` 代码路径成立不等于用户可以从 Web/SDK 只重试失败输入。当前仍没有 V2 per-input retry API。

## 3. 本次候选部署证据

- release：`/home/hoz2wx/radar-sim-9e18d70`；
- systemd：`radar-sim-v1.service`；
- `WorkingDirectory=/home/hoz2wx/radar-sim-9e18d70`；
- `ActiveState=active`、`SubState=running`、`NRestarts=0`；
- health：`ok=true`、`authentication_required=false`；
- candidate Connector package：
  `sha256:80cda2b869fa5a23b3e10389fc1204efac433d9dce6565c43ac2fdb8c0436922`；
- package headers：HTTP 200、contract 15、`content-length=8389244`；
- Connector 更新输出确认：contract 15 loaded、Unified central registration passed、watchdog registered、exact PC/user binding available；
- 保留回滚 unit：`/home/hoz2wx/.config/systemd/user/radar-sim-v1.service.bak-9e18d70`；
- 切换前后均确认没有 queued/running/needs_input Job。

## 4. 自动化测试复核

### 4.1 最新代码完整回归

```text
1651 passed, 12 skipped, 1 warning
```

第一次全量回归曾出现：

```text
1650 passed, 12 skipped, 1 failed
```

失败为：

```text
tests/test_release_deployment.py::test_hidden_launcher_runs_powershell_and_preserves_spaced_arguments
```

该测试单独重复 3 次得到 2 次通过、1 次失败；随后使用同一 `scripts/run_hidden.vbs` 做 20 次独立 smoke，结果 `20 passed`。因此判断为 Windows 隐藏启动器在系统负载/启动竞态下存在偶发不稳定，不能简单删除该失败或永久标记为环境噪声。生产安装器必须增加明确的进程存活/健康确认，而不是只依赖异步 `shell.Run(..., False)`。

### 4.2 改动范围定向回归

```text
213 passed, 1 skipped, 1 warning
```

覆盖 SDK、API、Control Stage、Result Catalog、Connector deployment、Agent build 和 Windows local E2E 相关测试。

## 5. 新用户 SDK 真实测试

### 5.1 新 owner 的首次状态

使用新 owner：`user-new-candidate-smoke`，没有修改现有用户配置，也没有提交新仿真。

真实结果：

- `capabilities()`：Windows `available=false/count=0`，Cluster `available=true/count=2`；
- `list_jobs()`：`0`；
- 读取已有用户 Job：HTTP 404 / `not_found`；
- 读取已有用户 Result：HTTP 404 / `result_unavailable`；
- 新用户下载 Connector launcher：HTTP 200，文件正常返回；
- 新用户校验 Windows local 配置：`valid=true`，但 `readiness.status=blocked`、`can_submit=false`，错误码 `windows_local_simulation_unavailable`。

这说明新 owner 不会错误领取已有用户的 Windows Agent；但由于当前是 no-auth，客户端仍可以主动伪造已有 owner，见第 8 节 P0。

### 5.2 已有用户 SDK 真实闭环

使用 `RadarSimClient` 最新代码访问 live candidate service：

- `wait_job("job_26028465ebeb")` 返回 `succeeded`；
- `download_job_result()` 返回 ZIP 大小 `32243932`；
- 实际下载 SHA-256：

```text
sha256:b2bb68288826db3fb71cf2402367cb37ebd1fff354b303e5e15e9d537b09fbb7
```

- 与服务端 Manifest metadata 一致；
- 这是一次真实的 SDK 等待和结果下载闭环，不是 MockTransport。

## 6. 新用户 Web 真实测试

使用 Playwright 打开 live Web：

```text
http://10.190.171.44:8877/
```

### 6.1 首次打开/身份/能力

页面标题和主界面正常加载。输入 `user-new-user-web-smoke` 保存后，页面显示：

- `Linux 服务已连接`；
- `当前账号尚未连接 Windows 电脑`；
- `任务需要连接这台电脑`；
- `一键连接本机`。

这部分新用户引导是正确的：没有把已有用户的 Windows Agent 显示成新用户可用。

### 6.2 任务中心空列表缺陷

点击“任务中心”后，网络请求返回正常：

```text
GET /api/v1/jobs?limit=20 -> 200
{"jobs":[],"count":0}
```

但页面持续显示：

```text
正在加载任务
```

原因在 `radar_sim_web/static/app.js` 的 `loadJobs()`：

1. 每次轮询开始时，如果 `state.jobs` 为空，先写入“正在加载任务”；
2. API 返回空列表后，`state.jobsSignature` 不变化；
3. 因为 signature 没变化，不调用 `renderJobs()`；
4. 下一轮轮询再次写入“正在加载任务”。

结果：新用户没有任务时，任务中心不会显示“当前没有任务”，而是永久像网络卡住一样。

风险等级：P2 UX/可观测性缺陷；如果用户据此判断服务不可用，会导致重复刷新、重复提交或误报服务故障。

建议修复：增加 `jobsLoaded`/`initialLoadCompleted`，或者每次 API 成功返回后都 render 空列表；只有真正未完成首次请求时才显示 loading。

### 6.3 自动路由文案缺陷

新用户填写 Windows 本地代码、编译脚本、Runtime 和本地数据，执行目标保持 `自动`，但没有连接 Windows Agent。真实 Web 检查结果：

- 页面显示“配置有效，当前将使用 Cluster 路径”；
- 页面同时显示“本地编译需要已连接的 Windows 电脑”；
- 后端实际返回 `readiness.status=blocked`、`can_submit=false`；
- 用户点击提交时会出现确认框，并提示任务会等待能力恢复；本次测试已取消确认，没有创建 Job。

当前没有错误执行，但用户容易形成错误理解：Cluster 不能直接访问用户本地 `C:/` 代码和脚本。建议 UI 把文案改为“配置格式有效，但当前不能提交：等待这台 Windows 电脑连接”，不要只显示“最终执行位置：Cluster”。

## 7. 实际稳定性测试

### 7.1 服务重启后的持久性

候选 release 切换本身执行了 systemd restart，之后：

- 服务仍 `active/running`；
- `NRestarts=0`；
- 历史 Job 仍可读取；
- 历史 Result 仍可读取；
- SDK `wait_job()` 和 ZIP 下载仍成功；
- 新 Connector 重新注册并确认 exact PC/user binding。

这证明当前已有成功 Job 的状态和结果在控制面 release 切换后仍可恢复。

### 7.2 健康接口连续访问

使用新 owner 连续访问 health 20 次：

```text
health_ok=20 bad=0
```

候选服务无重启，`NRestarts=0`。

### 7.3 尚未完成的真实稳定性场景

以下不能从本次测试推断通过：

- 正式 Bearer/SSO 双 owner live 隔离；
- 真实 250+ MF4 源到源传输；
- 真实断网后 TransferPlan 续传；
- 真实 Cluster 长队列、submit 后服务重启、结果目录晚到；
- 全新 Windows 用户从零安装且不依赖当前开发机残留；
- Defender/杀毒软件锁文件；
- partial 任务只重试失败输入；
- 结果存储实际低磁盘水位；当前 `default_result_catalog()` 没有从部署环境传入 `min_free_bytes`，新增 watermark 代码默认仍为关闭。

## 8. P0/P1/P2 风险结论

### P0：正式多用户被阻断

当前 live candidate 仍是：

```text
authentication_required=false
```

真实验证：

- 新用户读取 `job_26028465ebeb`：HTTP 404；
- 把 `X-Rsim-User` 改为 `user-hoz2wx`：HTTP 200；
- 新用户读取已有 Result：HTTP 404。

资源层隔离是有效的，但身份来源可伪造，所以不能称为正式多用户。必须启用 Bearer/SSO，并完成双 owner、双设备和结果下载交叉访问 live 验收。

### P1：仿真结果质量可能被错误判为成功

历史真实 Job `job_26028465ebeb` 的 3 个 Selena 日志均报告：

```text
Total number of signals not found: 22830
```

同时运行命令带：

```text
--tolerant
```

最终 Job/Manifest 仍是 `succeeded`。这对点云错乱问题尤其危险：进程成功退出不等于输入信号完整、点云结果正确。后续必须明确哪些缺失信号允许 warning，哪些点云/雷达关键输入必须让 Job 变成 `failed` 或 `partial`，并把缺失信号摘要写入诊断和 Manifest。

### P1：失败输入独立重试未交付

当前只有 Stage 级 retry。partial 任务的 `run_simulation` 通常已经 succeeded，无法通过现有 API/SDK/Web 选择失败 MF4 单独重跑。成功输入不重复运行的 checkpoint 逻辑存在，但用户入口和真实验收不足。

### P1/P2：真实环境覆盖不足

代码和单测覆盖较多，但以下真实门禁仍未通过：认证、Cluster、源到源大传输、断网、磁盘满、全新 Windows、partial retry。

### P2：隐藏启动器异步启动竞态

`run_hidden.vbs` 使用异步 `shell.Run(..., False)`。单独 20 次 smoke 全通过，但全量回归曾出现 marker 在 10 秒内未生成的偶发失败。安装器应通过健康检查确认 Connector 已真正启动，而不是把异步启动返回 0 当作成功。

### P2：任务中心空列表误显示 loading

不影响后端 Job，但会显著影响新用户对服务状态的判断，建议修复后加入浏览器回归测试。

## 9. 最终判定

当前候选版本：**受信内网单用户/已有用户场景可继续测试；正式多用户和新用户生产上线阻断。**

必须先处理或明确放行条件：

1. 启用真实认证，关闭生产 no-auth；
2. 修复任务中心空列表 loading；
3. 修复自动路由对本地 Windows 路径的用户文案和提交门禁；
4. 对 `--tolerant`/缺失信号建立点云关键输入质量门禁；
5. 实现 Web/SDK 的失败输入独立重试；
6. 完成双 owner、全新 Windows、Cluster、源到源传输和断网恢复真实验收；
7. 让结果 watermark 配置真正进入生产部署，而不只是代码提供可选参数。

本次审查没有修改业务代码；只做了候选部署、只读 live 测试、SDK 结果下载、Web 浏览器测试、Connector 更新和测试回归。工作区中的 `.zcode/`、`tmp-agent-home/` 是另一 AI 的本地运行痕迹，未纳入代码提交，不能作为产品交付物。

## 10. fresh Agent + UNC 数据 + 并发任务追加测试

按用户要求，将原 Windows Connector 安装根移出并保留 backup，使用新 owner 做 fresh install：

- 旧安装根：`C:\Users\HOZ2WX\AppData\Local\radar-sim`；
- 旧安装内容 backup：
  `C:\Users\HOZ2WX\AppData\Local\radar-sim-backup-fresh-20260817-185700`；
  `C:\Users\HOZ2WX\AppData\Local\radar-sim-residual-fresh-20260817-185854`；
- fresh owner：`user-fresh-agent-smoke`；
- fresh Agent ID：`agent-HOZ2WX-WX8-C-0001A-17bcd77a31b9`；
- fresh contract/status：`contract_current=true`、`available=true`、watchdog registered；
- 原始工程 `C:\BYD_OVS_CB` 和数据盘 `D:\data\...` 未删除。

### 10.1 无编译并发提交

两个 `source=existing` 任务同时提交，均使用上一任务的已有 Selena 产物和用户给出的 UNC 数据目录：

- local：`job_8583e9adc7bf`；
- Cluster：`job_36f358aa39d1`；
- 两者 `build_selena=skipped`，没有启动编译进程。

结果：

- Cluster Job 在 `environment_check` 失败：
  `CLUSTER_ENVIRONMENT_UNAVAILABLE: Manager XML-RPC port: unavailable; Submit path: unavailable`。
  任务没有进入数据传输/仿真，后续 Stage 正确取消。
- local Job 在 UNC 目录 discovery/hash 阶段长时间无进度；取消请求写入后，`prepare_data` 仍保持 running 多分钟，直到数据扫描结束后才变成 cancelled。

这暴露出两个稳定性事实：

1. `capabilities.cluster.available=true` 不能代表 Cluster Manager 当前可提交；真正的 `environment_check` 才是可信 readiness，UI/SDK 不能只依据 capability 快照允许用户认为 Cluster 可用。
2. UNC 大目录 discovery/hash 的取消响应不及时。代码传递了 cancel callback，但真实网络扫描期间仍会长期占用 Agent；需要分块取消、可取消 I/O 或将 discovery 变为可恢复 Stage，否则用户取消后会长时间看到 `cancelling`。

### 10.2 单文件 local existing 测试

为避开整目录扫描，使用同一 UNC 目录中的单个 MF4：

- Job：`job_5fc0235fb6a2`；
- `source=existing`；
- `build_selena=skipped`；
- `prepare_data=succeeded`；
- `preflight=succeeded`；
- `run_simulation=failed`；
- diagnosis：`simulation_failed` / `selena_failed`。

Selena 本地日志实际错误：

```text
boost::filesystem::status: The network name cannot be found: "\\szh-soc4.apac.bosch.com\urmszh_i_2208_089"
```

用户提交的 UNC 别名是：

```text
\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR\12-5-26_CBNA\12-5-26_CBNA
```

但 Agent 的 `local-runs.db` 中，输入路径已经被 canonicalize 成后端共享名：

```text
\\szh-soc4.apac.bosch.com\urmszh_i_2208_089$\AU_data\BYD_SR\12-5-26_CBNA\12-5-26_CBNA\Gen5_2009-01-01_03-57_0115.MF4
```

当前最可能的原因是：框架对 UNC 路径使用 `Path.resolve()` 后得到 DFS/backend canonical path；Agent 进程能够扫描该路径，但 Selena 子进程在当前 Windows 登录/网络凭据上下文下不能访问 canonical share。这个结论是基于真实路径和 Selena 日志的推断，必须用“保留原始 UNC 别名”和“canonical path”各跑一次最小命令进一步确认。

该问题属于框架/运行环境边界，不应继续归类为“Selena 内部失败”后忽略。至少需要：

- 保存原始用户 UNC path 与授权 canonical path 两份证据；
- 让 Selena 使用执行上下文可访问的路径表示，不能只把 `Path.resolve()` 结果写入 paramconfig；
- 在 preflight 中用与 Selena 相同的子进程/凭据上下文验证 network share；
- 若 Agent 能读而 Selena 不能读，Job 应在仿真前给出 `network_share_unavailable_for_child_process` 类框架诊断，而不是启动后才得到 `selena_failed`。
