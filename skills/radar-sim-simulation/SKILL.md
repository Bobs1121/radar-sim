---
name: radar-sim-simulation
description: "Run Selena radar simulations from any Agent conversation without the Web UI. The Skill discovers the current code/data environment, understands first-run and repeat-run language such as 'I changed this, run the simulation again', bootstraps or updates the source-free MCP/SDK/Connector capabilities, produces UserRunConfig 2.0, submits and monitors the Job, diagnoses failures, and retrieves verified results. Use for Selena/radar simulation, replay, re-run after code changes, configuration, validation, progress, diagnosis, retry, cancellation, or result download."
---

# Radar-sim Simulation

Use this Skill as the complete Agent-facing workflow for a Selena simulation.
The user should care about the data to simulate and the meaning of the run,
not MCP installation, SDK packages, Connector paths, server routing, Stage
DAGs, transfers, or internal identifiers.

## Non-negotiable public boundary

- Use the radar-sim MCP tools as the execution interface. Do not use the Web UI,
  invent REST calls, or reimplement SDK/MCP scheduling logic.
- Use `UserRunConfig 2.0` as the only public simulation configuration.
- Keep file bodies, MF4 content, Selena binaries, DLLs, Runtime XML content,
  and result ZIP content out of chat and MCP arguments. Pass paths and logical
  references only.
- Never infer a product, project, profile, recipe, Adapter, Radar source, or
  internal Runtime Bundle value from a directory name.
- Never checkout, reset, clean, stash, rewrite, or otherwise modify the user's
  code repository during discovery or configuration.
- Do not ask the user for MCP/SDK/Connector/server/Agent/Stage/transfer
  parameters. Those are capability preparation, not simulation semantics.

## 0. Bootstrap before MCP tools exist (internal only)

The Skill is the only user-delivered artifact. If radar-sim MCP tools are not
already available in the Agent, silently execute the bundled standard-library
bootstrap. Do not show its command, service URL, local path, release, checksum,
or installer output to the user. Never ask the user to clone radar-sim, install
from source, or manually run `pip`.

```text
python scripts/bootstrap_agent_tools.py
```

The bootstrap resolves the provider-owned service URL from, in order:
explicit Agent configuration, `RADAR_SIM_SERVICE_URL`, an existing local MCP
installation, and the bundled `references/service-profile.json`. It downloads
only the public `install.py` entrypoint, passes through the user credential
environment, and lets that installer verify the release Manifest and
SHA-256 before installing a versioned offline bundle containing SDK, MCP and
Skill. It never downloads a source checkout or persists a token.

After a successful bootstrap, internally register/reload the local stdio MCP
using the generated configuration. If the host supports dynamic MCP
registration, perform it automatically; otherwise use the host's normal
programmatic reload capability. Never ask the user to copy paths or edit JSON.
`scripts/start_mcp.py` is also available as a stdio command:
it bootstraps on first start, updates side-by-side on later starts, keeps MCP
JSON-RPC on stdout, and falls back to the last valid local release when the
service is temporarily unavailable.

Once MCP is registered, silently call `check_agent_tools`. A compatible current release
may run immediately; an incompatible release must be updated and the MCP
process reloaded before continuing. A newer compatible release is optional and
must not delay a valid run. The MCP update is restart-safe: the running process
is not overwritten, the old release remains available for rollback, and Skill
and SDK are updated together.

## 1. Interpret the user's run intent before discovering fields

Resolve intent using this evidence priority, without asking the user to repeat
values that are already confirmed:

1. explicit wording in the current user message;
2. a complete YAML or explicit semantic fields in the current message;
3. the last confirmed configuration in this Agent conversation;
4. the latest user-owned Job's public config, when the server exposes it;
5. a previously saved `.radar-sim/simulation.yaml` or equivalent project
   configuration, if present and readable;
6. read-only discovery of the current code/data environment.

Never let a filename or the mere existence of a Selena executable override a
clear user statement. Keep the previous configuration's data, Runtime, Radar
source, Adapter, MatFilter, target, and result preference when the user asks
for a repeat and does not change them.

At the start of every repeat-capable request, silently call
`get_simulation_state` with the current working-directory/data hints when
available. Treat a found active profile as the remembered configuration for
phrases such as “帮我再仿真一下刚刚的数据”“再跑一次”“继续验证”. Do not
show the state lookup or ask the user to identify the previous data again.

### Build intent versus actual compiler policy

The Skill classifies the *request*; the server and local build stage decide the
actual compiler action from evidence. Do not invent `incremental`, `full`, or
`clean` fields in `UserRunConfig 2.0`, and do not claim a compiler mode before
the build Stage reports it.

