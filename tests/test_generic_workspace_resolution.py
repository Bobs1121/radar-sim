import re

import pytest

from cli import agent as agent_module
from core.agent_asset_bindings import AgentAssetBindingStore
from core.agent_bindings import AgentBindingStore
from core.agent_build_stage import finish_selena_build, prepare_selena_build
from core.repo import WorkspaceFingerprint
from core.workspace_recognizer import WorkspaceRecognizer
from core.config import load_config, load_local_execution_config


def _make_unknown_workspace(tmp_path):
    workspace = tmp_path / "customer-checkout"
    selena_script = (
        workspace
        / "apl"
        / "vendor"
        / "bindings"
        / "newproduct"
        / "selena"
        / "jenkins_selena_build.bat"
    )
    package_script = (
        workspace
        / "apl"
        / "vendor"
        / "bindings"
        / "newproduct"
        / "buildscripts"
        / "build_package.bat"
    )
    selena_script.parent.mkdir(parents=True)
    package_script.parent.mkdir(parents=True)
    selena_script.write_text(
        "@echo off\n"
        "set selena_config=CUSTOM_OD25\n"
        'python3 "%root_path%\\ip_dc\\dc_tools\\R2D2.py" '
        "-m !selena_config! -B %root_path%\\ip_dc\\build\n",
        encoding="utf-8",
    )
    package_script.write_text("@echo off\n", encoding="utf-8")
    return workspace, selena_script, package_script


def test_unknown_workspace_derives_stable_internal_identity_and_output(tmp_path):
    workspace, selena_script, package_script = _make_unknown_workspace(tmp_path)
    empty_projects = tmp_path / "no-registered-projects"
    empty_projects.mkdir()
    recognizer = WorkspaceRecognizer(empty_projects)

    first = recognizer.recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(package_script),
        generic_only=True,
    )
    second = recognizer.recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(package_script),
        generic_only=True,
    )

    assert first.status == "resolved"
    assert first.adapter_key == "generic:selena-script"
    assert re.fullmatch(r"workspace-[0-9a-f]{24}", first.internal_project)
    assert second.internal_project == first.internal_project
    assert first.output_dir.casefold() == str(
        workspace / "ip_dc" / "build" / "CUSTOM_OD25"
    ).replace("\\", "/").casefold()


def test_unknown_workspace_identity_never_falls_back_to_legacy_project_config(tmp_path):
    workspace, selena_script, package_script = _make_unknown_workspace(tmp_path)
    recognizer = WorkspaceRecognizer(tmp_path / "no-projects")
    (tmp_path / "no-projects").mkdir()
    outcome = recognizer.recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(package_script),
    )

    with pytest.raises(FileNotFoundError, match="internal execution adapter"):
        load_config(outcome.internal_project)


def test_unknown_workspace_can_use_project_independent_local_execution_config(
    tmp_path, monkeypatch
):
    workspace, selena_script, package_script = _make_unknown_workspace(tmp_path)
    projects = tmp_path / "no-projects"
    projects.mkdir()
    outcome = WorkspaceRecognizer(projects).recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(package_script),
    )
    shared = tmp_path / "config" / "shared" / "selena_paramconfig_v1.txt"
    shared.parent.mkdir(parents=True)
    shared.write_text("input={{INPUT_MF4}}\noutput={{OUTPUT_MF4}}\n", encoding="utf-8")
    platform = tmp_path / "config" / "platforms" / "gen5_selena.yaml"
    platform.parent.mkdir(parents=True)
    platform.write_text("machine:\n  platform: gen5_selena\n", encoding="utf-8")
    monkeypatch.setattr("core.config.get_projects_dir", lambda: projects)
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path / "config")

    config = load_local_execution_config(
        outcome.internal_project,
        project_root=str(workspace),
    )

    assert config["_meta"]["project_independent_local_execution"] is True
    assert config["project"]["name"] == outcome.internal_project
    assert config["assets"]["config_template"] == str(shared)
    assert config["paths"]["project_root"] == str(workspace)


