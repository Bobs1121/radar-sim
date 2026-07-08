# radar-sim 开发计划

> **版本:** 4.0.0
> **日期:** 2026-06-16
> **状态:** v4 骨架完成 + 废弃模块清除 + 测试全部通过 (49/49)
> **关联文档:** PRD.md (v4.0.0), HANDOFF.md

---

## 1. 当前状态

### v4 完成部分 (2026-06-16)

- **配置系统** — 多项目 + assets 管理 ✅
- **编译模块** — build 命令 (HEX + Selena) ✅
- **分析插件系统** — analysis_runner + 4 个插件 ✅
- **CLI 命令** — analyze / ask / build / check / diff / history / init / open-vs ✅
- **测试** — 49/49 通过 ✅
- **废弃模块清除** — 17 个废弃模块/目录全部删除 ✅

### v3 已有的可用模块（已复用）

- `platforms/gen5_selena/mf4_reader.py` — MF4 信号提取 ✅
- `platforms/gen5_selena/log_parser.py` — 日志解析 ✅
- `platforms/gen5_selena/selena_builder.py` / `hex_builder.py` — 编译 ✅
- `core/tui.py` — 终端 UI ✅

### v3 已废弃模块（已清除 2026-06-16）

- scanner/parallel_runner/regression/preset — 参数扫描/回归，不符合需求
- corner_case_generator/advisor/auto_signal_selector — 超出范围
- orchestrator — pipeline 模型不再适用
- rule_engine/signal_diff/ai_analyzer — v4 已用新架构替代
- config_gen/engine — v4 platform.py 统一封装
- management/report — v4 不需要
- 测试文件: test_phase2.py, test_phase4.py, test_scanner.py
- 目录: corner_cases/, results/scan/, config/rules/signals/suites/
- 文档: DESIGN_PHASE5.md

---

## 2. v4 实施计划

### Phase 1: 核心重写（6d）

#### Stage 1.1: 配置系统重写（1d）

**目标**: 多项目配置 + assets 管理

```
config/
  default.yaml              # 全局默认
  projects/
    ovrs25/
      config.yaml           # 项目配置（编译路径、环境、assets 路径）
      signals.yaml          # 信号配置
      rules.yaml            # 规则配置
    <other>/
      config.yaml
      signals.yaml
      rules.yaml
```

**改动**:
- `core/config.py` 重写：
  - `load_config(project_name)` — 加载指定项目配置
  - `list_projects()` — 列出所有项目
  - `get_default_project()` — 获取默认项目
  - assets 路径解析
- 新建 `config/default.yaml`
- 新建 `config/projects/ovrs25/` 目录及配置文件
- 废弃旧的单一 `config.yaml` 结构（保持向后兼容）

**验收**:
```bash
python -c "from core.config import load_config; c = load_config('ovrs25'); print(c['project']['name'])"
```

#### Stage 1.2: 编译模块重写（1d）

**目标**: HEX 可中断编译 + Selena 独立编译

**改动**:
- `cli/build.py` 重写：
  - `build hex` — 显示进度，Ctrl+C 安全中断
  - `build selena` — Selena 编译
  - `build all` — 先 HEX 再 Selena
- `platforms/gen5_selena/builder.py`：
  - HEX 编译加入输出监控和进度显示
  - 记录中断状态（如写入 `.build_state` 文件）
  - 下次 build 时检测上一次是否完成

**验收**:
```bash
rsim build hex    # 编译中 Ctrl+C 可中断
rsim build selena # 正常编译
```

#### Stage 1.3: 分析插件系统（2d）

**目标**: 插拔式分析插件

**新建**:
- `core/analysis_plugin.py` — 插件接口：
  ```python
  class AnalysisPlugin(ABC):
      @property
      @abstractmethod
      def name(self) -> str:
      @abstractmethod
      def analyze(self, signals, context) -> PluginResult:
      def ask(self, question, signals, context) -> str:
  ```
- `core/analysis_runner.py` — 执行引擎：
  ```python
  class AnalysisRunner:
      def run(self, mf4_path: str, project: str, plugins: list[str], context: dict) -> list[PluginResult]:
          # 1. 提取信号
          # 2. 加载并执行插件
          # 3. 保存结果到 results/<project>/<timestamp>/
  ```
- `plugins/analysis/signal_summary.py` — 信号统计插件
- `plugins/analysis/rule_check.py` — 规则检查插件
- `plugins/analysis/default_report.py` — HTML 报告插件

**验收**:
```bash
rsim analyze D:/sim/output.mf4
# → 提取信号 → 运行默认插件 → 保存结果 → 显示摘要
```

#### Stage 1.4: CLI 命令重写（1d）

