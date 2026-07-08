"""Simulation config helpers and lightweight radar orientation detection."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Optional


RADAR_POSITION_MAP: dict[str, dict[str, str]] = {
    "FL": {"source": "RadarFL", "mounting_position": "CFL"},
    "FR": {"source": "RadarFR", "mounting_position": "CFR"},
    "RL": {"source": "RadarRL", "mounting_position": "CRL"},
    "RR": {"source": "RadarRR", "mounting_position": "CRR"},
}

OUTPUT_FILE_PATTERN = re.compile(r"out(?:\s*\(\d+\))?$", re.IGNORECASE)


def _data_root() -> Path:
    """Data root (follows RSIM_HOME; stdlib-only)."""
    import os
    home = os.environ.get("RSIM_HOME", "").strip()
    return Path(home).expanduser() if home else Path(__file__).resolve().parent.parent


def _results_runtime_dir(config: dict[str, Any]) -> Path:
    project = (
        config.get("_meta", {}).get("project")
        or config.get("project", {}).get("name")
        or "default"
    )
    # Per-process subdir so concurrent runs of the same project don't overwrite
    # each other's CRlog.log / paramconfig files.
    import os
    run_id = str(os.getpid())
    return _data_root() / "results" / project / "_runtime" / run_id


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _bool_text(value: Any, default: bool = False) -> str:
    if value is None:
        value = default
    return "true" if bool(value) else "false"


def get_simulation_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the effective simulation config from layered and legacy shapes."""
    legacy_sim = config.get("paths", {}).get("simulation", {}) or {}
    explicit_sim = config.get("simulation", {}) or {}
    sim = _deep_merge(legacy_sim, explicit_sim)

    assets = config.get("assets", {})
    runtime_dir = _results_runtime_dir(config)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    sim.setdefault("runtime_xml", assets.get("runtime_xml", ""))
    sim.setdefault("matfilefilter", assets.get("matfilefilter", ""))
    sim.setdefault("adapter_file", assets.get("adapter_file", ""))
    sim.setdefault("log_file", str(runtime_dir / "CRlog.log"))
    sim.setdefault("nogui", True)
    sim.setdefault("write_mat", True)
    sim.setdefault("tolerant", False)
    sim.setdefault("disable_sequence_check", False)
    sim.setdefault("enable_multibuffer_border", True)
    sim.setdefault("enable_doorkeeper", True)
    sim.setdefault("source", "")
    sim.setdefault("mounting_position", "")
    sim.setdefault("extra_args", [])
    sim.setdefault("datasets", [])
    sim.setdefault("paramconfig_dir", str(runtime_dir / "paramconfig"))
    sim.setdefault("auto_detect_radar", True)
    sim.setdefault("paramconfig_options", {})
    sim.setdefault("continue_on_failure", True)
    sim.setdefault("retry_failed_at_end", True)
    sim.setdefault("max_retries_per_file", 1)
    sim.setdefault("stall_timeout_sec", 180)
    sim.setdefault("max_duration_per_file_sec", 900)
    sim.setdefault("poll_interval_sec", 1)
    sim.setdefault("heartbeat_interval_sec", 15)

    normalized_datasets = []
    for item in sim.get("datasets", []) or []:
        if not isinstance(item, dict):
            continue
        ds = dict(item)
        if ds.get("input_mf4"):
            ds["input_mf4"] = os.path.normpath(str(ds["input_mf4"]))
        if ds.get("input_dir"):
            ds["input_dir"] = os.path.normpath(str(ds["input_dir"]))
        if ds.get("output_dir"):
            ds["output_dir"] = os.path.normpath(str(ds["output_dir"]))
        normalized_datasets.append(ds)
    sim["datasets"] = normalized_datasets

    return sim


def gen_output_path(input_mf4: str, output_dir: Optional[str] = None) -> str:
    """Generate `<stem>out.MF4` beside the input or under output_dir."""
    input_path = Path(input_mf4)
    target_dir = Path(output_dir) if output_dir else input_path.parent
    return str(target_dir / f"{input_path.stem}out.MF4")


def resolve_dataset_files(sim: dict[str, Any], dataset_name: str) -> tuple[dict[str, Any], list[str]]:
    """Resolve dataset config and input MF4 list for a named dataset."""
    for dataset in sim.get("datasets", []) or []:
        if dataset.get("name") != dataset_name:
            continue

        if dataset.get("input_mf4"):
            path = dataset["input_mf4"]
            return dataset, [path] if os.path.exists(path) else []

        input_dir = dataset.get("input_dir", "")
        if not input_dir or not os.path.isdir(input_dir):
            return dataset, []

        mf4_files = sorted(
            os.path.join(input_dir, name)
            for name in os.listdir(input_dir)
            if name.upper().endswith(".MF4") and not OUTPUT_FILE_PATTERN.search(Path(name).stem)
        )
        return dataset, mf4_files

    return {}, []


