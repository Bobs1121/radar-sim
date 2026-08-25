# radar-sim 文档入口

本文档目录只保留当前产品、开发、使用和发布入口；历史审计、旧版本 handoff 和已停用部署方式统一放在 [`archive/`](archive/README.md)，保留原始证据但不作为当前操作指南。

## 当前必须先读

1. [产品合同](PRODUCT_CONTRACT.md)：用户目标、支持矩阵和明确不支持范围。
2. [V2 架构](V2_ARCHITECTURE.md)：Web/SDK/控制面/数据面/Connector/Cluster 拓扑。
3. [详细设计](DETAILED_DESIGN.md)：API、Stage、状态和结果边界。
4. [AI/SDK 集成合同](AI_INTEGRATION_CONTRACT.md)：SDK 使用、YAML 草稿和未来 Skill 薄封装边界。
5. [SDK 指导手册](RADAR_SIM_SDK_GUIDE.md)：SDK、MCP、Skill 的正式安装、调用、异常和验收规范。
6. [SDK/MCP/Skill 分发](RADAR_SIM_DISTRIBUTION.md)：不下载源码时的 wheel、MCP 注册、Skill 分发和版本策略。
7. [发布与部署](release-deployment.md)：当前 Linux release、Connector、发布门禁和回滚规则。
8. [Windows Connector](windows-one-click-connector.md)：一次安装、升级、恢复和用户操作。
9. [当前已知边界](KNOWN_ISSUES.md)：只记录仍然存在的产品边界，不记录历史 Job 故障。
10. [当前交接](../HANDOFF.md)：最新生产状态和证据索引。

## 用户操作

- [用户配置指南](USER_GUIDE.md)
- [配置字段指南](config-guide.md)
- [环境职责](environment-setup.md)
- [Cluster/Windows E2E 验收记录](handoffs/2026-08-20-sdk-yaml-draft-skill-readiness.md)

## 开发与验收

- [控制面/数据面实施合同](CONTROL_DATA_PLANE_PLAN.md)
- [结果真实性合同](RESULT_TRUTH_CONTRACT.md)
- [2026-08-20 SDK/YAML/Skill 交接](handoffs/2026-08-20-sdk-yaml-draft-skill-readiness.md)
- [2026-08-18 已知问题修复交接](handoffs/2026-08-18-known-issues-fix-handoff.md)
- [2026-08-18 需求与风险审查交接](handoffs/2026-08-18-continuation-requirement-risk-review.md)

## 文档规则

- 新的当前状态只更新根 `HANDOFF.md` 和最新日期 handoff，不修改历史审计结论来追溯新状态。
- 当前合同发生变化时，同步更新 `PRODUCT_CONTRACT.md`、`V2_ARCHITECTURE.md`、`DETAILED_DESIGN.md`、`AI_INTEGRATION_CONTRACT.md` 和相关测试。
- 历史审计和旧部署说明只移动到 `archive/`，不删除；归档文档不能作为当前命令来源。
- 文档中的 release、测试数量、服务状态必须有对应 Git、测试或线上命令证据。
