# 2026-08-11 Business Convergence Master Handoff

> Status: implementation and focused integration review complete; not deployed
> and no real simulation was submitted in this round. Worker-specific evidence
> lives beside this file.

## Frozen product boundary

`radar-sim` is a thin orchestration and adaptation layer. It does not implement
Selena simulation behavior. It accepts one `UserRunConfig 2.0` document through
Web or SDK, resolves where each input can be read, prepares only the transfers
required by the selected execution target, invokes the existing build/simulation
tools, and reports their truthful per-input outcome.

- Linux is the control plane: Job/Stage/Event, routing, commands, summaries and
  logical references. It never compiles Selena or runs a Windows-local simulation.
- Windows uses one user-visible Connector. Internal capability labels may remain
  for compatibility, but users do not select light/full modes.
- Cluster input bytes move from their source directly to Cluster-accessible data
  storage. Local simulation inputs remain local when readable by the Windows
  execution node.
- Project recognition is allowed only for build-script/toolchain/output discovery.
  It must not select Runtime, data, MatFilter, Adapter or simulation parameters.
- Web, SDK and future Skill/MCP are adapters over the same API and SDK contract;
  they do not contain a second scheduler or transfer implementation.

## Product assumptions used for this round

These implementation boundaries were used for this P0 round:

1. A Linux caller's private local files are supported through the Python SDK in
   the first release. Browser-only Linux local-file access would require a future
   cross-platform Connector and is not silently emulated by uploading through the
   Linux control API.
2. The target architecture keeps large result archives out of the Linux control
   plane. Until a Cluster/result data-plane delivery adapter exists, the current
   Linux result archive is a declared compatibility boundary, not proof that
   direct result delivery is complete.

## Work packages and ownership

| Work package | Owner | Files/boundary | Exit condition |
|---|---|---|---|
| Stable identity | Luna `identity_unification` | Web/SDK owner, pairing, auth compatibility | Same logical user can use Web, SDK and one persistent Connector without browser-random identity drift |
| Bounded Cluster concurrency | Luna `cluster_concurrency` | Cluster executor/control scheduling | Long collection does not prevent another owner's ready Cluster job from progressing; claim/heartbeat/recovery remain safe |
| Resource routing | Luna `resource_routing` + root completion after model-capacity interruption | reachability, auto target, transfer orchestration, data bindings | Decisions consider all task resources; data authorization is owner/device scoped and project-independent; unsupported routes fail truthfully before simulation |
| Integration and public contract | root | compact business progress, result-delivery boundary, docs, combined tests | Web/SDK expose one coherent contract and all implementation limits are explicit |

Workers must not edit the root handoff concurrently. Each worker writes a unique
file under `docs/handoffs/`; root reviews and consolidates the final evidence.

## Mandatory route matrix

| Selena source | Data/input source | Target | Required behavior |
|---|---|---|---|
| existing shared | shared/Cluster-readable | Cluster | no Connector, no transfer, Linux schedules only |
| existing local | local | Cluster | source Connector/SDK writes Cluster data plane directly |
| build local | local or shared | Cluster | Windows builds; only missing local resources transfer directly; Cluster continues independently after publication |
| existing local | local | local | same Windows reads inputs and invokes the mature local simulator; no data transfer |
| build local | local | local | same Windows builds and invokes local simulator; no data transfer |
| remote/shared | remote/shared | local | read in place when the Windows execution node can access it; otherwise use a real `source_to_local` plan or return a stable unsupported/needs-input result |
| Linux-caller local | local | Cluster via SDK | SDK process executes the signed direct-transfer plan; Linux control API receives metadata only |

All resources (Selena directory, Runtime XML, data, MatFilter and optional
Adapter) are resolved independently. One resource becoming available must not
mark the others available. `target=auto` must choose only a route for which every
required resource has a proven original-read or direct-transfer path.

MatFilter has one bounded repository-inference exception: an explicit user path
always wins; when omitted, the SDK or Connector that can read the repository
searches generic Selena tooling locations and accepts only one highest-confidence
candidate. Missing or tied candidates require user input. Linux never walks a
Windows repository, and no product table or historical-job default participates.

## Multi-user release gates

- Owner identity must be stable across Web, SDK and Connector and authoritative
  in authenticated mode. `X-Rsim-User` alone remains trusted-intranet grouping,
  not security authentication.
- Jobs, Agents, transfers, results and actions remain owner-scoped.
- Shared Cluster execution uses bounded concurrency. One user's long poll cannot
  make the shared role look offline or monopolize all progress.
- Concurrency is bounded and observable; no unbounded thread creation.
- Restart recovery remains at-least-once and must not let one execution node
  claim a second task while an orphaned running task is assigned to it.

## Explicit non-goals

- No changes to Selena simulation algorithms or runnable correctness.
- No project-specific task branches or user-visible project registration.
- No large-file fallback through Linux request bodies.
- No automatic installation of Visual Studio or the user's simulation runtime.
- No heavy message broker, workflow engine or duplicate MCP scheduler.

## Verification policy

This round starts with focused contract/unit/integration tests only. Do not submit
real Cluster or local simulation jobs as an implementation shortcut. After code
review, the root agent records exact test commands and results, unresolved limits,
and any production deployment still required. A passing unit suite is not reported
as a real simulation success.

## Implemented outcome and evidence

- Stable no-auth identity is `user-<lowercase NTID/OS login>` across Web, SDK and Connector. Browser-random `web-*` identities are no longer generated. Formal Bearer-auth ownership remains authoritative.
- Generic Agent registration and heartbeat cannot self-declare protected Cluster worker identity. Linux and Gateway roles use bounded pools (default 2 each), independent heartbeat/current-task state, atomic claim, stale recovery and owner-fair ordering without hard quotas.
- Modern data-root authorization is `owner + device_id + normalized root`; legacy project-scoped rows remain readable only as migration compatibility. Project recognition is not used to decide data, Runtime, MatFilter, Adapter or transfer ownership.
- `existing + cluster` with caller-local resources skips Windows build/resolution and uses one source-side `prepare_data` barrier to transfer `runtime_bundle`, `runtime_xml`, `dataset`, `mat_filter` and optional `adapter` directly to Cluster storage. Linux receives only signed plan metadata, progress and path-free manifests.
- A user may submit a local job before installing the Connector. With no online Connector the Job remains queued and reports `windows_connection_required`; it is not permanently blocked. An online but incompatible execution computer produces a stable pre-simulation needs-input result.
- Web job details and SDK `Job` expose four stable `business_steps`; the internal Stage DAG remains available for audit/recovery.

Focused verification on Windows development host:

- Resource/transfer/Agent/SDK/Cluster-reference group: `161 passed, 2 skipped, 1 warning`.
- Identity/data-binding/transfer smoke group: `40 passed, 1 warning`.
- Business-progress and truthful unsupported-route group: `5 passed`.
- Final integrated control/API/identity/concurrency/business-progress group: `211 passed, 1 warning`.
- `py_compile`, `node --check radar_sim_web/static/app.js` and `git diff --check` passed at the intermediate integration point.

Known release boundaries:

- `source_to_local` is not released: target-specific Windows cache authorization is absent, so `TransferService` returns `source_to_local_unavailable` instead of writing to the Cluster root.
- A Linux caller's private files require the Python SDK process to execute `shared_copy`; pure browser Linux local-file access has no cross-platform Connector in P0.
- Large result archives still use the existing compatibility collection path. Direct Cluster-to-user result delivery remains future data-plane work.
- This round did not deploy to `10.190.171.44` and did not run a real Cluster/local simulation; automated evidence must not be reported as black-box success.