def classify_radar_position(x_pos: float, y_pos: float, threshold: float = 0.05) -> Optional[str]:
    """Classify radar corner from a mounting position vector."""
    if abs(x_pos) < threshold or abs(y_pos) < threshold:
        return None
    if x_pos > 0 and y_pos > 0:
        return "FL"
    if x_pos > 0 and y_pos < 0:
        return "FR"
    if x_pos < 0 and y_pos > 0:
        return "RL"
    if x_pos < 0 and y_pos < 0:
        return "RR"
    return None


def _extract_first_scalar(signal: Any) -> Optional[float]:
    values = getattr(signal, "samples", None)
    if values is None:
        values = getattr(signal, "values", None)
    if values is None or len(values) == 0:
        return None
    raw = values[0]
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _find_channel_name(available: list[str], suffixes: list[str], preferred_tokens: list[str]) -> Optional[str]:
    for token in preferred_tokens:
        for name in available:
            if token in name and any(name.endswith(suffix) for suffix in suffixes):
                return name
    for name in available:
        if any(name.endswith(suffix) for suffix in suffixes):
            return name
    return None


def detect_radar_orientation(mf4_path: str) -> Optional[dict[str, Any]]:
    """Infer FL/FR/RL/RR from MF4 metadata with minimal reads."""
    try:
        from asammdf import MDF
    except ImportError:
        return None

    if not os.path.exists(mf4_path):
        return None

    mdf = MDF(mf4_path, memory="minimum")
    try:
        available = list(mdf.channels_db.keys())

        x_name = _find_channel_name(
            available,
            [
                "_m_currentMounting._m_vectorCovariancePair.VectorCovariancePairBase._._m_muVector._m_data._m_data._m_value._0_",
                "_m_currentMounting._m_value._0_",
            ],
            ["PerSppRLocRunnable", "radarSensorPropertiesPort"],
        )
        y_name = _find_channel_name(
            available,
            [
                "_m_currentMounting._m_vectorCovariancePair.VectorCovariancePairBase._._m_muVector._m_data._m_data._m_value._1_",
                "_m_currentMounting._m_value._1_",
            ],
            ["PerSppRLocRunnable", "radarSensorPropertiesPort"],
        )
        if x_name and y_name:
            x_sig = mdf.get(x_name)
            y_sig = mdf.get(y_name)
            x_pos = _extract_first_scalar(x_sig)
            y_pos = _extract_first_scalar(y_sig)
            if x_pos is not None and y_pos is not None:
                position = classify_radar_position(x_pos, y_pos)
                if position:
                    mapping = RADAR_POSITION_MAP[position]
                    return {
                        "position": position,
                        "source": mapping["source"],
                        "mounting_position": mapping["mounting_position"],
                        "method": "mounting_position",
                        "confidence": 0.95,
                        "evidence": {"x": x_pos, "y": y_pos, "x_channel": x_name, "y_channel": y_name},
                    }

        explicit_rules = [
            ("RL", ["LRCR_LeTarSts", "g_depObjDxv_RadarRL_d"]),
            ("RR", ["RRCR_RiTarSts", "g_depObjDxv_RadarRR_d"]),
            ("FL", ["LFCR_", "FLCR_", "RadarFL"]),
            ("FR", ["RFCR_", "FRCR_", "RadarFR"]),
        ]
        for position, patterns in explicit_rules:
            for pattern in patterns:
                matched = next((name for name in available if pattern in name), None)
                if not matched:
                    continue
                try:
                    signal = mdf.get(matched)
                except Exception:
                    continue
                if _extract_first_scalar(signal) is None:
                    continue
                mapping = RADAR_POSITION_MAP[position]
                return {
                    "position": position,
                    "source": mapping["source"],
                    "mounting_position": mapping["mounting_position"],
                    "method": "explicit_signal",
                    "confidence": 0.8,
                    "evidence": {"channel": matched},
                }
    finally:
        mdf.close()

    upper_path = mf4_path.upper()
    for position in ("FL", "FR", "RL", "RR"):
        if f"RADAR{position}" in upper_path or f"_{position}_" in upper_path:
            mapping = RADAR_POSITION_MAP[position]
            return {
                "position": position,
                "source": mapping["source"],
                "mounting_position": mapping["mounting_position"],
                "method": "path_hint",
                "confidence": 0.35,
                "evidence": {"path": mf4_path},
            }
    return None


