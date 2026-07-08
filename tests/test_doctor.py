"""Tests for rsim doctor — system-level environment diagnostics.

Doctor probes the actual machine (VS/MATLAB/Qt/Boost installs, Python package
imports, cluster UNC reachability). Tests mock Path.exists and package imports
so they don't depend on the host actually having the toolchain installed.
"""

import json
from pathlib import Path

import pytest

from cli import doctor


@pytest.fixture(autouse=True)
def reset_findings():
    """Each test starts with a clean findings list."""
    doctor._FINDINGS.clear()
    yield
    doctor._FINDINGS.clear()


def _finding_map():
    return {f.name: f for f in doctor._FINDINGS}


def _exists_map(paths):
    """Build a set of path strings that 'exist', for a fake Path.exists."""
    return {str(Path(p)) for p in paths}


def test_doctor_reports_ok_when_all_paths_exist(monkeypatch):
    config = {
        "environment": {
            "matlab_root": "C:/MATLAB",
            "qt_path": "C:/Qt",
            "boost_root": "C:/Boost",
            "selena_env_path": "C:/selena_env",
            "python3_path": "C:/python3.exe",
            "vs_version": "2019",
        },
        "repos": {"inner_repo_root": "C:/BYD_OVS_CB"},
    }
    existing = _exists_map([
        "C:/MATLAB", "C:/Qt", "C:/Boost", "C:/selena_env", "C:/python3.exe",
        "C:/BYD_OVS_CB",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019",
    ])
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in existing)

    doctor.run_local_checks(config)
    m = _finding_map()
    assert m["MATLAB"].severity == "ok"
    assert m["Qt"].severity == "ok"
    assert m["Boost"].severity == "ok"
    assert m["Selena environment"].severity == "ok"
    assert m["Inner repo"].severity == "ok"
    assert m["Visual Studio"].severity == "ok"


def test_doctor_flags_missing_vs_install(monkeypatch):
    config = {"environment": {"vs_version": "2019"}, "repos": {}}
    # No VS dirs exist.
    monkeypatch.setattr(Path, "exists", lambda self: False)

    doctor._check_visual_studio(config)
    m = _finding_map()
    assert m["Visual Studio"].severity == "error"
    assert "Visual Studio" in m["Visual Studio"].detail or "No Visual Studio" in m["Visual Studio"].detail


def test_doctor_warns_on_vs_version_mismatch(monkeypatch):
    """Config asks for VS2022 but only VS2019 is installed → warning."""
    config = {"environment": {"vs_version": "2022"}}
    existing = _exists_map([r"C:\Program Files (x86)\Microsoft Visual Studio\2019"])
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in existing)

    doctor._check_visual_studio(config)
    m = _finding_map()
    assert m["Visual Studio"].severity == "warning"
    assert "2022" in m["Visual Studio"].detail


def test_doctor_flags_missing_configured_path(monkeypatch):
    """A configured path that doesn't exist on disk → error."""
    config = {"environment": {"matlab_root": "C:/nope/MATLAB"}}
    monkeypatch.setattr(Path, "exists", lambda self: False)

    doctor._check_matlab(config)
    m = _finding_map()
    assert m["MATLAB"].severity == "error"
    assert "C:/nope/MATLAB" in m["MATLAB"].detail


def test_doctor_warns_when_path_unset():
    """An unset environment field → warning (not error, since it may be optional)."""
    config = {"environment": {}}
    doctor._check_matlab(config)
    m = _finding_map()
    assert m["MATLAB"].severity == "warning"
    assert "not set" in m["MATLAB"].detail


def test_doctor_handles_deferred_env_path(monkeypatch):
    """Paths with %VAR% / $(...) are resolved at build time — not flagged missing."""
    config = {"environment": {"python3_path": "%SEL_ENV%/bin/python3.exe"}}
    monkeypatch.setattr(Path, "exists", lambda self: False)

    doctor._check_python3(config)
    m = _finding_map()
    assert m["Python3 (R2D2)"].severity == "ok"
    assert "deferred" in m["Python3 (R2D2)"].detail


def test_doctor_cluster_checks_reachable_unc(monkeypatch):
    config = {
        "cluster": {
            "workspace_root": r"\\share\cluster\workspace",
            "software_path": r"\\share\cluster\software",
        },
        "profiles": [
            {"name": "cloud-build", "backend": "cluster",
             "selena": {"source": "path", "exe": r"\\share\selena\selena.exe"}},
        ],
    }
    existing = _exists_map([
        r"\\share\cluster\workspace",
        r"\\share\cluster\software",
        r"\\share\selena\selena.exe",
    ])
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in existing)

    doctor.run_cluster_checks(config)
    m = _finding_map()
    assert m["Cluster workspace"].severity == "ok"
    assert m["Cluster software"].severity == "ok"
    assert m["Profile 'cloud-build' selena"].severity == "ok"


def test_doctor_cluster_profile_legacy_selena_exe_field(monkeypatch):
    """Legacy cluster.profiles[].selena_exe (underscore) is still recognized
    after list_profiles converts it to the unified shape."""
    config = {
        "cluster": {
            "workspace_root": r"\\share\ws",
            "software_path": r"\\share\sw",
            "profiles": [
                {"name": "shared", "selena_exe": r"\\share\selena\selena.exe",
                 "source": "RadarFC"},
            ],
        },
    }
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in _exists_map([
        r"\\share\ws", r"\\share\sw", r"\\share\selena\selena.exe",
    ]))
    doctor.run_cluster_checks(config)
    m = _finding_map()
    # Legacy cluster.profiles are backend=cluster after conversion; source=path
    # is inferred from selena_exe being set.
    assert "Profile 'shared' selena" in m
    assert m["Profile 'shared' selena"].severity == "ok"


