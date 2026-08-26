---
name: radar-sim-simulation
description: "Run Selena radar simulations from any Agent conversation without the Web UI. Automatically recover the last local simulation profile, configure only missing business inputs, choose build versus existing Selena from user intent, prepare MCP/SDK/Connector capabilities silently, submit and monitor the Job, retry recoverable failures, and automatically return a verified local result address. Use for Selena/radar simulation, replay, rerun after code changes, validation, monitoring, diagnosis, retry, cancellation, or result retrieval."
---

# Radar-sim Simulation

Use this Skill as the only Agent-facing workflow for Selena simulation. The user
normally supplies only the data to simulate. Keep all infrastructure and
intermediate processing internal.

## Rules

- Use radar-sim MCP tools; do not use the Web UI or invent REST/scheduling logic.
- Use only `UserRunConfig 2.0` for public simulation configuration.
- Never send file bodies. Pass paths and logical references only.
- Never infer project, profile, recipe, Adapter, Radar source, or Runtime Bundle
  values from filenames or directory names.
- Never modify, checkout, reset, clean, stash, or rewrite the user's repository.
- Never show MCP/SDK/Connector/server/Agent/Stage/transfer details in normal
  replies.

## Distribution and environment isolation

This Skill is distributed to unrelated users and machines. Never hard-code a
user name, machine name, Agent ID, token, server IP/URL, UNC path, absolute
workspace path, local result path, or installation release directory in the
Skill, its references, or its helper scripts.

- Resolve the workspace and data paths from the current Agent workspace, user
  input, and the current machine's standard environment directories.
- Resolve the service endpoint from the explicit installation configuration or
  environment (`RADAR_SIM_SERVICE_URL`/`RADAR_SIM_BASE_URL`) and only then from
  the local MCP configuration or a provider-generated service profile. A
  distributable `service-profile.json` must not contain a deployment-specific
  URL.
- Resolve the owner label from `RADAR_SIM_USER` or the current OS login only
  when the service contract requires owner routing. Never embed a person's
  identity in the Skill.
- Treat service profiles, MCP configs, credentials, Agent IDs, and result
  catalogs as per-user installation state. They may be generated locally, but
  must never be copied into the public Skill package or used as defaults for
  another user.

## Start silently

1. If radar-sim MCP tools are unavailable, internally run the bundled bootstrap
   or register `scripts/start_mcp.py`. Do not show commands, URLs, paths,
   versions, checksums, installer output, or self-check output.
2. Call `get_simulation_state` with the current workspace/data hints. If an
   active profile is found, use it for phrases such as “再仿刚刚的数据”“再跑
   一次”“我改了这里，重新验证”. Do not ask the user to identify the old data.
3. Only if no usable profile exists, read the current environment with the
   bundled bounded helper:

   ```text
   python scripts/discover_candidates.py --root <code-root> --data-root <data-root>
   ```

All three steps are internal. A transient preparation/update/wait condition is
not a user question.

## Copilot interaction contract

After the input closure gate is complete, run the simulation as one hands-off
workflow. The user's request to run, rerun, or validate is approval for the
routine operations inside this workflow: bounded local discovery, starting or
reusing the MCP, checking or updating the official Connector/SDK installation,
writing temporary files and results, submitting the Job, polling, retrying
retryable work, and downloading the result. Do not ask whether to allow,
confirm, continue, run, update, or download any of these steps.

- Never turn an internal step into a user-facing click instruction such as
  `Allow`, `Confirm`, `Continue`, `Run`, `Trust`, or `Approve`.
- Collect all unresolved business inputs in the single closure question before
  any simulation-side mutation. Once answered, do not pause for operational
  confirmation or expose an internal self-check status.
- Prefer MCP readiness/bootstrap tools and one stable non-interactive launcher.
  Do not split dependency preparation into a series of visible shell commands,
  terminal REPL statements, inline Python clients, or commands that wait for
  keyboard input. Use hidden/background execution and reader-friendly stderr
  progress when a local process is required.
- If the host presents a mandatory security approval that the Skill cannot
  control, do not manufacture a click, repeat the same request, or continue
  partially. Surface one concise blocker only when the host has actually
  rejected the required operation; otherwise continue automatically.
- This contract does not authorize repository mutation, arbitrary downloads,
  destructive cleanup, or unrelated external actions. Keep automatic changes
  inside the official radar-sim installation/runtime/result locations.

The normal user-visible output is only the final result address. Intermediate
progress, retries, readiness checks, and state recovery stay in the hidden
terminal/log stream unless the user explicitly asks for diagnostics or live
progress.

## Copilot host approval setup

When the host is VS Code/GitHub Copilot and approval prompts are not already
configured, guide the user through one setup before the first simulation-side
mutation. Prefer workspace-scoped `Bypass Approvals` for this trusted
radar-sim workspace: it removes tool/MCP/terminal confirmation dialogs while
still allowing the Skill to ask one consolidated business-input question.
Do not select `Autopilot` as the default for semantic configuration work; it
can auto-respond to agent questions.

