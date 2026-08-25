---
name: radar-sim-simulation
description: "Configure and run Selena radar simulations from any Agent conversation by discovering the current code/data environment, producing a UserRunConfig 2.0 YAML, asking the user only for ambiguous simulation inputs, automatically preparing required SDK/MCP/Connector capabilities, and submitting and monitoring the Job through radar-sim MCP tools. Use for requests to run, replay, configure, validate, monitor, diagnose, or retrieve a radar-sim/Selena simulation without the Web UI."
---

# Radar-sim Simulation

Use this Skill as the complete Agent-facing workflow for a Selena simulation. Hide infrastructure details from the user: the user should not need to understand MCP installation, Connector state, server routing, Stage DAGs, transfers, or internal identifiers. Ask only for values that change the meaning of the requested simulation.

## Operating contract

- Work from the Agent's current working directory and the user's stated paths.
- Use the radar-sim MCP tools as the execution interface. Do not use the Web UI, invent REST calls, or reimplement SDK/MCP scheduling logic.
- Use `UserRunConfig 2.0` as the only public configuration model.
- Keep file bodies, MF4 content, Selena binaries, DLLs, Runtime XML content, and result ZIP content out of chat and MCP arguments. Pass paths and logical references only.
- Never infer a product/project/profile/recipe, Adapter, Radar source, or internal Runtime Bundle value from directory names.
- Never checkout, reset, clean, stash, rewrite, or otherwise modify the user's code repository during discovery.

## End-to-end workflow

### 1. Discover the current environment read-only

If the user supplies a complete YAML or all required semantic fields, do not
scan the repository again. Call validation directly and use discovery only to
resolve fields that are actually missing. This is the fast path for repeated
runs from the same Agent conversation.

Use the bundled read-only helper when the Agent can execute Skill resources:
`python scripts/discover_candidates.py --root <code-root> --data-root <data-root>`. If the helper is unavailable, perform the same bounded checks with the host's read-only filesystem/Git tools.

1. Determine the current working directory and, when needed, the Git root, current branch, and whether the repository is dirty. Prefer the Selena nested repository over an unrelated outer product repository.
2. Find candidate Selena build scripts under the code root. Prefer explicit user configuration; otherwise recognize common names such as `jenkins_selena_build.bat` and `build_selena.bat` without assuming one.
3. Find candidate Runtime XML files near the code root, build script, and existing Selena output.
4. Find candidate existing Selena folders by bounded search for `Selena.exe` and colocated DLLs.
5. Find candidate MF4 files/directories only when the user did not provide `data.path`. Never select a directory containing multiple plausible MF4 inputs without confirmation.
6. Record each candidate with its path, evidence, and confidence. Do not treat the current Git root as proof that the user selected `selena.source=build`.

If discovery is unavailable or ambiguous, preserve that state and ask the user. Do not compensate by guessing. Do not scan generated `job_*`, `outputs`, `results`, log, or temporary directories as source-data candidates.

### 2. Build a YAML draft

Create an in-memory draft with only public fields:

```yaml
schema_version: "2.0"
selena:
  source: build                 # build | existing
  code_path: "..."
  branch: "..."
  selena_build_script: "..."
  package_build_script: "..."
  existing_path: ""             # required for existing
  runtime_xml: "..."
data:
  path: "..."
simulation:
  target: auto                 # auto | local | cluster
  source: ""
  adapter_file: ""
  mat_filter: ""
result:
  path: ""
```

Apply these defaults only when they are semantically safe:

- `simulation.target=auto` if the user did not state a target;
- empty `simulation.source`, `adapter_file`, and `mat_filter` when no explicit evidence requires a value;
- current branch as a proposed `selena.branch` only when it is visible and relevant; do not silently turn a discovered branch into a user requirement;
- never add `project`, `profile`, `recipe`, internal paths, Agent IDs, tokens, or Bundle IDs.

When the user asks to save a YAML file, use the path they specify. If no path is specified, propose `.radar-sim/simulation.yaml` and ask before writing it. If the user only asks to run, keep the draft in the conversation and do not create a repository file unless requested.

### 3. Ask one consolidated confirmation

Do not ask one question for every field. Present the proposed choices and unresolved items in one concise confirmation, for example:

```text
我在当前代码仓发现了以下仿真配置：
- Selena：使用 build，脚本为 ...
- Runtime：...
- 数据：发现 2 个 MF4 候选，待选择
- 目标：auto

请确认：使用上述 Selena build、Runtime 和数据候选，并提交本次仿真吗？
如果不是，请告诉我要使用的 Selena 来源、数据、Runtime 或执行目标。
```

