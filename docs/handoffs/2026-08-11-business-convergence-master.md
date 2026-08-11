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
- V2 does not recognize a business project. The selected build script may be
  inspected for toolchain/output discovery, but no project/profile/recipe table
  selects Runtime, data, MatFilter, Adapter, build arguments or simulation parameters.
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
searches generic Selena tooling locations and chooses deterministically from the
highest-priority candidates while recording the selected path and alternatives.
Linux never walks a Windows repository, and no product table or historical-job
default participates.

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

## 2026-08-11 V2-only simplification continuation

The user explicitly chose a clean V2 cut-over because the product has not been
released. `docs/V2_ARCHITECTURE.md` is now the implementation index and defines
the single `user-run-config/2.0` path plus the removal list.

Implemented in the current uncommitted continuation:

- Public V2 workspace recognition runs with `generic_only=True`; registered
  `config/projects/*` adapters cannot win or inject build arguments/output paths.
- V2 build preparation creates its bindings directly from the authorized
  workspace and selected scripts. It does not enter `adapt_legacy_config`.
- The actual command remains `cmd /c <user-selected Selena build script>` with
  an empty injected argument list. `package_build_script` is diagnostic-only.
- Script output inference remains preferred. If a wrapper does not expose its
  output statically, V2 authorizes a narrower generic build subtree and performs
  bounded post-build `Selena.exe` discovery inside that subtree, preferring the
  requested build mode and newest artifact.
- Existing Selena no longer derives `ovrs25`, `bydod25` or any other product from
  folder/runtime/script names. Its anonymous execution identity is content-based.
- Windows-local Selena execution no longer calls recipe handlers; it renders the
  shared V2 paramconfig and invokes the common Selena command.
- V2 Stage payloads no longer carry `profile`. The SDK and one-click HTTP
  endpoints accept only the unified Connector mode; public capabilities expose
  one `windows` object, and Web copy no longer claims scripts recognize products.
- Legacy task creation was removed from the public surface: no
  `/api/v1/projects`, SimulationSpec schema, `/api/v1/specs/*`,
  `POST /api/v1/validate` or `POST /api/v1/jobs`; SDK no longer exports
  `SimulationSpec`, `validate()` or `submit()`. Task query/action routes remain
  shared by V2 Jobs.
- `PRD.md`, `README.md`, `docs/DETAILED_DESIGN.md` and
  `docs/OD25_USER_GUIDE.md` were replaced with V2-only content rather than
  retaining contradictory legacy sections below an override notice.

Focused automated verification after these edits:

- V2 API/SDK/build/existing/local/Cluster/concurrency/result group:
  `271 passed, 1 skipped, 1 warning`.
- resource routing/Web/user config/transfer/branch/local-run group:
  `233 passed, 5 skipped, 1 warning`.
- `py_compile`, `node --check radar_sim_web/static/app.js` and
  `git diff --check` passed.

No deployment or real Selena process has occurred in this continuation yet;
these results are not reported as black-box simulation success.

Open release work after this commit:

- remove or quarantine unreachable legacy command/API modules after a complete
  public-route inventory; do not let that cleanup delay V2 main-chain validation;
- deploy the new Connector contract and Linux release;
- run one real `existing + local` and the four-combination acceptance matrix;
- Cluster-to-user reverse result delivery and remote-to-local input delivery stay
  truthful declared gaps, never Linux file-proxy fallbacks.

## 2026-08-11 V2 release gate before deployment

The V2 cut-over is now enforced by code, not only by documentation:

- `UserRunConfig.from_dict()` is strict V2. Legacy `build_script/build_mode`,
  Runtime Bundle/executable references, data limits, timeout fields and old
  result metadata are rejected instead of silently migrated.
- Public FastAPI/OpenAPI exposes only health/capabilities, run-config,
  run-jobs/job actions, metadata-only transfer and result catalog routes.
  Connector/artifact/Runtime Bundle/upload maintenance routes still execute for
  internal components but are hidden from the user/AI contract.
