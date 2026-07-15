from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.spec import SimulationSpec


def _spec_dict(**overrides):
    data = {
        "schema_version": "1.0",
        "project": "bydod25",
        "selena": {
            "mode": "auto",
            "branch": "",
            "artifact": "",
            "publish_path": "",
            "auto_build": True,
            "build_mode": "Release",
        },
        "data": {
            "path": "D:/measurement/CBNA_0117",
            "limit": 0,
            "required_signals": [],
        },
        "simulation": {
            "target": "auto",
            "profile": "default",
            "timeout_minutes": 0,
        },
        "result": {
            "name": "",
            "retain_days": 30,
        },
    }
    for key, value in overrides.items():
        data[key] = value
    return data


def test_yaml_text_file_and_dict_round_trip(tmp_path):
    text = """
schema_version: "1.0"
project: bydod25
selena:
  mode: auto
  branch: ""
  artifact: ""
  publish_path: team/feature-x
  auto_build: true
  build_mode: Release
data:
  path: 'D:\\measurement\\CBNA_0117'
  limit: 0
  required_signals: [ FCTA_State, "", FCTA_State, RCTA_State ]
simulation:
  target: auto
  profile: default
  timeout_minutes: 0
result:
  name: ""
  retain_days: 30
"""
    from_text = SimulationSpec.from_yaml(text)
    spec_file = tmp_path / "simulation.yaml"
    spec_file.write_text(text, encoding="utf-8")
    from_file = SimulationSpec.from_yaml(spec_file)
    from_dict = SimulationSpec.from_dict(from_text.to_dict())

    assert from_text == from_file == from_dict
    assert from_text.data.path == "D:/measurement/CBNA_0117"
    assert from_text.data.required_signals == ("FCTA_State", "RCTA_State")
    assert SimulationSpec.from_yaml(from_text.to_yaml()) == from_text


def test_json_schema_only_business_fields_and_enums():
    schema = SimulationSpec.json_schema()
    assert set(schema["properties"]) == {"schema_version", "project", "selena", "data", "simulation", "result"}
    assert set(schema["required"]) == {"project", "data"}
    dumped = json.dumps(schema)
    for forbidden in ["agent", "hostname", "cluster_manager", "vs_path", "tcc", "repo_root"]:
        assert forbidden not in dumped.lower()
    defs = schema["$defs"]
    assert defs["SelenaSpec"]["properties"]["mode"]["enum"] == ["auto", "current_workspace", "branch", "existing"]
    assert defs["SelenaSpec"]["properties"]["publish_path"]["type"] == "string"
    assert defs["SelenaSpec"]["properties"]["auto_build"]["type"] == "boolean"
    assert "null" not in json.dumps(defs["SelenaSpec"]["properties"]["auto_build"]).lower()
    assert "auto_build" not in defs["SelenaSpec"].get("required", [])
    assert defs["SimulationRunSpec"]["properties"]["target"]["enum"] == ["auto", "local", "cluster"]
    assert "path" in defs["DataSpec"]["required"]


def test_minimal_user_spec_requires_only_project_and_data_path():
    spec = SimulationSpec.from_dict({"project": "bydod25", "data": {"path": r"D:\measurement\run"}})

    assert spec.schema_version == "1.0"
    assert spec.selena.mode == "auto"
    assert spec.selena.auto_build is True
    assert spec.simulation.target == "auto"
    assert spec.simulation.profile == "default"
    assert spec.result.retain_days == 30
    assert spec.data.path == "D:/measurement/run"
    assert SimulationSpec.from_yaml(spec.to_yaml()) == spec


def test_canonical_json_and_fingerprint_are_path_separator_stable():
    windows = SimulationSpec.from_dict(_spec_dict(data={"path": "D:\\data\\run", "limit": 0, "required_signals": []}))
    forward = SimulationSpec.from_dict(_spec_dict(data={"path": "D:/data/run", "limit": 0, "required_signals": []}))
    assert windows.canonical_json() == forward.canonical_json()
    assert windows.fingerprint() == forward.fingerprint()
    assert windows.canonical_json() == json.dumps(windows.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_unc_path_normalization_preserves_unc_shape():
    spec = SimulationSpec.from_dict(_spec_dict(data={"path": "\\\\server\\share\\folder", "limit": 0, "required_signals": []}))
    assert spec.data.path == "//server/share/folder"


def test_equivalent_unc_paths_have_same_fingerprint():
    a = SimulationSpec.from_dict(_spec_dict(data={"path": "\\\\server\\share\\folder", "limit": 0, "required_signals": []}))
    b = SimulationSpec.from_dict(_spec_dict(data={"path": "//server//share/folder", "limit": 0, "required_signals": []}))
    assert a.data.path == b.data.path == "//server/share/folder"
    assert a.fingerprint() == b.fingerprint()


def test_logical_uri_scheme_is_preserved_and_normalized():
    spec = SimulationSpec.from_dict(_spec_dict(data={"path": "dataset://foo//bar///baz", "limit": 0, "required_signals": []}))
    assert spec.data.path == "dataset://foo/bar/baz"


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"selena": {"mode": "auto", "auto_build": True, "build_mode": "Release", "agent_id": "a1"}},
        {"data": {"path": "D:/x", "cluster_manager": "secret"}},
    ],
)
def test_extra_fields_rejected(payload):
    data = _spec_dict()
    for key, value in payload.items():
        if isinstance(value, dict) and key in data:
            data[key].update(value)
        else:
            data[key] = value
    with pytest.raises(ValidationError):
        SimulationSpec.from_dict(data)


