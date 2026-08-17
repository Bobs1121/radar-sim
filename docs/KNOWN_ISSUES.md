# 当前已知边界

> 更新时间：2026-08-17。实时阻断以 [`../HANDOFF.md`](../HANDOFF.md) 和最新审计 handoff 为准，不沿用历史任务状态。

1. Cluster→用户设备的反向 `source_to_local`/自动解压尚未开放；当前交付 owner-scoped ZIP/引用，SDK 可下载。
2. 无认证部署仅用于受信内网；`X-Rsim-User` 是路由标签，不是认证。
3. 独立 MCP Server/Skill 包尚未发布；AI Agent 当前应调用 Python SDK/REST API。
4. 关机或网络隔离的 Windows 设备不能由服务远程唤醒。
5. Cluster 结果收集已按数据集数量动态扩大扫描并保留完整 per-input manifest；仍需对接分页展示、磁盘配额/GC 和真实超大批量压力验收，不能把 UI 分页上限当成仿真完成判断。

历史 Cluster 端口、队列和具体 Job 故障已经从本页删除；它们只属于对应日期的 handoff。
