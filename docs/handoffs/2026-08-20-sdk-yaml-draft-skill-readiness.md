# 2026-08-20 SDK、YAML 草稿与 Skill 化准备交接

## 当前目标

保持现有 Web 仿真成功链路不变，补齐 YAML 草稿导入/导出，并确认 Python SDK 可作为未来 Skill 的唯一薄封装入口。

## YAML 合同

### 草稿阶段

`POST /api/v1/run-configs/import` 和 `POST /api/v1/run-configs/export` 接受完整或部分 `UserRunConfig 2.0` 映射：

- 可以只填写 `selena` 的一部分；
- 可以只填写 `data.path`；
- 可以只填写 `simulation` 的部分选项；
- 返回 `valid=true` 表示 YAML/字段形状有效；
- 返回 `complete=false`、`missing_fields` 和 `validation_errors` 表示还不能提交；
- 草稿不创建 Job，不切换分支，不访问本地路径，不执行 Cluster readiness，不触发编译/传输。

`PartialUserRunConfig` 只负责字段形状、路径规范化、公开枚举和 YAML round-trip，不执行跨字段完整性判断。

### 提交阶段

`validate_run()`、`submit_run()`、`submit_yaml()` 和 Web 的“检查配置/提交任务”继续使用严格 `UserRunConfig`：

- `selena.source=build` 必须有 `code_path`、`selena_build_script`、`runtime_xml`；
- `selena.source=existing` 必须有 `existing_path`、`runtime_xml`；
- 必须有 `data.path` 和 `simulation`；
- 未完成配置不能进入 Job DAG；
- 不允许为了补全而猜项目、路径、分支、运行参数或 Cluster 配置。

## SDK 当前公共入口

### 草稿

- `client.import_yaml(source)` / `client.import_run_config_yaml(source)`
- `client.export_yaml(config)` / `client.export_run_config_yaml(config)`

草稿返回 `complete`、`missing_fields`、`validation_errors`、规范化 `config` 和 `yaml_content`。

### 正式运行

- `validate_run(config)`
- `submit_run(config)` / `submit_yaml(path)`
- `capabilities()`、`get_job()`、`list_jobs()`
- `prepare_direct_transfers()` / `resume_direct_transfers()`
- `wait_job()` / `watch()`、`cancel()`、`retry_stage()`
- `retry_failed_inputs()`
- `diagnosis()`、`manifest()`、`download_job_result()`、`download_result()`

SDK 仍是 HTTP/控制面客户端，不在模型上下文中读取或传输 MF4、Selena、Runtime、DLL、结果 ZIP 正文。可读取源端执行 TransferPlan；永久错误、取消、checksum 错误不能转换成 waiting。

## Skill 化原则

未来 Skill 只能做以下事情：

1. 解析用户自然语言，决定调用哪个 SDK 方法；
2. 将 YAML 草稿和完整配置状态展示给用户；
3. 把 `missing_fields` 转成补充信息问题；
4. 把 Job/Diagnosis/action 转成下一步建议；
5. 透传 SDK 的稳定错误和结果引用。

Skill 禁止：

- 自己复制十阶段 DAG、Cluster 提交流程或结果判断；
- 自己做 Windows/UNC 路径替换、项目识别、编译参数推断；
- 把大文件正文读入模型上下文或经 Skill 消息转发；
- 将 `complete=false` 草稿直接提交；
- 将 `partial` 显示成全成功；
- 将 Selena 内部失败改写成框架失败或成功。

## Skill 发布前验收

- 完整 YAML：Web 与 SDK 导出内容等价，`validate_run` fingerprint 一致；
- 部分 YAML：Web/SDK 可导入、导出、返回缺失字段，严格 validate/submit 拒绝；
- SDK existing/build × local/Cluster 的调用方法和错误动作映射完整；
- 多用户并发不会共享 owner、Job、Transfer、Result；
- 单条/批量 partial、失败输入重试、取消、长任务等待和结果下载动作可由 Skill 透传；
- Skill 不实现任何第二套调度、传输或状态机。

## 当前验证基线

- 代码基线：`04e583a`（包含 `30ad4a6` 运行时代码及本次文档/验收记录）。
- 现有完整链路全仓回归：`1673 passed, 12 skipped, 1 warning`。
- 本次 YAML/SDK 定向回归：`36 passed, 1 warning`；Web/SDK/API/身份相关回归：`145 passed, 1 warning`。
- 线上运行 release：`/home/hoz2wx/radar-sim-30ad4a6`；本次 YAML 草稿代码完成后需要重新部署并复验线上 Web/SDK import/export。