def test_agent_auto_configures_unknown_workspace_without_project_registration(
    tmp_path, monkeypatch
):
    workspace, selena_script, package_script = _make_unknown_workspace(tmp_path)
    runtime_xml = tmp_path / "inputs" / "Runtime.xml"
    runtime_xml.parent.mkdir()
    runtime_xml.write_text("<runtime/>", encoding="utf-8")
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "rsim-home"))

    payload = {
        "contract": "user-run-config/2.0",
        "auto_configure": True,
        "code_path": str(workspace),
        "selena_build_script": str(selena_script),
        "package_build_script": str(package_script),
        "runtime_xml": str(runtime_xml),
    }
    first = agent_module._resolve_v2_run_config(payload)
    second = agent_module._resolve_v2_run_config(payload)

    assert first["status"] == "resolved"
    assert first["adapter_key"] == "generic:selena-script"
    assert re.fullmatch(r"workspace-[0-9a-f]{24}", first["internal_project"])
    assert second["internal_project"] == first["internal_project"]
    assert second["workspace_binding_id"] == first["workspace_binding_id"]
    assert first["selena_build_script_ref"].endswith("jenkins_selena_build.bat")
    assert first["package_build_script_ref"].endswith("build_package.bat")

    binding = AgentBindingStore().get(first["workspace_binding_id"])
    assert binding.project == first["internal_project"]
    assert binding.output_roots == (
        workspace / "ip_dc" / "build" / "CUSTOM_OD25",
    )

    snapshot = WorkspaceFingerprint(
        branch="feature/customer",
        commit="a" * 40,
        dirty=True,
        sha256="b" * 64,
        staged_diff_sha256="c" * 64,
        staged_diff_bytes=0,
        unstaged_diff_sha256="d" * 64,
        unstaged_diff_bytes=1,
        untracked=(),
    )
    monkeypatch.setattr(
        "core.agent_build_stage.inspect_workspace_identity", lambda *_args: snapshot
    )
    monkeypatch.setattr(
        "core.agent_build_stage.adapt_legacy_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 must not enter the legacy adapter")
        ),
    )
    prepared = prepare_selena_build(
        {
            "contract": "user-run-config/2.0",
            "project": first["internal_project"],
            "workspace_binding_id": first["workspace_binding_id"],
            "build_mode": "Release",
            "adapter_key": first["adapter_key"],
            "selena_build_script_ref": first["selena_build_script_ref"],
            "package_build_script_ref": first["package_build_script_ref"],
            "asset_bindings": first["asset_bindings"],
            "runtime_xml": str(runtime_xml),
        },
        AgentBindingStore(),
        asset_binding_store=AgentAssetBindingStore(),
    )

    assert prepared.project == first["internal_project"]
    assert prepared.command == ("cmd", "/c", str(selena_script.resolve()))
    assert prepared.artifact_path == (
        workspace
        / "ip_dc"
        / "build"
        / "CUSTOM_OD25"
        / "dc_tools"
        / "selena"
        / "core"
        / "Release"
        / "selena.exe"
    ).resolve()

    # A wrapper may place the artifact below a dynamically named subfolder
    # that static script inspection cannot know. Post-build discovery stays
    # inside the authorized output root and selects the requested build mode.
    actual = (
        workspace
        / "ip_dc"
        / "build"
        / "CUSTOM_OD25"
        / "dynamic-output"
        / "Release"
        / "Selena.exe"
    )
    actual.parent.mkdir(parents=True)
    actual.write_bytes(b"generic-v2-selena")
    result = finish_selena_build(prepared)
    assert result["artifact"]["logical_path"].endswith(
        "dynamic-output/Release/Selena.exe"
    )


def test_generic_identity_is_stable_when_optional_package_hint_changes(tmp_path):
    workspace, selena_script, package_script = _make_unknown_workspace(tmp_path)
    other_package = package_script.with_name("build_package_v2.bat")
    other_package.write_text("@echo off\n", encoding="utf-8")
    empty_projects = tmp_path / "no-registered-projects"
    empty_projects.mkdir()
    recognizer = WorkspaceRecognizer(empty_projects)

    original = recognizer.recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(package_script),
    )
    changed = recognizer.recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(other_package),
    )

    assert original.internal_project == changed.internal_project


def test_v2_script_without_static_output_uses_project_free_build_root(tmp_path):
    workspace = tmp_path / "new-customer-workspace"
    (workspace / "ip_dc").mkdir(parents=True)
    script = workspace / "tools" / "build_selena.bat"
    script.parent.mkdir()
    script.write_text("@echo off\ncall build-everything.bat\n", encoding="utf-8")

    outcome = WorkspaceRecognizer(tmp_path / "no-project-registry").recognize(
        str(workspace),
        selena_build_script=str(script),
        generic_only=True,
    )

    assert outcome.status == "resolved"
    assert outcome.adapter_key == "generic:selena-script"
    assert outcome.output_dir == str(workspace / "ip_dc" / "build").replace("\\", "/").casefold()


def test_agent_reuses_binding_when_only_optional_package_hint_changes(
    tmp_path, monkeypatch
):
    workspace, selena_script, package_script = _make_unknown_workspace(tmp_path)
    other_package = package_script.with_name("build_package_v2.bat")
    other_package.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "rsim-home"))
    common = {
        "auto_configure": True,
        "code_path": str(workspace),
        "selena_build_script": str(selena_script),
    }

    original = agent_module._resolve_v2_run_config(
        {**common, "package_build_script": str(package_script)}
    )
    changed = agent_module._resolve_v2_run_config(
        {**common, "package_build_script": str(other_package)}
    )
    repeated = agent_module._resolve_v2_run_config(
        {**common, "package_build_script": str(package_script)}
    )

    assert changed["internal_project"] == original["internal_project"]
    assert changed["workspace_binding_id"] == original["workspace_binding_id"]
    assert repeated["workspace_binding_id"] == original["workspace_binding_id"]
    assert {item.project for item in AgentBindingStore().list()} == {
        original["internal_project"],
    }
