# radar-sim V2 最终收敛审计

> 日期：2026-08-13
> 当前状态：project-free V2 的已有 Selena + 本地/Cluster 主链真实通过
> 当前 release：`8f8601c` / `/home/hoz2wx/radar-sim-8f8601c`

## 1. 不可回退的产品结论

radar-sim 是 Selena 外围自动化薄层。本质工作是按一份 `UserRunConfig 2.0` 准备源到目标路径、必要时执行用户给定编译脚本、确认 Selena.exe/DLL、拼接并下发 Selena 仿真指令、收集逐输入结果。

V2 完全不识别、不登记、也不按业务项目分支。允许从文件、脚本和元数据通用推导 MatFilter、Radar source、编译产物等具体参数；禁止先识别成某个项目，再套 project adapter、registry、profile、recipe、模板或专用 DAG。推导不足只要求用户补具体文件路径。

## 2. 当前真实证据

### existing + local

- Job：`job_1ebbef262a89`
- 输入：单条真实 MF4、已有 Selena 文件夹、Runtime XML、通用 MatFilter 推导。
- 结果：Job/Manifest succeeded，1/1 成功；输出 MF4 `239,051,624` bytes。
- 输出 SHA-256：`1a75992f5a87e543606b4d7831683f198d930d6e2e8cec412f242ebd42fbd440`。
- SDK ZIP 与本地物化目录：`D:/RadarSim/v2-results/job_1ebbef262a89/`。

### existing + cluster

- 历史阻塞 Job `job_a6cd945004f9`：当时 `SZHRADAR01:8123` 不可达，正确停在 `environment_check`，没有传输或仿真；保留为外部依赖诊断证据。
- 恢复后成功 Job：`job_bcf8bd2f1dbe`。
- 数据集直传：`443,266,984` bytes，SHA-256 `1c7bbbe1703da67e16ee7299181613333df4abbcba8337e6c81eb3462f86d23b`。
- Runtime bundle：8 files / `88,486,912` bytes；Runtime XML：`98,780` bytes；MatFilter：completed。
- Cluster run：`cluster-run:5409fd5266f84876acbcd22200484299`。
- Manifest：`radar-sim.run-manifest/2.0`，status succeeded，1/1 succeeded，0 failed，9 result files。
- ResultCatalog：`result:sha256:87db2f82a54b3811411b212725984065134d988e8a9653192c7ef93e17467fb1`。
- SDK ZIP：`12,173,015` bytes，SHA-256 `4f59686ad2e767d918d4635768ea7ce57df1a787491561f3251efe68b7ba9e8e`。
- Linux 只保存 TransferPlan、进度和引用；文件由 Windows Connector 直接写 Cluster 数据面。

以上证据只覆盖已有 Selena 路径，不冒充真实编译验收。

## 3. 通用鲁棒性收口

真实 Cluster 验收中，两次客户端请求被外部终止曾为同一 Job 生成孤立 dataset TransferPlan。成功任务未受影响，孤立计划已取消。`3cd10ae` 已实现通用幂等：

- request key 覆盖 owner、Job、Stage、mode、资源角色、排序后的文件元数据、源指纹和受管目标根；
- SQLite `BEGIN IMMEDIATE` 原子查找/签发，并发重试只得到一个计划；
- 未过期 pending/in-progress/completed 复用；failed/cancelled/expired/输入变化重新签发；
- 不包含 project 或产品身份，不针对某个用户或路径写特例。

验证：传输组 `53 passed`，扩大 API/SDK/调度组 `115 passed`，最终全仓 `1556 passed, 12 skipped, 1 warning`，零失败。唯一 warning 是 Starlette/httpx 弃用提示。

## 4. 当前部署证据

- Git：`8f8601c Remove project inference from V2 build flow`，已推送 `origin/codex/new-branch`；包含此前 `3cd10ae` TransferPlan 幂等修复。
- Linux：用户级 `radar-sim-v1.service`，WorkingDirectory `/home/hoz2wx/radar-sim-8f8601c`，`active/running`，`NRestarts=0`。
- 回滚目录：`/home/hoz2wx/radar-sim-3cd10ae`。
- 最终全仓：`1557 passed, 12 skipped, 1 warning`；Linux 候选门禁：`96 passed`。
- Connector ZIP：`8,336,982` bytes，SHA-256 `1e1daea6bcb8f0da1705b4377329959e94b704b7411747d2619e2d686207cf3f`；Range 下载 `206`。
- Connector 合同未提升；当前用户无需因服务端幂等修复重新安装，重启后现有 Connector 自动恢复轮询。

## 5. 明确未发布能力

- no-auth 仅为受信内网 owner 逻辑隔离，不是不可伪造认证；公网/不受信多租户必须先启用 Bearer/SSO。
- `source_to_local`、Cluster 结果反向直传/解压到任意设备、独立 MCP/Skill、关机唤醒未发布。
- `build + local`、`build + cluster` 已有代码和自动化合同，但需要最终 release 上的真实用户编译脚本黑盒证据后才可写“真实通过”。实现不得引入项目识别。
- 超大批量展示/扫描上限和 mixed-source 的逐条呈现可以后续增强，但必须继续使用文件元数据规则，不得演变为项目适配。

## 6. 下一位开发者启动顺序

1. 以根 `HANDOFF.md` 和本文件为当前事实，产品合同依次读取 `docs/PRODUCT_CONTRACT.md`、`PRD.md`、`docs/V2_ARCHITECTURE.md`、`docs/DETAILED_DESIGN.md`。
2. 先检查 `git status --short`；正常起点应为干净工作区。
3. 对照用户级 systemd unit、health 和 Git commit；不要误查系统级同名 inactive unit。
4. 任何新需求先判断是否能表达为通用输入、传输、编译指令、仿真指令或结果合同；若方案需要项目名/项目注册表，停止并重新设计。
5. 真实任务严格区分外围框架失败、Cluster 基础设施失败和 Selena 内部逐输入失败。
