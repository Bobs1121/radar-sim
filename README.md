# radar-sim V2

> 当前文档基线：2026-09-21。生产环境已迁移到 `10.190.181.243`；实时发布状态、测试和边界以最新 handoff 为准。

radar-sim 是 Selena 编译与雷达数据仿真的轻量自动化脚手架。它通过 Linux 控制面统一调度 Windows 本地编译、本地仿真和 Cluster 仿真；大文件由源设备直接进入执行目标，不经过 Linux Web/API 端口。

## 线上服务

| 项 | 当前值 |
|---|---|
| 服务地址 | `http://10.190.181.243:8877`（Web 与 `/api/v1` 同源） |
| 部署机 | Linux `hoz2wx@10.190.181.243` |
| 运行方式 | user-level systemd `radar-sim-v1.service`（`serve-v1`，端口 8877） |
| 当前 release | `/home/hoz2wx/radar-sim-main`（单一正式 release 目录） |
| 数据根 | `RSIM_HOME=/home/hoz2wx/.rsim-v1-git-smoke`，部署配置 `config/deployment.yaml` |

服务器目录规范：`~/radar-sim-main` 是唯一正式 release；历史版本目录与旧日志已清理。升级时按 `docs/release-deployment.md` 新建不可变 release 目录，验收通过后把 `radar-sim-main` 指向新版本并保留旧目录一个发布周期作回滚。

## 文档入口

当前文档从 [`docs/README.md`](docs/README.md) 开始；历史审计和旧部署文档位于 [`docs/archive/`](docs/archive/README.md)。

## 用户入口

- Web：打开 Linux 服务地址，导入/编辑同一份 YAML，提交和管理任务。
- Python SDK：后端产品、Linux 用户和 AI Agent/MCP/Skill 使用的唯一编程入口；Skill 不复制调度和状态机。
- Windows Connector：当配置包含 Windows 本地路径、需要编译或需要本地仿真时，一键安装一次；系统按任务自动准备所需能力。

## 最小 YAML

已有 Selena：

```yaml
schema_version: "2.0"
selena:
  source: existing
  existing_path: "C:/path/to/RelWithDebInfo"
  runtime_xml: "C:/path/to/Runtime.xml"
  branch: ""
  code_path: ""
  selena_build_script: ""
  package_build_script: ""
data:
  path: "D:/data/one.MF4"
simulation:
  target: cluster
  source: ""
  adapter_file: ""
  mat_filter: ""
result:
  path: ""
```

本地编译只把 `selena.source` 改为 `build`，填写 `code_path` 和 `selena_build_script`，清空 `existing_path`。`package_build_script` 可选，只用于依赖诊断。

用户只需填写 YAML 中的路径、脚本和仿真选项；身份、运行时对象和调度参数由系统推导。

## SDK

```python
from radar_sim_sdk import RadarSimClient, UserRunConfig

with RadarSimClient("http://10.190.181.243:8877") as client:
    config = UserRunConfig.from_yaml("radar-sim.yaml")
    validation = client.validate_run(config)
    job = client.submit_run(config)
    final_job = client.wait(job.id, timeout=3600)
    result_zip = client.download_job_result(final_job.id)
```

当 SDK 调用机本地文件需要进入 Cluster 时，`submit_run(..., auto_transfer=True)` 使用同一 TransferPlan 直接传输。Linux 只保存任务、状态和逻辑引用。

## Windows 首次使用

1. 在 Web 点击“一键连接本机”，或用 SDK `download_windows_connector()` 下载入口；
2. 双击运行一次；
3. Connector 保存 Linux 地址和用户身份，登录自启、断线重连；
4. Web 显示“本机已连接”后提交任务；
5. 服务端协议升级时按 Web 提示一键更新，身份和路径绑定保留。

已有 Selena 与全部输入都在 Cluster 可读共享路径时不需要安装 Connector。Linux 用户的私有本地文件通过 Linux 上的 Python SDK 直传，首版没有浏览器 Linux Connector。

## 仓库结构

```
cli/            命令行入口（rsim.py server / web / agent ...）
core/           控制面、API v1、调度、传输、状态机
radar_sim_sdk/  Python SDK（唯一编程入口）
radar_sim_mcp/  MCP Server（SDK 薄封装）
radar_sim_web/  Web 静态资源（打包随服务分发，服务端 /console 提供）
platforms/      Gen5 Selena 平台适配
plugins/        分析插件
scripts/        部署 / 打包脚本
skills/         radar-sim-simulation Skill
docs/           当前文档；docs/archive/ 历史审计与旧 handoff
tests/          自动化测试
vendor/         Windows Connector 离线 wheel
```

## 设计边界

- 编译命令：`cmd /c <用户选择的 Selena 脚本>`，不加项目参数；
- 本地/Cluster 仿真使用通用 Selena paramconfig；
- 用户显式 MatFilter/source 优先，空值通用推导；
- 单条 MF4 失败不取消批量其余数据；
- 本地结果落 `~/RadarSim/results/<job_id>` 或 `result.path/<job_id>`，Web 继续提供 ZIP；
- 框架不安装 VS，不修复 Selena 内部仿真问题。

## 开发与验收

```bash
# 本地快速回归（Windows 开发机）
python -m pytest -q tests/test_api_v1_fastapi.py tests/test_sdk.py tests/test_user_config.py
python -m py_compile core/api_v1.py core/api_v1_fastapi.py radar_sim_sdk/client.py
node --check radar_sim_web/static/app.js

# 发布门禁：全仓测试零失败
python -m pytest -q tests/
```

自动测试不能替代真实验收。发布前必须在目标 Linux 和新 Windows 用户上验证 existing/build + local/cluster 四组合、两用户隔离、直传和结果。

## 文档

- [产品合同](docs/PRODUCT_CONTRACT.md)
- [PRD](PRD.md)
- [V2 架构](docs/V2_ARCHITECTURE.md)
- [详细设计](docs/DETAILED_DESIGN.md)
- [用户指南](docs/USER_GUIDE.md)
- [统一 Connector](docs/windows-one-click-connector.md)
- [当前状态与 handoff](HANDOFF.md)