Require user confirmation for:

- `build` versus `existing` when not explicit;
- multiple build scripts, Runtime XMLs, existing Selena folders, or data candidates;
- a branch expectation that differs from the current workspace branch;
- an explicit Radar source that conflicts with file metadata;
- an Adapter candidate when it is not clear that this simulation needs it;
- saving a generated YAML file when the user did not specify the destination.

Do not ask the user to provide internal readiness, Connector, MCP, SDK, server, or transfer parameters. Handle those automatically in the next phase.

### 4. Prepare capabilities automatically

Before a long-running run, call the capability/version tools as needed:

1. Call `check_agent_tools`. If the local MCP/Skill/SDK contract is incompatible or the current process cannot safely call the required tools, update it through `update_agent_tools` and restart the MCP process before continuing. If only a newer compatible release is available, do not block a valid run on the optional update; record a short notice and continue. Surface only a short progress message; do not expose wheel paths or internal release details unless requested.
2. Call `check_windows_connector` when the draft needs Windows-local paths, Windows build, or local execution. If the Connector is missing or outdated, use the authorized install/update tool. If the host requires an explicit mutation confirmation, ask once in plain language: “是否允许我在本机安装/更新仿真连接组件？” Do not ask the user to perform the technical steps themselves.
3. Verify the exact local capability after installation/update. Aggregate capability counts are not enough to prove the current computer is ready.
4. Call `get_simulation_schema` only when the YAML contract is unknown or a field needs guidance. Call `get_simulation_readiness` only for `cluster` or unresolved `auto` routing; call `get_simulation_capabilities` when local/Windows capability affects the selected route. Do not submit until the configuration and required execution route are ready or the server returns an explicit actionable waiting state.

If an automatic preparation step cannot proceed, translate it into one actionable user request. Do not expose raw stack traces, internal paths, or implementation names.

### 5. Normalize and validate the YAML

Use the MCP tools in this order:

1. `import_simulation_yaml` for a draft or user-provided YAML;
2. `export_simulation_yaml` when a canonical YAML representation is needed;
3. `validate_simulation` with the final confirmed config.

If validation reports missing or ambiguous fields, ask only for those fields. Never submit an incomplete draft. Show the user the final semantic choices before submission when the run was assembled from discovery rather than supplied as a complete YAML.

### 6. Submit exactly once per logical request

- Generate one durable `idempotency_key` for the logical run and retain it in the conversation or task state.
- Call `submit_simulation` with the confirmed `config` or `yaml_text` and that key.
- If the response is lost or a transport error occurs, retry with the same key. Never generate a replacement key merely because the response was lost.
- Preserve the returned `job_id` and report it to the user in a short status line.

### 7. Monitor and finish

- Use `wait_simulation` for bounded waiting. Start with a short observation interval, then back off to 15–30 seconds for long-running execution. Use `get_simulation_events` with the returned cursor only when a stage changes, the user asks for logs, or a terminal diagnosis is needed.
- A wait timeout means observation ended; it does not mean the server cancelled the Job. Query the Job again.
- If the Job returns `needs_input`, read its `waiting` and `actions`, ask the user only for the requested semantic input, then use the documented resume/retry tool.
- For `failed`, call `diagnose_simulation`; for terminal successful or partial Jobs, call `get_simulation_manifest`.
- Treat `partial` as partial, never as full success. Do not claim success from `artifacts_available` alone.
- Use `download_simulation_result` only when the user requests local result files. Return the local path, checksum, and summary—not file bytes.

## User-facing result

Return:

1. the confirmed YAML or a concise summary of it;
2. `job_id` and current/terminal status;
3. progress and any waiting action;
4. final diagnosis and Manifest summary;
5. result path/checksum when a download was requested;
6. one actionable next step if the run is blocked or failed.

Do not return MCP envelopes verbatim unless the user asks for diagnostics. Translate internal codes into plain language while preserving the stable error code in parentheses when useful.

## References

- Read [configuration-policy.md](references/configuration-policy.md) for repository discovery, candidate selection, YAML mapping, and confirmation rules.
- Read [tool-contract.md](references/tool-contract.md) for exact MCP tool names, input envelopes, output states, error handling, and automatic capability preparation.

This Skill does not implement scheduling, Selena execution, transfer, result truth rules, or authentication. Delegate those operations to the radar-sim MCP/SDK contract.
