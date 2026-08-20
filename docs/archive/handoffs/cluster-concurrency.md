# Cluster Stage 有界并发交接

更新时间：2026-08-11

## 结论

`agents.current_task_id` 是单值，不能让同一个 `agent_id` 的多个线程并行 claim/heartbeat。`ClusterStageExecutor` 现在为 Linux stage 和 platform gateway 各自启动一个有界 worker pool（默认每角色 2 个，单角色上限 16）；角色根 ID 继续作为 worker 0，额外 worker 使用稳定 ID：

- `linux-v2-stage-executor-worker-1` …
- `cluster-v2-platform-gateway-worker-1` …

API/stage binder 写入的 `required_agent_id`/初始 `assigned_agent_id` 仍是角色根 ID，避免改变现有绑定合同。生产注册走 `ControlService.register_cluster_worker()`，由服务端 allowlist 固定 role→node_kind、worker index/ID 和内部 marker；generic `register_agent()` 与 heartbeat 都会剥离所有 server-owned worker 字段。ControlService 只允许合法 `<role>-worker-N` 且 node_kind/marker 一致的 worker 将角色根绑定纳入 claim 候选。普通 Agent 即使自报 `claim_group`/prefix/marker 也不能获得该扩展。原子 `UPDATE ... WHERE status='queued'` 和实际 worker `assigned_agent_id` 不变。

每个 worker 仍只有一个当前 Stage 和一个 heartbeat 线程，因此取消检查、heartbeat、完成归属、stale reclaim 和 at-least-once attempt 语义保持原路径。`collect_results` 的长轮询只占用它自己的 worker，不阻塞同角色其他 worker；同一 role 的 queued claim 先按该 owner 当前 running role-stage 数升序，再按现有 FIFO(created/order/task_id) 排序：有其他 owner 时避免一个 owner 占满 pool，只有一个 owner 时仍允许其使用多个 worker。不引入外部队列或触碰仿真内部。

## 变更与验证

- `core/cluster_stage_executor.py`：角色 worker 数量参数、稳定 worker ID、注册元数据和多 loop 启动；每角色默认 2、每角色最大 16。
- `core/control_service.py`：worker 角色根绑定的最小、安全 claim-group 兼容逻辑和按 owner running 数的轻量公平排序；heartbeat 仅过滤 server-owned worker 字段，result/reclaim 逻辑未改。
- `tests/test_cluster_stage_executor.py`：两个 Linux root-bound Stage 在不同 worker ID 上同时进入执行；检查注册/claim-group。
- `tests/test_concurrency.py`：role worker 可领取根绑定 Stage，普通/伪造 Agent 不能靠自报 metadata 领取；Alice 两个旧 Stage + Bob 一个 Stage 时，第二 worker 优先 Bob。

已运行：

```text
python -m pytest tests/test_cluster_stage_executor.py tests/test_concurrency.py -q
27 passed
python -m pytest tests/test_cluster_stage_executor.py::test_role_worker_pool_claims_two_linux_stages_without_shared_identity tests/test_concurrency.py::test_cluster_worker_claims_role_bound_stage_without_relaxing_other_bindings tests/test_concurrency.py::test_cluster_worker_claim_prefers_owner_with_fewer_running_role_stages -q
3 passed
```

## 边界与剩余风险

- 并发上限是每角色 pool size（构造器可调，值会被限制为 1..16），不是全局任务数；Linux 与 gateway 分开隔离。
- 已运行中的 worker ID 若因部署缩容不再注册，其 task 仍依赖现有 stale-agent reclaim 周期回收；不会被另一 worker 直接夺取，避免双执行。
- `register_cluster_worker` 是进程内控制服务原语，HTTP Agent 注册不会获得内部 marker；部署仍应保护控制服务注册入口，marker 不是完整的身份认证系统。
