from __future__ import annotations

import pytest
from types import SimpleNamespace

from core.agent_policy import NODE_KIND_WINDOWS_AGENT
from core.environment_snapshot import (
    EnvironmentCheckResult,
    EnvironmentSnapshot,
    EnvironmentSnapshotError,
    inspect_selena_build_environment,
)


BINDING_ID = "workspace:sha256:" + "a" * 24


def test_ready_snapshot_is_path_free_and_satisfies_build_requirements():
    snapshot = EnvironmentSnapshot(
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        project="ovrs25",
        workspace_binding_id=BINDING_ID,
        scope="selena_build",
        checks=(
            EnvironmentCheckResult("workspace_binding", "source.workspace.read", "passed"),
            EnvironmentCheckResult("selena_build_toolchain", "build.selena", "passed"),
        ),
        created_at=10,
        expires_at=310,
    )

    result = snapshot.to_dict()
    assert result["status"] == "ready"
    assert result["snapshot_id"].startswith("environment:sha256:")
    assert snapshot.satisfies(["workspace_binding", "selena_build_toolchain"])
    assert "C:\\" not in str(result)


def test_snapshot_rejects_path_leak_in_public_message():
    with pytest.raises(EnvironmentSnapshotError, match="absolute path"):
        EnvironmentCheckResult(
            "workspace_binding",
            "source.workspace.read",
            "failed",
            message="C:/secret/workspace is missing",
        )


def test_build_environment_inspection_returns_ready_snapshot_without_running_build():
    calls = []

    def prepare(payload, store):
        calls.append((dict(payload), store))
        return object()

    snapshot = inspect_selena_build_environment(
        {"project": "ovrs25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=prepare,
    )

    assert calls
    assert snapshot.status == "ready"
    assert snapshot.expires_at == 400
    assert snapshot.satisfies(["workspace_binding", "selena_build_toolchain", "artifact_local_staging"])


def test_build_environment_inspection_returns_blocked_path_free_failure():
    def prepare(payload, store):
        raise ValueError("configured workspace does not match binding")

    snapshot = inspect_selena_build_environment(
        {"project": "ovrs25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=prepare,
    )

    assert snapshot.status == "blocked"
    assert snapshot.checks[0].code == "selena_build_environment_unavailable"


def test_expected_branch_mismatch_is_a_non_blocking_visible_warning():
    before = SimpleNamespace(
        to_dict=lambda: {
            "branch": "feature/actual",
            "commit": "a" * 40,
            "dirty": True,
            "sha256": "b" * 64,
        }
    )
    prepared = SimpleNamespace(before=before, package_build_script_path=None)
    snapshot = inspect_selena_build_environment(
        {
            "project": "ovrs25",
            "workspace_binding_id": BINDING_ID,
            "build_mode": "Release",
            "expected_branch": "feature/expected",
        },
        object(),
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=lambda _payload, _store: prepared,
    )

    branch_check = next(item for item in snapshot.checks if item.requirement_id == "workspace_branch_expectation")
    assert snapshot.status == "ready"
    assert branch_check.status == "passed"
    assert branch_check.code == "workspace_branch_mismatch"
    assert "feature/expected" in branch_check.message
    assert "feature/actual" in branch_check.message


def test_environment_adapts_visual_studio_before_capturing_final_workspace_snapshot():
    calls = {"prepare": 0}
    before = SimpleNamespace(
        to_dict=lambda: {
            "branch": "feature/current",
            "commit": "a" * 40,
            "dirty": True,
            "sha256": "b" * 64,
        }
    )

    def prepare(_payload, _store):
        calls["prepare"] += 1
        return SimpleNamespace(
            before=before,
            build_script_path="jenkins.bat",
            package_build_script_path=None,
        )

    installation = SimpleNamespace(year="2015", tag="vs14", toolset="v140")
    adaptation = SimpleNamespace(changed=True, installation=installation)
    snapshot = inspect_selena_build_environment(
        {"project": "bydod25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=prepare,
        vs_adapter=lambda _path: adaptation,
    )

    assert calls["prepare"] == 2
    assert snapshot.status == "ready"
    check = next(item for item in snapshot.checks if item.requirement_id == "visual_studio_toolchain")
    assert check.code == "selena_build_script_vs_adapted"
    assert "Visual Studio 2015" in check.message


def test_environment_prepares_package_generated_dependencies():
    before = SimpleNamespace(
        to_dict=lambda: {"branch": "main", "commit": "a" * 40, "dirty": False, "sha256": "b" * 64}
    )
    prepared = SimpleNamespace(
        before=before,
        build_script_path="jenkins.bat",
        package_build_script_path="cmake_build.bat",
        authorized=SimpleNamespace(workspace_root="D:/workspace"),
    )
    generated = SimpleNamespace(
        generator="D:/workspace/ip_if/tools/pad_gen/bin/pad_generator.pl",
        changed=True,
        generated_targets=("apl/byd/padrpm",),
    )
    installation = SimpleNamespace(year="2015", tag="vs14", toolset="v140")

    snapshot = inspect_selena_build_environment(
        {"project": "bydod25", "workspace_binding_id": BINDING_ID},
        object(),
        agent_id="agent-a",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        prepare_fn=lambda _payload, _store: prepared,
        vs_adapter=lambda _path: SimpleNamespace(changed=False, installation=installation),
        generated_dependency_preparer=lambda *_args: generated,
    )

    check = next(item for item in snapshot.checks if item.requirement_id == "package_generated_dependencies")
    assert check.status == "passed"
    assert check.code == "package_generated_dependencies_prepared"
