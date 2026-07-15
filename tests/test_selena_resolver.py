from __future__ import annotations

import json
from dataclasses import replace

import pytest

import core.repo as repo_module
from core.artifacts import SelenaArtifact
from core.repo import WorkspaceFingerprint
from core.selena_resolver import (
    SourceResolutionContext,
    SelenaResolutionOutcome,
    apply_selena_resolution,
    apply_resolution_to_resolved_spec,
    apply_resolution_to_stage_plan,
    resolve_selena,
)
from core.spec import ProjectCatalog, ProjectProfile, SimulationSpec, UserBindings
from core.stages import plan_simulation_stages


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def spec(mode: str = "auto", *, target: str = "cluster", artifact_id: str = "", branch: str = "main") -> SimulationSpec:
    selena = {
        "mode": mode,
        "branch": "",
        "artifact": "",
        "auto_build": True,
        "build_mode": "Release",
    }
    if mode == "branch":
        selena["branch"] = branch
    if mode == "existing":
        selena["artifact"] = artifact_id
        selena["auto_build"] = False
    return SimulationSpec.from_dict(
        {
            "schema_version": "1.0",
            "project": "demo",
            "selena": selena,
            "data": {"path": "D:/data/case", "limit": 0, "required_signals": []},
            "simulation": {"target": target, "profile": target if target != "auto" else "cluster", "timeout_minutes": 0},
            "result": {"name": "", "retain_days": 30},
        }
    )


def catalog(*, target: str = "cluster") -> ProjectCatalog:
    return ProjectCatalog(
        project="demo",
        display_name="Demo",
        platform="gen5_selena",
        default_profile="cluster",
        selected_profile=target,
        default_build_mode="Release",
        profiles=(
            ProjectProfile(
                name="cluster",
                description="Cluster",
                target="cluster",
                selena_source="existing",
                required_signals=(),
                timeout_minutes=0,
            ),
            ProjectProfile(
                name="local",
                description="Local",
                target="local",
                selena_source="build",
                required_signals=(),
                timeout_minutes=0,
            ),
        ),
    )


def bindings(*, workspace: bool = True) -> UserBindings:
    return UserBindings(
        project="demo",
        workspace_path="C:/workspace/demo" if workspace else "",
        selena_build_script="C:/workspace/demo/build_selena.bat",
        environment_build_script="C:/workspace/demo/build_env.bat",
        existing_selena=(),
    )


def fingerprint(*, dirty: bool = True) -> WorkspaceFingerprint:
    return WorkspaceFingerprint(
        branch="main",
        commit="1" * 40,
        dirty=dirty,
        sha256="f" * 64,
        staged_diff_sha256="0" * 64,
        staged_diff_bytes=0,
        unstaged_diff_sha256="2" * 64 if dirty else "0" * 64,
        unstaged_diff_bytes=10 if dirty else 0,
        untracked=(),
    )


def artifact(**patch) -> SelenaArtifact:
    data = {
        "id": "artifact-cluster",
        "project": "demo",
        "owner": "alice",
        "visibility": "shared",
        "branch": "main",
        "commit": "1" * 40,
        "source_kind": "branch",
        "dirty": False,
        "dirty_fingerprint": "",
        "source_changed_during_build": False,
        "build_mode": "Release",
        "toolchain_fingerprint": "toolchain:v1",
        "binary_checksum": SHA_A,
        "interface_manifest": {},
        "signal_manifest": {},
        "storage_ref": "artifact://demo/cluster",
        "accessibility": "cluster",
        "health": "ready",
        "created_by": "builder",
        "created_at": 100,
        "retain_until": 1000,
    }
    data.update(patch)
    return SelenaArtifact(**data)


def context(**patch) -> SourceResolutionContext:
    data = {
        "project_revision": "demo",
        "owner": "alice",
        "evaluated_at": 100,
        "workspace_binding_id": "binding-1",
        "workspace_project": "demo",
        "workspace_fingerprint": fingerprint(),
        "branch_commits": {"main": "1" * 40},
        "artifacts": (artifact(),),
    }
    data.update(patch)
    return SourceResolutionContext(**data)


