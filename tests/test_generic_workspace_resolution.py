import re

from cli import agent as agent_module
from core.agent_asset_bindings import AgentAssetBindingStore
from core.agent_bindings import AgentBindingStore
from core.agent_build_stage import prepare_selena_build
from core.repo import WorkspaceFingerprint
from core.workspace_recognizer import WorkspaceRecognizer


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
    )
    second = recognizer.recognize(
        str(workspace),
        selena_build_script=str(selena_script),
        package_build_script=str(package_script),
    )

    assert first.status == "resolved"
    assert first.adapter_key == "generic:selena-script"
    assert re.fullmatch(r"workspace-[0-9a-f]{24}", first.internal_project)
    assert second.internal_project == first.internal_project
    assert first.output_dir.casefold() == str(
        workspace / "ip_dc" / "build" / "CUSTOM_OD25"
    ).replace("\\", "/").casefold()


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
        "core.agent_build_stage.capture_source_snapshot", lambda *_args: snapshot
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


def test_generic_identity_changes_when_the_build_contract_changes(tmp_path):
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

    assert original.internal_project != changed.internal_project


def test_agent_does_not_reuse_binding_after_generic_script_contract_changes(
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

    assert changed["internal_project"] != original["internal_project"]
    assert changed["workspace_binding_id"] != original["workspace_binding_id"]
    assert repeated["workspace_binding_id"] == original["workspace_binding_id"]
    assert {item.project for item in AgentBindingStore().list()} == {
        original["internal_project"],
        changed["internal_project"],
    }
