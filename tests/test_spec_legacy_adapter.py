from __future__ import annotations

import copy
import json

import pytest

from core.spec import LegacyConfigAdapterError, SimulationSpec, adapt_legacy_config


def _legacy_config() -> dict:
    return {
        "_meta": {"project": "demo"},
        "project": {"name": "Display Demo", "platform": "gen5_selena"},
        "project_root": "C:/workspace/demo",
        "repos": {"inner_repo_root": "C:/workspace/demo/inner", "outer_repo_root": "C:/workspace/demo"},
        "build": {
            "build_mode": "Debug",
            "selena_build_script": "C:/workspace/demo/build_selena.bat",
            "env_build_script": "C:/workspace/demo/build_env.bat",
        },
        "selena": {"build_mode": "RelWithDebInfo"},
        "simulation": {
            "datasets": [{"name": "first", "input_dir": r"D:\measurements\first"}],
        },
        "cluster": {
            "timeout_min": 120,
            "required_input_signals": [" ClusterSig ", "ClusterSig", ""],
            "kill_password": "do-not-export",
            "manager": "cluster-manager",
            "queue": "internal-queue",
            "software_path": r"\\cluster\software",
            "workspace_root": r"\\cluster\workspace",
        },
        "active_profile": "local-build",
        "profiles": [
            {
                "name": "local-build",
                "description": "Local build",
                "backend": "local",
                "selena": {"source": "build", "exe": ""},
                "data": {"copy": False, "required_signals": [" LocalSig ", "LocalSig", ""]},
            },
            {
                "name": "branch-build",
                "description": "Branch build",
                "backend": "cluster",
                "selena": {"source": "build", "exe": "", "selena_branch": "feature/wp1b"},
                "data": {"copy": False, "required_signals": ["BranchSig"]},
                "cluster": {"timeout_min": 45},
            },
            {
                "name": "fallback-cluster",
                "description": "Cluster fallback signals",
                "backend": "cluster",
                "selena": {"source": "build", "exe": ""},
                "cluster": {"timeout_min": 30},
            },
            {
                "name": "existing-selena",
                "description": "Existing Selena",
                "backend": "cluster",
                "selena": {"source": "path", "exe": "C:/shared/selena.exe"},
                "data": {"copy": False, "required_signals": []},
                "cluster": {"timeout_min": 90},
            },
            {
                "name": "existing-literal",
                "description": "Existing source spelling",
                "backend": "cluster",
                "selena": {"source": "existing", "exe": "C:/shared/selena2.exe"},
                "data": {"copy": False, "required_signals": []},
                "cluster": {"timeout_min": 60},
            },
        ],
    }


def test_current_workspace_mapping_uses_active_profile():
    bundle = adapt_legacy_config(_legacy_config())

    assert bundle.spec.project == "demo"
    assert bundle.spec.selena.mode == "current_workspace"
    assert bundle.spec.selena.auto_build is True
    assert bundle.spec.selena.branch == ""
    assert bundle.spec.selena.artifact == ""
    assert bundle.spec.selena.build_mode == "Debug"
    assert bundle.spec.data.path == "D:/measurements/first"
    assert bundle.spec.data.limit == 0
    assert bundle.spec.data.required_signals == ("LocalSig",)
    assert bundle.spec.simulation.target == "local"
    assert bundle.spec.simulation.profile == "local-build"
    assert bundle.spec.simulation.timeout_minutes == 0
    assert bundle.spec.result.name == ""
    assert bundle.spec.result.retain_days == 30


def test_branch_mapping_uses_explicit_profile_before_active_profile():
    bundle = adapt_legacy_config(_legacy_config(), profile="branch-build")

    assert bundle.spec.selena.mode == "branch"
    assert bundle.spec.selena.branch == "feature/wp1b"
    assert bundle.spec.selena.auto_build is True
    assert bundle.spec.simulation.target == "cluster"
    assert bundle.spec.simulation.profile == "branch-build"
    assert bundle.spec.simulation.timeout_minutes == 45
    assert bundle.spec.data.required_signals == ("BranchSig",)


@pytest.mark.parametrize("profile", ["existing-selena", "existing-literal"])
def test_existing_mapping_uses_logical_artifact_and_binding(profile):
    bundle = adapt_legacy_config(_legacy_config(), profile=profile)

    assert bundle.spec.selena.mode == "existing"
    assert bundle.spec.selena.auto_build is False
    assert bundle.spec.selena.branch == ""
    assert bundle.spec.selena.artifact == f"legacy:demo:{profile}"
    assert bundle.spec.simulation.target == "cluster"

    binding = next(item for item in bundle.user_bindings.existing_selena if item.profile == profile)
    assert binding.artifact_id == bundle.spec.selena.artifact
    assert binding.executable_path.startswith("C:/shared/selena")
    yaml_text = bundle.spec.to_yaml()
    assert "C:/shared/selena" not in yaml_text