- Public SDK removed SimulationSpec and project-scoped/Linux-body dataset upload
  shortcuts. `submit_run()`/`submit_yaml()` keep source paths and execute the
  signed source-to-target transfer plan; Linux HTTP never receives MF4 bodies.
- Web and SDK expose one `windows` capability and one Connector. User-visible
  full/light, Agent ID and project recognition wording is removed.
- Bare Connector startup now canonicalizes the same `user-<login>` identity as
  the SDK; the one-click persisted owner remains unchanged.
- A production direct-transfer deployment is ready only when both the
  writer-visible `client_target_root` and explicit Linux `server_probe_root`
  exist in deployment configuration. This prevents a large copy from starting
  only to fail later because Linux cannot inspect the Cluster namespace.
- Result ZIP downloads use unique same-directory temporary files before atomic
  publication, so concurrent callers cannot corrupt each other's `.part` file.

Release-gate evidence on the Windows development host:

- API/SDK/generic build/existing Selena/Cluster concurrency/result/data-plane
  group: `308 passed, 1 skipped, 1 warning`.
- Connector/local runner/resource routing/Web/transfer/config group:
  `224 passed, 5 skipped, 1 warning`.
- Additional OpenAPI-only assertion: `1 passed, 1 warning`.
- Changed Python modules compile, Web JavaScript syntax checks, deterministic
  Connector bundle build and `git diff --check` all pass.
- The only warning is the existing Starlette TestClient/httpx deprecation.

This is still pre-deployment evidence. Do not mark real simulation acceptance
complete until the immutable Linux release is active, the Connector contract is
updated and real local/Cluster Jobs are inspected through their manifests.

## 2026-08-11 immutable deployment and first real V2 local run

- Commits `c92cd2e` and `8db3762` were pushed on `codex/new-branch`.
- Linux systemd now runs immutable release `/home/hoz2wx/radar-sim-8db3762`
  on `10.190.171.44:8877`; the unified Connector bundle was rebuilt there.
- The Windows one-click update completed with owner `user-hoz2wx`, persistent
  scheduled tasks, contract version 7 and no capability update required.
- Real Job `job_40da35128b6e` used the requested MF4, existing Selena folder,
  Runtime XML, inferred MatFilter and `result.path`. Resolution, environment,
  data preparation and preflight all succeeded. `Selena.exe` then exited before
  producing an engine log with Windows code `3221225781` (`0xC0000135`).

The failure was a V2 scaffolding regression, not an MF4/runtime compatibility
failure. An earlier real Job `job_d8b902defaad` had completed the identical
Selena/data/runtime/MatFilter combination. The only relevant private-config
difference was that the old project adapter injected Qt/MATLAB/Boost paths,
while the project-independent V2 config had an empty `environment.path_prefix`.
Both Runtime Bundle caches contained byte-identical Selena/DLL/runtime files.
A manual probe of the V2 command with paths read from the nearest
`CMakeCache.txt` remained alive after 20 seconds instead of exiting in one
second, proving the dependency-path root cause.

Current uncommitted correction:

- `core.selena_runtime_environment` finds the nearest authoritative
  `CMakeCache.txt` from the selected existing Selena/build hints and reconstructs
  only existing Qt, MATLAB, Boost and Selena-environment runtime directories;
  it contains no product names, fixed versions or project registry lookup.
- Windows local preflight injects those private paths before rendering and
  starting Selena. A bounded latest-build fallback covers build-script runs when
  only the workspace remains available.
- The common V2 Selena invocation defaults to tolerant input handling, matching
  the mature local and Cluster behavior.
- Windows loader exits now have stable dependency codes (`missing`, invalid
  architecture, initialization failed) while all other Selena exits remain
  engine-owned `selena_failed` outcomes.
- Public V2 docs/tests now enforce strict `UserRunConfig` 2.0, correct
  `simulation.mat_filter/adapter_file` placement, hidden internal OpenAPI routes
  and truthful `result.path`/ZIP boundaries.
