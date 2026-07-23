import pytest
from pydantic import ValidationError

from core.user_config import UserRunConfig


def _build_config(**patch):
    config = {
        "schema_version": "2.0",
        "selena": {
            "source": "build",
            "code_path": r"D:\bydod25fr\byd",
            "branch": "",
            "selena_build_script": r"D:\bydod25fr\build_selena.bat",
            "package_build_script": r"D:\bydod25fr\package.bat",
            "runtime_xml": r"D:\data\Runtime.xml",
        },
        "data": {"path": r"D:\measurements\run"},
        "simulation": {
            "target": "cluster",
            "adapter_file": r"D:\data\adapter.txt",
            "mat_filter": r"D:\data\signals.filter",
        },
    }
    config.update(patch)
    return config


def test_build_config_has_no_project_profile_or_environment_concept():
    config = UserRunConfig.from_dict(_build_config())
    assert config.selena.code_path == "D:/bydod25fr/byd"
    raw = config.to_dict()
    for forbidden in ["project", "profile", "cluster", "agent", "toolchain", "mount_map"]:
        assert forbidden not in raw


def test_branch_is_optional_expectation_for_the_same_current_workspace_flow():
    current = UserRunConfig.from_dict(_build_config())
    branch_values = _build_config()
    branch_values["selena"]["branch"] = "feature/FRGVBYDP-21653"
    branch = UserRunConfig.from_dict(branch_values)
    assert current.selena.branch == ""
    assert branch.selena.branch == "feature/FRGVBYDP-21653"
    assert current.selena.source == branch.selena.source == "build"


def test_existing_requires_existing_path_and_runtime_xml():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "existing_path": r"D:\Selena",
        "runtime_xml": r"D:\Selena\Runtime.xml",
    }
    parsed = UserRunConfig.from_dict(config)
    assert parsed.selena.existing_path == "D:/Selena"
    assert parsed.selena.runtime_xml == "D:/Selena/Runtime.xml"
    assert parsed.selena.code_path == ""


def test_existing_missing_existing_path_is_rejected():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "runtime_xml": r"D:\Selena\Runtime.xml",
    }
    with pytest.raises(ValidationError, match="existing_path"):
        UserRunConfig.from_dict(config)


def test_existing_missing_runtime_xml_is_rejected():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "existing_path": r"D:\Selena",
    }
    with pytest.raises(ValidationError, match="runtime_xml"):
        UserRunConfig.from_dict(config)


def test_existing_with_old_bundle_is_rejected():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "bundle": "selena-bundle:sha256:" + "a" * 64,
    }
    with pytest.raises(ValidationError, match="existing_path"):
        UserRunConfig.from_dict(config)


def test_existing_with_old_executable_is_rejected():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "executable": r"D:\Selena\selena.exe",
        "runtime_xml": r"D:\Selena\Runtime.xml",
    }
    with pytest.raises(ValidationError, match="existing_path"):
        UserRunConfig.from_dict(config)


@pytest.mark.parametrize("field", ["selena_build_script", "package_build_script", "runtime_xml"])
def test_build_source_required_fields(field):
    config = _build_config()
    config["selena"][field] = ""
    with pytest.raises(ValidationError, match=field):
        UserRunConfig.from_dict(config)


def test_mat_filter_is_always_required():
    config = _build_config()
    config["simulation"]["mat_filter"] = ""
    with pytest.raises(ValidationError, match="mat_filter"):
        UserRunConfig.from_dict(config)


def test_adapter_file_is_optional():
    config = _build_config()
    config["simulation"]["adapter_file"] = ""
    parsed = UserRunConfig.from_dict(config)
    assert parsed.simulation.adapter_file == ""


def test_project_field_is_rejected_not_exported():
    config = _build_config()
    config["project"] = "bydod25"
    with pytest.raises(ValidationError, match="project"):
        UserRunConfig.from_dict(config)


