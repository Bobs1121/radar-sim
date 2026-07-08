"""rsim doctor — system-level environment diagnostics.

Distinct from ``rsim check`` (which validates *config consistency*), ``doctor``
probes the *actual machine* — whether VS/MATLAB/Qt/Boost/selena_environment are
really installed where the config says (or in common fallback locations),
whether the Python packages import, and whether the cluster UNC shares are
reachable. Output is graded ok / warning / error with repair hints pointing at
``docs/environment-setup.md``.

Used by ``scripts/bootstrap.ps1`` (Mode B one-click deploy) for the preflight
and post-install self-check steps.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# doctor needs project config to read environment.* paths.
NO_CONFIG = False


def register(subparsers):
    p = subparsers.add_parser(
        "doctor",
        help="System-level environment diagnostics (VS/MATLAB/Qt/Boost/packages/cluster)",
    )
    p.add_argument(
        "--backend",
        choices=["local", "cluster", "all"],
        default="",
        help="Which backend's checks to run. Default: auto — 'all' if any local-backend "
        "profile or environment toolchain path is configured, else 'cluster' only. "
        "Use this to avoid false-positive VS/MATLAB/Qt/Boost errors on cluster-only "
        "machines (Mode A).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text",
    )


def _infer_backend(config: dict) -> str:
    """Auto-pick the backend scope from config.

    If the config declares any local-backend profile, or sets any local toolchain
    path (matlab_root/qt_path/boost_root/selena_env_path/vs_version), the user is
    expected to have the full local toolchain → run 'all'. Otherwise this is a
    cluster-only access point (Mode A) → run 'cluster' only, avoiding false
    VS/MATLAB/Qt/Boost errors.
    """
    profiles = config.get("profiles", []) or []
    if any(p.get("backend") == "local" for p in profiles):
        return "all"
    env = config.get("environment", {}) or {}
    local_toolchain_keys = (
        "matlab_root", "qt_path", "boost_root", "BOOST_ROOT",
        "selena_env_path", "vs_version", "python3_path",
    )
    if any(str(env.get(k) or "").strip() for k in local_toolchain_keys):
        return "all"
    return "cluster"


# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------

class Finding:
    """One diagnostic finding: category, severity, message, hint."""

    def __init__(self, category: str, name: str, severity: str, detail: str, hint: str = ""):
        self.category = category
        self.name = name
        self.severity = severity  # ok | warning | error
        self.detail = detail
        self.hint = hint

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "name": self.name,
            "severity": self.severity,
            "detail": self.detail,
            "hint": self.hint,
        }


_FINDINGS: list[Finding] = []


def _record(category: str, name: str, severity: str, detail: str, hint: str = "") -> None:
    _FINDINGS.append(Finding(category, name, severity, detail, hint))


def _path_exists(path: str) -> bool:
    return bool(path) and Path(path).exists()


def _is_windows() -> bool:
    return sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# Local backend checks
# ---------------------------------------------------------------------------

def _check_visual_studio(config: dict) -> None:
    """Detect VS2019/2022/2017 install. Reuses builder._detect_vs_postfix logic."""
    env = config.get("environment", {})
    vs_version = str(env.get("vs_version", "") or "")
    if not _is_windows():
        _record("local", "Visual Studio", "warning",
                "VS detection only runs on Windows (current: %s)" % sys.platform,
                "Run `rsim doctor` on the Windows build machine.")
        return

    vs_root = r"C:\Program Files (x86)\Microsoft Visual Studio"
    found = {}
    for year, tag in (("2019", "vs16"), ("2022", "vs17"), ("2017", "vs15")):
        if Path(vs_root, year).exists():
            found[year] = tag

    if not found:
        _record("local", "Visual Studio", "error",
                "No Visual Studio 2017/2019/2022 found under '%s'" % vs_root,
                "Install VS2019 (recommended) -- see docs/environment-setup.md section 3.")
        return

    years = ", ".join(sorted(found))
    if vs_version and vs_version not in found:
        _record("local", "Visual Studio", "warning",
                "Config asks for VS%s but only VS%s installed" % (vs_version, years),
                "Set environment.vs_version in local.yaml to one of: %s" % years)
    else:
        _record("local", "Visual Studio", "ok",
                "VS%s detected (postfix -vs %s)" % (years, ", ".join(found.values())))


def _check_path_tool(config: dict, env_key: str, label: str, hint: str) -> None:
    """Check a config environment path field exists; warn with hint if not."""
    env = config.get("environment", {})
    value = str(env.get(env_key, "") or "")
    if not value:
        _record("local", label, "warning",
                "environment.%s not set in config" % env_key,
                hint)
        return
    if _path_exists(value):
        _record("local", label, "ok", value)
    else:
        _record("local", label, "error",
                "%s not found: %s" % (label, value),
                hint)


def _check_matlab(config: dict) -> None:
    _check_path_tool(
        config, "matlab_root", "MATLAB",
        "Set environment.matlab_root to your MATLAB install (e.g. C:/Program Files/MATLAB/R2023b).",
    )


def _check_qt(config: dict) -> None:
    _check_path_tool(
        config, "qt_path", "Qt",
        "Set environment.qt_path to the Qt msvc2015_64 dir.",
    )


def _check_boost(config: dict) -> None:
    env = config.get("environment", {})
    value = str(env.get("boost_root") or env.get("BOOST_ROOT") or config.get("boost_root", "") or os.environ.get("BOOST_ROOT", "") or "")
    if not value:
        _record("local", "Boost", "warning",
                "environment.boost_root / BOOST_ROOT not set",
                "Set environment.boost_root in local.yaml.")
    elif _path_exists(value):
        _record("local", "Boost", "ok", value)
    else:
        _record("local", "Boost", "error",
                "Boost not found: %s" % value,
                "Set environment.boost_root to the actual install path.")


def _check_selena_env(config: dict) -> None:
    _check_path_tool(
        config, "selena_env_path", "Selena environment",
        "Set environment.selena_env_path to the selena_environment WIN64 dir.",
    )


def _check_python3(config: dict) -> None:
    env = config.get("environment", {})
    value = str(env.get("python3_path", "") or "")
    if not value:
        # Not always required; R2D2 may use PATH python.
        _record("local", "Python3 (R2D2)", "ok",
                "environment.python3_path unset -- R2D2 will use python on PATH")
        return
    if _path_exists(value):
        _record("local", "Python3 (R2D2)", "ok", value)
    else:
        # Deferred env paths (e.g. %SEL_ENV%\...) can't be resolved statically.
        if "%" in value or "$(" in value:
            _record("local", "Python3 (R2D2)", "ok",
                    "deferred env path (resolved at build time): %s" % value)
        else:
            _record("local", "Python3 (R2D2)", "error",
                    "python3 path not found: %s" % value,
                    "Set environment.python3_path to an existing python3.exe.")


def _check_inner_repo(config: dict) -> None:
    repos = config.get("repos", {})
    inner = str(repos.get("inner_repo_root", "") or "")
    if not inner:
        _record("local", "Inner repo", "warning",
                "repos.inner_repo_root not set",
                "Set repos.inner_repo_root in local.yaml (e.g. C:/BYD_OVS_CB/apl/byd).")
    elif _path_exists(inner):
        _record("local", "Inner repo", "ok", inner)
    else:
        _record("local", "Inner repo", "error",
                "Inner repo not found: %s" % inner,
                "Clone/checkout the inner source tree at this path.")


def _check_python_packages() -> None:
    """Check that the packages rsim needs are importable in this interpreter."""
    packages = [
        ("yaml", "PyYAML", "pip install PyYAML"),
        ("asammdf", "asammdf", "pip install asammdf  (needed for analyze/diff/ask)"),
        ("rich", "rich", "pip install rich  (needed for fancy CLI output)"),
    ]
    for module, label, hint in packages:
        try:
            __import__(module)
            _record("python", label, "ok", "importable")
        except ImportError:
            _record("python", label, "warning",
                    "not importable in current interpreter",
                    hint)


def run_local_checks(config: dict) -> None:
    _check_visual_studio(config)
    _check_matlab(config)
    _check_qt(config)
    _check_boost(config)
    _check_selena_env(config)
    _check_python3(config)
    _check_inner_repo(config)
    _check_python_packages()


# ---------------------------------------------------------------------------
# Cluster backend checks
# ---------------------------------------------------------------------------

def _check_cluster_unc(config: dict) -> None:
    """Check cluster shared workspace / software paths are reachable."""
    cluster = config.get("cluster", {}) or {}
    workspace = str(cluster.get("workspace_root", "") or "")
    software = str(cluster.get("software_path", "") or "")

    for label, path in (("Cluster workspace", workspace), ("Cluster software", software)):
        if not path:
            _record("cluster", label, "warning",
                    "cluster.%s not set" % ("workspace_root" if "workspace" in label else "software_path"),
                    "Set cluster.* UNC paths in config.yaml.")
            continue
        if _path_exists(path):
            _record("cluster", label, "ok", path)
        else:
            _record("cluster", label, "error",
                    "%s not reachable: %s" % (label, path),
                    "Mount/verify network access to this UNC share.")


def _check_cluster_dataset_profile(config: dict) -> None:
    """Check every cluster-backend profile's selena source resolves.

    Uses ``core.profiles.list_profiles`` so legacy ``cluster.profiles[]`` entries
    (converted to the unified shape) are also covered.
    """
    from core.profiles import list_profiles

    try:
        all_profiles = list_profiles(config)
    except Exception:
        all_profiles = config.get("profiles", []) or []

    cluster_profiles = [p for p in all_profiles if p.get("backend") == "cluster"]
    if not cluster_profiles:
        _record("cluster", "Cluster profiles", "warning",
                "No cluster-backend profile defined in config",
                "Add a profile with backend: cluster for cluster runs.")
        return
    for prof in cluster_profiles:
        name = prof.get("name", "<unnamed>")
        selena = prof.get("selena", {}) or {}
        source = selena.get("source", "")
        selena_exe = str(selena.get("exe") or prof.get("selena_exe") or "")
        if source == "path":
            if selena_exe and _path_exists(selena_exe):
                _record("cluster", "Profile '%s' selena" % name, "ok", selena_exe)
            elif selena_exe:
                _record("cluster", "Profile '%s' selena" % name, "error",
                        "selena.exe not reachable: %s" % selena_exe,
                        "Verify the shared selena.exe UNC path.")
            else:
                _record("cluster", "Profile '%s' selena" % name, "warning",
                        "selena.source=path but no exe given", "Set selena.exe.")
        elif source == "build":
            _record("cluster", "Profile '%s' selena" % name, "ok",
                    "source=build (selena copied from local build output)")
        else:
            _record("cluster", "Profile '%s' selena" % name, "warning",
                    "selena.source unset (default build)", "Set selena.source explicitly.")


def run_cluster_checks(config: dict) -> None:
    _check_cluster_unc(config)
    _check_cluster_dataset_profile(config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args, config):
    _FINDINGS.clear()
    backend = getattr(args, "backend", "") or ""
    if not backend:
        backend = _infer_backend(config)
        if not getattr(args, "json", False):
            print(f"(auto) backend scope: {backend}")
    # In --json mode, silence third-party INFO logs (numexpr/asammdf) so stdout
    # is pure JSON — callers pipe stdout without tripping over log noise.
    if getattr(args, "json", False):
        import logging
        logging.getLogger().setLevel(logging.WARNING)
    if backend in ("local", "all"):
        run_local_checks(config)
    if backend in ("cluster", "all"):
        run_cluster_checks(config)

    if getattr(args, "json", False):
        import json
        print(json.dumps({"findings": [f.to_dict() for f in _FINDINGS]}, indent=2, ensure_ascii=False))
    else:
        _print_human()

    errors = sum(1 for f in _FINDINGS if f.severity == "error")
    return 1 if errors else 0


def _print_human() -> None:
    if not _FINDINGS:
        print("No findings.")
        return
    print("rsim doctor - system-level diagnostics")
    print("=" * 60)
    current_category = None
    for f in _FINDINGS:
        if f.category != current_category:
            current_category = f.category
            print("\n[%s]" % f.category)
        mark = {"ok": "OK ", "warning": "W  ", "error": "!!"}[f.severity]
        print(f"  [{mark}] {f.name}: {f.detail}")
        if f.hint and f.severity != "ok":
            print(f"        -> {f.hint}")

    errors = sum(1 for f in _FINDINGS if f.severity == "error")
    warnings = sum(1 for f in _FINDINGS if f.severity == "warning")
    oks = sum(1 for f in _FINDINGS if f.severity == "ok")
    print("\n" + "=" * 60)
    print(f"Summary: {oks} ok, {warnings} warning(s), {errors} error(s)")
    if errors:
        print("Fix the errors above (see docs/environment-setup.md) and re-run `rsim doctor`.")
    elif warnings:
        print("No blocking errors -- warnings are optional but worth reviewing.")
    else:
        print("All checks passed.")
