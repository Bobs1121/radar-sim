# 仿真结果真实性与下载契约

本文定义 Linux 调度服务中 Cluster 仿真的业务结果。Web、SDK，以及后续
Skill/MCP 必须复用同一份结果，不得根据进度条、日志关键字或 Cluster Web
页面自行推断成功。

## 1. 结果层级

一次 Cluster 仿真有三层状态：

1. `ClusterResult.state`：服务端保存的 Cluster 执行结果。
2. `manifest.status`：面向 Web/SDK 的公开业务结果。
3. `Job.status`：任务中心显示的最终状态。

对新任务，三层必须一致：

- 所有 `result.ini` 成功且存在有效 MF4：`succeeded`。
- 任一 `result.ini` 失败、缺少要求的输出，或结构化失败计数大于零：
  `failed`。
- 仿真失败不因为结果归档成功而变成成功；归档只是诊断产物。

Cluster Web 的 `finished` 只表示 Worker 已结束，不表示 Selena 仿真成功。
最终业务结论以 Cluster 工作目录中的 `result.ini` 聚合结果为准。

## 2. 结构化判定

服务只使用下列证据纠正矛盾状态：

- `summary.fail_count > 0`
- `summary.failed_count > 0`
- 明确的失败状态

`summary.errors` 只用于诊断，不参与成功/失败推断。成功任务可以保留非致命
警告，因此“有错误文本”不能作为自动改判依据。

## 3. 失败结果下载

只要 Cluster 已产生结果文件，成功和失败任务都会生成公共 `result_ref`。
失败归档可以包含：

- `result.ini`
- 仿真日志
- 已产生的部分 MF4
- Cluster 检查器识别的其他结果文件

公共归档只保存相对于受控 Cluster 工作目录的文件，不暴露物理共享盘路径。
如果归档时源文件仍在变化，收集阶段保持可重试，不能先把 Cluster Run 固化
为终态。

如果 Worker 尚未产生任何可归档文件，任务仍可失败，但不会伪造一个不可下载
的 `result_ref`；结构化错误仍保留在 manifest 中。

## 4. 历史矛盾记录

服务启动时会安全归一化公开 Job/manifest：

- `manifest.status=succeeded` 但结构化失败计数大于零时，公开状态改为
  `failed`。
- Job 已经是 `failed` 但 manifest 仍是 `succeeded` 时，同样修正 manifest。
- `errors` 非空但失败计数为零的成功记录保持成功。

历史 `ClusterResult` 是不可变审计记录，不做危险的原地覆盖。若旧
`ClusterResult.state` 与其结构化 summary 矛盾，公开 manifest 在读取该结果
构建时归一化为失败。原始 Stage Attempt 也保留，作为问题追溯证据。

## 5. Web、SDK、Skill/MCP 的使用原则

上层入口只消费以下稳定字段：

- `Job.status`
- `manifest.status`
- `manifest.summary`
- `manifest.result_ref`

上层不得：

- 把 Stage 执行成功等同于仿真成功；
- 解析自然语言日志决定结果；
- 使用 Cluster Web 的 `finished` 作为业务成功；
- 绕过 `result_ref` 暴露服务器物理路径。

后续 Skill/MCP 应封装现有 SDK/API，而不是建立第二套调度或结果判断逻辑。