def test_current_workspace_mode_requires_binding_and_fingerprint_then_resolves():
    no_binding = resolve_selena(spec("current_workspace"), catalog(), bindings(workspace=False), context(workspace_binding_id=""))
    assert no_binding.status == "needs_input"
    assert no_binding.code == "workspace_binding_required"

    no_fingerprint = resolve_selena(
        spec("current_workspace"),
        catalog(),
        bindings(),
        context(workspace_fingerprint=None),
    )
    assert no_fingerprint.status == "needs_input"
    assert no_fingerprint.code == "workspace_fingerprint_required"

    outcome = resolve_selena(spec("current_workspace"), catalog(), bindings(), context())
    assert outcome.status == "resolved"
    assert outcome.resolution == "workspace_build"
    assert outcome.workspace_binding_id == "binding-1"
    assert outcome.dirty is True
    assert outcome.dirty_fingerprint == "f" * 64


def test_branch_mode_requires_binding_and_exact_commit_snapshot():
    missing_binding = resolve_selena(spec("branch"), catalog(), bindings(workspace=False), context(workspace_binding_id=""))
    assert missing_binding.status == "needs_input"
    assert missing_binding.code == "workspace_binding_required"

    missing_commit = resolve_selena(spec("branch"), catalog(), bindings(), context(branch_commits={}))
    assert missing_commit.status == "needs_input"
    assert missing_commit.code == "branch_commit_required"

    outcome = resolve_selena(spec("branch"), catalog(), bindings(), context())
    assert outcome.status == "resolved"
    assert outcome.resolution == "branch_build"
    assert outcome.branch == "main"
    assert outcome.commit == "1" * 40
    assert outcome.dirty is False


def test_existing_mode_supports_explicit_artifact_and_recommended_artifact():
    missing = resolve_selena(spec("existing", artifact_id="missing"), catalog(), bindings(), context())
    assert missing.status == "needs_input"
    assert missing.code == "artifact_snapshot_required"

    explicit = resolve_selena(spec("existing", artifact_id="artifact-cluster"), catalog(), bindings(), context())
    assert explicit.status == "resolved"
    assert explicit.resolution == "artifact"
    assert explicit.artifact_id == "artifact-cluster"

    recommended = resolve_selena(spec("existing"), catalog(), bindings(), context())
    assert recommended.status == "resolved"
    assert recommended.resolution == "artifact"
    assert recommended.artifact_id == "artifact-cluster"


def test_auto_prefers_authorized_workspace_then_falls_back_to_artifact():
    workspace = resolve_selena(spec("auto"), catalog(), bindings(), context())
    assert workspace.status == "resolved"
    assert workspace.resolution == "workspace_build"
    assert workspace.evidence["reason"] == "auto_workspace_preferred"

    fallback = resolve_selena(
        spec("auto"),
        catalog(),
        bindings(workspace=False),
        context(workspace_binding_id="", workspace_fingerprint=None),
    )
    assert fallback.status == "resolved"
    assert fallback.resolution == "artifact"
    assert fallback.artifact_id == "artifact-cluster"

    none = resolve_selena(
        spec("auto"),
        catalog(),
        bindings(workspace=False),
        context(workspace_binding_id="", workspace_fingerprint=None, artifacts=()),
    )
    assert none.status == "needs_input"
    assert none.code == "selena_candidate_required"


