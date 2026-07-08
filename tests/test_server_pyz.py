"""Tests for the control-server zipapp build (scripts/build_server_pyz.py).

The zipapp is the Linux distribution artifact: a single stdlib-only file that
runs ``python rsim_server.pyz server serve``. These tests guard two invariants
that matter for cross-platform deployment:

1. The build produces a runnable .pyz.
2. Every bundled server file imports only Python stdlib (no PyYAML/asammdf/
   openai) — if someone adds a third-party import to a server-side module, the
   zipapp would silently break on a bare Linux box with no pip packages.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Modules the zipapp bundles for the server. Must stay in sync with
# scripts/build_server_pyz.py SERVER_FILES.
BUNDLED_MODULES = [
    "rsim.py",
    "core/__init__.py",
    "core/control_service.py",
    "core/control_http.py",
    "core/user.py",
    "cli/__init__.py",
    "cli/server.py",
]

# stdlib top-level packages that are fine to import in the server bundle.
_STDLIB_PREFIXES = {
    "os", "sys", "json", "threading", "sqlite3", "time", "pathlib", "http",
    "urllib", "socketserver", "html", "functools", "typing", "re", "io",
    "collections", "hashlib", "secrets", "getpass", "socket", "enum",
    "datetime", "contextlib", "dataclasses", "__future__", "argparse", "uuid",
    "importlib", "traceback", "shutil", "tempfile", "zipapp", "platform",
    "logging", "warnings", "weakref", "copy", "math",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module)
    return mods


def test_data_root_falls_back_outside_pyz(tmp_path, monkeypatch):
    """_data_root() must never resolve inside a .pyz archive.

    When RSIM_HOME is unset and the module lives inside a zipapp, the fallback
    must not use __file__'s parent (that's the .pyz itself — mkdir would fail
    with NotADirectoryError, as seen on real Linux deployment).
    """
    import core.control_service as cs

    # Simulate the zipapp layout: __file__ is <tmp>/rsim_server.pyz/core/ctl.py
    pyz_layout = tmp_path / "rsim_server.pyz" / "core" / "control_service.py"
    pyz_layout.parent.mkdir(parents=True)
    pyz_layout.touch()
    monkeypatch.setattr(cs, "__file__", str(pyz_layout), raising=False)
    monkeypatch.delenv("RSIM_HOME", raising=False)

    root = cs._data_root()
    # Must NOT be inside the .pyz (would be /tmp/.../rsim_server.pyz/...).
    assert ".pyz" not in str(root)
    assert not str(root).startswith(str(tmp_path / "rsim_server.pyz"))
    # Must be a real, creatable directory.
    (root / "results").mkdir(parents=True, exist_ok=True)


def test_data_root_repo_checkout_fallback(tmp_path, monkeypatch):
    """In a normal repo checkout, fallback uses __file__'s parent.parent."""
    import core.control_service as cs

    repo_layout = tmp_path / "repo" / "core" / "control_service.py"
    repo_layout.parent.mkdir(parents=True)
    repo_layout.touch()
    monkeypatch.setattr(cs, "__file__", str(repo_layout), raising=False)
    monkeypatch.delenv("RSIM_HOME", raising=False)

    root = cs._data_root()
    assert root == tmp_path / "repo"


def test_data_root_respects_rsim_home(tmp_path, monkeypatch):
    """RSIM_HOME always wins, and is creatable."""
    import core.control_service as cs

    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "custom"))
    root = cs._data_root()
    assert root == tmp_path / "custom"
    (root / "results").mkdir(parents=True, exist_ok=True)


def test_bundled_modules_are_stdlib_only():
    """No bundled server file may import a third-party package.

    The zipapp runs on bare Linux with no pip install; a stray PyYAML/asammdf
    import would crash it at startup. If this fails, either remove the import
    from the server path or add the file's dependency to the Docker image.
    """
    third_party: list[tuple[str, str]] = []
    for rel in BUNDLED_MODULES:
        src = ROOT / rel
        if not src.exists():
            pytest.fail(f"bundled module missing from repo: {rel}")
        for mod in _imported_modules(src):
            top = mod.split(".")[0]
            if top == "core" or top == "cli":
                continue  # intra-project
            if top not in _STDLIB_PREFIXES:
                third_party.append((rel, mod))
    assert not third_party, (
        f"server bundle has third-party imports (would break bare-Linux run): "
        f"{third_party}"
    )