def test_yaml_roundtrip_is_stable():
    first = UserRunConfig.from_dict(_build_config())
    exported = first.to_yaml()
    second = UserRunConfig.from_yaml(exported)
    assert second == first
    assert second.fingerprint() == first.fingerprint()
    assert "bundle:" not in exported
    assert "executable:" not in exported
    assert "result:" not in exported
    assert "timeout_minutes:" not in exported
    assert "limit:" not in exported


def test_existing_yaml_exports_only_existing_fields():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "existing_path": r"D:\Selena",
        "runtime_xml": r"D:\Selena\Runtime.xml",
    }
    exported = UserRunConfig.from_dict(config).to_yaml()
    assert "existing_path:" in exported
    assert "runtime_xml:" in exported
    for field in ("code_path:", "branch:", "selena_build_script:", "package_build_script:", "bundle:", "executable:"):
        assert field not in exported


def test_legacy_build_script_maps_to_selena_build_script():
    config = _build_config()
    config["selena"].pop("selena_build_script")
    config["selena"]["build_script"] = r"D:\bydod25fr\build_selena.bat"
    config["selena"]["build_mode"] = "Debug"
    parsed = UserRunConfig.from_dict(config)
    assert parsed.selena.selena_build_script == "D:/bydod25fr/build_selena.bat"
    assert parsed.selena.package_build_script == "D:/bydod25fr/package.bat"
    assert not hasattr(parsed.selena, "build_script")
    assert not hasattr(parsed.selena, "build_mode")


def test_legacy_build_fields_never_exported():
    config = _build_config()
    config["selena"].pop("selena_build_script")
    config["selena"]["build_script"] = r"D:\bydod25fr\build_selena.bat"
    config["selena"]["build_mode"] = "Debug"
    raw = UserRunConfig.from_dict(config).to_dict()
    assert "build_script" not in raw["selena"]
    assert "build_mode" not in raw["selena"]
    assert raw["selena"]["selena_build_script"] == "D:/bydod25fr/build_selena.bat"


def test_existing_source_preserves_optional_workspace_evidence():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "existing_path": r"D:\Selena",
        "runtime_xml": r"D:\Selena\Runtime.xml",
        "code_path": r"D:\workspace",
        "branch": "release/od25",
        "selena_build_script": r"D:\workspace\build_selena.bat",
        "package_build_script": r"D:\workspace\build_package.bat",
    }
    parsed = UserRunConfig.from_dict(config)
    assert parsed.selena.source == "existing"
    assert parsed.selena.code_path == "D:/workspace"
    assert parsed.to_dict()["selena"]["selena_build_script"].endswith("build_selena.bat")
    exported = parsed.to_yaml()
    assert "code_path:" in exported
    assert "package_build_script:" in exported


def test_existing_build_script_evidence_requires_code_path():
    config = _build_config()
    config["selena"] = {
        "source": "existing",
        "existing_path": r"D:\Selena",
        "runtime_xml": r"D:\Selena\Runtime.xml",
        "selena_build_script": r"D:\workspace\build_selena.bat",
    }
    with pytest.raises(ValidationError, match="code_path"):
        UserRunConfig.from_dict(config)


def test_legacy_data_limit_is_silently_dropped():
    config = _build_config()
    config["data"]["limit"] = 100
    parsed = UserRunConfig.from_dict(config)
    assert "limit" not in parsed.to_dict()["data"]
    assert "limit" not in parsed.to_yaml()


def test_legacy_simulation_timeout_minutes_is_silently_dropped():
    config = _build_config()
    config["simulation"]["timeout_minutes"] = 120
    parsed = UserRunConfig.from_dict(config)
    assert "timeout_minutes" not in parsed.to_dict()["simulation"]
    assert "timeout_minutes" not in parsed.to_yaml()


def test_legacy_result_block_is_silently_dropped():
    config = _build_config()
    config["result"] = {"name": "my-run", "retain_days": 7}
    parsed = UserRunConfig.from_dict(config)
    assert "result" not in parsed.to_dict()
    assert "result" not in parsed.to_yaml()