| User language/evidence | Semantic intent | YAML source | Expected policy |
|---|---|---|---|
| “我改了这里，帮我重新仿一下” / “修改后再跑” / “修复后验证” | code changed, repeat the prior run | `build` when the prior run was build; otherwise switch from `existing` to `build` | normally incremental; the backend proves skip/incremental/full |
| “再仿一次” / “用刚才配置再跑” with a prior `build` run | repeat configured build | preserve prior `build` config | skip if code/artifact evidence is unchanged; otherwise incremental unless a positive incompatibility requires full |
| “再仿一次” with a prior `existing` run | repeat existing artifact | preserve `existing` config | no Selena compile |
| “编译后仿真” / “选择 Selena 编译” / “当前代码仿真” | build current workspace | `build` | backend decides skip/incremental/full |
| “用现有 Selena” / “不要编译” / “用上次产物” | explicitly reuse artifact | `existing` | no compile; code edits do not affect this run |
| “切到某分支后仿真” | branch-constrained build | `build` plus the requested `branch` expectation | full only if the artifact provenance/branch is positively incompatible; never checkout automatically |
| “清理后全量编译” | explicit destructive build request | `build` | request is not silently translated into a public YAML field; if the current contract cannot express it, report that constraint and ask for confirmation before any destructive action |

The phrase “重新仿一下” alone is not enough to choose `build` versus
`existing` when no previous confirmed configuration exists. Discover the
environment and ask one consolidated semantic question. If the user says they
changed code while the previous configuration was `existing`, treat that as a
meaningful conflict: explain that an existing binary cannot contain the edit,
propose `build`, and ask only if the required build fields cannot be resolved.

Once a configuration has been confirmed, mark it as the active run profile in
Agent/task state. For all later “再仿一次”“改后重跑”“继续验证” requests,
reuse it automatically. Do not reopen the build/existing, Runtime, target, or
source questions unless the user explicitly changes one of them.

When the build Stage returns policy evidence, translate it for the user:

- `skipped` + unchanged evidence: “代码和 Selena 产物一致，跳过编译。”
- `incremental` or an unknown-code-change fallback: “检测到代码变化，或无法安全证明未变化，执行增量编译。”
- `full` with a branch/artifact incompatibility reason: “Selena 产物与当前分支不匹配，执行全量编译。”
- missing provenance alone is not permission to delete a valid output; follow
  the backend policy and never claim full merely because evidence is missing.

## 2. Discover the current environment read-only

If the user supplies a complete YAML or all required semantic fields, do not
scan the repository again. Call validation directly and use discovery only to
resolve fields that are actually missing. This is the fast path for repeated
runs.

Use the bundled helper when the Agent can execute Skill resources:

```text
python scripts/discover_candidates.py --root <code-root> --data-root <data-root>
```

Perform bounded, read-only checks:

1. determine the working directory, relevant Git root, current branch, and
   dirty state; prefer the Selena nested repository over an unrelated outer
   product repository;
2. find Selena build scripts such as `jenkins_selena_build.bat` or
   `build_selena.bat` without assuming one;
3. find Runtime XML candidates near the code root, build script, and existing
   Selena output;
4. find existing Selena folders by bounded search for `Selena.exe` and
   colocated DLLs;
5. find source MF4 files/directories only when `data.path` is not supplied;
   exclude generated `job_*`, `outputs`, `results`, logs, temporary folders,
   and result names ending in `out.MF4`;
6. record candidate path, evidence, and confidence. A bound reached is
   unresolved ambiguity, not permission to guess.

Never treat the current Git root as proof that the user selected `build`.
Never read file bodies into the conversation. Never select a directory with
multiple plausible MF4 inputs without confirmation.

## 3. Build a semantic YAML draft

Use only these public fields:

```yaml
schema_version: "2.0"
selena:
  source: build                 # build | existing
  code_path: "..."
  branch: ""
  selena_build_script: "..."
  package_build_script: ""
  existing_path: ""
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

Safe defaults:

- `simulation.target=auto` when the user did not state a target;
- empty `simulation.source`, `adapter_file`, and `mat_filter` unless explicit
  evidence or validation requires them;
- current branch as a proposed value only when it is relevant to the user's
  stated branch intent; do not silently turn discovery into a branch request;
- no `project`, `profile`, `recipe`, Agent ID, token, Cluster path, Runtime
  Bundle ID, or private server field.

If the user asks to save YAML without giving a destination, propose
`.radar-sim/simulation.yaml` and ask before writing. For a run request, keep the
draft in Agent/task state; do not create or modify repository files merely to
remember a run.

## 4. Ask one consolidated semantic confirmation (first configuration only)

Ask only for information that changes the meaning of the simulation:

- build current code or use an existing Selena artifact;
- which data candidate to simulate when there is more than one;
- which Runtime XML/branch/build script when candidates are ambiguous;
- a target or Radar source when the user's intent cannot resolve it;
- whether an explicit code-change request should switch an existing-artifact
  configuration to a current-workspace build.

Present the proposed configuration, evidence, and unresolved choices once.
After the user confirms it, do not ask any routine confirmation again. Do not
ask separate questions about Connector, MCP, SDK, server, transfer, readiness,
or Stage IDs. If the user says “就用刚才的配置”, reuse the last confirmed
semantic values and continue automatically.

## 5. Automatic execution policy

After the first semantic configuration is confirmed, run the whole lifecycle
without conversational pauses:

1. restore the active profile silently; if the user supplies a new data path,
   change only `data.path` and preserve the other confirmed fields;
2. prepare/update local capabilities silently;
3. normalize and validate the active YAML;
4. submit once with a durable idempotency key;
5. wait/poll repeatedly until terminal, automatically continuing after an
   observation timeout;
6. retry transient transport, transfer, or Stage actions when the tool contract
   marks them retryable, reusing the same Job/key and never duplicating work;
7. collect diagnosis and Manifest automatically;
8. download only when the user requested a local result.

Do not narrate these internal phases. A successful run may expose only a short
status such as “仿真已提交，正在运行” and then the business result. Keep
internal capability/version/update logs in the tool trace, not the user reply.

Only pause for a business decision that cannot be derived safely: a missing
data/path choice, a contradictory explicit user request, a host-level security
policy that refuses a required machine mutation, or a server `needs_input`
action whose type is genuinely semantic. A transient timeout, update check,
Connector startup, transfer retry, or build-policy decision is never a reason
to ask the user.

## 6. Prepare capabilities automatically

After semantic intent is resolved and before a long run:

1. call `get_simulation_state` first for repeat-language requests; update the
   active profile only when the user explicitly changes semantic input;
2. call `check_agent_tools`; update/reload incompatible or stale local Agent
   Tools automatically through the versioned bootstrap;
3. call `check_windows_connector` when Windows paths, build, or local execution
   are required; if missing/outdated, call the authorized install/update tool;
4. verify exact-device status after Connector installation; aggregate counts do
   not prove that this computer is ready;
5. call `get_simulation_schema` only when the contract is unknown or a field
   needs guidance; call readiness/capabilities only for the selected route or
   unresolved `auto`.

Do not ask the user to perform technical installation steps. If the host has a
machine-mutation policy that rejects an automatic Connector update, return one
plain-language blocker only; do not repeat confirmation prompts for each
internal step. Hide raw traces, service URLs, local paths, and release details.

## 7. Normalize, validate, and submit exactly once

Use this order:

1. `import_simulation_yaml` for a draft or supplied YAML;
2. `export_simulation_yaml` when a canonical representation is needed;
3. `validate_simulation` with the final confirmed config;
4. `submit_simulation` with one durable `idempotency_key`.

If validation reports missing/ambiguous fields, ask only for those business
fields. If a submit response is lost, retry with the same idempotency key; do
not create a second Job. A valid active profile proceeds without a new
confirmation.

## 8. Monitor and finish

- Use `wait_simulation` with bounded waits in a loop until terminal or
  `needs_input`. A timeout ends one observation window; it does not cancel the
  server Job, so continue polling automatically.
- Use `get_simulation_events` with the returned cursor when a Stage changes, the
  user asks for logs/progress, or diagnosis needs evidence.
- On `needs_input`, first execute every automatic action in `actions`; ask only
  when the remaining action is a genuine business decision.
- On `failed`, call `diagnose_simulation`; on terminal success or partial state,
  call `get_simulation_manifest`.
- Treat `partial` as partial, never as full success. `artifacts_available` is
  not proof that the simulation succeeded.
- Download results only when requested; return local path, checksum, and a
  manifest summary, never file bytes.

## User-facing result

Return a concise summary of the confirmed data/configuration, `job_id`, current
or terminal status, progress, diagnosis, and Manifest summary. Report compiler
policy only from Stage evidence. Do not expose routine bootstrap, update,
Connector, proxy, server, or Stage-internal processing. Give one actionable
next step only for a genuinely blocked or failed run. Do not return MCP
envelopes verbatim unless the user requests diagnostics.

Read [configuration-policy.md](references/configuration-policy.md) for
candidate selection and saved-configuration rules, and
[tool-contract.md](references/tool-contract.md) for exact tool envelopes,
bootstrap, capability, and error handling. This Skill does not implement
scheduling, Selena execution, transfer, result truth, or authentication; it
delegates those operations to the radar-sim MCP/SDK contract.
