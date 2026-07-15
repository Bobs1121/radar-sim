from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import core.repo as repo_module
import core.source_resolution_runtime as runtime
from core.artifacts import ArtifactCatalog, SelenaArtifact
from core.selena_resolver import SourceResolutionContext
from core.spec import ProjectCatalog, ProjectProfile, SimulationSpec, UserBindings
from tests.test_api_v1_service import spec_dict

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def project_catalog(project: str = "bydod25") -> ProjectCatalog:
    return ProjectCatalog(
        project=project,
        display_name=project,
        platform="gen5_selena",
        default_profile="default",
        selected_profile="default",
        default_build_mode="Release",
        profiles=(
            ProjectProfile(
                name="default",
                description="Default",
                target="cluster",
                selena_source="existing",
                required_signals=(),
                timeout_minutes=0,
            ),
        ),
        revision="revision-a",
    )


def user_bindings(project: str = "bydod25", workspace_path: str = r"D:\secret\workspace") -> UserBindings:
    return UserBindings(
        project=project,
        workspace_path=workspace_path,
        selena_build_script=workspace_path + r"\build_selena.bat" if workspace_path else "",
        environment_build_script="",
        existing_selena=(),
    )


def artifact(**patch) -> SelenaArtifact:
    data = {
        "id": "artifact-shared",
        "project": "bydod25",
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
        "storage_ref": "artifact://bydod25/shared",
        "accessibility": "cluster",
        "health": "ready",
        "created_by": "builder",
        "created_at": 100.0,
        "retain_until": 1000.0,
    }
    data.update(patch)
    if "storage_ref" not in patch and data["id"] != "artifact-shared":
        data["storage_ref"] = f"artifact://bydod25/{data['id']}"
    return SelenaArtifact(**data)


def bundle(project: str = "bydod25", workspace_path: str = r"D:\secret\workspace"):
    return SimpleNamespace(
        project_catalog=project_catalog(project),
        user_bindings=user_bindings(project, workspace_path),
    )


def test_runtime_central_does_not_inspect_workspace_or_resolve_git_refs(monkeypatch, tmp_path):
    def fail_io(*_args, **_kwargs):
        raise AssertionError("central runtime performed workspace I/O")

    monkeypatch.setattr(repo_module, "inspect_workspace", fail_io)
    monkeypatch.setattr(repo_module, "resolve_git_ref", fail_io)
    catalog = ArtifactCatalog(tmp_path / "control.db")
    catalog.register(artifact())

    inputs = runtime.build_legacy_source_resolution_inputs(
        "alice",
        SimulationSpec.from_dict(spec_dict()),
        catalog_factory=lambda owner: catalog,
        config_loader=lambda project, profile, data_path: bundle(project),
        now_fn=lambda: 123.0,
        inspect_local_workspace=False,
    )

    assert inputs.context.evaluated_at == 123.0
    assert inputs.context.workspace_binding_id.startswith("workspace:sha256:")
    assert inputs.context.workspace_fingerprint is None
    assert inputs.context.branch_commits == {}
    assert [item.id for item in inputs.context.artifacts] == ["artifact-shared"]


def test_runtime_artifact_snapshot_includes_shared_and_owner_private_only(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "control.db")
    shared = catalog.register(artifact(id="shared", binary_checksum=SHA_A, visibility="shared"))
    alice_private = catalog.register(
        artifact(id="alice-private", binary_checksum=SHA_B, visibility="private", owner="alice")
    )
    catalog.register(artifact(id="bob-private", binary_checksum=SHA_C, visibility="private", owner="bob"))

    inputs = runtime.build_legacy_source_resolution_inputs(
        "alice",
        SimulationSpec.from_dict(spec_dict()),
        catalog_factory=lambda owner: catalog,
        config_loader=lambda project, profile, data_path: bundle(project),
        now_fn=lambda: 123.0,
    )

    assert {item.id for item in inputs.context.artifacts} == {shared.id, alice_private.id}


def test_runtime_inspect_local_workspace_is_explicit_and_output_does_not_leak_path(monkeypatch, tmp_path):
    captured = {}

    def fake_context_from_io(**kwargs):
        captured["workspace_path"] = kwargs["workspace_path"]
        return SourceResolutionContext(
            project_revision=kwargs["project_revision"],
            owner=kwargs["owner"],
            evaluated_at=kwargs["evaluated_at"],
            workspace_binding_id=kwargs["workspace_binding_id"],
            workspace_project=kwargs["workspace_project"],
            workspace_fingerprint=None,
            branch_commits={},
            artifacts=kwargs["artifacts"],
        )

    monkeypatch.setattr(runtime, "build_source_resolution_context_from_io", fake_context_from_io)
    catalog = ArtifactCatalog(tmp_path / "control.db")
    secret_path = r"D:\secret\workspace"

    inputs = runtime.build_legacy_source_resolution_inputs(
        "alice",
        SimulationSpec.from_dict(spec_dict()),
        catalog_factory=lambda owner: catalog,
        config_loader=lambda project, profile, data_path: bundle(project, secret_path),
        now_fn=lambda: 123.0,
        inspect_local_workspace=True,
    )

    dumped = json.dumps(inputs.context.artifact_snapshot() + (inputs.context.__dict__.copy(),), default=str)
    assert captured["workspace_path"] == secret_path
    assert secret_path not in dumped
    assert "build_selena.bat" not in dumped


def test_runtime_rejects_invalid_config_loader_shape_without_leaking_paths(tmp_path):
    with pytest.raises(Exception) as excinfo:
        runtime.build_legacy_source_resolution_inputs(
            "alice",
            SimulationSpec.from_dict(spec_dict()),
            catalog_factory=lambda owner: ArtifactCatalog(tmp_path / "control.db"),
            config_loader=lambda project, profile, data_path: (_ for _ in ()).throw(ValueError(r"D:\secret\local.yaml")),
            now_fn=lambda: 123.0,
        )

    dumped = json.dumps(
        {
            "code": excinfo.value.code,
            "message": excinfo.value.message,
            "action_type": excinfo.value.action_type,
        },
        sort_keys=True,
    )
    assert excinfo.value.code == "source_config_invalid"
    assert excinfo.value.status_code == 422
    assert "D:\\secret" not in dumped
    assert "local.yaml" not in dumped


def test_workspace_binding_id_is_stable_across_windows_path_spelling_and_recipe_changes():
    first = user_bindings(workspace_path=r"D:\\Secret\\Workspace\\")
    second = UserBindings(
        project=first.project,
        workspace_path="d:/secret/workspace",
        selena_build_script=r"D:\\other\\recipe.bat",
        environment_build_script="",
        existing_selena=(),
    )
    assert runtime.logical_workspace_binding_id(first) == runtime.logical_workspace_binding_id(second)


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), -1.0])
def test_runtime_rejects_non_finite_or_negative_clock(clock, tmp_path):
    with pytest.raises(Exception) as excinfo:
        runtime.build_legacy_source_resolution_inputs(
            "alice",
            SimulationSpec.from_dict(spec_dict()),
            catalog_factory=lambda owner: ArtifactCatalog(tmp_path / "control.db"),
            config_loader=lambda project, profile, data_path: bundle(project),
            now_fn=lambda: clock,
        )
    assert excinfo.value.code == "source_clock_invalid"
