# Result delivery, RadarFC and Coding-Agent acceptance handoff

> Updated: 2026-08-11 16:05 Asia/Shanghai
> Owner: root
> Status: active; do not report the whole scenario complete until every P0 gate below is checked.

## Product scenario to preserve

A user edits a local Selena workspace from a Coding Agent terminal. A future
radar-sim Skill or MCP is a thin adapter over the public Python SDK and the one
Linux control-plane API. The same submitted YAML chooses current-workspace or
existing Selena, local or Cluster simulation, data/runtime/MatFilter/Adapter and
an optional result destination. Windows-only compilation runs on the user's
persistent unified Connector. Local simulation stays on that Windows device.
Cluster input and output file bodies move directly between the source/target
device and the Cluster data plane; Linux receives only configuration, plans,
progress, logical references and manifests.

Accepted result contract:

- optional `result.path` means an extracted, directly consumable result directory;
- the owner-scoped result ZIP remains available through Web and SDK;
- local simulation writes to the local destination without a transfer;
- Cluster results return directly to the submitting Connector/SDK device, not through Linux;
- an empty path resolves on the receiving device to `RadarSim/results/{job_id}` under the user's home;
- pure Web usage without a Connector cannot write an arbitrary browser-machine path and therefore keeps ZIP download only.

## Confirmed completed evidence

- [x] `job_d8b902defaad` is a real successful local simulation. Its owner-scoped
  result is `result:sha256:c3a5d95a78dd5ca58041fe6128d5e39ce506fcbe31c888df3d6a799f56e9e2eb`.
- [x] The result catalog archive exists and is 12,163,891 bytes.
- [x] SDK black-box retrieval succeeded:
  `manifest(job_id) -> result_ref -> download_result()` streamed the ZIP,
  validated SHA-256 and atomically produced `radar-sim-result-dc0a067681f7.zip`.
- [x] The Web `Result is unavailable` root cause was a raw binary request that
  omitted `X-Rsim-User`; commit `a5cb28f` centralized owner/auth headers and
  added owner isolation integration coverage.
- [x] A second browser defect was found during real black-box use: Connector and
  result Blob downloads released their object URL in the same browser task,
  leaving full-size `Unconfirmed *.crdownload` files on managed Chromium.
  Commit `f32cf99` now uses one delayed-release `triggerBlobDownload()` helper,
  adds positive UI feedback and cache-busts `app.js`.
- [x] Front radar support is part of the public contract: `FC`/`RadarFC`
  normalize to `RadarFC`, mounting position `front`; Web includes
  `RadarFC（前雷达）`. A real MF4 sample under `D:/data/bydod25/...` was resolved
  as `RadarFC` by acquisition metadata without a full MF4 scan.
- [x] Connector execution contract was raised to v5 so an old Connector cannot
  claim RadarFC-aware work. This Windows PC was updated once through the same
  one-click endpoint. Online capability evidence is
  `update_required=false`, `outdated_count=0`, `required_contract_version=5`,
  Windows unified available count 1.
- [x] The local `result.path` execution change requires Connector contract v6.
  Production and this PC now report `update_required=false`,
  `outdated_count=0`, `required_contract_version=6`.
- [x] The apparent two Python processes are one supervised process chain, not
  duplicate Agents: hidden PowerShell supervisor -> venv Python launcher ->
  base Python interpreter. The server registers one Windows device.

## Current deployment truth

- Production runs immutable release `/home/hoz2wx/radar-sim-ce236bb` at
  `http://10.190.171.44:8877`; systemd is active with `NRestarts=0`.
- `ce236bb` is pushed to `origin/codex/new-branch`.
- Production Connector package: 8,329,620 bytes, SHA-256
  `45cc9a9c39a5dac5d40924d9bc0567d571b92d416caf9c74a9c83f818a2c2b38`.

## P0 remaining implementation and acceptance