**改动**:
- `cli/analyze.py` — 数据分析入口（调用 AnalysisRunner）
- `cli/ask.py` — AI 问答（调用最近结果的 AI 插件）
- `cli/diff.py` — 结果对比
- `cli/open_vs.py` — 打开 VS 工程
- `rsim.py` — 更新参数解析（`--project` 全局选项）
- 删除 `cli/verify.py`, `cli/run.py`, `cli/corner_case.py`, `cli/advise.py`, `cli/preset.py`, `cli/regression.py`, `cli/scan.py`

**验收**:
```bash
rsim --project ovrs25 analyze D:/sim/output.mf4
rsim --project ovrs25 ask "FCTA 激活了吗？"
rsim diff results/ovrs25/20260614/ results/ovrs25/20260613/
rsim open-vs
```

#### Stage 1.5: 测试重写（1d）

**改动**:
- 重写 `tests/` 下所有测试，对齐 v4 架构
- 移除与废弃模块相关的测试
- 新增分析插件测试
- 新增多项目配置测试

**验收**: 所有测试通过

---

### Phase 2: AI 问答 + 对比分析（2.5d）

| 模块 | 任务 | 验收 |
|------|------|------|
| AI QA 插件 | `plugins/analysis/ai_qa.py` — 基于分析结果的问答 | `rsim ask "问题"` |
| Diff 分析 | 两次结果对比 + 变化解读 | `rsim diff d1 d2` |
| 历史管理 | 分析结果存档 + 快速检索 | `rsim history` |

---

### Phase 3: 体验打磨（2.5d）

| 模块 | 任务 | 验收 |
|------|------|------|
| VS 集成 | `open-vs` 命令 + VS 工程定位 | `rsim open-vs` 打开 VS |
| 环境自检 | 启动时自动检测所有依赖 | `rsim check` |
| 配置向导 | `rsim init <project>` 初始化项目 | 交互式初始化 |
| 清理 | 移除废弃模块、整理代码 | 代码整洁 |

---

## 3. 文件变更清单

### 新建文件

| 文件 | 功能 |
|------|------|
| `core/analysis_plugin.py` | 分析插件接口 |
| `core/analysis_runner.py` | 分析执行引擎 |
| `cli/ask.py` | AI 问答命令 |
| `cli/open_vs.py` | 打开 VS 命令 |
| `plugins/analysis/signal_summary.py` | 信号统计插件 |
| `plugins/analysis/default_report.py` | 默认报告插件 |
| `plugins/analysis/ai_qa.py` | AI 问答插件 |
| `config/default.yaml` | 全局默认配置 |
| `config/projects/ovrs25/config.yaml` | 项目配置 |
| `config/projects/ovrs25/signals.yaml` | 信号配置 |
| `config/projects/ovrs25/rules.yaml` | 规则配置 |

### 重写文件

| 文件 | 改动 |
|------|------|
| `core/config.py` | 多项目 + assets 管理 |
| `core/models.py` | 移除 SimConfig，新增 AnalysisContext/PluginResult |
| `cli/build.py` | HEX 可中断 + Selena 独立 |
| `cli/analyze.py` | 调用 AnalysisRunner |
| `cli/diff.py` | 对比分析结果 |
| `rsim.py` | 添加 --project 参数 |
| `platforms/gen5_selena/builder.py` | 进度显示 + 中断支持 |

### 删除文件

| 文件 | 原因 |
|------|------|
| `cli/verify.py` | 自动化 pipeline 不再适用 |
| `cli/run.py` | 不控制仿真运行 |
| `cli/corner_case.py` | 不需要 |
| `cli/advise.py` | 不需要 |
| `cli/preset.py` | 不需要 |
| `cli/regression.py` | 不需要 |
| `cli/scan.py` | 不需要 |
| `core/orchestrator.py` | pipeline 模型废弃 |
| `plugins/scanner/scanner.py` | 不做扫描 |
| `plugins/scanner/parallel_runner.py` | 不做并行 |
| `plugins/scanner/regression.py` | 不做回归 |
| `plugins/scanner/preset.py` | 不需要 |
| `plugins/analysis/corner_case_generator.py` | 不需要 |
| `plugins/analysis/advisor.py` | 不需要 |
| `plugins/analysis/auto_signal_selector.py` | 不需要 |
| `DESIGN_PHASE5.md` | v3 设计，已废弃 |

---

## 4. 迁移策略

### 对用户的影响

- 现有 `config.yaml` 格式向后兼容（自动迁移到 `config/projects/<name>/config.yaml`）
- `rsim verify` 命令移除，改为 `rsim build + rsim analyze` 两步
- 其他命令（check, signal, rule）保持可用

### 代码迁移步骤

1. 先写 v4 新代码（config, analysis_plugin, analysis_runner, cli/analyze, cli/build）
2. 验证新代码工作正常
3. 再删除废弃模块
4. 最后清理测试

---

