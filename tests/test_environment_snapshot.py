from __future__ import annotations

import pytest
from pathlib import Path
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


def test_nested_selena_branch_snapshot_is_used_for_branch_evidence():
    workspace_before = SimpleNamespace(
        to_dict=lambda: {
            "branch": "outer-main", "commit": "a" * 40, "dirty": False, "sha256": "b" * 64,
        }
    )
    selena_before = SimpleNamespace(
        to_dict=lambda: {
            "branch": "feature/selena", "commit": "c" * 40, "dirty": True, "sha256": "d" * 64,
        }
    )
    snapshot = inspect_selena_build_environment(
        {
            "project": "ovrs25", "workspace_binding_id": BINDING_ID,
            "build_mode": "Release", "expected_branch": "feature/selena",
            "branch_repo_ref": "apl/base/bindings/xpeng",
        },
        object(),
        agent_id="agent-alice-host1", node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=lambda _payload, _store: SimpleNamespace(
            before=workspace_before, branch_before=selena_before, package_build_script_path=None,
        ),
    )
    assert snapshot.workspace["branch"] == "feature/selena"
    check = next(item for item in snapshot.checks if item.requirement_id == "workspace_branch_expectation")
    assert check.code == ""


def test_nested_selena_branch_mismatch_names_the_selena_subrepository():
    before = SimpleNamespace(
        to_dict=lambda: {
            "branch": "feature/actual", "commit": "a" * 40, "dirty": False, "sha256": "b" * 64,
        }
    )
    snapshot = inspect_selena_build_environment(
        {
            "project": "ovrs25", "workspace_binding_id": BINDING_ID,
            "build_mode": "Release", "expected_branch": "feature/expected",
            "branch_repo_ref": "apl/base/bindings/xpeng",
        },
        object(),
        agent_id="agent-alice-host1", node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=lambda _payload, _store: SimpleNamespace(
            before=before, branch_before=before, package_build_script_path=None,
        ),
    )
    check = next(item for item in snapshot.checks if item.requirement_id == "workspace_branch_expectation")
    assert check.code == "workspace_branch_mismatch"
    assert "Selena 子仓" in check.message


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


def test_environment_disables_embedded_clean_before_build_handoff(tmp_path: Path):
    script = tmp_path / "product" / "compile.bat"
    output = tmp_path / "product" / "out"
    script.parent.mkdir(parents=True)
    output.mkdir()
    script.write_text(
        "python3 tools/R2D2.py -m config -clean\n"
        "python3 tools/R2D2.py -bm Release\n",
        encoding="utf-8",
    )
    (output / "selena.exe").write_bytes(b"previous build")
    before = SimpleNamespace(
        to_dict=lambda: {"branch": "main", "commit": "a" * 40, "dirty": False, "sha256": "b" * 64}
    )
    prepared = SimpleNamespace(
        before=before,
        build_script_path=script,
        artifact_path=output / "selena.exe",
        authorized=SimpleNamespace(output_roots=(output,)),
        package_build_script_path=None,
        clean=False,
    )

    snapshot = inspect_selena_build_environment(
        {"project": "demo", "workspace_binding_id": BINDING_ID},
        object(),
        agent_id="agent-a",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        prepare_fn=lambda *_args: prepared,
        vs_adapter=lambda _path: SimpleNamespace(changed=False),
    )

    assert script.read_text(encoding="utf-8").splitlines()[0].startswith("rem radar-sim:")
    check = next(item for item in snapshot.checks if item.requirement_id == "incremental_build_policy")
    assert check.code == "selena_clean_commands_suppressed"
    assert "incrementally" in check.message


def test_environment_only_confirms_package_script_without_running_generators():
    before = SimpleNamespace(
        to_dict=lambda: {"branch": "main", "commit": "a" * 40, "dirty": False, "sha256": "b" * 64}
    )
    prepared = SimpleNamespace(
        before=before,
        build_script_path="jenkins.bat",
        package_build_script_path="cmake_build.bat",
        authorized=SimpleNamespace(workspace_root="D:/workspace"),
    )
    installation = SimpleNamespace(year="2015", tag="vs14", toolset="v140")

    snapshot = inspect_selena_build_environment(
        {"project": "bydod25", "workspace_binding_id": BINDING_ID},
        object(),
        agent_id="agent-a",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        prepare_fn=lambda _payload, _store: prepared,
        vs_adapter=lambda _path: SimpleNamespace(changed=False, installation=installation),
    )

    check = next(item for item in snapshot.checks if item.requirement_id == "package_build_script")
    assert check.status == "passed"
    assert "实际编译前准备" in check.message


def test_environment_missing_workspace_binding_has_chinese_actionable_reason():
    snapshot = inspect_selena_build_environment(
        {"project": "xpengod25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-a",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        prepare_fn=lambda *_args: (_ for _ in ()).throw(ValueError("binding not found")),
    )

    check = snapshot.checks[0]
    assert check.code == "workspace_binding_missing"
    assert "尚未登记" in check.message
    assert "连接这台电脑" in check.action


def test_environment_reports_each_bounded_check_progress():
    before = SimpleNamespace(
        to_dict=lambda: {
            "branch": "feature/selena", "commit": "a" * 40,
            "dirty": False, "sha256": "b" * 64,
        }
    )
    progress = []
    inspect_selena_build_environment(
        {"project": "xpengod25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-a",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        prepare_fn=lambda *_args: SimpleNamespace(
            before=before, branch_before=before, build_script_path=None,
            package_build_script_path=None,
        ),
        progress_fn=progress.append,
    )

    assert any("代码仓" in item for item in progress)
    assert any("Selena 子仓" in item for item in progress)
