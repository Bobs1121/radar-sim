# Result delivery worker handoff

## 已完成

- `core/result_delivery.py` 提供接收设备本地的 `result.path` 解析和目录物化：
  空值使用 `~/RadarSim/results/<job_id>`；显式值作为保存根并追加
  `<job_id>`。拒绝文件、文件系统根、`..` 回溯、Windows 设备名和符号链接。
- 物化只复制受验证的结果文件，先写同级临时目录再原子发布；重复执行按
  `manifest.json` + 内容 checksum 幂等返回 `already_present`，不删除/修改源目录。
- Windows Agent 的本地 `collect_results` 执行一次物化并保留 ZIP catalog；
  `finalize_manifest` 仅复用 path-free `delivery` 摘要，不再次扫描大结果目录。
- local stage binding 将 `result.path` 传递到 Agent，并将
  `status/file_count/checksum[/code]` 传递给 finalize；物理路径不进入公开结果。

## 验证

`pytest -q tests/test_result_delivery.py tests/test_windows_full_local_e2e.py tests/test_local_stage_binding.py tests/test_direct_transfer.py tests/test_direct_transfer_clients.py tests/test_cluster_stage_executor.py`

结果：`68 passed, 3 skipped`。

## 未实现 / blocker

Cluster → Connector/SDK 的反向结果直传没有安全原语：当前
`TransferService.issue_plan()` 拒绝 `source_to_local`，`gateway_upload` 也未开放，
且 `TransferPlan` 仅表达客户端到 Cluster 的目标根。为避免伪造完成或让正文经过
Linux，本 slice 不新增协议、不伪装成功；Cluster 结果继续保留服务端 ZIP，需后续
提供目标 Agent 授权和签发反向 TransferPlan 后再接入。
