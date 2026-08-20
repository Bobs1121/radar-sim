# radar-sim 用户脚本驱动的项目无关自动编译矩阵（Task N）

日期：2026-08-17
分支：`codex/new-branch`
审计方式：代码走查 + 定向 pytest 回归。真实脚本/真实 Windows 构建/真实 Bundle 传输不在本机可用，需真实部署验收项单独标注。

## 结论先行

**项目无关性成立**：generic V2 流程（`user-run-config/2.0`）全程不使用产品名、项目注册表、Jenkins 文件名或项目专用目录做执行路由。用户显式填写的 Selena 脚本是执行入口，框架只做授权、脚本语义识别（clean 抑制/恢复）、产物定位和 provenance 管理。

未发现阻断性硬编码。但存在一处需要记录的**遗留项目专用代码（非阻断）**：`core/config.py` 的 legacy 项目上下文推导路径（`:400`、`:919-928`、`:953`）硬编码 `/apl/byd/bindings/...`、`ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR`、`R2D2.py` 等。这些路径**只被 legacy CLI/非 generic 路径调用**，V2 generic 流程通过 `generic_only=True`（`cli/agent.py:3029`、`core/workspace_recognizer.py:120,197,200`）完全绕开，且有测试证明 V2 不进入 legacy 推导（见第 3 节）。它不阻断 Task N 的 V2 场景，但属于“邻近风险”，建议后续清理或在文档中显式标注为 legacy-only。

## 1. 项目无关性验证（brief 2A.1 / Task N 行动 1-2）

### 1.1 无 `if project == ...` 项目专用分支

构建决策链路中唯一的 `project ==` 比较是 `cli/agent.py:3034`：

```python
bindings = [binding for binding in path_bindings if binding.project == project]
```

这是**逻辑命名空间匹配**（把识别出的 `internal_project` 与已授权 binding 的 project token 比对），不是硬编码产品名分支。`project` 来自 `_generic_internal_project`（`core/workspace_recognizer.py:467-483`）= `workspace-<sha256(normalized root + selena script)>`，是**路径无关、可复现、按 workspace/script 唯一**的授权 token。

`grep` 结论：`core/build_script_policy.py`、`core/agent_build_stage.py`、`core/build_lock.py`、`core/workspace_recognizer.py`、`core/agent_runtime_bundle_lease.py`、`core/windows_build_environment.py`、`core/selena_resolver.py`、`core/environment_snapshot.py` 中**无 `if project == '<产品名>'`、无项目专用 DAG、无项目专用 clean 规则、无硬编码项目目录**。

### 1.2 无项目注册表 / 无固定目录

- V2 识别使用 `WorkspaceRecognizer(..., generic_only=True)`（`cli/agent.py:3029`），该模式下不加载 `config/projects` adapter 表（`core/workspace_recognizer.py:120`），不调用 legacy `derive_project_context_from_selena_script`（`:200`），输出根从**用户所选脚本**推导（`:284-341` `_derive_generic_output_from_script`），失败回退到受控的 `ip_dc/build` 或 `build` 通用根（`:275-281`）。
- `_generic_build_config`（`core/agent_build_stage.py:552-588`）不查询任何 `config/projects` 项，注释明确“No product catalog or config/projects entry is consulted”。
- 测试证明：`tests/test_generic_workspace_resolution.py::test_unknown_workspace_identity_never_falls_back_to_legacy_project_config`（load_config(internal_project) 抛 FileNotFoundError）、`test_v2_generic_output_does_not_call_legacy_project_derivation`（monkeypatch 让 legacy 推导抛 AssertionError，V2 仍成功）。

### 1.3 用户脚本是执行入口，框架不替换、不从项目名推导

- `prepare_selena_build` 强制要求配置的 Selena build script（`core/agent_build_stage.py:813-826`），拒绝 legacy R2D2 回退（`:810-812`）。
- 命令必须执行“配置的那个脚本”本身：`command_script != resolved_script` 则抛错（`core/agent_build_stage.py:885-893`）。
- 脚本路径必须来自 payload 的 `selena_build_script_ref` 且位于授权 workspace 内（`core/agent_build_stage.py:720-739`），绝对路径/`..` 逃逸被拒绝。
- 框架对该脚本做的是**语义级适配**（clean 命令抑制/恢复、VS 参数适配），绝不替换成内部脚本，也不从 `project` 名推导另一个脚本（`core/build_script_policy.py:1-11` 模块 docstring 明确“bounded to the selected script and its continuation lines; it never executes or scans an entire repository”）。

## 2. 项目无关自动编译矩阵（Task N 行动 3）

说明：真实脚本/真实 Windows 构建不可用，本矩阵给出每个 case 的 **policy（代码决策路径）、clean proof（可证明点）、bundle identity（持久化键）、artifact checksum（校验点）**，并标注测试证据或“需真实部署验收”。