def test_artifact_project_build_mode_and_local_cluster_accessibility_are_enforced():
    wrong_build = resolve_selena(
        spec("existing", artifact_id="debug"),
        catalog(),
        bindings(),
        context(artifacts=(artifact(id="debug", build_mode="Debug"),)),
    )
    assert wrong_build.status == "impossible"
    assert wrong_build.code == "artifact_build_mode_incompatible"

    cluster_target_local_artifact = resolve_selena(
        spec("existing", artifact_id="local-only", target="cluster"),
        catalog(),
        bindings(),
        context(artifacts=(artifact(id="local-only", accessibility="local"),)),
    )
    assert cluster_target_local_artifact.status == "impossible"
    assert cluster_target_local_artifact.code == "artifact_target_incompatible"

    local_outcome = resolve_selena(
        spec("existing", target="local"),
        catalog(target="local"),
        bindings(),
        context(artifacts=(artifact(id="local", accessibility="local"),)),
    )
    assert local_outcome.status == "resolved"
    assert local_outcome.evidence["target_decision"]["selected"] == "local"

    auto_target = resolve_selena(
        spec("existing", target="auto"),
        catalog(),
        bindings(),
        context(artifacts=(artifact(id="shared", accessibility="shared"),)),
    )
    assert auto_target.status == "resolved"
    assert auto_target.evidence["target_decision"]["selected"] == "cluster"


def test_resolution_outputs_do_not_leak_paths_scripts_or_executables():
    outcome = resolve_selena(spec("auto"), catalog(), bindings(), context())
    resolved = apply_resolution_to_resolved_spec({"status": "pending", "decisions": {}}, outcome, "rev-1")
    dumped = json.dumps({"outcome": outcome.to_dict(), "resolved": resolved}, sort_keys=True)

    for forbidden in [
        "C:/workspace/demo",
        "build_selena.bat",
        "build_env.bat",
        "C:/shared/selena.exe",
        "executable_path",
        "workspace_path",
    ]:
        assert forbidden not in dumped
    assert resolved["status"] == "partial"


def test_apply_resolution_to_stage_plan_skips_only_final_artifact_resolution_and_keeps_original_plan():
    plan = plan_simulation_stages(spec("auto"))
    artifact_outcome = resolve_selena(
        spec("existing", artifact_id="artifact-cluster"),
        catalog(),
        bindings(),
        context(),
    )
    updated = apply_resolution_to_stage_plan(plan, artifact_outcome)

    original = {stage.stage_type: stage for stage in plan.stages}
    changed = {stage.stage_type: stage for stage in updated.stages}
    assert original["prepare_source"].initial_status == "queued"
    assert original["build_selena"].initial_status == "queued"
    assert changed["prepare_source"].initial_status == "skipped"
    assert changed["build_selena"].initial_status == "skipped"
    assert changed["prepare_source"].skip_reason

    workspace_outcome = resolve_selena(spec("current_workspace"), catalog(), bindings(), context())
    workspace_plan = apply_resolution_to_stage_plan(plan, workspace_outcome)
    assert {stage.stage_type: stage.initial_status for stage in workspace_plan.stages}["prepare_source"] == "queued"


def test_resolve_selena_does_not_perform_workspace_git_or_catalog_io(monkeypatch):
    def fail_io(*_args, **_kwargs):
        raise AssertionError("resolver performed I/O")

    monkeypatch.setattr(repo_module, "inspect_workspace", fail_io)
    monkeypatch.setattr(repo_module, "resolve_git_ref", fail_io)

    outcome = resolve_selena(spec("auto"), catalog(), bindings(), context())
    assert outcome.status == "resolved"
    assert outcome.resolution == "workspace_build"


def test_private_dirty_artifact_is_owner_only_and_never_recommended():
    private_dirty = artifact(
        id="private-dirty",
        visibility="private",
        dirty=True,
        dirty_fingerprint="sha256:dirty",
    )
    own = resolve_selena(
        spec("existing", artifact_id=private_dirty.id),
        catalog(),
        bindings(),
        context(artifacts=(private_dirty,)),
    )
    assert own.status == "resolved"
    assert own.dirty is True
    assert own.dirty_fingerprint == "sha256:dirty"

    other_owner = resolve_selena(
        spec("existing", artifact_id=private_dirty.id),
        catalog(),
        bindings(),
        context(owner="bob", artifacts=(private_dirty,)),
    )
    assert other_owner.status == "impossible"
    assert other_owner.code == "artifact_visibility_incompatible"

    recommended = resolve_selena(
        spec("existing"),
        catalog(),
        bindings(),
        context(artifacts=(private_dirty,)),
    )
    assert recommended.status == "needs_input"


