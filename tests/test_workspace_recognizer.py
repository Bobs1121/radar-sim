from pathlib import Path

import pytest

from core.workspace_recognizer import WorkspaceRecognizer, _is_within, _normalize_path


def _write_adapter(
    root: Path,
    name: str,
    *,
    workspace: str,
    script: str,
    recipe: str = "",
    output: str = "",
    package_script: str = "",
) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    recipe_yaml = f'  recipe: "{recipe}"\n' if recipe else ""
    (folder / "config.yaml").write_text(
        "project:\n"
        f'  name: "{name}"\n'
        '  platform: "gen5_selena"\n'
        f"{recipe_yaml}"
        "repos:\n"
        f'  outer_repo_root: "{workspace}"\n'
        f'  inner_repo_root: "{workspace}"\n'
        "build:\n"
        f'  selena_build_script: "{script}"\n'
        f'  env_build_script: "{package_script}"\n'
        f'  build_output: "{output}"\n',
        encoding="utf-8",
    )


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    _write_adapter(
        root,
        "bydod25",
        workspace="D:/bydod25fr/byd",
        script="D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat",
        recipe="g3n_fvg3_od25",
        output="D:/bydod25fr/byd/build/full_dsp",
    )
    return root


def test_real_config_shape_recognizes_hidden_recipe(projects):
    result = WorkspaceRecognizer(projects).recognize("D:\\BYDOD25FR\\BYD")
    assert result.status == "resolved"
    assert result.adapter_key == "recipe:g3n_fvg3_od25"
    assert result.build_script.endswith("jenkins_selena_build.bat")
    assert result.output_dir == "d:/bydod25fr/byd/build/full_dsp"
    assert result.confidence == 0.9


def test_explicit_script_has_strongest_match(projects):
    result = WorkspaceRecognizer(projects).recognize(
        "D:/bydod25fr/byd",
        "D:/bydod25fr/byd/apl/byd/selena/jenkins_selena_build.bat",
    )
    assert result.status == "resolved"
    assert result.confidence == 1.0
    assert "explicit_build_script" in result.evidence


def test_package_script_is_authorized_and_participates_in_recognition(tmp_path):
    root = tmp_path / "projects"
    _write_adapter(
        root,
        "demo",
        workspace="D:/demo",
        script="D:/demo/tools/build_selena.bat",
        package_script="D:/demo/tools/build_package.bat",
        output="D:/demo/out",
    )
    result = WorkspaceRecognizer(root).recognize(
        "D:/demo",
        selena_build_script="D:/demo/tools/build_selena.bat",
        package_build_script="D:/demo/tools/build_package.bat",
    )
    assert result.status == "resolved"
    assert result.internal_project == "demo"
    assert result.package_build_script.endswith("build_package.bat")
    assert "explicit_package_build_script" in result.evidence


def test_configured_output_is_rebased_to_user_checkout(projects):
    result = WorkspaceRecognizer(projects).recognize(
        "E:/users/alice/byd",
        selena_build_script="E:/users/alice/byd/apl/byd/selena/jenkins_selena_build.bat",
        package_build_script="E:/users/alice/byd/tools/package.bat",
    )
    assert result.status == "resolved"
    assert result.internal_project == "bydod25"
    assert result.output_dir == "e:/users/alice/byd/build/full_dsp"


def test_real_bydod25_adapter_recognizes_both_scripts_on_any_drive():
    projects = Path(__file__).resolve().parents[1] / "config" / "projects"
    result = WorkspaceRecognizer(projects).recognize(
        "E:/users/alice/byd",
        selena_build_script="E:/users/alice/byd/apl/byd/selena/jenkins_selena_build.bat",
        package_build_script="E:/users/alice/byd/apl/byd/tools/builder/cmake_build.bat",
    )

    assert result.status == "resolved"
    assert result.internal_project == "bydod25"
    assert result.adapter_key == "recipe:g3n_fvg3_od25"
    assert result.package_build_script.endswith("/apl/byd/tools/builder/cmake_build.bat")
    assert result.output_dir == "e:/users/alice/byd/build/full_dsp"