- [x] Switch Linux service atomically to `/home/hoz2wx/radar-sim-ce236bb`,
  verify health, service restart count and Connector/Cluster capabilities.
- [ ] In a real browser, refresh the cache-busted page, open
  `job_d8b902defaad`, click `下载结果 ZIP`, and verify a final `.zip` rather
  than `Result is unavailable` or a lingering `.crdownload`.
- [x] Add `result.path` to the one user YAML contract, Web import/edit/export
  and SDK model without exposing device IDs, transfer roots or project names.
- [x] Add a job-oriented SDK convenience that reads the manifest and
  downloads the owner-scoped ZIP with checksum verification; preserve the
  existing low-level `download_result(result_ref, destination)` API.
- [x] Resolve an empty result destination only on the receiving device. Never
  put a resolved home directory or physical path in public Job/Manifest/MCP
  responses.
- [x] Local simulation copies/extracts successful and partial outputs directly
  to `result.path` while retaining catalog ZIP publication.
- [ ] Cluster result delivery reuses the existing direct-transfer kernel for a
  reverse Cluster-to-device plan. Result bytes must not traverse the Linux Web
  API or appear in MCP/model payloads.
- [ ] If the target Connector/SDK device is offline, the Job/result remains
  durable and exposes a structured waiting/action state; simulation success is
  not rewritten as failure. Delivery retry must be idempotent.
- [x] Batch/partial success preserves every input outcome in both the extracted
  directory manifest and owner-scoped ZIP.
- [x] Web and SDK tests cover the same owner, destination and result truth
  semantics. Future AI adapter coverage remains pending with the adapter.

## MCP and Skill readiness review

- [x] Architecture contract says Skill/MCP must be thin wrappers over
  `RadarSimClient` and `/api/v1`; it must never implement another scheduler,
  parse project names or carry file bodies.
- [ ] There is currently no installable radar-sim MCP Server or Skill package in
  the repository. `docs/AI_INTEGRATION_CONTRACT.md` explicitly calls it future
  work. Do not tell users that installing MCP/Skill works today.
- [ ] After the SDK/result-path gates pass, implement one thin AI adapter with
  tools mapped to `submit_yaml`, `get/list/watch/wait`, `diagnosis`,
  `cancel/retry`, manifest/result retrieval and local result delivery.
- [ ] Add an end-to-end Coding-Agent acceptance: dirty workspace is compiled
  unchanged, branch mismatch is a warning only, the selected local/Cluster
  simulation finishes, per-input truth is returned, and result files arrive at
  the user-local resolved destination.

## Validation already run for the current code

- Windows release gate for result delivery, Web/API/SDK/config, local/Cluster
  stages, direct transfer, Connector policy and deployment: 361 passed,
  3 skipped, one existing Starlette/httpx deprecation warning.
- Linux candidate release gate for `/home/hoz2wx/radar-sim-ce236bb`:
  253 passed, 1 skipped.
- Real local result materialization from `job_d8b902defaad`: 239,051,624-byte
  MF4 plus path-free manifest, followed by an idempotent `already_present` retry.
- Earlier RadarFC/result broad Windows gate: 192 passed, 1 skipped, 1 warning.
- Earlier Linux server-relevant gate for `60299a8`: 151 passed, 1 skipped.
- Do not use the old broad `tests/test_cluster.py` Linux run as a release gate:
  nine tests encode Windows path assumptions and are tracked as test portability
  debt, not production evidence.

## Next executable actions

1. Have one normal user browser confirm the final ZIP filename; automated
   browser evidence already proves owner-scoped HTTP 200 and complete bytes.
2. Next sprint: design one authorized Cluster-to-device reverse TransferPlan;
   do not route result bodies through Linux and do not create a second protocol.
3. Add a durable delivery retry action for a temporarily offline destination
   while preserving simulation success and the catalog ZIP.
4. After those result gates, implement the thin MCP/Skill package over the SDK
   and run one real dirty-workspace Coding-Agent end-to-end acceptance.