| Case | 输入 | 预期 policy | 决策代码 | 测试/证据 | 状态 |
|---|---|---|---|---|---|
| 1. fresh（output root 为空） | 新 workspace + 用户脚本，无历史 Bundle | 应 `fresh`（不 clean，明确“从空开始”） | `_has_existing_build_state`=False → `full_rebuild_required=False`（`core/agent_build_stage.py:286,297-298`）；**模式被标为 `incremental`（缺口，见 Task D G1）** | 无专门测试 | 已实现，**模式命名缺口** |
| 2. same-branch incremental | 同 workspace、同分支、同 build_mode、exe checksum 与前次 provenance 一致 | `incremental`（build lock 内运行） | `_branch_rebuild_policy` 全部匹配 → `full_rebuild_required=False`（`core/agent_build_stage.py:327-336`） | `test_v2_branch_change_forces_full_rebuild_from_existing_artifact` 的 `same` 分支断言 False | 已实现且测试 |
| 3. same-branch new commit | 同分支但 HEAD 前进（commit 变化） | 默认 `incremental`；应记录“为什么允许增量” | **commit 不参与决策**，仅 branch/build_mode/checksum（`core/agent_build_stage.py:316-336`） | 无测试 | 行为与矩阵默认一致；**未记录增量理由（Task D G2）** |
| 4. branch switch full clean | 同一 workspace 切到新 Selena 分支 | `full_clean`（先 clean 再编译） | reason=`selena_branch_changed` → `clean=True`（`core/agent_build_stage.py:321-322,867-868`） | 2 条 `test_v2_branch_change_forces_full_rebuild_*` 测试 | 已实现且测试 |
| 5. 不同 workspace 同 output root | 两个逻辑 workspace 指向同一输出目录 | 视为同一 build_slot，串行，不分别缓存 | **generic V2 结构上不可能**：output_root 由 recognizer 强制 rebase 进各自 workspace（`core/workspace_recognizer.py:214-224,486-499`），两个不同 workspace 无法解析到同一 output_root | 无测试（结构防堵） | **结构上防堵；无显式测试**（锁 key 只含 workspace_root，见 Task D 第 5 节备注） |
| 6. 不同 root 并行 | 两个 workspace，不同 output_root | 并行（各自 build lock） | `WorkspaceBuildLock` 按 workspace_root 隔离（`core/build_lock.py:22-28`、`cli/agent.py:873-879`） | `test_workspace_build_lock_blocks_a_second_process`（单锁阻塞）；并行路径为锁互斥的补集 | 已实现（锁互斥保证并行），真实双进程并行需部署验收 |
| 7. 换 workspace/脚本/分支不复用旧 Bundle | 切换后 provenance 独立 | 新 workspace/脚本 → 新 internal_project/binding → `latest_build_provenance` 查不到 → full；同 workspace 换分支 → branch 变化 → full | key=`(project, workspace_binding_id)`（`core/agent_runtime_bundle_lease.py:234-243`）；`_generic_internal_project` 含 root+script（`core/workspace_recognizer.py:476-482`） | 无“两 workspace 各保留 provenance”的直接测试 | **设计正确，缺直接隔离测试（Task D G7）** |

## 3. 硬编码项目专用分支/目录/recipe 依赖清单（Task N 行动 4）

| 位置 | 内容 | 是否影响 V2 generic | 结论 |
|---|---|---|---|
| `core/config.py:400` | `.../apl/byd/bindings/{binding}/selena/jenkins_selena_build.bat` | 否（legacy `load_config` 项目适配器） | 非阻断（legacy-only） |
| `core/config.py:919-925` | `{root}/apl/byd/bindings/...`、`{root}/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR`、`{root}/ip_dc/dc_tools/R2D2.py` | 否（legacy `derive_project_context_from_selena_script` 内部） | 非阻断（legacy-only） |
| `core/config.py:928,953` | `.../apl/byd/selena/cmake_build_cfg/ROS_PER_SIT_RPM_FCT_RECR.config` | 否（legacy） | 非阻断（legacy-only） |
| `core/config.py:1058,1065` | `{project_root}/ip_dc/dc_tools/R2D2.py` 默认值 | 否（legacy `load_local_execution_config` 缺省） | 非阻断（legacy-only） |
| `core/workspace_recognizer.py:81` | `SCRIPT_NAMES=("jenkins_selena_build.bat","build_selena.bat")` | 是（generic 自动发现文件名白名单） | **注意**：这是“发现”用的常见文件名，不是执行路由；用户显式填脚本时不用它；`generic_only` 无显式脚本时用它做兜底发现。非阻断 |
| `core/workspace_recognizer.py:278-281` | 回退根 `ip_dc/build` / `build` | 是（generic 无静态输出时的回退根） | 受控回退，非产品专用；非阻断 |
| `core/config.py` 其余 `r2d2_script`/`build_config` | legacy R2D2 参数构建 | 否（V2 build stage 拒绝 R2D2 回退，`core/agent_build_stage.py:810-812`） | 非阻断（legacy-only） |