def test_missing_adapter_output_is_derived_from_user_selena_script(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _write_adapter(
        root,
        "ovrs25",
        workspace="C:/BYD_OVS_CB",
        script="C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/selena/jenkins_selena_build.bat",
        package_script="C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/buildscripts/package.bat",
        output="",
    )
    monkeypatch.setattr(
        "core.config.derive_project_context_from_selena_script",
        lambda _script: {"build_output": "C:/BYD_OVS_CB/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR"},
    )

    result = WorkspaceRecognizer(root).recognize(
        "C:/BYD_OVS_CB",
        selena_build_script="C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/selena/jenkins_selena_build.bat",
        package_build_script="C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/buildscripts/package.bat",
    )

    assert result.status == "resolved"
    assert result.output_dir == "c:/byd_ovs_cb/ip_dc/build/ros_per_sit_rpm_fct_recr"
    assert result.output_dir != result.workspace_root


@pytest.mark.parametrize(
    "script",
    [
        "D:/bydod25fr/other/build.bat",
        "D:/bydod25fr/byd/../../other/build.bat",
        "D:/bydod25fr/byd-other/build.bat",
    ],
)
def test_explicit_script_cannot_escape_workspace(projects, script):
    result = WorkspaceRecognizer(projects).recognize("D:/bydod25fr/byd", script)
    assert result.status == "unresolved"
    assert result.evidence == ("build_script_outside_workspace",)


def test_auto_discovers_one_script_without_project_concept(tmp_path):
    workspace = tmp_path / "workspace"
    script = workspace / "apl" / "foo" / "jenkins_selena_build.bat"
    script.parent.mkdir(parents=True)
    script.write_text("@echo off", encoding="utf-8")
    empty = tmp_path / "projects"
    empty.mkdir()
    result = WorkspaceRecognizer(empty).recognize(str(workspace))
    assert result.status == "resolved"
    assert result.adapter_key == "generic:selena-script"
    assert result.build_script.endswith("jenkins_selena_build.bat")


def test_multiple_discovered_scripts_do_not_guess(tmp_path):
    workspace = tmp_path / "workspace"
    for name in ("a", "b"):
        script = workspace / name / "jenkins_selena_build.bat"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("@echo off", encoding="utf-8")
    empty = tmp_path / "projects"
    empty.mkdir()
    result = WorkspaceRecognizer(empty).recognize(str(workspace))
    assert result.status == "unresolved"
    assert result.evidence == ("adapter_not_recognized",)


def test_two_equal_internal_adapters_are_ambiguous_and_not_exposed(tmp_path):
    root = tmp_path / "projects"
    script = "D:/same/apl/x/jenkins_selena_build.bat"
    _write_adapter(root, "one", workspace="D:/same", script=script, recipe="one")
    _write_adapter(root, "two", workspace="D:/same", script=script, recipe="two")
    result = WorkspaceRecognizer(root).recognize("D:/same")
    assert result.status == "ambiguous"
    assert set(result.candidates) == {"recipe:one", "recipe:two"}
    public = result.public_dict()
    assert public["candidate_count"] == 2
    assert "recipe:one" not in str(public)
    assert "recipe:two" not in str(public)


def test_public_result_never_leaks_paths_or_adapter(projects):
    result = WorkspaceRecognizer(projects).recognize("D:/bydod25fr/byd")
    public = result.public_dict()
    assert set(public) == {"status", "confidence", "evidence", "candidate_count"}
    serialized = str(public).casefold()
    for secret in ("d:/", "bydod25fr", "g3n_fvg3_od25", "recipe:"):
        assert secret not in serialized


def test_unknown_and_relative_paths_are_unresolved(projects):
    assert WorkspaceRecognizer(projects).recognize("Z:/unknown").status == "unresolved"
    assert WorkspaceRecognizer(projects).recognize("relative/path").evidence == ("code_path_must_be_absolute",)


def test_dot_segments_and_windows_case_are_canonicalized():
    assert _normalize_path("D:/Root/a/../b") == "d:/root/b"
    assert _is_within("D:/ROOT", "d:\\root\\child")
    assert not _is_within("D:/root", "D:/root-other/child")
