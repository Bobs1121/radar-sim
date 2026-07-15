"""Focused tests for the local-only v5 Selena build Stage kernel."""

from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.agent_build_stage as build_stage
from core.agent_bindings import AgentBindingStore
from core.repo import WorkspaceFingerprint


EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def snapshot(sha: str = "a" * 64, commit: str = "b" * 40, dirty: bool = False):
    return WorkspaceFingerprint(
        branch="feature/test",
        commit=commit,
        dirty=dirty,
        sha256=sha,
        staged_diff_sha256=EMPTY_SHA,
        staged_diff_bytes=0,
        unstaged_diff_sha256=EMPTY_SHA,
        unstaged_diff_bytes=0,
        untracked=(),
    )


@pytest.fixture
def local_binding(tmp_path: Path):
    workspace = tmp_path / "workspace"
    output = workspace / "out"
    scripts = workspace / "scripts"
    output.mkdir(parents=True)
    scripts.mkdir()
    script = scripts / "build_selena.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    store = AgentBindingStore(tmp_path / "bindings.db")
    binding = store.register("demo", workspace, (output,))
    return SimpleNamespace(
        workspace=workspace,
        output=output,
        script=script,
        store=store,
        binding=binding,
    )


def prepare(local_binding, monkeypatch, **patch):
    state = local_binding
    user_bindings = SimpleNamespace(
        project="demo",
        workspace_path=str(state.workspace),
        selena_build_script=str(state.script),
    )
    monkeypatch.setattr(
        build_stage,
        "adapt_legacy_config",
        lambda *_args, **_kwargs: SimpleNamespace(user_bindings=user_bindings),
    )
    monkeypatch.setattr(build_stage, "capture_source_snapshot", lambda *_args: snapshot())
    payload = {
        "project": "demo",
        "workspace_binding_id": state.binding.binding_id,
        "build_mode": "Release",
        "clean": False,
    }
    payload.update(patch.pop("payload", {}))
    config = {"build": {"selena_build_script": str(state.script)}}
    return build_stage.prepare_selena_build(
        payload,
        state.store,
        config_loader=patch.pop("config_loader", lambda _project: config),
        command_builder=patch.pop(
            "command_builder",
            lambda _config, _mode, _clean: (["cmd", "/c", str(state.script)], str(state.workspace)),
        ),
        artifact_resolver=patch.pop(
            "artifact_resolver",
            lambda _config, _mode: str(state.output / "selena.exe"),
        ),
        **patch,
    )


def test_prepare_happy_path_is_frozen_and_local_only(local_binding, monkeypatch):
    prepared = prepare(local_binding, monkeypatch)
    assert prepared.project == "demo"
    assert prepared.binding_id == local_binding.binding.binding_id
    assert prepared.command[:2] == ("cmd", "/c")
    assert prepared.cwd == local_binding.workspace.resolve()
    with pytest.raises(FrozenInstanceError):
        prepared.clean = True
    build_stage.verify_prepared_build(prepared)


def test_verify_rejects_script_changed_after_prepare(local_binding, monkeypatch):
    prepared = prepare(local_binding, monkeypatch)
    local_binding.script.write_text("@echo changed\n", encoding="utf-8")
    with pytest.raises(build_stage.AgentBuildStageError, match="changed"):
        build_stage.verify_prepared_build(prepared)


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_prepare_rejects_ambiguous_clean(local_binding, monkeypatch, value):
    with pytest.raises(build_stage.AgentBuildStageError, match="clean"):
        prepare(local_binding, monkeypatch, payload={"clean": value})


@pytest.mark.parametrize("key", ["workspace_root", "output_root", "selena_build_script", "exe_path"])
def test_prepare_rejects_any_path_field(local_binding, monkeypatch, key):
    with pytest.raises(build_stage.AgentBuildStageError, match="local path"):
        prepare(local_binding, monkeypatch, payload={key: "harmless-looking"})


def test_prepare_rejects_missing_or_project_mismatched_binding(local_binding, monkeypatch):
    with pytest.raises(build_stage.AgentBuildStageError):
        prepare(
            local_binding,
            monkeypatch,
            payload={"workspace_binding_id": "workspace:sha256:" + "a" * 24},
        )
    with pytest.raises(build_stage.AgentBuildStageError):
        prepare(local_binding, monkeypatch, payload={"project": "other"})


def test_prepare_rejects_configured_workspace_mismatch(local_binding, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(
        build_stage,
        "adapt_legacy_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            user_bindings=SimpleNamespace(
                project="demo",
                workspace_path=str(other),
                selena_build_script=str(local_binding.script),
            )
        ),
    )
    monkeypatch.setattr(build_stage, "capture_source_snapshot", lambda *_args: snapshot())
    with pytest.raises(build_stage.AgentBuildStageError, match="does not match"):
        build_stage.prepare_selena_build(
            {
                "project": "demo",
                "workspace_binding_id": local_binding.binding.binding_id,
                "build_mode": "Release",
            },
            local_binding.store,
            config_loader=lambda _project: {},
            command_builder=lambda *_args: (["cmd", "/c", str(local_binding.script)], str(local_binding.workspace)),
            artifact_resolver=lambda *_args: str(local_binding.output / "selena.exe"),
        )


