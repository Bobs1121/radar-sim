# 当前已知边界

> 更新时间：2026-08-13。实时阻断以 [`../HANDOFF.md`](../HANDOFF.md) 为准，不沿用历史任务状态。

1. Cluster→用户设备的反向 `source_to_local`/自动解压尚未开放；当前交付 owner-scoped ZIP/引用，SDK 可下载。
2. 无认证部署仅用于受信内网；`X-Rsim-User` 是路由标签，不是认证。
3. 独立 MCP Server/Skill 包尚未发布；AI Agent 当前应调用 Python SDK/REST API。
4. 关机或网络隔离的 Windows 设备不能由服务远程唤醒。
5. 超大批量的 Cluster 逐输入展示/扫描上限仍需显式 truncation 合同，见根 handoff backlog。

历史 Cluster 端口、队列和具体 Job 故障已经从本页删除；它们只属于对应日期的 handoff。
