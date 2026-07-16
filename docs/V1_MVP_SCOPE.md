# radar-sim V1 首版交付范围

> 状态：当前最高实施优先级
> 确认日期：2026-07-16
> 与总 PRD 的关系：这是完整产品中的第一条纵向子功能；`PRD.md` 和 `docs/PRODUCT_CONTRACT.md` 保持不变，其余组合进入后续版本。

## 当前交付状态（2026-07-15）

- 已完成统一公开 SDK 方法 `RadarSimClient.submit_yaml(yaml_path)`；`submit_cluster_yaml()` 仅作为首版严格校验兼容入口；
- 已完成调用机可达目录的 Selena.exe + 全部同目录 DLL + Runtime XML 校验、内部归档和上传；
- 已完成 Linux 服务可达共享/挂载目录的同等自动导入，不依赖 Windows Agent；
- 已完成本地数据、MatFilter、可选 Adapter 的透明上传及统一逻辑引用；
- 已完成 Runtime Bundle 私有解包、按 manifest role 定位任意文件名 Runtime XML、Cluster preflight、Config.cfg 生成、提交和结果回收；
- mocked 纵向门禁已从同一个 SDK 方法真实走到 `submit_cluster_job()` 并取得外部任务 ID；V1 聚焦回归 `101 passed`，全量回归 `1197 passed, 8 skipped`；随后补充 UNC 到 Linux 授权挂载解析，相关专项 `45 passed`；
- 已部署到 `10.190.171.44:8878`，以 systemd user service 常驻并显式关闭首版认证；服务器本机健康检查返回 200；
- 已使用下文同一份五路径 YAML 和同一个 SDK 方法完成真实 Cluster 烟测：`job_bad6f07479e5` 状态为 `succeeded`，1 个任务成功，产出 MF4 为 537,269,680 bytes；用户未填写 radar、mounting position、project、output root 或 Cluster 参数；
- 目标服务器当前对调用机直连 `8878` 仍受入站网络策略限制；本机快速验收通过 SSH 隧道访问 `127.0.0.1:8878`，正式多用户发布前需由服务器管理员放行或配置反向代理。

## 1. V1 唯一目标

用户提供一个 YAML，通过 Python SDK 的一个方法提交给 Linux 服务。Linux 服务准备已有 Selena 产物、Runtime、MatFilter、可选 Adapter 和数据，然后在 Cluster 上创建并触发仿真任务。

V1 不做：Selena 编译、本地仿真、Windows full/light 安装体验、自动分支切换。相关能力保留在总 PRD，V1 交付后继续开发。

## 2. V1 唯一用户配置

```yaml
schema_version: "2.0"

selena:
  source: existing
  existing_path: "D:/path/to/Selena-folder"
  runtime_xml: "D:/path/to/Runtime.xml"

data:
  path: "D:/path/to/data"

simulation:
  target: cluster
  adapter_file: ""                 # ovrs25 可空；内部识别出的其他配方按规则校验
  mat_filter: "D:/path/to/MatFilter.cfg"
```

用户不填写 project、recipe、Runtime Bundle、output_root、共享盘类型、Cluster manager、group/subgroup、priority、Python 2、提交命令、凭证或输出目录。

## 3. V1 唯一 SDK 用法

目标公开方法：

```python
from radar_sim_sdk import RadarSimClient

with RadarSimClient("http://10.190.171.44:8878") as client:
    job = client.submit_yaml("simulation.yaml")
    print(job.id)
```

该方法内部自动完成：

1. 读取并校验同一份 YAML；首版使用 `source=existing`、`target=cluster`，后续组合仍复用此方法；
2. `existing_path` 在 SDK 调用机可达时，校验唯一 `Selena.exe`、同目录全部 DLL 和 Runtime XML，生成内部归档并上传；若 Linux/共享存储直接可达，则由 Linux staging 读取；
3. `data.path` 在 SDK 调用机可达时递归检索 MF4 并上传；共享路径由 Linux 直接解析；
4. MatFilter 必须可达并上传；Adapter 非空时上传；
5. 向 Linux 提交原始用户配置及内部准备引用；
6. 返回统一 Job，可继续读取 Stage/Event/日志和最终结果。

首版按用户决定暂不要求令牌；服务器部署需显式使用开发期无认证开关并限制在受信内网。认证在下一 Sprint 恢复，且不会进入仿真 YAML。

## 4. Cluster 实际输入映射

现有 `core.cluster.prepare_cluster_job()` 和 `submit_cluster_job()` 证明 Cluster 实际需要：

| Cluster Config.cfg / 运行输入 | 来源 |
|---|---|
| `selenaPathExe` | 内部解包后的 `bin/Selena.exe`，同目录包含所有 DLL |
| `runTimeConfigFile` | 用户 `selena.runtime_xml`，绑定进内部 Selena 归档 |
| `matfilefilter` | 用户 `simulation.mat_filter` |
| `adapterFile` | 用户 `simulation.adapter_file`；允许按内部配方为空 |
| `datafile_path` | 用户唯一 `data.path` 解析/上传后的 Cluster 可达位置 |
| simulation Python script | 系统生成 |
| output/log/temporary path | 系统生成 |
| group/subgroup/priority/manager/credential | Linux 部署配置 |

## 5. V1 完成门禁

以下全部通过前不得宣布 V1 完成：

- 示例 YAML 可被 SDK 方法直接读取并提交；
- SDK 调用参数只需要 YAML 路径，不要求用户先上传 Bundle/数据/配置资产；
- 本地 Selena 文件夹测试证明 exe、全部同目录 DLL 和 Runtime 被上传并用于 Cluster 包；
- 本地数据和共享数据都仍然只使用 `data.path`；
- MatFilter 必填、ovrs25 Adapter 可空的预检真实生效；
- Linux 没有 Selena 编译或本地仿真 Stage；
- mocked Cluster 纵向测试从 SDK 调用跑到 `submit_cluster_job()` 并获得外部任务 ID；
- 在 `10.190.171.44` 部署后完成健康检查和目标环境 Cluster 烟测；
- 使用说明只展示上述 YAML 和一个 SDK 方法。
