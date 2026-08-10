These wheels are only for the radar-sim Windows connector scaffold:

- PyYAML 6.0.3
- httpx 0.28.1 and its HTTP transport dependencies
- pydantic 2.13.4 and its validation dependencies
- CPython 3.10, 3.11 and 3.12 Windows x64 binary variants where required

The installer first reuses matching packages already visible in the user's
system-site Python environment. It uses this directory only for missing or
incompatible connector dependencies, then falls back to the user's configured
pip index/proxy. Selena, Visual Studio, runtime/DLLs and simulation engines are
not part of this wheelhouse.