## 5. 里程碑

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | PRD v4.0.0 + HANDOFF | ✅ 完成 |
| M2 | 配置系统重写 | 待开始 |
| M3 | 编译模块重写 | 待开始 |
| M4 | 分析插件系统 | 待开始 |
| M5 | CLI 命令重写 | 待开始 |
| M6 | 测试全部通过 | 待开始 |
| M7 | 废弃模块清理 | 待开始 |
| M8 | Phase 2 — AI QA + diff | 待开始 |

---

## 6. Phase 4：模块化双通路仿真（2026-07）

> **关联:** PRD.md §11 仿真执行后端（v4 扩展），plan: `vectorized-wibbling-biscuit.md`

### 背景

v4 原定位"不控制仿真运行"已被实践超越：`rsim run`（本地自动仿真）和 `rsim cluster`（集群仿真）均已实现并验证。Phase 4 把本地+云端双通路正式模块化，统一配置模型，补齐数据自适应与环境检查。

### 完成项

| 模块 | 内容 | 状态 |
|------|------|------|
| `core/data.py` | MF4 发现、可达性校验、按需迁移、信号扫描（从 cluster.py 抽取） | ✅ |
| `core/profiles.py` | 统一 profile 模型（backend/selena/data），向后兼容 cluster.profiles | ✅ |
| `core/environment.py` | 本地/集群后端统一检查入口 | ✅ |
| `cli/run.py` | 接入 `--profile`/`--select`，目录扫描选择，公盘原地引用，结果校验 | ✅ |
| `cli/cluster.py` | 新增 `cluster run`（prepare→submit→wait→fetch），Selena来源/数据迁移自适应 | ✅ |
| ovrs25 config | 顶层 `profiles` 加 `local-build` 默认 profile | ✅ |
| 测试 | `test_data.py`/`test_profiles.py` 新增 + cluster adaptivity 测试，全量 159 passed | ✅ |
| PRD.md | §11 仿真执行后端 + §5.1 CLI 表 | ✅ |
| docs/config-guide.md | profile 配置文档 | 待补 |

### 设计要点

- **profile 模式**：一个项目多个 profile，`--profile <name>` 切换整组（Selena来源+数据策略+后端）。
- **校验优先 + 按需迁移**：公盘 UNC 数据原地引用；本地数据上云时 `copy=true` 迁移，`copy=false` 报错不静默提交。
- **Selena 来源自适应**：`source=build` 派生自编译产物（集群自动打包推送），`source=path` 用已有 exe。
- **向后兼容**：旧 `cluster.profiles` 自动转换；default profile 走本地编译+原地引用。

### 验收

```bash
rsim --project ovrs25 check --backend local                                  # 本地环境检查
rsim --project ovrs25 run --profile local-build --dataset CBNA_23-4-26-local --select   # 本地闭环
rsim --project ovrs25 cluster run --profile byd-ovrs-bl01v7-er-shared --dataset BYD_SR   # 云端 dry-run
pytest -q                                                                     # 159 passed
```

---

## 7. Phase 5：Linux 控制面 + Windows Agent 服务化迁移（2026-07-07 盘点）

### 目标

把 `radar-sim` 从“单机工具”演进为“Linux 稳定控制壳 + Windows 本机执行 agent”：

- Linux 只运行 control server、Web/SDK 入口、SQLite job/log 存储和用户路由。
- Windows 用户机运行 `rsim agent`，真实执行本机 Selena 编译、本地仿真、TCC 修复、cluster gateway 操作。
- 不把 `selena.exe`、VS、TCC、`.bat` 构建链迁到 Linux。

### 当前已完成

| 模块 | 状态 | 说明 |
|------|------|------|
| `core/control_service.py` | 完成 | agents/jobs/tasks/logs SQLite 状态机，ordered task，cancel，reclaim。 |
| `core/control_http.py` | 完成 | stdlib HTTP JSON API，多用户 `X-Rsim-User` 路由。 |
| `cli/server.py` | 完成 | `serve/create-job/get-job/get-logs/cancel/reclaim`，server 命令不需要加载项目配置。 |
| `cli/agent.py` | 完成 | Windows polling agent，认领任务后调用本机 `rsim` 子命令。 |
| `core/user.py` | 完成 | `RSIM_USER` / OS user 到 per-user DB。 |
| `core/remote_control.py` | 完成 | web/SDK 侧远程 control client。 |
| `core/web_control.py` | 完成 | web task shape 适配 control plane，本地/远程模式共用。 |
| `rsim web --server-url` | 完成 | web 可只做远程 server 前端，不启动内置 agent。 |
| zipapp / Docker | 完成 | `scripts/build_server_pyz.py` 和 `Dockerfile` 支持轻量 Linux server 分发。 |
| 部署/runbook 文档 | 初版完成 | `docs/linux-server-deploy.md`、`docs/e2e-linux-windows-runbook.md`、`docs/KNOWN_ISSUES.md`。 |

