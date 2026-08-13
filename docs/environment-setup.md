# V2 环境职责

radar-sim 只准备外围路径、传输和任务指令，不安装 Visual Studio，也不搭建 Selena 本地仿真环境。

- 需要 Windows 本地文件、编译或本地仿真：从 Web/SDK 安装一个统一 Connector，见 [`windows-one-click-connector.md`](windows-one-click-connector.md)。
- 本地编译：用户提供代码仓与 Selena 编译脚本；系统校验本机环境并给出可操作提示。
- 本地仿真：默认用户已搭好成熟仿真环境；Connector 原地执行。
- Cluster 仿真：Linux 控制面只调度，输入从源设备直达 Cluster 数据面。

具体产品边界见 [`PRODUCT_CONTRACT.md`](PRODUCT_CONTRACT.md)。
