# radar-sim Selena 构建槽位与产物 provenance 审计（Task D）

日期：2026-08-17
分支：`codex/new-branch`
审计方式：代码走查 + 定向 pytest 回归（本机无真实 Windows/Cluster 构建日志，需真实部署验收项单独标注）。
范围：`core/agent_build_stage.py`、`core/build_script_policy.py`、`core/agent_runtime_bundle_lease.py`、`core/build_lock.py`、`core/workspace_recognizer.py`，以及执行入口 `cli/agent.py`、`core/environment_snapshot.py`、`core/runtime_bundle.py`。

## 结论先行

编译策略的核心骨架是**按真实构建边界（workspace/binding/script/output root）**而不是按项目名决策：

- 决策入口 `_branch_rebuild_policy`（`core/agent_build_stage.py:266-336`）只比较 **branch、build_mode、entrypoint checksum**，从不读取项目名/目录名做判断；
- `build_slot` 语义由 `WorkspaceBuildLock`（`core/build_lock.py`）按 workspace 单飞串行化（`cli/agent.py:858-889`）；
- provenance 持久化到 Agent 本地 SQLite `runtime_bundle_leases`（`core/agent_runtime_bundle_lease.py:220-262`），按 `(project, workspace_binding_id)` 隔离；
- 真实的 `selena_branch_changed → full → clean=True` 已在本机策略预演和线上 attempt=4 事件证明（见背景 handoff），本次审计用代码 + 测试复证了该分支。

但第 5.3 节决策矩阵**只有部分行被实现且测试**。逐行结论如下（详见第 2 节）：**9 行中 3 行已实现且测试（分支不同、空根无 provenance、脚本被替换防护），3 行已实现但缺少直接测试（无 provenance、build_mode 变化、无 clean 命令阻断），3 行存在实现或命名缺口（fresh 被误标为 incremental、commit/source fingerprint 不参与决策、toolchain/build script checksum 不参与决策、未记录 `incremental_reused=true`、未记录 `clean_applied` 结构化字段）。**

风险等级：非阻断（P1）。没有任何一行会让“旧分支产物被静默当作新分支增量基础”的已知故障复发；但矩阵的“fresh/incremental 命名”“commit 变化决策”“clean_applied 结构化证据”三项需要补强才能满足 brief 5.2/5.4 的字面要求。

## 1. 代码走查：构建决策链路

完整决策链路（以 v2 user-run-config/2.0 为例）：

```text
用户提交（project 是逻辑 token，非产品名）
  -> prepare_selena_build(contract=user-run-config/2.0)      core/agent_build_stage.py:609
       -> _generic_build_config(project, binding)            core/agent_build_stage.py:552（不查 config/projects）
       -> _branch_rebuild_policy(...)                        core/agent_build_stage.py:266（决策矩阵核心）
       -> full_rebuild_required=True 时 clean=True           core/agent_build_stage.py:867-868
       -> adapt_build_script_for_incremental(allow_clean=clean)  core/agent_build_stage.py:899-914
            clean 语义无法识别且需要 full -> 抛错阻断          core/agent_build_stage.py:909-912
  -> cli/agent.py 持有 WorkspaceBuildLock（按 workspace_root） cli/agent.py:858-889
  -> 再次 _prepare_v5_selena_build（锁内重新校验脚本 checksum） cli/agent.py:894
  -> _verify_v5_selena_build -> verify_prepared_build（脚本未变） core/agent_build_stage.py:968-977
  -> 执行脚本
  -> _finish_v5_selena_build -> finish_selena_build（产出 build_policy 摘要） core/agent_build_stage.py:980-1101
  -> stage_runtime_bundle_from_build（Bundle + toolchain fingerprint） core/agent_build_stage.py:1104-1169
  -> AgentRuntimeBundleLeaseStore.create（持久化 manifest/source） core/agent_runtime_bundle_lease.py:108-163
```

