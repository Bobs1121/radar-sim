from pathlib import Path

import pytest

from core.agent_asset_bindings import AgentAssetBindingStore
from core.agent_bindings import AgentBindingStore
from core.agent_build_stage import AgentBuildStageError, prepare_selena_build, stage_runtime_bundle_from_build
from core.repo import WorkspaceFingerprint
from core.agent_source_lease import AgentSourceLease


def _snapshot(dirty=False):
    return WorkspaceFingerprint(
        branch="feature/demo",
        commit="a" * 40,
        dirty=dirty,
        sha256="b" * 64,
        staged_diff_sha256="c" * 64,
        staged_diff_bytes=0,
        unstaged_diff_sha256="d" * 64,
        unstaged_diff_bytes=0,
        untracked=(),
    )


def test_v2_build_stages_only_exe_dll_and_bound_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    output = workspace / "build"
    output.mkdir(parents=True)
    script = workspace / "build.bat"
    script.write_text("@echo off", encoding="utf-8")
    package_script = workspace / "package.bat"
    package_script.write_text("@echo off", encoding="utf-8")
    exe = output / "selena.exe"
    exe.write_bytes(b"exe")
    (output / "runtime.dll").write_bytes(b"dll")
    assets = tmp_path / "assets"
    assets.mkdir()
    runtime = assets / "Runtime.xml"
    adapter = assets / "adapter.txt"
    mat_filter = assets / "signals.filter"
    runtime.write_text("<runtime/>", encoding="utf-8")
    adapter.write_text("adapter", encoding="utf-8")
    mat_filter.write_text("filter", encoding="utf-8")
    binding_store = AgentBindingStore(tmp_path / "bindings.db")
    binding = binding_store.register("internal-demo", workspace, (output,))
    asset_store = AgentAssetBindingStore(tmp_path / "bindings.db")
    asset_binding = asset_store.register(assets)
    config = {
        "project": {"name": "demo"},
        "repos": {"inner_repo_root": str(workspace), "outer_repo_root": str(workspace)},
        "build": {
            "selena_build_script": str(script),
            "env_build_script": str(package_script),
            "build_output": str(output),
            "script_args_template": [],
        },
    }
    monkeypatch.setattr("core.agent_build_stage.capture_source_snapshot", lambda *_args: _snapshot())
    prepared = prepare_selena_build(
        {
            "contract": "user-run-config/2.0",
            "project": "internal-demo",
            "workspace_binding_id": binding.binding_id,
            "build_mode": "Release",
            "adapter_key": "recipe:demo",
            "selena_build_script_ref": "build.bat",
            "package_build_script_ref": "package.bat",
            "asset_bindings": {
                "runtime_xml": asset_binding.binding_id,
            },
            "runtime_xml": str(runtime),
            "adapter_file": str(adapter),
            "mat_filter": str(mat_filter),
        },
        binding_store,
        asset_binding_store=asset_store,
        config_loader=lambda _project: config,
        command_builder=lambda *_args: (["cmd", "/c", str(script)], str(workspace)),
        artifact_resolver=lambda *_args: str(exe),
    )
    result = {
        "before": _snapshot().to_dict(),
        "after": _snapshot().to_dict(),
        "source_changed_during_build": False,
    }
    staged = stage_runtime_bundle_from_build(
        prepared, result, created_at=100.0, staging_root=tmp_path / "staging"
    )
    assert {item["role"] for item in staged["runtime_bundle"]["files"]} == {
        "entrypoint", "runtime_library", "runtime_config"
    }
    assert "simulation_assets" not in staged
    serialized = str(staged["runtime_bundle"])
    assert "adapter_key" not in serialized
    assert "adapter.txt" not in serialized
    assert staged["runtime_bundle_identity"] == {"adapter_key": "recipe:demo"}


def test_v2_runtime_bundle_refuses_source_change(tmp_path):
    class Prepared:
        contract = "user-run-config/2.0"

    with pytest.raises(AgentBuildStageError, match="source changed"):
        stage_runtime_bundle_from_build(
            Prepared(), {"source_changed_during_build": True}, created_at=1.0, staging_root=tmp_path
        )


def test_branch_build_rebases_script_output_and_cwd_into_worktree(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    output = workspace / "build"
    script = workspace / "tools" / "build.bat"
    package_script = workspace / "tools" / "package.bat"
    output.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text("@echo off", encoding="utf-8")
    package_script.write_text("@echo off", encoding="utf-8")
    worktree = tmp_path / "controlled" / "job" / "worktree"
    (worktree / "tools").mkdir(parents=True)
    (worktree / "tools" / "build.bat").write_text("@echo off", encoding="utf-8")
    (worktree / "tools" / "package.bat").write_text("@echo off", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    runtime = assets / "Runtime.xml"
    adapter = assets / "adapter.txt"
    mat_filter = assets / "signals.filter"
    runtime.write_text("<runtime/>", encoding="utf-8")
    adapter.write_text("a", encoding="utf-8")
    mat_filter.write_text("m", encoding="utf-8")
    bindings = AgentBindingStore(tmp_path / "bindings.db")
    binding = bindings.register("internal-demo", workspace, (output,))
    asset_store = AgentAssetBindingStore(tmp_path / "bindings.db")
    asset_binding = asset_store.register(assets)
    source = AgentSourceLease(
        lease_id="source-lease:sha256:" + "c" * 64, prepare_stage_id="source-1", prepare_attempt=1,
        project="internal-demo", workspace_binding_id=binding.binding_id, requested_ref="feature/demo",
        commit="d" * 40, repo_path=workspace, worktree_path=worktree,
        controlled_root=tmp_path / "controlled", created_at=1, expires_at=100, status="ready",
    )
    config = {
        "project": {"name": "demo"},
        "repos": {"inner_repo_root": str(workspace), "outer_repo_root": str(workspace)},
        "build": {
            "selena_build_script": str(script), "env_build_script": str(package_script),
            "build_output": str(output), "script_args_template": [],
        },
    }
    observed = {}

    def command_builder(rebased, _mode, _clean):
        observed["config"] = rebased
        return ["cmd", "/c", rebased["build"]["selena_build_script"]], rebased["repos"]["inner_repo_root"]

    monkeypatch.setattr("core.agent_build_stage.capture_source_snapshot", lambda *_args: _snapshot())
    prepared = prepare_selena_build(
        {
            "contract": "user-run-config/2.0", "project": "internal-demo",
            "workspace_binding_id": binding.binding_id, "build_mode": "Release",
            "adapter_key": "recipe:demo", "branch": "feature/demo", "commit": "d" * 40,
            "selena_build_script_ref": "tools/build.bat",
            "package_build_script_ref": "tools/package.bat",
            "source_lease_ref": source.lease_id,
            "asset_bindings": {"runtime_xml": asset_binding.binding_id},
            "runtime_xml": str(runtime), "adapter_file": str(adapter), "mat_filter": str(mat_filter),
        },
        bindings, source_lease=source, asset_binding_store=asset_store,
        config_loader=lambda _project: config, command_builder=command_builder,
        artifact_resolver=lambda rebased, _mode: str(Path(rebased["build"]["build_output"]) / "selena.exe"),
    )
    assert prepared.cwd == worktree.resolve()
    assert prepared.build_script_path == (worktree / "tools" / "build.bat").resolve()
    assert prepared.artifact_path == (worktree / "build" / "selena.exe").resolve()
    assert str(workspace) not in str(observed["config"])
