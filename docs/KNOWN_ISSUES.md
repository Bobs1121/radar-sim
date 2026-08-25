# 当前已知边界

> 更新时间：2026-08-21。实时阻断以 [`../HANDOFF.md`](../HANDOFF.md) 和最新修复 handoff 为准，不沿用历史任务状态。

1. Cluster→用户设备的反向 `source_to_local`/自动解压尚未开放；当前交付 owner-scoped ZIP/引用，SDK 可下载。
2. Cluster Manager `SZHRADAR01:8123` 是外部依赖；本次验收期间曾短暂不可达，现已恢复。executor/gateway 心跳在线不代表 Cluster 可提交，Web/SDK 必须继续使用真实 readiness；再次不可达时返回 `cluster_readiness_unavailable`，不传输、不编译、不创建外部 Cluster 任务。
3. 无认证部署仅用于受信内网；`X-Rsim-User` 是路由标签，不是认证。
4. MCP Server/Skill 包已在仓库提供，但仍需按 `docs/RADAR_SIM_SDK_GUIDE.md` 安装；认证开启时 Connector 自动配对仍需部署方提供短期 pairing。
5. Agent Tools Bundle 已部署并完成 Windows Python 3.13 黑盒安装、重复安装和更新路径验证；认证开启部署的 Connector 自动配对仍需短期 pairing 服务。
6. 关机或网络隔离的 Windows 设备不能由服务远程唤醒。
7. Cluster 结果收集已按数据集数量动态扩大扫描并保留完整 per-input manifest；结果归档已接入 retention、过期 GC 和磁盘水位。仍需对接分页展示并做真实超大批量/低磁盘压力验收，不能把 UI 分页上限当成仿真完成判断。

历史 Cluster 端口、队列和具体 Job 故障已经从本页删除；它们只属于对应日期的 handoff。