环境检查阶段（`environment_check`）在编译前先做一次结构化策略检查，输出 `incremental_build_policy` check（`core/environment_snapshot.py:436-487`），含 `selena_full_rebuild_required` / `selena_clean_commands_suppressed` / `selena_clean_explicitly_allowed` 等 code，并在需要 full 且无 clean 命令时同样阻断（`core/environment_snapshot.py:446-449`）。

### 1.1 `max_candidates=512` 深层目录漏检修复

修复点：`_has_existing_build_state`（`core/agent_build_stage.py:217-244`）。

```python
if has_existing_build_artifact(artifact_path, output_roots, max_candidates=512):
    return True
for raw_root in output_roots or ():
    root = Path(raw_root)
    ...
    next(root.iterdir())   # 只要授权 output root 下存在任何条目即视为有既有构建状态
    return True
```

也就是说，`build_script_policy.has_existing_build_artifact`（`core/build_script_policy.py:198-239`）仍保留 `max_candidates=512` 作为“找到 `selena.exe`”的加速扫描，但**provenance 决策不再依赖前 512 个条目**：只要授权 output root 非空（`next(root.iterdir())` 直接命中任意一层），就认为存在既有构建状态，进入 provenance 决策。这正是 brief 1.2 中 `job_26028465ebeb` 深层目录 `ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR/...` 漏检的修复机制。

测试证明：`tests/test_agent_build_stage.py::test_v2_branch_change_forces_full_rebuild_when_exe_is_nested_in_output_tree` —— 在深层目录只放 `old.obj`（没有 `selena.exe`），断言 `existing_build_detected=True`、`full_rebuild_required=True`、reason=`selena_branch_changed`。

## 2. 第 5.3 节决策矩阵逐行核对

| # | 矩阵条件 | 预期模式 | 代码路径 | 测试证据 | 状态 |
|---|---|---|---|---|---|
| 1 | output root 为空、无历史 Bundle、无旧 object | `fresh`（不执行 clean，但必须明确记录“从空构建状态开始”，不能宣传为增量复用） | `_has_existing_build_state`=False → `_branch_rebuild_policy` 返回 `full_rebuild_required=False`；`finish_selena_build` 输出 `build_policy.mode="incremental"`（`core/agent_build_stage.py:1074`） | 无专门测试 | **缺口：fresh 被标成 `incremental`，违反“不能把它宣传为增量复用”**。`cli/agent.py:787-793` 在无 reason 时回退 `default_incremental` |
| 2 | output root 非空但无 provenance | `full_clean`（有 clean 命令则执行，没有则阻断） | `_branch_rebuild_policy`：`latest_build_provenance` 返回 None → reason=`existing_artifact_provenance_unavailable` → full（`core/agent_build_stage.py:317-318`） | 无直接测试（`_branch_rebuild_policy` 测试都用 FakeLeaseStore 返回 dict） | 已实现，**缺直接测试** |
| 3 | provenance branch 不同 | `full_clean` | `requested_branch.casefold() != previous_branch.casefold()` → reason=`selena_branch_changed` → full（`core/agent_build_stage.py:321-322`）；`clean=True`（`:867-868`）；脚本 clean 命令被恢复执行 | `test_v2_branch_change_forces_full_rebuild_from_existing_artifact`、`test_v2_branch_change_forces_full_rebuild_when_exe_is_nested_in_output_tree` | **已实现且测试** |
| 4 | branch 相同但 commit/dirty/source fingerprint 改变 | 默认 `incremental`，若依赖图/脚本不能证明安全则 `full_clean`；记录为什么允许增量 | **commit 不参与决策**。只比较 branch + build_mode + entrypoint checksum。same-branch-new-commit 且 exe checksum 不变 → `incremental`（reason 为空，不记录“为什么允许增量”） | 无测试 | **部分缺口：commit/source fingerprint 不参与决策，也未记录增量理由（“禁止静默猜测”未满足）** |
| 5 | build mode/config/toolchain/build script 不同 | `full_clean` | **仅 build_mode 参与决策**（reason=`selena_build_mode_changed`，`core/agent_build_stage.py:323-324`）；toolchain fingerprint 与 build script checksum 只被写入 Bundle（`core/agent_build_stage.py:1130-1132`），**不参与重建决策** | 无 build_mode 变化测试 | **部分缺口：toolchain/build script 变化不会触发 full_clean** |
| 6 | output root/workspace/script 变化，或 provenance 与 artifact checksum 不一致 | `full_clean` | workspace/script 变化 → internal_project/binding 变化 → `latest_build_provenance` 按新 key 查不到 → full（行 2 路径）；exe checksum 不一致 → reason=`existing_artifact_content_changed`（`core/agent_build_stage.py:331-332`） | `selena_branch_changed` 两条测试间接覆盖了 key 隔离；content_changed 无直接测试 | 已实现，content_changed **缺直接测试** |
| 7 | provenance 完整一致、artifact/DLL/Runtime checksum 一致、同一 build slot | `incremental`，只在 build lock 内运行，记录 `incremental_reused=true` | `full_rebuild_required=False` → incremental；在 `WorkspaceBuildLock` 内执行（`cli/agent.py:858-889`）；**未输出 `incremental_reused=true` 字段** | `same` 分支断言 `full_rebuild_required=False`（test_v2_branch_change...） | 已实现（lock 内运行）；**缺 `incremental_reused=true` 记录** |
| 8 | branch/ref/commit 无法解析 | `full_clean` 或 `needs_input` | `_branch_rebuild_policy`：`not requested_branch or not previous_branch` → reason=`selena_branch_identity_unavailable` → full（`core/agent_build_stage.py:319-320`）；resolver 层：commit 无法解析 → `needs_input(branch_commit_required)`（`core/selena_resolver.py:418-424`） | 无直接测试 | 已实现（full 路径），缺直接测试 |
| 9 | clean 语义无法识别 | `blocked`（不得用增量代替全量） | `prepare_selena_build`：`if full_rebuild_required and not policy_result.clean_command_lines: raise "full rebuild is required but no clean command is available"`（`core/agent_build_stage.py:909-912`）；environment 层同逻辑（`core/environment_snapshot.py:446-449`） | 无直接测试 | 已实现，**缺直接测试** |