def test_auto_build_rules_branch_current_existing_auto():
    branch = SimulationSpec.from_dict(_spec_dict(selena={"mode": "branch", "branch": "feature/x", "artifact": "", "build_mode": "Release"}))
    assert branch.selena.auto_build is True

    current = SimulationSpec.from_dict(_spec_dict(selena={"mode": "current_workspace", "branch": "", "artifact": "", "build_mode": "Release"}))
    assert current.selena.auto_build is True

    existing = SimulationSpec.from_dict(_spec_dict(selena={"mode": "existing", "branch": "", "artifact": "", "build_mode": "Release"}))
    assert existing.selena.auto_build is False

    auto = SimulationSpec.from_dict(_spec_dict(selena={"mode": "auto", "branch": "main", "artifact": "sel-1", "auto_build": False, "build_mode": "Release"}))
    assert auto.selena.auto_build is False


def test_publish_path_is_business_relative_and_round_trips():
    spec = SimulationSpec.from_dict(
        _spec_dict(selena={
            "mode": "current_workspace",
            "branch": "",
            "artifact": "",
            "publish_path": r"team\feature-x",
            "build_mode": "Release",
        })
    )
    assert spec.selena.publish_path == "team/feature-x"
    assert SimulationSpec.from_yaml(spec.to_yaml()) == spec


@pytest.mark.parametrize("publish_path", ["../escape", "/server/path", r"C:\server\path", "shared://selena/x"])
def test_publish_path_rejects_infrastructure_or_traversal(publish_path):
    with pytest.raises(ValidationError):
        SimulationSpec.from_dict(
            _spec_dict(selena={
                "mode": "current_workspace",
                "branch": "",
                "artifact": "",
                "publish_path": publish_path,
                "build_mode": "Release",
            })
        )


@pytest.mark.parametrize(
    "selena",
    [
        {"mode": "branch", "branch": "", "artifact": "", "auto_build": True, "build_mode": "Release"},
        {"mode": "branch", "branch": "feature/x", "artifact": "", "auto_build": False, "build_mode": "Release"},
        {"mode": "current_workspace", "branch": "", "artifact": "", "auto_build": False, "build_mode": "Release"},
        {"mode": "existing", "branch": "", "artifact": "", "auto_build": True, "build_mode": "Release"},
        {"mode": "auto", "branch": "", "artifact": "", "auto_build": None, "build_mode": "Release"},
    ],
)
def test_auto_build_conflicts_rejected(selena):
    with pytest.raises(ValidationError):
        SimulationSpec.from_dict(_spec_dict(selena=selena))


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": "2.0"},
        {"project": ""},
        {"data": {"path": "", "limit": 0, "required_signals": []}},
        {"data": {"path": "D:/x", "limit": -1, "required_signals": []}},
        {"simulation": {"target": "auto", "profile": "", "timeout_minutes": 0}},
        {"simulation": {"target": "auto", "profile": "default", "timeout_minutes": -1}},
        {"selena": {"mode": "auto", "branch": "", "artifact": "", "auto_build": True, "build_mode": ""}},
        {"result": {"name": "", "retain_days": 0}},
    ],
)
def test_invalid_values_rejected(patch):
    data = _spec_dict()
    for key, value in patch.items():
        data[key] = value
    with pytest.raises(ValidationError):
        SimulationSpec.from_dict(data)


def test_yaml_top_level_must_be_mapping():
    with pytest.raises(ValueError, match="mapping"):
        SimulationSpec.from_yaml("")
    with pytest.raises(ValueError, match="mapping"):
        SimulationSpec.from_yaml("- a\n- b\n")


def test_required_signals_are_trimmed_and_stably_deduped():
    spec = SimulationSpec.from_dict(_spec_dict(data={"path": "D:/x", "limit": 0, "required_signals": [" A ", "B", "A", "", "B", "C"]}))
    assert spec.data.required_signals == ("A", "B", "C")
    assert spec.to_dict()["data"]["required_signals"] == ["A", "B", "C"]


def test_model_is_immutable():
    spec = SimulationSpec.from_dict(_spec_dict())
    with pytest.raises(ValidationError):
        spec.project = "other"
    with pytest.raises(ValidationError):
        spec.data.path = "D:/other"