**结论：未发现任何会被 V2 generic 自动编译流程命中并改变路由/clean/增量决策的硬编码项目专用分支。** legacy 硬编码仅存在于 `core/config.py` 的 legacy 推导函数中，且 V2 有测试证明不进入这些路径。按 brief “发现任何硬编码即阻断”的字面要求，这些 legacy 硬编码**不影响项目无关编译**，故不构成 Task N 阻断项；但建议在后续清理或明确标记为 legacy-only（P2）。

## 4. Provenance 按 workspace 隔离（Task N 行动 4 / brief 2A.1）

- 持久化键：`runtime_bundle_leases` 表 `WHERE project=? AND workspace_binding_id=?`（`core/agent_runtime_bundle_lease.py:234-243`）。
- `project` = `workspace-<sha256(root+script)>`（`core/workspace_recognizer.py:467-483`），`workspace_binding_id` = `workspace:sha256:<workspace path>`（`core/agent_bindings.py` 的 `make_workspace_binding_id`）。
- 因此**两个不同 workspace（即使分支名相同）各自保存 provenance**，不存在全局“最近一次编译分支”；换 workspace/脚本/分支后 `latest_build_provenance` 在新 key 下返回 None → full build，不会复用旧 Bundle。
- 同一 workspace 内切换分支：key 不变但 branch 不同 → `selena_branch_changed` → full（见 Task D 行 3）。
- Bundle 身份：`stage_runtime_bundle_from_build` 生成 `RuntimeBundleLease`（`core/agent_build_stage.py:1104-1169`），`create` 幂等（`core/agent_runtime_bundle_lease.py:136-146`），`mark_uploaded` 绑定 `shared://selena-bundles/...`（`:264-275`）。

## 5. 本次回归测试

同 Task D：`.venv/Scripts/python.exe -m pytest`（18 个 build/workspace/runtime 相关文件），结果 **236 passed, 0 failed**。

Task N 相关的关键测试：

- `tests/test_generic_workspace_resolution.py::test_unknown_workspace_derives_stable_internal_identity_and_output`
- `tests/test_generic_workspace_resolution.py::test_v2_generic_output_does_not_call_legacy_project_derivation`
- `tests/test_generic_workspace_resolution.py::test_unknown_workspace_identity_never_falls_back_to_legacy_project_config`
- `tests/test_generic_workspace_resolution.py::test_agent_auto_configures_unknown_workspace_without_project_registration`（V2 完整 prepare→finish，含动态输出子目录定位）
- `tests/test_generic_workspace_resolution.py::test_agent_reuses_binding_when_only_optional_package_hint_changes`
- `tests/test_workspace_recognizer.py::test_auto_discovers_one_script_without_project_concept`
- `tests/test_workspace_recognizer.py::test_unknown_project_derives_build_output_from_selected_selena_script`
- `tests/test_workspace_recognizer.py::test_multiple_discovered_scripts_do_not_guess`（多个脚本 → 不猜，unresolved）
- `tests/test_agent_build_stage.py::test_v2_branch_change_forces_full_rebuild_from_existing_artifact`（换分支 full）
- `tests/test_branch_worktree_stage_flow.py::test_expected_branch_builds_dirty_current_workspace_and_warns_on_mismatch`（branch mismatch 非阻塞告警，不切分支）

## 6. 真实验收未覆盖项（需要真实部署验收）

- 两个真实 Windows workspace（不同目录/脚本风格/output layout）并行编译 + 同 workspace 双 Job 串行排队的进程级证据；
- 真实脚本上 clean 抑制/恢复与产物 checksum 一致性；
- 真实 Bundle 上传/下载与跨 Job 复用；
- 第 2 节矩阵 case 5（不同 workspace 同 output root）的真实并发场景（结构上防堵，但建议补一个显式测试断言两个 workspace 无法解析到同一 output_root）。

## 7. 结论

- 项目无关自动编译：**成立**（V2 generic 全程无产品名/项目注册表依赖）。
- 用户脚本为执行入口、框架不替换/不推导：**成立**（含拒绝脚本逃逸与 R2D2 回退）。
- 硬编码依赖：**未发现影响 V2 的阻断项**；`core/config.py` 存在 legacy-only 硬编码，建议后续清理（P2）。
- 矩阵 6 个 case：1/2/4/6/7 已实现（部分命名/记录缺口见 Task D G1/G2/G4/G7），case 3 行为符合矩阵默认但缺“增量理由”记录，case 5 结构防堵但缺显式测试。