补充说明：矩阵第 4 行“commit 改变默认 incremental”在语义上与代码一致（代码就是按 branch 相同即增量），但 brief 要求“记录为什么允许增量；禁止静默猜测”，当前 incremental 时 `reason=""`，没有记录基于 branch/build_mode/checksum 匹配的增量许可证据。建议在 `build_policy` 增加 `incremental_basis`（如 `branch_build_mode_entrypoint_checksum_match`）字段。

## 3. 第 5.2 节 provenance 字段持久化核对

持久化载体：

- `AgentRuntimeBundleLeaseStore`（SQLite `runtime_bundle_leases` 表）：`core/agent_runtime_bundle_lease.py:80-100`（表结构）、`:108-163`（create）、`:220-262`（`latest_build_provenance`）。
- Bundle manifest source（`RuntimeSourceEvidence`）：`core/runtime_bundle.py:56-92`（branch/commit/dirty/dirty_fingerprint/build_mode/toolchain_fingerprint/adapter_key）。
- Build result 摘要（`finish_selena_build`）：`core/agent_build_stage.py:1069-1093`（build_policy、before/after、artifact）。

| 5.2 字段 | 持久化位置 | 决策时是否读取 | 状态 |
|---|---|---|---|
| `workspace_binding_id`、逻辑 execution identity | 表列 `workspace_binding_id` + `project` | 是（查询 key） | 已持久化 |
| `workspace_root_fingerprint` / `output_root_fingerprint` | 未单独持久化；仅 via `project`（workspace-<sha256(root+script)>）与 binding | 间接（key 变化） | **未按字段持久化**（brief 建议保留） |
| Selena `branch/ref`、实际 `commit`、dirty/source fingerprint | `RuntimeSourceEvidence.branch/commit/dirty/dirty_fingerprint` → manifest_json | `branch` 是；**`commit` 不参与决策** | branch 持久化；commit 持久化但未用于决策 |
| build mode/config | `RuntimeSourceEvidence.build_mode` | 是（`build_mode_changed`） | 已持久化 |
| build script checksum、clean command checksum/行号 | **未持久化**。script checksum 只存在于 `PreparedSelenaBuild`/toolchain fingerprint 哈希（`core/agent_build_stage.py:1130-1132`） | 否 | **未按字段持久化** |
| VS/CMake/Python/Perl toolchain fingerprint | `RuntimeSourceEvidence.toolchain_fingerprint` | 否 | 持久化但**未参与决策** |
| `Selena.exe`、DLL、Runtime XML checksum/size | Bundle manifest files（`RuntimeFile`） | entrypoint checksum 是；DLL/Runtime XML 否 | 持久化；仅 entrypoint 参与决策 |
| Runtime Bundle ID、创建时间、build stage/attempt | 表列 `build_stage_id/build_attempt/created_at` + `manifest.id` | 否（展示用） | 已持久化 |
| `build_policy.mode`（fresh/incremental/full_clean） | `finish_selena_build` 输出 `"mode": "full"/"incremental"` | — | **命名不一致**：代码用 `full`/`incremental`，brief 用 `fresh`/`incremental`/`full_clean`；fresh 被归入 `incremental` |
| `clean_required` / `clean_applied` / `clean_proof` / `reason` | `reason` 在 build_policy 中；**`clean_required`/`clean_applied`/`clean_proof` 无输出字段** | — | **缺口：`clean_applied` 未记录为结构化字段**（见第 4 节） |

