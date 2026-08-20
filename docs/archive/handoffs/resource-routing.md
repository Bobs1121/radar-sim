# Resource routing and project-free data binding handoff

> Date: 2026-08-11
> Status: implemented and integrated by root after the Luna worker stopped on model-capacity exhaustion; not deployed and not black-box simulated.

## Product result

- Route selection evaluates Selena workspace/folder, Runtime XML, data, MatFilter and optional Adapter independently. Project recognition remains only a build/toolchain/output-discovery aid.
- Modern local data authorization is scoped by stable owner, Connector device ID and normalized root. Existing project-scoped SQLite rows remain readable for migration but no new business decision depends on them.
- `existing + cluster` with caller-local inputs uses one source-side `prepare_data` barrier. The Connector/SDK transfers each role directly to the Cluster data plane and returns only Manifest metadata to Linux. `resolve_spec` and `register_artifact` are skipped for this route; no VS/build dependency is requested.
- Shared/dataset logical inputs remain zero-copy. Mixed shared/local inputs transfer only local roles.
- Local execution uses one same-owner Windows execution node only when it can validate all caller-local resources. A new user with no Connector stays in a resumable `windows_connection_required` wait.
- A real `source_to_local` target is not implemented. Both API and `TransferService` reject it with `source_to_local_unavailable`; the Cluster target root is never reused as a fake Windows cache.

## Main implementation surfaces

- `core/api_v1.py`: resource inventory, conservative `target=auto`, direct-transfer Stage metadata, truthful local wait/unsupported behavior, path-free public response.
- `core/agent_data_bindings.py`: owner/device binding IDs and schema migration with legacy project compatibility.
- `core/control_service.py`: owner/device matching, path-free transfer Manifest projections, source-side Stage claim.
- `core/stage_binder.py`: modern binding preference and existing-Selena direct-transfer handoff compatibility.
- `cli/agent.py`: registration publishes owner/device bindings; existing/compiled resources use the common direct-transfer kernel.
- `radar_sim_sdk/client.py`: Web-equivalent transfer preparation for caller-readable files, including Linux SDK callers.

## Verification

Resource/transfer/Agent/SDK/Cluster-reference group:

```text
161 passed, 2 skipped, 1 warning in 70.33s
```

Integrated control/API/identity/concurrency/business-progress group:

```text
211 passed, 1 warning in 62.42s
```

The warning is the existing Starlette `httpx` deprecation warning. No real simulation task was submitted.

## Remaining boundaries

- Pure browser access to a Linux workstation's private local files is unsupported in P0; use the Python SDK on that workstation.
- Remote-to-Windows local input delivery requires a future target-specific Windows cache adapter and authorization protocol.
- Legacy body-upload methods remain for compatibility but are not used by Web, `submit_run`, `submit_yaml`, Skill or MCP paths.
