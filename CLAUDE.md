# CLAUDE.md — Foundational Mandates & Anti-Drift Guardrails

This file contains strict behavioral guidelines and architectural mandates. Claude Code CLI automatically reads this file. You must treat it as an absolute priority over general workflows to prevent context drift and ensure architectural integrity.

---

## 1. 🎯 Single Source of Truth & Zero-Drift Rule
- **Primary Directive**: The project's ultimate design and constraints are defined in **`PRD.md`** (specifically **Section 1.5: Scenario Matrix** and **Section 1.6: Compatibility Validation**). 
- **Pre-flight Constraint**: Before editing ANY code in `core/`, `platforms/`, or `cli/`, you **MUST** read `PRD.md` to ensure your proposed changes are fully aligned with the multi-user, multi-client, high-concurrency architecture.
- **No Speculative Coding**: Never implement "just-in-case" alternative paths or bypass type-safety. Every change must be surgically focused and maintain structural rigor.

---

## 2. 🛡️ Concurrency & Sandboxing Guardrails (Anti-Collision Rules)
You must strictly enforce these three physical isolation rules in all code refactoring tasks:
1. **Directory Isolation**: Never use `os.getpid()` for temporary runtime directory paths inside `core/simulation.py`. You **MUST** use unique task-level UUIDs or `task_id` strings to prevent multiple threads from overwriting each other's configurations.
2. **Git Worktree Isolation**: When preparing code branches for dynamic user compilation in `core/repo.py`, you **MUST** implement `git worktree` sandboxing to isolate checkouts and compiles, preventing file conflicts under concurrent user builds.
3. **Agent Job Pinning**: Ensure task-claims in `core/control_service.py` are strictly pinned via `assigned_agent_id` to prevent task stealing among multiple connected user agents.
4. **UNC Path Translation**: Ensure UNC path translations (via `linux_mount_map`) are preserved and tested on Linux to support cross-platform Windows simulation execution safely.

---

## 3. ⏱️ Micro-Checkpointing & Memory Management
To prevent memory/attention decay in long sessions:
- **Write Checkpoints**: After completing any subtask (e.g., refactoring one Python module or writing a test), you **MUST** summarize the current file state, modifications, and pending items into `docs/handoff.md` (or `CHECKPOINT.md`).
- **Test Before Proceeding**: Never stack modifications without testing. You must execute and pass tests for the modified module before moving to the next file.

---

## 🛠️ Build and Validation Commands
- **Run Environment Checks**: `python rsim.py check`
- **Run Tests**: `pytest` or `python -m pytest tests/`
- **Run Specific Test**: `pytest tests/test_simulation.py`
- **Run Control Server**: `python rsim.py server serve --port 8877`
- **Compile Check**: `python -m py_compile <file_path>`
- **Lint Check**: `flake8 core/ cli/` (if available)


---

## 👥 4. 内部多智能体团队模拟协议 (Internal Multi-Agent Team Simulation)
当你被下达最高指令并启动自主运行模式时，你必须在内部模拟一个 **"三维 Agent 开发团队"** 并协同工作。在你的思考过程 (Thinking) 中，请显式使用以下角色进行内部对齐，无需人类干预：

1. **[Architect-Planner (架构与规划师)]**：
   - **职责**：负责全局业务拆解。收到最高指令后，首先读取 `PRD.md`，制定阶段性执行路线图并写入 `CHECKPOINT.md`。
   - **红线**：禁止 Worker 直接改代码。必须先由 Planner 决定重构模块的物理隔离边界。

2. **[Worker-Engineer (搬砖与编码器)]**：
   - **职责**：执行具体的、手术刀式的代码修改。严格按照 `CLAUDE.md` 的防碰撞并发规则编写 Python 逻辑。
   - **红线**：每次修改代码时，单次只修改一个文件，完成修改后立刻交由 QA 角色。

3. **[QA-Reviewer (质量与安全审计)]**：
   - **职责**：独立执行验证。每次 Worker 修改完代码后，立即执行 `pytest` 或 `python -m py_compile` 静态编译检查。
   - **自纠错逻辑**：如果测试挂掉，QA 角色负责拦截并向 Planner 报告报错堆栈。由 Planner 重新调整路线，Worker 进行二次修改，直到测试 100% 通过（PASS）才允许提交 Checkpoint。