结论：核心 source identity（branch/build_mode/entrypoint checksum）已按 workspace 持久化并可被决策读取；但 5.2 中 workspace/output root fingerprint、build script checksum、clean 命令证据、`clean_applied/clean_proof`、`incremental_reused` 等字段**未按 brief 要求持久化**。

## 4. 第 5.4 节“真全量”四类证据核对

| 证据类别 | 要求 | 代码/测试可证明性 | 状态 |
|---|---|---|---|
| 1. 决策证据 | `build_policy.mode=full_clean`、`reason=selena_branch_changed` | `finish_selena_build` 输出 `mode="full"` + `reason="selena_branch_changed"`（`core/agent_build_stage.py:1073-1079`）；`cli/agent.py:796` 事件 `Selena build policy: full (selena_branch_changed)`；`cli/agent.py:800` 事件 `full Selena rebuild required: ...` | 可证明（模式名为 `full`，非 `full_clean`） |
| 2. 脚本证据 | 执行前第 N 行从注释恢复为 active command；script checksum 与执行版本一致 | `adapt_build_script_for_incremental(allow_clean=True)` 恢复被抑制行（`core/build_script_policy.py:178-195`、`:265-282`）；`verify_prepared_build` 执行前重算脚本 checksum（`core/agent_build_stage.py:968-977`）；测试 `test_explicit_clean_restores_a_line_previously_suppressed`、`test_non_batch_scripts_use_their_own_comment_syntax_and_restore` | 可证明（测试覆盖恢复逻辑） |
| 3. 运行证据 | Agent 事件记录 `clean_applied` / `full Selena rebuild required`；不是只看 `echo Cleaning` | `cli/agent.py:800` 记录 `full Selena rebuild required`；**`clean_applied` 无结构化事件**；脚本不再用 `echo Cleaning` 作为成功依据（`is_clean_command` 明确把 `echo Cleaning` 判为非 clean，`core/build_script_policy.py:118-139` + 测试断言 `is_clean_command("echo Cleaning the environment") is False`） | **部分可证明：`full Selena rebuild required` 有；`clean_applied` 缺** |
| 4. 产物证据 | clean 前后构建状态/generation 或 clean marker 可解释；最终 Bundle branch/commit/checksum 与请求一致 | `finish_selena_build` 输出 before/after 快照、artifact checksum（`core/agent_build_stage.py:1080-1093`）；Bundle manifest 固化 branch/commit/entrypoint checksum（`core/runtime_bundle.py`）；`source_changed_during_build` 门禁阻止歧义 Bundle（`core/agent_build_stage.py:1123-1124`） | 可证明（代码路径），真实日志需部署验收 |

