# WP2 dependency decision: FastAPI + Uvicorn + HTTPX

Date: 2026-07-10

## Decision

Adopt minimal FastAPI/Uvicorn for the optional v5 `/api/v1` server and HTTPX for the optional Python SDK transport.

Direct pins:

| Package | Version | License | Python | Purpose |
|---|---:|---|---|---|
| FastAPI | 0.139.0 | MIT | >=3.10 | Thin `/api/v1` HTTP adapter, validation error integration, OpenAPI/schema route |
| Uvicorn | 0.50.2 | BSD-3-Clause | >=3.10 | Optional `rsim server serve-v1` ASGI runtime |
| HTTPX | 0.28.1 | BSD-3-Clause | >=3.8 | SDK connection pooling, timeout control, streaming/SSE transport |
| Pydantic | 2.13.4 | MIT | >=3.9 | Existing WP1 `SimulationSpec` model shared by API/Web/SDK |

References: https://fastapi.tiangolo.com/, https://www.uvicorn.org/, https://www.python-httpx.org/, https://docs.pydantic.dev/

## Resolved runtime set

TEMP venv resolved product set audited:

```text
annotated-doc==0.0.4
annotated-types==0.7.0
anyio==4.14.1
certifi==2026.6.17
click==8.4.2
colorama==0.4.6
fastapi==0.139.0
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.18
pydantic==2.13.4
pydantic_core==2.46.4
starlette==1.3.1
typing_extensions==4.16.0
typing-inspection==0.4.2
uvicorn==0.50.2
```

Licenses reviewed from package metadata / upstream project classifiers: FastAPI MIT; Uvicorn BSD-3-Clause; HTTPX/httpcore BSD-3-Clause; Pydantic/pydantic-core MIT; Starlette BSD-3-Clause; AnyIO MIT; click/colorama BSD-3-Clause; h11 MIT; certifi MPL-2.0; idna BSD-3-Clause; annotated-types/annotated-doc/typing-inspection MIT; typing_extensions PSF-2.0.

## Gate evidence

Commands were run in an isolated TEMP venv and download directory, then cleaned up.

- Install/import smoke: `fastapi==0.139.0`, `uvicorn==0.50.2`, `httpx==0.28.1`, `pydantic==2.13.4`.
- FastAPI TestClient smoke: `GET /health` returned expected JSON.
- Starlette/FastAPI `StreamingResponse` smoke: `text/event-stream` streamed `id/event/data`.
- HTTPX streaming smoke: `MockTransport` + `Client.stream(...).iter_lines()` returned expected chunks.
- `pip-audit -r resolved-product.txt --progress-spinner off`: `No known vulnerabilities found`.
- Offline wheel check with `pip download --only-binary=:all:`:
  - Windows CPython 3.12 x64 (`win_amd64`, `cp312`): 17 wheels.
  - Linux CPython 3.12 x86_64 (`manylinux2014_x86_64`, `cp312`): 17 wheels.
  - Linux CPython 3.10 x86_64 (`manylinux2014_x86_64`, `cp310`): 17 wheels.

No credential-bearing index, proxy, or mirror URLs were printed in the gate output.

## Packaging boundary

`setup.py` keeps default `install_requires` unchanged (`PyYAML>=6.0`). New pins are optional extras only:

- `v5-server`: FastAPI, Uvicorn, Pydantic.
- `sdk`: HTTPX, Pydantic.
- `v5`: combined server + SDK pins.

Legacy Python 3.9/base imports remain usable because `core.config`, `core.control_service`, and `rsim server serve` do not import FastAPI/HTTPX/Uvicorn. Installing `.[v5-server]` on Python 3.9 should fail clearly through the selected packages' Python >=3.10 metadata instead of silently omitting server dependencies.

## Proxy, SSL, and enterprise network policy

SDK default is `trust_env=True`, so standard enterprise `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`, `SSL_CERT_FILE`, and `SSL_CERT_DIR` are honored by HTTPX. SDK users can pass `verify=...`, `headers=...`, `transport=...`, or a prebuilt `httpx.Client` for tests and controlled deployments. The SDK never defaults to `verify=False`.

Uvicorn/FastAPI are server-side optional runtime dependencies. Deployment behind enterprise TLS termination should keep certificates/proxy policy outside user `SimulationSpec`.

## Maintenance / release status

The selected versions are stable PyPI releases, not development prereleases:

- FastAPI 0.139.0: stable release, 2026-07-01.
- Uvicorn 0.50.2: stable release, 2026-07-06.
- HTTPX 0.28.1: stable 0.x release; intentionally not the 1.0 development prerelease.
- Pydantic 2.13.4: existing WP1 accepted pin.

Internal third-party notice, legal approval, and mirror availability remain release-gate items before WP10 packaging. If internal mirror/offline approval fails, do not make these dependencies mandatory.

## Architectural constraints

- FastAPI routes are thin HTTP/model/error/SSE adapters only.
- `core/api_v1.py` is framework-agnostic and does not import FastAPI, Uvicorn, or HTTPX.
- SDK wraps API/YAML/transport only and does not copy profile, target, capability, Cluster, or scheduler rules.
- SSE uses Starlette/FastAPI `StreamingResponse`; no `sse-starlette` dependency.
- Uvicorn minimal install is used; no `uvicorn[standard]`, no cloud CLI.

## Rollback

If the dependency gate later fails under internal release constraints, fall back to:

- stdlib `http.server` v1 routes next to the legacy control handler;
- SDK transport implemented with `urllib.request`;
- polling-only `watch()` if SSE streaming cannot be approved.
