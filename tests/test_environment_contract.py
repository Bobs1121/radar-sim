from pathlib import Path

import yaml

from core.environment_contract import plan_environment_requirements
from core.spec import SimulationSpec, adapt_legacy_config
from tests.test_spec_legacy_adapter import _legacy_config


def _spec(*, mode: str = "auto", target: str = "auto") -> SimulationSpec:
    selena = {"mode": mode, "build_mode": "Release"}
    if mode == "existing":
        selena["artifact"] = "selena-1"
    return SimulationSpec.from_dict(
        {
            "project": "demo",
            "data": {"path": "D:/data/run"},
            "selena": selena,
            "simulation": {"target": target},
        }
    )


def test_build_to_cluster_plan_keeps_windows_build_and_cluster_runtime_separate():
    plan = plan_environment_requirements(_spec(target="cluster"), project_adapter="g3n_fvg3_od25")
    by_id = {item["id"]: item for item in plan["requirements"]}

    assert plan["project_adapter"] == "g3n_fvg3_od25"
    assert by_id["selena_build_toolchain"]["node_kinds"] == ["windows_agent", "windows_full"]
    assert by_id["cluster_runtime"]["node_kinds"] == ["linux_executor", "platform_gateway"]
    assert "windows_agent" not in by_id["cluster_runtime"]["node_kinds"]


def test_existing_local_requires_full_windows_but_no_build_toolchain():
    plan = plan_environment_requirements(_spec(mode="existing", target="local"))
    by_id = {item["id"]: item for item in plan["requirements"]}

    assert "selena_build_toolchain" not in by_id
    assert by_id["local_simulation_runtime"]["node_kinds"] == ["windows_full"]


def test_environment_plan_contains_no_machine_or_cluster_configuration():
    plan = plan_environment_requirements(_spec(target="cluster"))
    raw = str(plan).lower()
    for forbidden in ["c:/", "d:/", "\\\\", "password", "group", "matlab_root", "vs_version"]:
        assert forbidden not in raw


def test_project_adapter_is_project_owned_not_user_yaml():
    cfg = _legacy_config()
    cfg["project"]["recipe"] = "demo_recipe"
    bundle = adapt_legacy_config(cfg)

    assert bundle.project_catalog.adapter == "demo_recipe"
    assert "demo_recipe" not in bundle.spec.to_yaml()


def test_versioned_project_configs_do_not_contain_secret_fields():
    root = Path(__file__).resolve().parents[1] / "config" / "projects"
    for path in root.glob("*/config.yaml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cluster = payload.get("cluster") or {}
        assert "kill_password" not in cluster, path
        assert "password" not in cluster, path
        assert "token" not in cluster, path
