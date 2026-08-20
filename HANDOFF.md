# radar-sim 当前交接

> 更新时间：2026-08-20
> 当前代码分支：`codex/new-branch`
> 当前代码 release：`6c59efd`
> 当前 Linux release：`/home/hoz2wx/radar-sim-6c59efd`
> 回滚 release：`/home/hoz2wx/radar-sim-93947c8`
> 线上地址：`http://10.190.171.44:8877`

这是当前状态的唯一入口。历史审计、旧 handoff 和停用部署文档统一在 [`docs/archive/`](docs/archive/README.md)，不能把归档文档当作当前操作步骤。

## 产品定位

radar-sim 是 Selena 编译与仿真的外围自动化框架，不实现 Selena 内部算法，不修改仿真服务器排队逻辑。Web 和 Python SDK 使用同一 `UserRunConfig 2.0`、同一 `/api/v1`、同一 Job/Stage/Manifest。

支持：

- 多用户逻辑隔离；
- 单条和批量 MF4；
- `existing` / `build` Selena 来源；
- 用户填写 Selena 编译脚本、代码目录、Runtime XML 和可选输出/依赖信息；
- `local` / `cluster` / `auto`；
- Windows/SDK 源到 Cluster 直传和共享路径零复制；
- 长任务、取消、断线恢复、partial 逐输入恢复；
- Web/SDK 结果 Manifest、ZIP、retention、GC 和磁盘水位。

不在范围：Selena 内部结果内容、点云正确性、认证安全、远端到本地 Windows 的通用 `source_to_local`、独立 MCP/Skill 实现和关机唤醒。

## 当前实现状态

- Connector 合同版本：`15`；当前线上 Windows Connector `available=true`、`count=1`；
- Cluster Linux executor：`2`；platform gateway：`2`；
- 结果水位：`RSIM_RESULT_MIN_FREE_BYTES=1073741824`；
- YAML import/export 支持完整或不完整草稿，返回 `complete`、`missing_fields`、`validation_errors`；
- `validate_run()`、`submit_run()`、`submit_yaml()` 仍严格要求完整配置；草稿不会创建 Job；
- SDK 提供 `import_yaml()`/`export_yaml()`、长任务等待、直传恢复、partial 失败输入重试、结果下载；
- Cluster readiness 在提交前和 preflight 前均检查；build+Cluster readiness 失败不会继续编译/传输；
- readiness 探测采用单飞缓存和有界请求等待；外部 Cluster 挂起时返回可重试 blocker，不占住 Web/SDK 提交请求；
- Cluster 外部 Manager 曾在本次验收中短暂不可达；最新复核已恢复 `SZHRADAR01:8123`，真实 `/api/v1/run-configs/validate` 返回 `cluster_ready`、`can_submit=true`。readiness 单飞缓存和 blocker 降级保护仍保留；不能只依据 executor/gateway 心跳放行；
- `/api/v1/cluster/readiness` 已作为 Web 目标门禁；Cluster 不 ready 时禁用 Cluster 选项，若本地 Connector 也不可用则自动、本地、Cluster 和提交按钮全部禁用；直接 API/SDK 提交也在创建 Job 前返回 `503`；
- Web 顶部存在 Connector 必要更新提示；当前只有合同版本过旧才阻断，兼容包更新提示仍是后续增强项。

## 测试与线上证据

- 全仓回归：`1678 passed, 12 skipped, 1 warning`；唯一 warning 是 Starlette/httpx 弃用提示；
- Web/SDK/API/身份相关回归：`214 passed, 1 warning`；
- 线上 health：`ok=true`；
- 线上 capability：Windows 1、Cluster 2；
- 线上 Web 首页：HTTP `200`；
- 线上 SDK partial YAML import/export round-trip：通过；完整 YAML round-trip：通过；未创建测试 Job；
- 线上 Connector 包：`8408008 bytes`，SHA-256 `3bc651bdad28104dcdc2471f241e4b31e0f1c097df175f73973ca3fe4695161b`，合同 `15`；
- 两个并发 `/api/v1/run-configs/validate` 黑盒请求在 Manager 不可达期间均约 `8.2s` 返回 `200` 和 `cluster_readiness_unavailable`，未创建 Job；Manager 恢复后真实复核 `1.768s` 返回 `200`、`can_submit=true`、`cluster_ready`；
- 服务器只保留当前 release `6c59efd` 和回滚 release `93947c8`；
- 当前控制库无 `queued/running` Job；两个真实用户可在各自 Connector/配置通过后重新提交 Cluster 仿真。

## 当前文档入口

- [文档总入口](docs/README.md)
- [产品合同](docs/PRODUCT_CONTRACT.md)
- [V2 架构](docs/V2_ARCHITECTURE.md)
- [详细设计](docs/DETAILED_DESIGN.md)
- [控制面/数据面合同](docs/CONTROL_DATA_PLANE_PLAN.md)
- [AI/SDK 集成合同](docs/AI_INTEGRATION_CONTRACT.md)
- [发布与部署](docs/release-deployment.md)
- [Windows Connector](docs/windows-one-click-connector.md)
- [当前已知边界](docs/KNOWN_ISSUES.md)
- [SDK/YAML/Skill 准备交接](docs/handoffs/2026-08-20-sdk-yaml-draft-skill-readiness.md)

## 分支与发布

`codex/new-branch` 是当前开发/发布分支，已推送远端；远端 `main` 是其祖先，可在文档整理和最终回归通过后做 fast-forward 合并。合并前必须确认：全仓测试通过、工作区只剩用户明确保留的未跟踪目录、服务器无活动 Job、当前 release 可回滚。

任何 Connector 合同不兼容变更必须提升合同版本；兼容性代码包更新不能伪装成必要升级。自动热更新尚未开放，当前更新入口是 Web/SDK 下载同源脚本后由 Windows 用户执行一次。

## 工作区注意事项

`.zcode/` 和 `tmp-agent-home/` 是未跟踪目录，属于用户工作环境，本次整理不读取、不提交、不删除。
