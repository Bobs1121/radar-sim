# radar-sim V2

radar-sim 是 Selena 编译与雷达数据仿真的轻量自动化脚手架。它通过 Linux 控制面统一调度 Windows 本地编译、本地仿真和 Cluster 仿真；大文件由源设备直接进入执行目标，不经过 Linux Web/API 端口。

## 用户入口

- Web：打开 Linux 服务地址，导入/编辑同一份 YAML，提交和管理任务。
- Python SDK：后端产品、Linux 用户和未来 AI Skill/MCP 使用的唯一编程入口。
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

with RadarSimClient("http://10.190.171.44:8877") as client:
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

## 设计边界

- 编译命令：`cmd /c <用户选择的 Selena 脚本>`，不加项目参数；
- 本地/Cluster 仿真使用通用 Selena paramconfig；
- 用户显式 MatFilter/source 优先，空值通用推导；
- 单条 MF4 失败不取消批量其余数据；
- 本地结果落 `~/RadarSim/results/<job_id>` 或 `result.path/<job_id>`，Web 继续提供 ZIP；
- 框架不安装 VS，不修复 Selena 内部仿真问题。

## 开发与验收

```powershell
python -m pytest -q tests/test_api_v1_fastapi.py tests/test_sdk.py tests/test_user_config.py
python -m py_compile core/api_v1.py core/api_v1_fastapi.py radar_sim_sdk/client.py
node --check radar_sim_web/static/app.js
```

自动测试不能替代真实验收。发布前必须在目标 Linux 和新 Windows 用户上验证 existing/build + local/cluster 四组合、两用户隔离、直传和结果。

## 文档

- [产品合同](docs/PRODUCT_CONTRACT.md)
- [PRD](PRD.md)
- [V2 架构](docs/V2_ARCHITECTURE.md)
- [详细设计](docs/DETAILED_DESIGN.md)
- [用户指南](docs/OD25_USER_GUIDE.md)
- [统一 Connector](docs/windows-one-click-connector.md)
- [当前 handoff](docs/handoffs/2026-08-11-business-convergence-master.md)
