"""Tests for cli/agent_binding.py — local workspace binding CLI."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agent_bindings import AgentBindingError


def _load_module():
    path = Path(__file__).resolve().parents[1] / "cli" / "agent_binding.py"
    spec = importlib.util.spec_from_file_location("cli_agent_binding", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load_module()


def _make_parser(module):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    module.register(subparsers)
    return parser


def _parse(module, *argv):
    parser = _make_parser(module)
    return parser.parse_args(["agent-binding", *argv])


# ---------------------------------------------------------------------------
# Parser requirement tests
# ---------------------------------------------------------------------------

def test_parser_register_requires_project_and_workspace_and_output(module):
    parser = _make_parser(module)
    with pytest.raises(SystemExit):
        parser.parse_args(["agent-binding", "register", "--project", "p"])


def test_parser_health_requires_binding_id_and_project(module):
    parser = _make_parser(module)
    with pytest.raises(SystemExit):
        parser.parse_args(["agent-binding", "health", "--binding-id", "id"])


def test_parser_delete_requires_binding_id(module):
    parser = _make_parser(module)
    with pytest.raises(SystemExit):
        parser.parse_args(["agent-binding", "delete"])


def test_parser_list_optional_project_and_json(module):
    args = _parse(module, "list", "--project", "foo", "--json")
    assert args.project == "foo"
    assert args.json is True


def test_parser_data_register_requires_project_and_root(module):
    parser = _make_parser(module)
    with pytest.raises(SystemExit):
        parser.parse_args(["agent-binding", "data-register", "--project", "p"])


def test_parser_register_repeated_output_roots(module):
    args = _parse(
        module,
        "register",
        "--project", "p",
        "--workspace-root", "/tmp/ws",
        "--output-root", "/tmp/ws/out1",
        "--output-root", "/tmp/ws/out2",
    )
    assert args.output_root == ["/tmp/ws/out1", "/tmp/ws/out2"]


def test_default_no_config_attribute(module):
    assert module.NO_CONFIG is True


# ---------------------------------------------------------------------------
# Functional tests with temporary DB and real directories
# ---------------------------------------------------------------------------

def test_register_prints_public_dict_no_paths(module, tmp_path, monkeypatch, capsys):
    ws = tmp_path / "workspace"
    out = tmp_path / "workspace" / "output"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    args = _parse(
        module,
        "register",
        "--project", "demo",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(args, {}) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["project"] == "demo"
    assert result["output_root_count"] == 1
    assert result["configured"] is True
    assert result["healthy"] is True
    assert "id" in result
    assert str(ws) not in captured.out
    assert str(out) not in captured.out
    assert str(db) not in captured.out


def test_data_register_and_list_are_path_free(module, tmp_path, capsys):
    root = tmp_path / "measurements"
    root.mkdir()
    db = tmp_path / "bindings.db"
    register_args = _parse(
        module,
        "data-register",
        "--project", "ovrs25",
        "--data-root", str(root),
        "--db", str(db),
    )
    assert module.run(register_args, {}) == 0
    registered = json.loads(capsys.readouterr().out.strip())
    assert registered["id"].startswith("data-root:sha256:")
    assert str(root) not in str(registered)

    list_args = _parse(module, "data-list", "--db", str(db), "--json")
    assert module.run(list_args, {}) == 0
    listed = json.loads(capsys.readouterr().out.strip())
    assert listed == [registered]


def test_register_multiple_output_roots(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out1 = ws / "build"
    out2 = ws / "dist"
    ws.mkdir()
    out1.mkdir()
    out2.mkdir()

    db = tmp_path / "bindings.db"
    args = _parse(
        module,
        "register",
        "--project", "multi",
        "--workspace-root", str(ws),
        "--output-root", str(out1),
        "--output-root", str(out2),
        "--db", str(db),
    )
    assert module.run(args, {}) == 0

    result = json.loads(capsys.readouterr().out.strip())
    assert result["output_root_count"] == 2


def test_register_idempotent_update(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    args = _parse(
        module,
        "register",
        "--project", "idem",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(args, {}) == 0
    first = json.loads(capsys.readouterr().out.strip())

    # Re-register with same canonical pair is idempotent.
    assert module.run(args, {}) == 0
    second = json.loads(capsys.readouterr().out.strip())
    assert second["id"] == first["id"]
    assert second["created_at"] == first["created_at"]


def test_list_prints_public_dicts(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    reg = _parse(
        module,
        "register",
        "--project", "listme",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(reg, {}) == 0
    capsys.readouterr()  # drain register output

    lst = _parse(module, "list", "--db", str(db))
    assert module.run(lst, {}) == 0

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line]
    assert len(lines) == 1
    pub = json.loads(lines[0])
    assert pub["project"] == "listme"
    assert str(ws) not in captured.out
    assert str(db) not in captured.out


def test_list_json_mode_deterministic(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    reg = _parse(
        module,
        "register",
        "--project", "jdemo",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(reg, {}) == 0
    capsys.readouterr()  # drain register output

    lst = _parse(module, "list", "--db", str(db), "--json")
    assert module.run(lst, {}) == 0

    captured = capsys.readouterr()
    arr = json.loads(captured.out.strip())
    assert isinstance(arr, list)
    assert len(arr) == 1
    assert arr[0]["project"] == "jdemo"


def test_list_filter_by_project(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    for proj in ("alpha", "beta"):
        reg = _parse(
            module,
            "register",
            "--project", proj,
            "--workspace-root", str(ws),
            "--output-root", str(out),
            "--db", str(db),
        )
        assert module.run(reg, {}) == 0
        capsys.readouterr()  # drain each register output

    lst = _parse(module, "list", "--db", str(db), "--project", "alpha")
    assert module.run(lst, {}) == 0

    lines = [line for line in capsys.readouterr().out.strip().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["project"] == "alpha"


def test_health_uses_resolve_authorized_roots(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    reg = _parse(
        module,
        "register",
        "--project", "hlth",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(reg, {}) == 0
    binding_id = json.loads(capsys.readouterr().out.strip())["id"]

    h = _parse(module, "health", "--binding-id", binding_id, "--project", "hlth", "--db", str(db))
    assert module.run(h, {}) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["id"] == binding_id
    assert result["project"] == "hlth"
    assert result["healthy"] is True
    assert result["output_root_count"] == 1
    assert str(ws) not in captured.out
    assert str(out) not in captured.out
    assert str(db) not in captured.out


def test_health_missing_binding_raises(module, tmp_path):
    db = tmp_path / "bindings.db"
    h = _parse(module, "health", "--binding-id", "workspace:sha256:000000000000000000000000", "--project", "missing", "--db", str(db))
    with pytest.raises(AgentBindingError):
        module.run(h, {})


def test_health_project_mismatch_raises(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    reg = _parse(
        module,
        "register",
        "--project", "right",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(reg, {}) == 0
    binding_id = json.loads(capsys.readouterr().out.strip())["id"]

    h = _parse(module, "health", "--binding-id", binding_id, "--project", "wrong", "--db", str(db))
    with pytest.raises(AgentBindingError):
        module.run(h, {})


def test_delete_prints_id_and_deleted(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    reg = _parse(
        module,
        "register",
        "--project", "delme",
        "--workspace-root", str(ws),
        "--output-root", str(out),
        "--db", str(db),
    )
    assert module.run(reg, {}) == 0
    binding_id = json.loads(capsys.readouterr().out.strip())["id"]

    d = _parse(module, "delete", "--binding-id", binding_id, "--db", str(db))
    assert module.run(d, {}) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["id"] == binding_id
    assert result["deleted"] is True
    assert str(db) not in captured.out


def test_delete_missing_raises(module, tmp_path):
    db = tmp_path / "bindings.db"
    d = _parse(module, "delete", "--binding-id", "workspace:sha256:000000000000000000000000", "--db", str(db))
    with pytest.raises(AgentBindingError):
        module.run(d, {})


def test_reopen_store_uses_default_path_when_db_omitted(module, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    default_db = tmp_path / "agent" / "bindings.db"
    monkeypatch.setenv("RSIM_HOME", str(tmp_path))

    reg = _parse(
        module,
        "register",
        "--project", "defaultdb",
        "--workspace-root", str(ws),
        "--output-root", str(out),
    )
    assert module.run(reg, {}) == 0
    assert default_db.exists()


def test_register_validates_repeated_outputs_via_store(module, tmp_path):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    args = _parse(
        module,
        "register",
        "--project", "bad",
        "--workspace-root", str(ws),
        "--output-root", str(tmp_path / "outside"),
        "--db", str(db),
    )
    with pytest.raises(AgentBindingError):
        module.run(args, {})


def test_stdout_contains_no_temp_path(module, tmp_path, capsys):
    ws = tmp_path / "ws"
    out = ws / "out"
    ws.mkdir()
    out.mkdir()

    db = tmp_path / "bindings.db"
    for subcmd, extra in (
        ("register", ["--project", "x", "--workspace-root", str(ws), "--output-root", str(out)]),
        ("list", []),
        ("health", ["--binding-id", "workspace:sha256:000000000000000000000000", "--project", "x"]),
        ("delete", ["--binding-id", "workspace:sha256:000000000000000000000000"]),
    ):
        # health/delete may raise, but we still capture stdout before the raise.
        args = _parse(module, subcmd, *extra, "--db", str(db))
        try:
            module.run(args, {})
        except AgentBindingError:
            pass

    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out
    assert str(ws) not in captured.out
    assert str(out) not in captured.out
    assert str(db) not in captured.out