def test_prepare_requires_authorized_configured_script(local_binding, monkeypatch, tmp_path):
    local_binding.script.unlink()
    with pytest.raises(build_stage.AgentBuildStageError, match="missing"):
        prepare(local_binding, monkeypatch)

    outside = tmp_path / "outside.bat"
    outside.write_text("@echo off", encoding="utf-8")
    local_binding.script = outside
    with pytest.raises(build_stage.AgentBuildStageError, match="outside"):
        prepare(local_binding, monkeypatch)


def test_prepare_rejects_no_script_fallback(local_binding, monkeypatch):
    monkeypatch.setattr(
        build_stage,
        "adapt_legacy_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            user_bindings=SimpleNamespace(
                project="demo",
                workspace_path=str(local_binding.workspace),
                selena_build_script="",
            )
        ),
    )
    monkeypatch.setattr(build_stage, "capture_source_snapshot", lambda *_args: snapshot())
    with pytest.raises(build_stage.AgentBuildStageError, match="requires"):
        build_stage.prepare_selena_build(
            {
                "project": "demo",
                "workspace_binding_id": local_binding.binding.binding_id,
                "build_mode": "Release",
            },
            local_binding.store,
            config_loader=lambda _project: {},
            command_builder=lambda *_args: ([], None),
            artifact_resolver=lambda *_args: str(local_binding.output / "selena.exe"),
        )


@pytest.mark.parametrize(
    "command,cwd,pattern",
    [
        ([], None, "empty"),
        (["cmd", "/c", "bad\x00script"], None, "invalid"),
        (["python", "build.py"], None, "configured"),
        (["cmd", "/c", "other.bat"], None, "unavailable|configured"),
    ],
)
def test_prepare_rejects_untrusted_command(local_binding, monkeypatch, command, cwd, pattern):
    with pytest.raises(build_stage.AgentBuildStageError, match=pattern):
        prepare(
            local_binding,
            monkeypatch,
            command_builder=lambda *_args: (command, cwd),
        )


def test_prepare_rejects_cwd_and_artifact_escape(local_binding, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(build_stage.AgentBuildStageError, match="working directory"):
        prepare(
            local_binding,
            monkeypatch,
            command_builder=lambda *_args: (["cmd", "/c", str(local_binding.script)], str(outside)),
        )
    with pytest.raises(build_stage.AgentBuildStageError, match="artifact path"):
        prepare(
            local_binding,
            monkeypatch,
            artifact_resolver=lambda *_args: str(outside / "selena.exe"),
        )
    with pytest.raises(build_stage.AgentBuildStageError, match="filename"):
        prepare(
            local_binding,
            monkeypatch,
            artifact_resolver=lambda *_args: str(local_binding.output / "other.exe"),
        )


def test_finish_returns_redacted_evidence_and_detects_change(local_binding, monkeypatch):
    prepared = prepare(local_binding, monkeypatch)
    (local_binding.output / "selena.exe").write_bytes(b"binary")
    monkeypatch.setattr(
        build_stage,
        "capture_source_snapshot",
        lambda *_args: snapshot(sha="c" * 64),
    )
    result = build_stage.finish_selena_build(prepared)
    assert result["source_changed_during_build"] is True
    assert result["artifact"]["logical_path"] == "selena.exe"
    assert result["artifact"]["checksum"].startswith("sha256:")
    assert str(local_binding.workspace.resolve()) not in json.dumps(result)


@pytest.mark.parametrize("content", [None, b""])
def test_finish_rejects_missing_or_empty_artifact(local_binding, monkeypatch, content):
    prepared = prepare(local_binding, monkeypatch)
    if content is not None:
        (local_binding.output / "selena.exe").write_bytes(content)
    with pytest.raises(build_stage.AgentBuildStageError):
        build_stage.finish_selena_build(prepared)


def test_real_git_prepare_and_finish(local_binding, monkeypatch):
    subprocess.run(["git", "init"], cwd=local_binding.workspace, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=local_binding.workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=local_binding.workspace, check=True)
    (local_binding.workspace / ".gitignore").write_text("out/\n", encoding="utf-8")
    (local_binding.workspace / "source.txt").write_text("source", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=local_binding.workspace, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=local_binding.workspace, check=True, capture_output=True)
    monkeypatch.setattr(
        build_stage,
        "adapt_legacy_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            user_bindings=SimpleNamespace(
                project="demo",
                workspace_path=str(local_binding.workspace),
                selena_build_script=str(local_binding.script),
            )
        ),
    )
    prepared = build_stage.prepare_selena_build(
        {
            "project": "demo",
            "workspace_binding_id": local_binding.binding.binding_id,
            "build_mode": "Release",
        },
        local_binding.store,
        config_loader=lambda _project: {"build": {"selena_build_script": str(local_binding.script)}},
        command_builder=lambda *_args: (["cmd", "/c", str(local_binding.script)], str(local_binding.workspace)),
        artifact_resolver=lambda *_args: str(local_binding.output / "selena.exe"),
    )
    (local_binding.output / "selena.exe").write_bytes(b"binary")
    result = build_stage.finish_selena_build(prepared)
    assert result["artifact"]["size"] == 6