@pytest.mark.parametrize(
    ("item", "code"),
    [
        (artifact(id="expired", retain_until=50), "artifact_retention_expired"),
        (artifact(id="changed", source_changed_during_build=True), "artifact_not_shareable"),
        (artifact(id="degraded", health="degraded"), "artifact_health_not_ready"),
    ],
)
def test_explicit_artifact_rejects_expired_changed_or_unhealthy(item, code):
    outcome = resolve_selena(
        spec("existing", artifact_id=item.id),
        catalog(),
        bindings(),
        context(artifacts=(item,), evaluated_at=100),
    )
    assert outcome.status == "impossible"
    assert outcome.code == code


def test_context_validates_commits_workspace_project_and_project_revision():
    with pytest.raises(ValueError, match="exact commit"):
        context(branch_commits={"main": "abc"})
    with pytest.raises(ValueError, match="workspace fingerprint"):
        context(workspace_fingerprint=replace(fingerprint(), commit="abc"))
    for invalid_time in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError, match="evaluated_at"):
            context(evaluated_at=invalid_time)

    wrong_workspace = resolve_selena(
        spec("current_workspace"),
        catalog(),
        bindings(),
        context(workspace_project="other"),
    )
    assert wrong_workspace.status == "needs_input"
    assert wrong_workspace.code == "workspace_binding_required"

    revision_catalog = replace(catalog(), revision="revision-a")
    mismatch = resolve_selena(
        spec("existing", artifact_id="artifact-cluster"),
        revision_catalog,
        bindings(),
        context(project_revision="revision-b"),
    )
    assert mismatch.status == "impossible"
    assert mismatch.code == "project_revision_mismatch"


def test_unknown_profile_is_rejected():
    payload = spec("existing", artifact_id="artifact-cluster").to_dict()
    payload["simulation"]["profile"] = "missing-profile"
    outcome = resolve_selena(SimulationSpec.from_dict(payload), catalog(), bindings(), context())
    assert outcome.status == "impossible"
    assert outcome.code == "simulation_profile_unknown"


def test_atomic_resolution_application_keeps_overall_partial_and_stage_snapshot_consistent():
    plan = plan_simulation_stages(spec("auto"))
    artifact_outcome = resolve_selena(
        spec("existing", artifact_id="artifact-cluster"),
        catalog(),
        bindings(),
        context(),
    )
    application = apply_selena_resolution(plan, artifact_outcome, project_revision="revision-a")
    resolved = application.resolved_spec_dict()
    stages = {stage.stage_type: stage for stage in application.stage_plan.stages}

    assert resolved["status"] == "partial"
    assert resolved["project_revision"] == "revision-a"
    assert resolved["decisions"]["selena"]["artifact_id"] == "artifact-cluster"
    assert application.stage_plan.resolved_spec == resolved
    assert application.mutated_stages == ("prepare_source", "build_selena", "register_artifact")
    assert stages["prepare_source"].initial_status == "skipped"
    assert stages["build_selena"].initial_status == "skipped"
    assert stages["register_artifact"].initial_status == "skipped"


def test_resolved_application_clears_stale_error_and_outcome_invariants_are_enforced():
    outcome = resolve_selena(spec("current_workspace"), catalog(), bindings(), context())
    resolved = apply_resolution_to_resolved_spec(
        {"status": "needs_input", "code": "old", "action": "old", "decisions": {}},
        outcome,
        "revision-a",
    )
    assert resolved["status"] == "partial"
    assert "code" not in resolved and "action" not in resolved

    with pytest.raises(ValueError, match="requires a resolution"):
        SelenaResolutionOutcome(status="resolved", code="x", action="x")