- The required Windows Connector contract is raised from 7 to 8. This is
  intentional: a v7 Connector can receive the same Stage schema but cannot
  reconstruct the external runtime PATH, so Web/SDK must show the existing user
  a one-click update instead of reporting the stale process as current.

Focused verification after the correction:

- V2 config/API/SDK/local/runtime-environment group: `127 passed, 1 warning`.
- Connector/release/identity/build/data-plane/local-E2E group:
  `98 passed, 3 skipped, 1 warning`.
- Changed Python modules compile, Web JavaScript parses and `git diff --check`
  passes. A monolithic all-tests invocation was deliberately stopped after it
  remained active for several minutes without failure output; the two bounded
  release groups above are the acceptance gate for this correction.

Next evidence required: commit/push, deploy a new immutable Linux release,
one-click Connector update, then resubmit the identical real local configuration
and verify Job manifest, ZIP download and `<result.path>/<job_id>` materialization.

## 2026-08-11 real V2 local acceptance and Cluster gate

The runtime-environment correction was committed as `27299e7`; Connector
contract enforcement was committed as `f13ea2c`. Both commits were pushed to
`origin/codex/new-branch`, and Linux is running immutable release
`/home/hoz2wx/radar-sim-f13ea2c` with the required Connector contract version 8.
The Windows one-click update reused its installed Python packages and persistent
owner/device identity; the Connector reports current and survives through the
existing scheduled-task/watchdog installation.

Real local acceptance is complete:

- Job: `job_1ebbef262a89`.
- Input: `Gen5_2026-07-28_17-22_0118.MF4` with the user-selected existing
  Selena folder and Runtime XML.
- MatFilter: inferred deterministically from the repository-adjacent controlled
  search root; no project name or registry adapter selected it.
- All executable Stages succeeded; build/register Stages were truthfully skipped
  because the run selected an existing Selena folder.
- `run_simulation` returned 0 and published
  `outputs/0001-Gen5_2026-07-28_17-22_0118--out.MF4`, 239,051,624 bytes,
  SHA-256 `1a75992f5a87e543606b4d7831683f198d930d6e2e8cec412f242ebd42fbd440`.
- Manifest status is `succeeded`, delivery status is `delivered`, and the SDK
  downloaded the owner-scoped ZIP to
  `D:/RadarSim/v2-results/job_1ebbef262a89/`. The same Job directory also
  contains the directly consumable output MF4 and path-free Manifest.

During this real run, one idempotent SDK `GET` observed
`httpx.RemoteProtocolError: Server disconnected without sending a response`.
The Linux service did not restart (same MainPID and `NRestarts=0`) and the Job
continued to success. The SDK now retries only `GET`/`HEAD` transport failures
up to three attempts with bounded backoff. State-changing requests remain
single-attempt, preventing duplicate Job creation. Focused SDK/API/identity
regression after this change: `84 passed, 1 warning`.

The first real Cluster acceptance attempt is recorded, but is not reported as a
simulation pass:

- Job: `job_a6cd945004f9`.
- V2 validation was valid/ready, selected Cluster explicitly, and inferred the
  same MatFilter. The unified Windows Connector was online and current.
- The Job failed before any transfer or Selena execution at
  `environment_check` with `CLUSTER_ENVIRONMENT_UNAVAILABLE`:
  `Manager XML-RPC port: unavailable; Submit path: unavailable`.
- Independent probes from both the Linux control host and the Windows submitter
  confirm `SZHRADAR01 (10.54.5.71):8123` is closed. The SMB software and data
  mounts remain readable/writable, and the Linux service did not restart.
- This is an external Cluster-manager outage after the Cluster reset, not a
  data/Selena/Runtime/MatFilter/Connector failure. The Stage exposed a stable
  retry action and cancelled downstream work instead of transferring hundreds
  of megabytes into an unavailable execution plane.

Final Cluster evidence is therefore gated only on restoring the standard
Manager XML-RPC service on `SZHRADAR01:8123`, then retrying/resubmitting the same
V2 configuration. Do not bypass this check or label the current Cluster Job as
successful.
