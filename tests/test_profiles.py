"""Tests for core.profiles: unified profile model + backwards compatibility."""

import pytest

from core.profiles import (
    apply_profile,
    get_profile,
    list_profiles,
    resolve_selena_exe,
    active_backend,
    active_profile_name,
)


def _base_config(tmp_path):
    """Minimal config with a local build selena.exe and one legacy cluster profile."""
    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").write_text("", encoding="utf-8")
    return {
        "_meta": {"project": "test"},
        "project": {"name": "test", "platform": "gen5_selena"},
        "paths": {"build_output": str(tmp_path / "build")},
        "assets": {"runtime_xml": str(tmp_path / "rt.xml"), "config_template": str(tmp_path / "tmpl.txt")},
        "simulation": {"source": "RadarFL", "mounting_position": "CFL", "runtime_xml": str(tmp_path / "rt.xml")},
        "selena": {"exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe", "build_mode": "RelWithDebInfo"},
        "cluster": {
            "required_input_signals": ["g_Sig"],
            "profiles": [
                {
                    "name": "shared-cluster",
                    "description": "legacy cluster profile",
                    "selena_exe": r"\\share\selena.exe",
                    "source": "RadarFC",
                    "subgroup": "PSS1",
                    "simulation_prio": 1,
                    "required_input_signals": [],
                }
            ],
        },
    }


def test_list_profiles_includes_default(tmp_path):
    profiles = list_profiles(_base_config(tmp_path))
    assert profiles[0]["name"] == "default"
    assert profiles[0]["backend"] == "local"
    assert profiles[0]["selena"]["source"] == "build"


def test_list_profiles_converts_legacy_cluster_profiles(tmp_path):
    profiles = list_profiles(_base_config(tmp_path))
    shared = next(p for p in profiles if p["name"] == "shared-cluster")
    assert shared["backend"] == "cluster"
    assert shared["selena"]["source"] == "path"
    assert shared["selena"]["exe"] == r"\\share\selena.exe"
    assert shared.get("source") == "RadarFC"


def test_unified_top_level_profiles(tmp_path):
    config = _base_config(tmp_path)
    # Drop legacy cluster.profiles so this tests pure top-level profiles.
    config["cluster"] = {"required_input_signals": ["g_Sig"]}
    config["profiles"] = [
        {
            "name": "local-build",
            "backend": "local",
            "selena": {"source": "build", "exe": ""},
            "data": {"copy": False, "required_signals": []},
        },
        {
            "name": "cloud-path",
            "backend": "cluster",
            "selena": {"source": "path", "exe": r"\\share\selena.exe"},
            "data": {"copy": False},
            "cluster": {"subgroup": "PSS2"},
        },
    ]
    names = [p["name"] for p in list_profiles(config)]
    assert names == ["default", "local-build", "cloud-path"]


def test_top_level_and_legacy_profiles_merge(tmp_path):
    """Top-level profiles and legacy cluster.profiles are both available."""
    config = _base_config(tmp_path)  # has cluster.profiles=[shared-cluster]
    config["profiles"] = [
        {"name": "local-build", "backend": "local", "selena": {"source": "build", "exe": ""}, "data": {"copy": False}},
    ]
    names = [p["name"] for p in list_profiles(config)]
    assert "local-build" in names
    assert "shared-cluster" in names  # legacy still merged


def test_apply_profile_default_is_noop(tmp_path):
    config = _base_config(tmp_path)
    applied = apply_profile(config, "")
    assert active_profile_name(applied) == "default"
    assert active_backend(applied) == "local"


def test_apply_profile_cluster_overlays_fields(tmp_path):
    config = _base_config(tmp_path)
    applied = apply_profile(config, "shared-cluster")
    assert active_backend(applied) == "cluster"
    assert applied["cluster"]["selena_exe"] == r"\\share\selena.exe"
    assert applied["cluster"]["subgroup"] == "PSS1"
    assert applied["simulation"]["source"] == "RadarFC"


def test_apply_profile_unknown_raises(tmp_path):
    with pytest.raises(ValueError):
        apply_profile(_base_config(tmp_path), "nope")


def test_resolve_selena_exe_from_build(tmp_path):
    config = _base_config(tmp_path)
    profile = {"selena": {"source": "build", "exe": ""}}
    exe = resolve_selena_exe(config, profile)
    assert exe.endswith("selena.exe")
    assert "build" in exe


def test_resolve_selena_exe_from_path():
    profile = {"selena": {"source": "path", "exe": r"\\share\selena.exe"}}
    exe = resolve_selena_exe({}, profile)
    assert exe == r"\\share\selena.exe"


def test_get_profile_returns_named(tmp_path):
    config = _base_config(tmp_path)
    profile = get_profile(config, "shared-cluster")
    assert profile["backend"] == "cluster"


def test_apply_profile_records_active_profile_on_cluster_block(tmp_path):
    """cluster.active_profile must be set for cluster-side checks that read it."""
    config = _base_config(tmp_path)
    applied = apply_profile(config, "shared-cluster")
    assert applied["cluster"]["active_profile"] == "shared-cluster"