def test_doctor_cluster_profile_source_path_missing_exe(monkeypatch):
    """source=path but selena.exe unreachable → error (not silent warning)."""
    config = {
        "cluster": {"workspace_root": r"\\share\ws", "software_path": r"\\share\sw"},
        "profiles": [
            {"name": "p", "backend": "cluster",
             "selena": {"source": "path", "exe": r"\\share\missing\selena.exe"}},
        ],
    }
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in _exists_map([
        r"\\share\ws", r"\\share\sw",
    ]))
    doctor.run_cluster_checks(config)
    m = _finding_map()
    assert m["Profile 'p' selena"].severity == "error"
    assert "selena.exe" in m["Profile 'p' selena"].detail


def test_doctor_cluster_flags_unreachable_unc(monkeypatch):
    config = {
        "cluster": {"workspace_root": r"\\share\missing", "software_path": ""},
        "profiles": [],
    }
    monkeypatch.setattr(Path, "exists", lambda self: False)

    doctor.run_cluster_checks(config)
    m = _finding_map()
    assert m["Cluster workspace"].severity == "error"
    assert m["Cluster software"].severity == "warning"  # unset → warning
    assert m["Cluster profiles"].severity == "warning"  # no cluster profile


def test_doctor_json_output_format(monkeypatch, capsys):
    config = {"environment": {}, "repos": {}, "cluster": {}, "profiles": []}
    monkeypatch.setattr(Path, "exists", lambda self: False)

    class Args:
        backend = "all"
        json = True

    rc = doctor.run(Args(), config)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "findings" in data
    assert isinstance(data["findings"], list)
    assert all("severity" in f for f in data["findings"])
    # No VS → at least one error.
    assert any(f["severity"] == "error" for f in data["findings"])
    assert rc == 1


def test_doctor_returns_zero_when_no_errors(monkeypatch):
    config = {
        "environment": {
            "matlab_root": "C:/MATLAB", "qt_path": "C:/Qt", "boost_root": "C:/Boost",
            "selena_env_path": "C:/se", "python3_path": "C:/py.exe", "vs_version": "2019",
        },
        "repos": {"inner_repo_root": "C:/repo"},
        "cluster": {
            "workspace_root": "C:/ws", "software_path": "C:/sw",
        },
        "profiles": [
            {"name": "cloud-build", "backend": "cluster", "selena": {"source": "build"}},
        ],
    }
    existing = _exists_map([
        "C:/MATLAB", "C:/Qt", "C:/Boost", "C:/se", "C:/py.exe", "C:/repo", "C:/ws", "C:/sw",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2019",
    ])
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in existing)

    class Args:
        backend = "all"
        json = False

    rc = doctor.run(Args(), config)
    # Python package checks may add warnings if asammdf/rich not installed in
    # the test env, but those are warnings, not errors — rc is 0 with no errors.
    errors = [f for f in doctor._FINDINGS if f.severity == "error"]
    assert errors == []
    assert rc == 0


def test_doctor_backend_filter_runs_only_local(monkeypatch):
    """--backend local skips cluster checks."""
    config = {
        "environment": {}, "repos": {},
        "cluster": {"workspace_root": r"\\missing\share"},
        "profiles": [],
    }
    monkeypatch.setattr(Path, "exists", lambda self: False)

    class Args:
        backend = "local"
        json = True

    doctor.run(Args(), config)
    categories = {f.category for f in doctor._FINDINGS}
    assert "local" in categories
    assert "python" in categories
    assert "cluster" not in categories


def test_doctor_infer_backend_cluster_only(monkeypatch):
    """No local profile + no toolchain paths → auto backend 'cluster' only.
    Avoids false VS/MATLAB/Qt/Boost errors on Mode A access points."""
    config = {
        "environment": {},
        "cluster": {"workspace_root": r"\\share\ws", "software_path": r"\\share\sw"},
        "profiles": [{"name": "cloud", "backend": "cluster",
                      "selena": {"source": "path", "exe": r"\\share\selena.exe"}}],
    }
    monkeypatch.setattr(Path, "exists", lambda self: str(self) in _exists_map([
        r"\\share\ws", r"\\share\sw", r"\\share\selena.exe",
    ]))

    class Args:
        backend = ""  # auto
        json = True

    doctor.run(Args(), config)
    categories = {f.category for f in doctor._FINDINGS}
    assert "cluster" in categories
    assert "local" not in categories  # no false VS/MATLAB/Qt/Boost errors


def test_doctor_infer_backend_all_when_local_profile_present(monkeypatch):
    """A local-backend profile → auto backend 'all' (user has full toolchain)."""
    config = {
        "environment": {},
        "profiles": [{"name": "local-build", "backend": "local"}],
    }
    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert doctor._infer_backend(config) == "all"


def test_doctor_infer_backend_all_when_toolchain_paths_set():
    """Toolchain paths in environment.* → auto backend 'all'."""
    config = {"environment": {"matlab_root": "C:/MATLAB"}, "profiles": []}
    assert doctor._infer_backend(config) == "all"
