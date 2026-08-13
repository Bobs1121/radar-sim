# V2 无项目路由原则

V2 不识别、登记或选择业务项目。用户提供的代码仓、Selena 编译脚本、已有产物、Runtime、数据、MatFilter、Adapter 和 source 只是当前任务的资源证据。

- `build`：执行用户选择的 Selena 编译脚本，并从脚本输出/受控搜索根确认 `Selena.exe + DLL`。
- `existing`：使用用户选择的产物文件夹，不进入编译或 Visual Studio 依赖链。
- MatFilter：显式路径优先；留空时在用户资源范围内做有界推导，缺失时要求补充，不按项目回退。
- Cluster：只读取 deployment-wide 基础设施配置；项目名不得决定路径、参数或执行器。

内部 `execution_identity` 只用于缓存、授权和追踪，不是业务项目。完整合同见 [`PRODUCT_CONTRACT.md`](PRODUCT_CONTRACT.md)。
