# Stable Web / SDK / Windows Connector identity handoff

## Implemented contract

- The trusted no-auth path keeps `X-Rsim-User` as a logical grouping label;
  it is not authentication. Authenticated `/api/v1` requests continue to take
  owner only from the Bearer principal and ignore the header.
- Web no longer invents a random `web-*` owner. On first no-auth use it asks
  for a durable user label (normally the company NTID), normalizes it to
  `user-<lowercase-label>`, and stores it locally. Clearing browser storage or
  changing browsers therefore requires re-entering the same label, not a new
  Connector installation.
- The SDK default is `user-<lowercase OS login>` instead of a machine hash.
  Integrations spanning multiple computers should pass the same normalized
  `user-...` value explicitly.
- The one-click launcher receives the stable owner from the Web/SDK request.
  `bootstrap.ps1` preserves an existing explicit owner; only old generated
  `web-*`/`sdk-*` owners are migrated when a new stable owner is supplied,
  preserving Agent ID, install root and path bindings.
- An existing browser with `rsimBrowserUserId` keeps that legacy owner long
  enough to read its old jobs and displays a one-time upgrade prompt. The
  upgrade is explicit; old jobs are not silently rewritten or merged.

## Verification

Focused checks are recorded by the parent agent after integration. Relevant
local checks for this slice are `node --check radar_sim_web/static/app.js`,
the SDK identity test in `tests/test_sdk.py`, and connector/deployment tests
covering `scripts/bootstrap.ps1` and the rendered owner header.

## Remaining boundary

No-auth identity labels can be forged by any trusted-intranet caller. A formal
deployment must enable Bearer authentication; authenticated one-click device
pairing remains a separate short-lived pairing feature and is intentionally
not enabled by this change.