def test_project_id_priority_and_catalog_profile_summary():
    cfg = _legacy_config()
    bundle = adapt_legacy_config(cfg, project="explicit-project", profile="fallback-cluster")

    catalog = bundle.project_catalog
    assert bundle.spec.project == "explicit-project"
    assert catalog.project == "explicit-project"
    assert catalog.display_name == "Display Demo"
    assert catalog.platform == "gen5_selena"
    assert catalog.default_profile == "default"
    assert catalog.selected_profile == "fallback-cluster"
    assert catalog.default_build_mode == "Debug"

    profiles = {profile.name: profile for profile in catalog.profiles}
    assert {"default", "local-build", "branch-build", "fallback-cluster", "existing-selena", "existing-literal"} <= set(profiles)
    assert profiles["fallback-cluster"].target == "cluster"
    assert profiles["fallback-cluster"].selena_source == "build"
    assert profiles["fallback-cluster"].required_signals == ("ClusterSig",)
    assert profiles["fallback-cluster"].timeout_minutes == 30


def test_catalog_and_user_bindings_keep_path_and_secret_boundaries():
    bundle = adapt_legacy_config(_legacy_config(), profile="existing-selena")

    catalog_json = json.dumps(bundle.project_catalog.to_dict(), sort_keys=True)
    for forbidden in [
        "C:/workspace",
        "C:/shared/selena.exe",
        "build_selena.bat",
        "build_env.bat",
        "do-not-export",
        "cluster-manager",
        "internal-queue",
        "\\\\cluster",
    ]:
        assert forbidden not in catalog_json

    bindings_json = json.dumps(bundle.user_bindings.to_dict(), sort_keys=True)
    assert "C:/workspace/demo/inner" in bindings_json
    assert "C:/workspace/demo/build_selena.bat" in bindings_json
    assert "C:/workspace/demo/build_env.bat" in bindings_json
    assert "C:/shared/selena.exe" in bindings_json
    for forbidden in ["do-not-export", "cluster-manager", "internal-queue", "\\\\cluster"]:
        assert forbidden not in bindings_json


def test_data_path_override_and_missing_error():
    overridden = adapt_legacy_config(_legacy_config(), data_path=r"E:\override\run")
    assert overridden.spec.data.path == "E:/override/run"

    cfg = _legacy_config()
    cfg["simulation"] = {"datasets": []}
    with pytest.raises(LegacyConfigAdapterError, match="pass data_path"):
        adapt_legacy_config(cfg)


def test_unknown_profile_is_clear_adapter_error():
    with pytest.raises(LegacyConfigAdapterError, match="Unknown legacy profile 'missing'"):
        adapt_legacy_config(_legacy_config(), profile="missing")


def test_adapter_does_not_mutate_input_dict():
    cfg = _legacy_config()
    original = copy.deepcopy(cfg)

    adapt_legacy_config(cfg, profile="branch-build")

    assert cfg == original


def test_build_mode_priority_build_then_selena_then_release():
    cfg = _legacy_config()
    assert adapt_legacy_config(cfg).spec.selena.build_mode == "Debug"

    del cfg["build"]["build_mode"]
    assert adapt_legacy_config(cfg).spec.selena.build_mode == "RelWithDebInfo"

    del cfg["selena"]["build_mode"]
    assert adapt_legacy_config(cfg).spec.selena.build_mode == "Release"


def test_core_config_facade_matches_pure_adapter(monkeypatch):
    cfg = _legacy_config()

    import core.config as legacy_config

    monkeypatch.setattr(legacy_config, "load_config", lambda project=None: copy.deepcopy(cfg))

    pure = adapt_legacy_config(cfg, project="facade-project", profile="branch-build", data_path="D:/facade")
    facade = legacy_config.load_simulation_spec_bundle(
        "facade-project",
        profile="branch-build",
        data_path="D:/facade",
    )

    assert facade.spec.fingerprint() == pure.spec.fingerprint()
    assert facade.project_catalog.project == pure.project_catalog.project
    assert facade.user_bindings.project == pure.user_bindings.project


def test_round_trip_hash_and_separator_normalization():
    backslash = adapt_legacy_config(_legacy_config(), data_path=r"D:\data\case")
    forward = adapt_legacy_config(_legacy_config(), data_path="D:/data/case")

    restored = SimulationSpec.from_yaml(backslash.spec.to_yaml())

    assert restored == backslash.spec
    assert restored.canonical_json() == backslash.spec.canonical_json()
    assert restored.fingerprint() == backslash.spec.fingerprint()
    assert backslash.spec.fingerprint() == forward.spec.fingerprint()