Read [copilot-approvals.md](references/copilot-approvals.md) for the host UI,
workspace `settings.json`, terminal/MCP approval, CLI, and managed-policy
fallbacks. Do not silently edit the user's approval settings or enable global
auto-approval. Once the user has configured a workspace/session approval, do
not instruct them to repeat it on every simulation.

## Interpret intent

Apply this priority: explicit current wording → supplied YAML/fields → active
profile → latest user Job config → saved config → read-only discovery.

- “我改了这里，帮我重新仿一下”“修改后再跑” → use current workspace
  `selena.source=build`, preserving the active data/runtime/target fields.
- “编译当前代码” → `build`.
- “用现有 Selena”“不要编译”“用上次产物” → `existing`.
- “再仿一次” → preserve the active `build`/`existing` choice.
- If an existing-artifact profile conflicts with an explicit code-change request,
  switch to `build` when its required fields are known; ask only when a real
  business field cannot be resolved.

Do not add `incremental`, `full`, or `clean` to the public YAML. The backend
decides skipped/incremental/full from branch, source-change, and Selena artifact
evidence. Report that policy only when the user asks for it or the final result
requires a diagnosis.

## Configure once

Use the active profile or complete YAML directly without rescanning. Otherwise
create the standard public config described in
[configuration-policy.md](references/configuration-policy.md).

### Input closure gate

Complete input collection before any simulation-side mutation. In one internal
preflight pass, recover state, discover all candidates, import the partial YAML,
and collect every missing/ambiguous business field. If anything is unresolved,
ask one consolidated question and stop there. Do not update Connector, transfer
data, build Selena, submit a Job, or ask a new question after those phases start.
After the answer, merge it, import/validate again, and require a complete
configuration before capability preparation or submission.

Ask one consolidated question only for unresolved business meaning, such as:

- which data to use when multiple candidates exist;
- build current code or use an existing Selena artifact;
- an ambiguous Runtime XML, build script, branch, target, or Radar source.

After the user confirms a configuration, save/use it as the active profile and
never ask routine confirmation again. MCP/SDK/Connector preparation is automatic.

## Efficiency without semantic shortcuts

Use the shortest safe path for repeated work:

- If the active profile matches the current workspace and the user supplies no
  new configuration meaning, recover state, replace only an explicitly new
  data path, then import/validate. Do not call schema, candidate discovery,
  export, capabilities, or readiness tools again unless the route or a missing
  field requires them.
- Within one simulation request, call Agent Tools and Connector checks at most
  once. Do not reinstall a component that is already current/online, and do not
  repeat a successful readiness check before submission unless the server asks
  for it or a prior operation changed the environment.
- Observe a running Job with `wait_simulation` using a 60–120 second
  observation window and a 2–5 second poll interval. Do not combine repeated
  `get_simulation`, `get_simulation_events`, and short waits; use events only
  for diagnosis or an explicit progress request. A timeout is an observation
  boundary, not a reason to submit again.
- Keep Selena build, data transfer, preflight, execution, collection, and
  checksum verification under the backend's dependency order. Never split or
  parallelize inputs merely to reduce wall time unless the backend explicitly
  reports isolated worker capacity and preserves deterministic output mapping.

## Run automatically

1. Silently call `check_agent_tools`; update/reload incompatible tools.
2. Silently call `check_windows_connector` when the selected route needs it;
   automatically install/update through the official local policy.
3. Validate with `import_simulation_yaml` and `validate_simulation`.
4. Submit exactly once with a durable `idempotency_key`.
5. Call `wait_simulation` with a long observation window and adaptive bounded
   polling until terminal. A timeout only ends one observation window; continue
   polling automatically with the same Job/key.
6. Retry only actions marked retryable, reusing the same Job/key. Never create a
   duplicate Job because a response or observation was lost.
7. On failure call `diagnose_simulation`; on success/partial call
   `get_simulation_manifest`.
8. When artifacts are available, automatically call `download_simulation_result`
   with `extract=true` and return the verified local result directory. The
   archive remains an internal recovery artifact; do not return its path as the
   normal result address. Do this by default; do not wait for the user to ask
   for a download.

Execute these phases without progress narration. Show progress only when the
user explicitly asks for it.

Never launch MCP with `python -`, `python -c` wrappers, heredocs, or an inline
stdio client. Use the generated direct versioned-Python MCP configuration; use
`scripts/start_mcp.py` only for first-run bootstrap/fallback. The local terminal
may show only the launcher status and reader-friendly progress lines on stderr.

## Final reply

For success, keep the normal final reply to:

```text
仿真完成
结果地址：<verified local extracted result directory>
```

Add `job_id` or checksum only when useful or requested. For partial results,
say `仿真部分完成` and return the available result address. For failure, return
one concise diagnosis and one actionable next step. Do not return YAML, MCP
envelopes, Stage lists, internal logs, or capability checks unless explicitly
requested.

Read [tool-contract.md](references/tool-contract.md) for exact tool envelopes,
error handling, state persistence, bootstrap, and result rules. Scheduling,
Selena execution, transfer, and result truth remain SDK/server responsibilities.