def build_effective_simulation(
    config: dict[str, Any],
    input_mf4: str,
    *,
    output_mf4: Optional[str] = None,
    dataset: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the per-run effective simulation config."""
    sim = copy.deepcopy(get_simulation_config(config))
    dataset = dataset or {}
    sim = _deep_merge(sim, dataset)

    input_mf4 = os.path.normpath(input_mf4)
    output_dir = dataset.get("output_dir") or sim.get("output_dir")
    sim["input_mf4"] = input_mf4
    sim["output_mf4"] = os.path.normpath(output_mf4 or gen_output_path(input_mf4, output_dir))

    detect_requested = sim.get("auto_detect_radar", True)
    source = str(sim.get("source", "") or "").strip().lower()
    mounting = str(sim.get("mounting_position", "") or "").strip().lower()
    needs_detection = detect_requested and (not source or source == "auto" or not mounting or mounting == "auto")
    if needs_detection:
        detection = detect_radar_orientation(input_mf4)
        if detection:
            sim.setdefault("radar_detection", detection)
            sim["source"] = detection["source"]
            sim["mounting_position"] = detection["mounting_position"]

    explicit_paramconfig = str(
        dataset.get("paramconfig_path")
        or (config.get("simulation", {}) or {}).get("paramconfig_path")
        or ""
    ).strip()
    if explicit_paramconfig:
        paramconfig_path = Path(explicit_paramconfig)
        paramconfig_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        paramconfig_dir = Path(sim.get("paramconfig_dir") or (_results_runtime_dir(config) / "paramconfig"))
        paramconfig_dir.mkdir(parents=True, exist_ok=True)
        paramconfig_path = paramconfig_dir / f"{Path(input_mf4).stem}.txt"
    sim["paramconfig_path"] = str(paramconfig_path)
    sim["runtime_xml"] = os.path.normpath(str(sim.get("runtime_xml", ""))) if sim.get("runtime_xml") else ""
    sim["matfilefilter"] = os.path.normpath(str(sim.get("matfilefilter", ""))) if sim.get("matfilefilter") else ""
    sim["log_file"] = os.path.normpath(str(sim.get("log_file", ""))) if sim.get("log_file") else ""
    return sim


def apply_simulation_to_config(config: dict[str, Any], sim: dict[str, Any]) -> dict[str, Any]:
    """Return a config copy with per-run simulation values materialized."""
    result = copy.deepcopy(config)
    result["simulation"] = copy.deepcopy(sim)

    paths = dict(result.get("paths", {}))
    if sim.get("input_mf4"):
        paths["input_mf4"] = sim["input_mf4"]
    if sim.get("output_mf4"):
        paths["output_mf4"] = sim["output_mf4"]
    paths["simulation"] = copy.deepcopy(sim)
    result["paths"] = paths

    assets = dict(result.get("assets", {}))
    if sim.get("runtime_xml"):
        assets["runtime_xml"] = sim["runtime_xml"]
    if sim.get("matfilefilter"):
        assets["matfilefilter"] = sim["matfilefilter"]
    if sim.get("paramconfig_path"):
        assets["fixed_config_path"] = sim["paramconfig_path"]
    result["assets"] = assets
    return result


def build_paramconfig_placeholders(config: dict[str, Any], sim: dict[str, Any]) -> dict[str, str]:
    """Build placeholder substitutions for Selena paramconfig rendering."""
    assets = config.get("assets", {})
    extra_lines = []
    for key, value in (sim.get("paramconfig_options", {}) or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = _bool_text(value)
        extra_lines.append(f"{key}={value}")
    return {
        "{{ASSETS_DIR}}": str(assets.get("root", "")),
        "{{PROJECT_ROOT}}": str(config.get("project_root", "")),
        "{{TOOLS_DIR}}": str(Path(sim.get("paramconfig_path", "")).parent),
        "{{INPUT_MF4}}": str(sim.get("input_mf4", "")),
        "{{OUTPUT_MF4}}": str(sim.get("output_mf4", "")),
        "{{RUNTIME_XML}}": str(sim.get("runtime_xml") or assets.get("runtime_xml", "")),
        "{{MATFILEFILTER}}": str(sim.get("matfilefilter") or assets.get("matfilefilter", "")),
        "{{ADAPTER_FILE}}": str(sim.get("adapter_file") or assets.get("adapter_file", "")),
        "{{LOG_FILE}}": str(sim.get("log_file", "")),
        "{{SOURCE}}": str(sim.get("source", "")),
        "{{MOUNTING_POSITION}}": str(sim.get("mounting_position", "")),
        "{{NOGUI}}": _bool_text(sim.get("nogui"), True),
        "{{WRITE_MAT}}": _bool_text(sim.get("write_mat"), True),
        "{{TOLERANT}}": _bool_text(sim.get("tolerant"), False),
        "{{DISABLE_SEQUENCE_CHECK}}": _bool_text(sim.get("disable_sequence_check"), False),
        "{{ENABLE_MULTIBUFFER_BORDER}}": _bool_text(sim.get("enable_multibuffer_border"), True),
        "{{ENABLE_DOORKEEPER}}": _bool_text(sim.get("enable_doorkeeper"), True),
        "{{EXTRA_PARAMCONFIG_LINES}}": "\n".join(extra_lines),
    }