def test_build_pyz_runs_and_serves(tmp_path):
    """build_server_pyz.build() produces a .pyz that can start a server.

    We invoke ``server create-job`` (no network) rather than ``serve`` to keep
    the test fast and side-effect-free — it still exercises the full import
    chain of the zipapp entry point.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_server_pyz", ROOT / "scripts" / "build_server_pyz.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[arg-type]

    out = tmp_path / "rsim_server.pyz"
    module.build(out)
    assert out.exists() and out.stat().st_size > 0

    # The archive must contain the dedicated __main__.py + all bundled modules.
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "__main__.py" in names
    for rel in BUNDLED_MODULES:
        assert rel in names, f"pyz missing bundled module: {rel}"

    # The pyz must be runnable standalone (simulates bare-Linux invocation).
    env = {
        "RSIM_HOME": str(tmp_path / "rsim_home"),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, str(out), "server", "create-job", "local.check",
         "--project", "ovrs25", "--db-path", str(tmp_path / "test.db")],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, (
        f"pyz create-job failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "job_id" in result.stdout


def test_create_job_project_flag_lands_in_payload(tmp_path):
    """``--project`` on create-job must put project into the task payload.

    Regression guard: the create-job subcommand defines its own ``--project``
    (default ""), which used to clobber a project supplied via the flag or via
    --payload-json because the CLI defaults overwrote the JSON payload
    unconditionally.

    Runs rsim.py in a subprocess so the dynamic cli.* module loading inside
    rsim.py doesn't pollute this process's sys.modules for later tests.
    """
    import json

    db = tmp_path / "proj.db"
    result = subprocess.run(
        [sys.executable, str(ROOT / "rsim.py"), "server", "create-job", "local.check",
         "--project", "ovrs25", "--backend", "local", "--db-path", str(db)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    job = json.loads(result.stdout)
    payload = job["tasks"][0]["payload"]
    assert payload.get("project") == "ovrs25", payload
    assert payload.get("backend") == "local", payload


def test_create_job_payload_json_project_survives(tmp_path):
    """``--payload-json '{"project":"x"}'`` must not be clobbered by empty CLI defaults."""
    import json

    db = tmp_path / "json.db"
    result = subprocess.run(
        [sys.executable, str(ROOT / "rsim.py"), "server", "create-job", "local.check",
         "--payload-json", '{"project":"ovrs25","backend":"local"}',
         "--db-path", str(db)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    job = json.loads(result.stdout)
    payload = job["tasks"][0]["payload"]
    assert payload.get("project") == "ovrs25", payload
    assert payload.get("backend") == "local", payload


def test_server_list_agents_cli_reads_db(tmp_path):
    """``rsim server list-agents`` prints agents previously written to the DB.

    Also guards the UTF-8 stdout fix (KI-3.1): the JSON must print without a
    charmap crash even when agent metadata contains non-ASCII content.
    """
    import json

    # Seed the DB with an agent directly via the service (the CLI has no
    # register subcommand; registration happens over HTTP in production).
    from core.control_service import ControlService

    db = tmp_path / "agents.db"
    service = ControlService(db_path=db)
    service.register_agent(
        "win-01", agent_id="agent-a", hostname="winhost1",
        platform="Windows", capabilities=["local.check"],
        metadata={"note": "端到端测试 agent"},  # non-ASCII to exercise UTF-8 stdout
    )

    result = subprocess.run(
        [sys.executable, str(ROOT / "rsim.py"), "server", "list-agents",
         "--db-path", str(db)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    data = json.loads(result.stdout)
    assert len(data["agents"]) == 1
    agent = data["agents"][0]
    assert agent["agent_id"] == "agent-a"
    assert agent["name"] == "win-01"
    assert agent["hostname"] == "winhost1"
    assert agent["capabilities"] == ["local.check"]
    assert agent["metadata"]["note"] == "端到端测试 agent"
