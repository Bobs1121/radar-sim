# V2 用户配置入口

公共配置只有 `UserRunConfig 2.0`，不存在用户项目、platform、recipe、profile 或多层项目配置。

字段合同和 YAML 示例见：

- [`PRODUCT_CONTRACT.md`](PRODUCT_CONTRACT.md)
- [`V2_ARCHITECTURE.md`](V2_ARCHITECTURE.md)
- [`../config/user-run.example.yaml`](../config/user-run.example.yaml)

仓库中仍存在的 `config/projects`、`config/platforms`、`config/recipes` 只供未删除的 legacy CLI/测试读取，不得用于新 Web、SDK 或 REST API 业务路由。