### 当前验证

```bash
python -m pytest tests\test_control_service.py tests\test_control_http.py tests\test_control_agent.py tests\test_user.py tests\test_remote_control.py tests\test_web_control.py tests\test_reclaim.py tests\test_server_pyz.py -q
# 56 passed

python -m pytest -q
# 336 passed in 105.85s
```

### 待改造清单

#### P0：文档与实现对齐

- `docs/e2e-linux-windows-runbook.md` 引用了 `GET /api/agents`，当前未实现。
- `docs/server-deploy.md` 仍有“web --server-url 当前未实现”的旧注记，需更新为已实现。
- README 控制面章节需要补三种模式：embedded / remote / legacy。

#### P1：可观测性 API ✅ 2026-07-07 完成

- ✅ `ControlService.list_agents()`（`core/control_service.py`）。
- ✅ `GET /api/agents`（`core/control_http.py`），返回 agent_id/name/status/last_heartbeat/current_task_id/capabilities/hostname/platform。
- ✅ `rsim server list-agents`（`cli/server.py`，UTF-8 stdout 修复 KI-3.1）。
- ✅ web 前端 `/api/agents`（`cli/web.py` + `core/web_control.py:list_agents_via_control` + `RemoteControlClient.list_agents`），嵌入式/远程模式共用。
- 可选：`GET /api/summary` 返回 queued/running/succeeded/failed 数量。⏳ 未做（list_agents + list_jobs 已覆盖运维查询需求）。
- 测试：+5（service/http/server_cli/web_control/remote），全量 338 → 343 passed。loopback 验证 agent 注册后 `GET /api/agents` 与 `rsim server list-agents` 均可见，投 task 期间 status=idle↔busy 反映正确。

#### P2：server 内置周期 reclaim

- `rsim server serve --reclaim-interval <sec>`。
- `--stale-after` / `--max-attempts` 复用现有 `reclaim_stale_tasks`。
- 默认策略需保守，避免误判长时间任务；heartbeat 正常时不能回收。

#### P3：最小鉴权

- `RSIM_SERVER_TOKEN` / `--token`。
- server 校验 `Authorization: Bearer` 或 `X-Rsim-Token`。
- agent、RemoteControlClient、web remote mode 统一带 token。
- 文档明确当前 `X-Rsim-User` 只是路由，不是安全边界。

#### P4：真实跨机回归

- 构建 `dist/rsim_server.pyz`。✅ 已重建（含 create-job 修复 + list-agents + UTF-8）。
- Linux server 启动，Windows agent 连接。✅ 2026-07-07 跨机完成（10.190.171.44 Ubuntu 22.04 + Python 3.10.12）。
- 验证：agent list、local.check、build dry path、run dry-run、cancel、reclaim。
  - **loopback 已验证**（2026-07-07）：local.check ✅、run_sim dry-run ✅、cancel ✅、reclaim ✅。详见 HANDOFF.md「端到端 loopback 回归」。
  - **跨机已验证**（2026-07-07）：health ✅、agent 注册+list-agents ✅、local.check ✅、run_sim dry-run ✅、cancel ✅、reclaim+恢复 ✅。详见 HANDOFF.md「真实跨机端到端回归」。
  - **agent list ✅ 已实现**（P1 已完成并跨机验证）。
  - **build dry path ⚠️**：build 命令无 `--dry-run`，真实编译需 VS 工具链，留待。
- 可用真实数据时跑一条 CBNA smoke，并记录 job_id、agent_id、输出大小和日志证据。⏳ 待跑（本机数据 `D:/data/byd/...CBNA_23-4-26` 可用，agent 在 Windows 本机执行）。
- **本次回归修复**：
  - server create-job project 丢失 bug（`--project`/`--payload-json` 的 project 被空默认值覆盖，阻断跨机投递传 project）。`cli/server.py` 修复 + 回归测试。
  - P1 可观测性 API（`list_agents` 全链路：service/HTTP/CLI/web/remote）。
  - `test_control_agent.py` Linux 可移植性（`python3` 兼容）。

#### P5：SDK 化

- 在 `core.remote_control.RemoteControlClient` 之上包装稳定 SDK。
- 固定 API：create/check/build/run/status/logs/cancel。
- 前端和外部自动化逐步改为依赖 SDK，而不是直接拼 HTTP payload。

### 关键约束

- 跨机 job payload 不要传本机绝对路径。Linux 投递给 Windows agent 时优先传 `project` 和 `dataset`，由 agent 在 Windows 本机解析。
- remote web 只负责投 job 和查状态，执行必须有 Windows agent 在线。
- Linux server 不安装重依赖，不导入 PyYAML/asammdf/openai；server-only zipapp 必须保持 stdlib-only。