结论：**四类证据中 1、2、4 可由代码 + 测试证明；第 3 类“clean_applied”结构化事件缺失**，当前只有“full Selena rebuild required”字符串事件。真实 Windows 构建日志、真实线上 provenance 展示仍需真实部署验收（本机无该环境）。

## 5. 构建槽位与锁

- `WorkspaceBuildLock`（`core/build_lock.py:22-92`）：按 `os.path.normcase(os.path.abspath(workspace))` 的 SHA-256 生成 `.lock`，使用 `msvcrt.locking`（NT）/`fcntl.flock`（POSIX）跨进程互斥；`acquire(wait=True)` 排队而非失败；进程崩溃 OS 自动释放锁，无 stale-lock 恢复问题。
- 锁粒度：`cli/agent.py:873-879` 以 `authorized.workspace_root` 为 key。**同一 workspace_root 串行，不同 workspace_root 并行**。
- build_slot 语义（`device/workspace_binding_id/script/output_root`）：因为 internal_project（`workspace-<sha256(root+script)>`）与 workspace_binding_id（`workspace:sha256:<root>`）都由 workspace/script 派生，output_root 由 recognizer 强制 rebase 进用户 checkout（`core/workspace_recognizer.py:214-224`、`:486-499`），所以**两个不同 workspace 在 generic V2 流程下无法解析到同一个 output_root**——结构上防止了“不同逻辑项目共用一个 output root 却分别缓存”的矩阵行 6 情形。但锁本身只按 workspace_root 互斥：若未来允许外部共享 output_root（非 V2 路径），锁不会覆盖“同一 output root 被不同 workspace 使用”的串行化，需在锁 key 中纳入 output_root。当前 V2 路径不会出现该情形，故不阻断，但作为设计备注记录。
- 锁测试：`tests/test_build_diagnostics.py::test_workspace_build_lock_blocks_a_second_process`（跨进程阻塞与释放）。

## 6. 阻断情形与 fail-closed 汇总

| 情形 | 处理 | 代码 |
|---|---|---|
| 需要 full 但脚本无可识别 clean 命令 | `blocked`，抛错，不默认增量 | `core/agent_build_stage.py:909-912`；`core/environment_snapshot.py:446-449` |
| 无历史 provenance 且 output root 非空 | full_clean | `core/agent_build_stage.py:317-318` |
| branch/ref/commit 无法解析 | full（build 决策）或 needs_input（resolver） | `core/agent_build_stage.py:319-320`；`core/selena_resolver.py:418-424` |
| 脚本在 prepare 后被修改 | 执行前校验 checksum 失败 | `core/agent_build_stage.py:968-977` |
| 脚本非普通文件/在授权 workspace 外/路径字段出现在 payload | 抛错 | `core/agent_build_stage.py:158-177, 813-844` |

## 7. 缺口与建议

