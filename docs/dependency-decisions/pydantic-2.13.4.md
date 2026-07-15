# Dependency decision: Pydantic 2.13.4

> WP: WP1-A `SimulationSpec v1` model spike
> Status: Conditional pass
> Date: 2026-07-10

## Decision

Use `pydantic==2.13.4` only in the `v5-spec` extra for WP1-A. Do not add it to `install_requires`; legacy control-plane installs remain PyYAML-only until WP1 is complete and accepted.

## Version pin and official source

- Package: `pydantic==2.13.4`
- Core dependency observed: `pydantic-core==2.46.4`
- Official metadata supplied by main agent: latest stable release as of 2026-05-06, MIT license, Python `>=3.9`, Production/Stable.
- Runtime checked in current environment: Python 3.12.10, Pydantic 2.13.4, pydantic-core 2.46.4.

## Smoke result

Pydantic validation, `extra="forbid"`, frozen model setup, serialization, validation error reporting, and JSON Schema generation were verified in a TEMP-only command. The first inline command had a syntax error; the corrected rerun passed.

## Offline wheel result

Using the currently configured pip source with URLs redacted, `pip download --only-binary=:all:` succeeded in a system TEMP directory and was cleaned up after the check.

Verified targets:

| Target | Result | Wheels observed |
|---|---|---|
| Windows CPython 3.12 x64 | Pass | `pydantic-2.13.4-py3-none-any.whl`, `pydantic_core-2.46.4-cp312-cp312-win_amd64.whl`, `annotated_types-0.7.0`, `typing_extensions-4.16.0`, `typing_inspection-0.4.2` |
| Linux CPython 3.12 x86_64 | Pass | `pydantic-2.13.4-py3-none-any.whl`, `pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`, plus pure-Python dependencies |
| Linux CPython 3.10 x86_64 | Pass | `pydantic-2.13.4-py3-none-any.whl`, `pydantic_core-2.46.4-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`, plus pure-Python dependencies |

## Vulnerability and maintenance status

TEMP venv audit command:

```text
pip-audit --progress-spinner off
```

Result:

```text
No known vulnerabilities found
PIP_AUDIT_EXIT=0
```

The audit included the temporary venv with `pip-audit`, `pydantic==2.13.4`, and their installed dependencies. No internal URL, token, or secret is recorded here.

## Proxy and certificate impact

- Downloads and audit package installation succeeded through the configured corporate pip source.
- Pip output showed an internal mirror URL; it is intentionally redacted from this record.
- No certificate failure was observed during this spike.

## Internal mirror / vendoring recommendation

For reproducible offline Windows/Linux packaging, mirror or vendor these pinned wheels:

- `pydantic==2.13.4`
- `pydantic-core==2.46.4` for Windows CPython 3.12 x64 and Linux CPython 3.10/3.12 x86_64
- `annotated-types==0.7.0`
- `typing-extensions==4.16.0`
- `typing-inspection==0.4.2`

## License review

Main-agent metadata identifies Pydantic as MIT. Before product release, the mirrored wheel set should be included in the standard internal third-party notice review.

## Rollback plan

If Pydantic becomes unavailable, fails internal approval, or cannot be packaged offline, WP1 falls back to dataclass/enum models plus explicit validation and hand-maintained JSON Schema as defined in `docs/DETAILED_DESIGN.md`.