| 缺口 | 影响 | 建议 |
|---|---|---|
| G1：fresh 构建被标为 `incremental`（mode 命名与 5.2 `fresh` 不一致），且无“从空构建状态开始”记录 | 违反 5.3 行 1 “不能把它宣传为增量复用” | 在 `_branch_rebuild_policy`/`finish_selena_build` 增加 `fresh` 模式（`existing_build_detected=False` 时），并记录 `fresh_start=true`；加测试 |
| G2：commit/dirty/source fingerprint 不参与重建决策；incremental 无许可理由 | 矩阵行 4 的“记录为什么允许增量；禁止静默猜测”未满足；same-branch-new-commit 不会因 commit 变化强制 full（但 brief 允许默认 incremental，故属记录缺口而非安全漏洞） | 增加 `previous_commit` 与 `requested_commit` 比较作为增量许可证据；输出 `incremental_basis` |
| G3：toolchain fingerprint / build script checksum 不参与决策 | 矩阵行 5 的“toolchain/build script 不同 → full_clean”未实现 | 将 `latest_build_provenance` 扩展返回 toolchain/script checksum，并在 `_branch_rebuild_policy` 比较 |
| G4：`clean_applied` / `clean_proof` / `incremental_reused=true` 未记录 | 5.2/5.4 要求的结构化字段缺失；5.4 运行证据只有字符串事件 | 在 `finish_selena_build`/Agent 事件中输出 `clean_applied`、`clean_proof`（如 clean 命令行号 + 恢复前后 checksum）、`incremental_reused` |
| G5：workspace/output root fingerprint、build script checksum 未按字段持久化 | 5.2 最小字段未全部落库 | 在 `runtime_bundle_leases` 增加 sidecar 字段或在 manifest source 中补充 |
| G6：矩阵多行缺直接测试（行 2、4、5、6 content、8、9） | 只覆盖了 `selena_branch_changed` | 按 brief 5.4 后段列举场景补测试：无 provenance、同分支不同 commit、build_mode 变化、无 clean 命令阻断、多个 selena.exe、脚本 line continuation、不同注释语法、R2D2 --clean、CMake/MSBuild clean |
| G7：provenance 按 `(project, workspace_binding_id)` 隔离，但无“两个 workspace 各自保留 provenance、不共享最近一次编译分支”的直接测试 | Task N 关注点 | 增加两 workspace 同分支名、不同 root 的隔离测试（见 Task N 文档） |

## 8. 真实验收未覆盖项（需要真实部署验收）

以下无法在本机用代码/测试证明，需真实 Windows/Cluster 验收：

- 真实 Windows 编译脚本（含 `R2D2 --clean`、CMake/MSBuild clean、注释/行续接变体）上 `clean_applied`/clean marker 的实际表现；
- 真实线上 provenance 展示与 Bundle 下载校验；
- 两个真实 workspace 并行编译 + 同 workspace 双 Job 串行排队的真实进程级证据；
- 第 5.4 节四类证据在真实 Job 事件流中的完整串接（目前只有 `job_26028465ebeb` 的决策/脚本/事件部分证据记录于背景 handoff）。

## 9. 本次回归测试

执行命令：

```text
.venv/Scripts/python.exe -m pytest tests/test_build_script_policy.py tests/test_agent_build_stage.py tests/test_workspace_recognizer.py tests/test_generic_workspace_resolution.py tests/test_agent_runtime_bundle_lease.py tests/test_branch_worktree_stage_flow.py tests/test_build_diagnostics.py tests/test_environment_snapshot.py tests/test_agent_cli_policy.py tests/test_selena_resolver.py tests/test_agent_runtime_bundle_build.py tests/test_windows_build_environment.py tests/test_existing_selena_agent_resolution.py tests/test_source_resolution_runtime.py tests/test_windows_full_local_e2e.py tests/test_existing_bundle_local_flow.py tests/test_agent_policy.py tests/test_agent_source_lease.py -q
```

结果：`158 + 78 = 236 passed, 0 failed`（第一批 75、第二批 57、第三批 26、第四批 78）。

直接证明决策矩阵/修复的测试：

- `tests/test_agent_build_stage.py::test_v2_branch_change_forces_full_rebuild_from_existing_artifact`
- `tests/test_agent_build_stage.py::test_v2_branch_change_forces_full_rebuild_when_exe_is_nested_in_output_tree`（max_candidates=512 修复的回归）
- `tests/test_build_script_policy.py::test_explicit_clean_restores_a_line_previously_suppressed`（5.4 脚本证据）
- `tests/test_environment_snapshot.py::test_environment_disables_embedded_clean_before_build_handoff`（结构化 policy check）
- `tests/test_build_diagnostics.py::test_workspace_build_lock_blocks_a_second_process`（build_slot 串行）
- `tests/test_generic_workspace_resolution.py::test_v2_generic_output_does_not_call_legacy_project_derivation`（V2 不进入项目专用推导）

## 10. 审计边界

本审计只覆盖构建/槽位/provenance；不覆盖认证、Cluster、结果交付等 Task B/C/G/H 范畴。模式命名差异（`full` vs `full_clean`）在 brief 与代码间存在，需主 AI 汇总时确认是否作为统一合同字段修正。
