"""Simulation config helpers and lightweight radar orientation detection."""

from __future__ import annotations

import copy
import json
import mmap
import os
import re
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional


RADAR_POSITION_MAP: dict[str, dict[str, str]] = {
    "FC": {"source": "RadarFC", "mounting_position": "front"},
    "FL": {"source": "RadarFL", "mounting_position": "CFL"},
    "FR": {"source": "RadarFR", "mounting_position": "CFR"},
    "RL": {"source": "RadarRL", "mounting_position": "CRL"},
    "RR": {"source": "RadarRR", "mounting_position": "CRR"},
}

OUTPUT_FILE_PATTERN = re.compile(r"out(?:\s*\(\d+\))?$", re.IGNORECASE)

# One data folder normally contains recordings from the same radar mounting.
# Cache a successful adapter probe so a multi-file local run does not reopen a
# multi-gigabyte MF4 for every input.  Failed probes are not cached.
_RADAR_DETECTION_CACHE: dict[str, dict[str, Any]] = {}
_RADAR_DETECTION_CACHE_LOCK = threading.Lock()

# These are the fixed-size MDF 4 blocks needed to read acquisition-source
# metadata.  The light Windows Agent deliberately does not install asammdf;
# keeping this small reader here lets it preserve the same group ordering as
# the full parser without decoding samples or copying the MF4 payload.
_MF4_COMMON = struct.Struct("<4sI2Q")
_MF4_HEADER = struct.Struct("<4sI9Q2h4B2Q")
_MF4_DATA_GROUP = struct.Struct("<4sI6QB7s")
_MF4_CHANNEL_GROUP = struct.Struct("<4sI10Q2H3I")
_MF4_SOURCE_INFORMATION = struct.Struct("<4sI5Q3B5s")
_MF4_METADATA_OFFSET = 0x40


def _read_mf4_text(mapped: mmap.mmap, address: int) -> str:
    """Read one MDF4 TX/MD block without materializing the recording."""

    if not address or address < 0 or address + _MF4_COMMON.size > len(mapped):
        return ""
    try:
        block_id, _reserved, block_size, _links = _MF4_COMMON.unpack_from(mapped, address)
    except (struct.error, ValueError):
        return ""
    if block_id not in (b"##TX", b"##MD"):
        return ""
    if block_size < _MF4_COMMON.size or address + block_size > len(mapped):
        return ""
    raw = bytes(mapped[address + _MF4_COMMON.size : address + block_size]).split(b"\0", 1)[0]
    raw = raw.strip(b" \r\t\n")
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", "ignore").strip()
    except (AttributeError, UnicodeError):
        return ""


def _discover_mf4_acquisition_sources_stdlib(mf4_path: str) -> list[str]:
    """Read MDF4 acquisition sources using only the standard library.

    MDF4 stores data groups as a linked list from the header block.  Each
    channel group points at a source-information block whose path is the
    acquisition name (for example ``RadarRL``).  Walking those links is
    deterministic and avoids the incorrect result produced by scanning raw
    bytes: the first textual occurrence in a file is not necessarily the
    first acquisition group.

    This is intentionally metadata-only.  Unsupported/corrupt MDF versions
    return an empty list and the normal best-effort orientation fallback can
    continue.
    """

    path = str(mf4_path or "").strip()
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as stream:
            if os.fstat(stream.fileno()).st_size < _MF4_METADATA_OFFSET + _MF4_HEADER.size:
                return []
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                try:
                    header = _MF4_HEADER.unpack_from(mapped, _MF4_METADATA_OFFSET)
                except (struct.error, ValueError):
                    return []
                if header[0] != b"##HD":
                    return []

                found: list[str] = []
                data_group_address = int(header[4])
                visited_data_groups: set[int] = set()
                while data_group_address and data_group_address not in visited_data_groups:
                    if len(visited_data_groups) >= 10000:
                        break
                    visited_data_groups.add(data_group_address)
                    try:
                        data_group = _MF4_DATA_GROUP.unpack_from(mapped, data_group_address)
                    except (struct.error, ValueError):
                        break
                    if data_group[0] != b"##DG":
                        break

                    channel_group_address = int(data_group[5])
                    visited_channel_groups: set[int] = set()
                    while channel_group_address and channel_group_address not in visited_channel_groups:
                        if len(visited_channel_groups) >= 10000:
                            break
                        visited_channel_groups.add(channel_group_address)
                        try:
                            channel_group = _MF4_CHANNEL_GROUP.unpack_from(
                                mapped, channel_group_address
                            )
                        except (struct.error, ValueError):
                            break
                        if channel_group[0] != b"##CG":
                            break

                        source_address = int(channel_group[7])
                        if source_address:
                            try:
                                source_info = _MF4_SOURCE_INFORMATION.unpack_from(
                                    mapped, source_address
                                )
                            except (struct.error, ValueError):
                                source_info = None
                            if source_info and source_info[0] == b"##SI":
                                # MDF4's path is the acquisition source.  A
                                # few recorders leave path empty and put the
                                # same value in name, hence the fallback.
                                raw_source = _read_mf4_text(mapped, int(source_info[5]))
                                raw_source = raw_source or _read_mf4_text(
                                    mapped, int(source_info[4])
                                )
                                source = canonical_radar_source(raw_source)
                                if source and source not in found:
                                    found.append(source)

                        channel_group_address = int(channel_group[4])
                    data_group_address = int(data_group[4])
                return found
    except (OSError, ValueError, mmap.error):
        return []


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
    # Task-safe subdir: use _meta._run_id (unique per load_config() call) so
    # concurrent requests in the same process (ThreadingHTTPServer threads share
    # os.getpid()) get isolated runtime dirs. Fallback to pid for configs built
    # without going through _finalize_layered_config (backward compat).
    import os
    run_id = str(config.get("_meta", {}).get("_run_id") or os.getpid())
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


def canonical_radar_source(value: Any) -> str:
    """Return one of the four supported corner acquisition source names."""

    folded = str(value or "").strip().casefold()
    for position, mapping in RADAR_POSITION_MAP.items():
        source = mapping["source"]
        if source.casefold() == folded:
            return source
    return ""


def discover_radar_acquisition_sources(mf4_path: str) -> list[str]:
    """Read valid MF4 acquisition sources in deterministic group order.

    This helper is intentionally small and local to the data-owning process.
    The Linux direct-transfer executor must consume its result as metadata and
    must not call it for a worker-visible path.
    """

    path = str(mf4_path or "").strip()
    if not path or not os.path.exists(path):
        return []

    # The acquisition source lives in the MDF4 metadata linked list.  Reading
    # those few blocks is sufficient and normally completes in milliseconds,
    # even for multi-gigabyte recordings.  Do this before importing asammdf:
    # constructing ``MDF`` may scan the complete channel database and added
    # minutes of avoidable setup time to every first local simulation.
    metadata_sources = _discover_mf4_acquisition_sources_stdlib(path)
    if metadata_sources:
        return metadata_sources
    try:
        from asammdf import MDF
    except (ImportError, OSError):
        return _discover_mf4_acquisition_sources_stdlib(path)
    try:
        mdf = MDF(path, memory="minimum")
    except Exception:
        return _discover_mf4_acquisition_sources_stdlib(path)
    found: list[str] = []
    try:
        for group in getattr(mdf, "groups", ()) or ():
            channel_group = getattr(group, "channel_group", None)
            if channel_group is None and isinstance(group, dict):
                channel_group = group.get("channel_group")
            acquisition = getattr(channel_group, "acq_source", None)
            raw_source = getattr(acquisition, "path", "")
            source = canonical_radar_source(raw_source)
            if source and source not in found:
                found.append(source)
    except Exception:
        return []
    finally:
        try:
            mdf.close()
        except Exception:
            pass
    return found or _discover_mf4_acquisition_sources_stdlib(path)


def normalize_radar_metadata(value: Any) -> dict[str, str]:
    """Whitelist transfer radar metadata and derive a consistent mapping.

    The wire format uses flat source-fingerprint keys, while the projected
    transfer resource uses ``source``/``mounting_position``.  Unknown or
    inconsistent values are ignored rather than turning orientation into a
    new transfer barrier.
    """

    raw = dict(value or {}) if isinstance(value, dict) else {}
    source = canonical_radar_source(
        raw.get("radar_source") or raw.get("source") or raw.get("radar")
    )
    mounting = str(
        raw.get("radar_mounting_position")
        or raw.get("mounting_position")
        or ""
    ).strip().upper()
    by_mounting = {
        mapping["mounting_position"].upper(): mapping["source"]
        for mapping in RADAR_POSITION_MAP.values()
    }
    if not source and mounting in by_mounting:
        source = by_mounting[mounting]
    if not source:
        return {}
    return {
        "source": source,
        "mounting_position": next(
            mapping["mounting_position"]
            for mapping in RADAR_POSITION_MAP.values()
            if mapping["source"] == source
        ),
    }


def _first_dataset_mf4(path: str) -> str:
    candidate = Path(str(path or "").strip())
    try:
        if candidate.is_file() and candidate.suffix.casefold() == ".mf4":
            return str(candidate)
        if candidate.is_dir():
            files = sorted(
                (item for item in candidate.rglob("*") if item.is_file() and item.suffix.casefold() == ".mf4"),
                key=lambda item: item.as_posix().casefold(),
            )
            return str(files[0]) if files else ""
    except (OSError, RuntimeError):
        return ""
    return ""


def detect_radar_transfer_metadata(path: str) -> dict[str, str]:
    """Infer flat radar metadata for a local direct-transfer dataset."""

    mf4_path = _first_dataset_mf4(path)
    if not mf4_path:
        return {}
    sources = discover_radar_acquisition_sources(mf4_path)
    if sources:
        source = sources[0]
    else:
        try:
            detection = detect_radar_orientation(mf4_path)
        except Exception:
            detection = None
        source = canonical_radar_source((detection or {}).get("source"))
    metadata = normalize_radar_metadata({"radar_source": source})
    if not metadata:
        return {}
    return {
        "radar_source": metadata["source"],
        "radar_mounting_position": metadata["mounting_position"],
    }


def detect_radar_transfer_metadata_safe(path: str, *, timeout_seconds: float = 30.0) -> dict[str, str]:
    """Infer transfer metadata without letting an optional MDF parser kill its caller.

    Direct transfer runs inside the long-lived Connector.  Some third-party
    MDF parser/native-library failures terminate the Python process instead of
    raising a catchable exception.  Prefer the stdlib MDF4 metadata reader and
    isolate the heavier compatibility fallback in a short-lived subprocess.
    Missing metadata is optional and must never block or crash file transfer.
    """

    mf4_path = _first_dataset_mf4(path)
    if not mf4_path:
        return {}
    sources = _discover_mf4_acquisition_sources_stdlib(mf4_path)
    if sources:
        metadata = normalize_radar_metadata({"radar_source": sources[0]})
        return {
            "radar_source": metadata["source"],
            "radar_mounting_position": metadata["mounting_position"],
        } if metadata else {}

    script = (
        "import json,sys; "
        "from core.simulation import detect_radar_transfer_metadata; "
        "print(json.dumps(detect_radar_transfer_metadata(sys.argv[1])))"
    )
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, mf4_path],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(float(timeout_seconds), 0.1),
            creationflags=creationflags,
        )
        if completed.returncode != 0:
            return {}
        payload = json.loads((completed.stdout or "").strip())
        normalized = normalize_radar_metadata(payload)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not normalized:
        return {}
    return {
        "radar_source": normalized["source"],
        "radar_mounting_position": normalized["mounting_position"],
    }


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


def _read_first_channel_sample(mdf: Any, name: str) -> Any:
    """Read one sample, selecting a concrete group/index for duplicates.

    MF4 files commonly contain the same generated channel name in more than
    one runnable/group (for example left and right radar instances).  Calling
    ``MDF.get(name)`` is ambiguous and, without a record limit, can materialize
    gigabytes.  The channel database gives us the concrete occurrences; use
    the first one as the deterministic adapter probe and read one record only.
    """
    occurrences = list((getattr(mdf, "channels_db", {}) or {}).get(name) or ())
    if not occurrences:
        return mdf.get(name, record_offset=0, record_count=1)
    last_error: Exception | None = None
    for occurrence in occurrences:
        try:
            group, index = occurrence
            return mdf.get(
                name,
                group=int(group),
                index=int(index),
                record_offset=0,
                record_count=1,
            )
        except Exception as exc:  # one duplicate may be an unavailable group
            last_error = exc
    if last_error is not None:
        raise last_error
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
            x_sig = _read_first_channel_sample(mdf, x_name)
            y_sig = _read_first_channel_sample(mdf, y_name)
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
                    signal = _read_first_channel_sample(mdf, matched)
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
    except Exception:
        # Orientation is an adapter hint, not an authorization gate.  A
        # malformed/ambiguous MF4 header must fall through to the normal
        # Selena invocation instead of failing the user's task.
        pass
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
        cache_key = os.path.normcase(os.path.normpath(str(Path(input_mf4).parent)))
        with _RADAR_DETECTION_CACHE_LOCK:
            detection = copy.deepcopy(_RADAR_DETECTION_CACHE.get(cache_key))
        if detection is None:
            # Keep local execution consistent with the Cluster direct-transfer
            # path.  Acquisition-source metadata is the authoritative source
            # selection (a recording may contain more than one radar group),
            # while the sample-based mounting probe is only a fallback.  The
            # helper uses the stdlib MDF4 reader when asammdf is unavailable,
            # so a newly installed unified Agent does not need an extra
            # heavyweight dependency just to prepare a paramconfig.
            try:
                available_sources = discover_radar_acquisition_sources(input_mf4)
            except Exception:
                available_sources = []
            if available_sources:
                selected_source = available_sources[0]
                mapping = next(
                    (
                        item
                        for item in RADAR_POSITION_MAP.values()
                        if item["source"] == selected_source
                    ),
                    None,
                )
                if mapping:
                    detection = {
                        "position": next(
                            position
                            for position, item in RADAR_POSITION_MAP.items()
                            if item["source"] == selected_source
                        ),
                        "source": mapping["source"],
                        "mounting_position": mapping["mounting_position"],
                        "method": "acquisition_source",
                        "confidence": 0.9 if len(available_sources) == 1 else 0.85,
                        "evidence": {
                            "available_sources": list(available_sources),
                            "selected_source": selected_source,
                            "selection_method": (
                                "single_acq_source"
                                if len(available_sources) == 1
                                else "first_acq_source"
                            ),
                        },
                    }
            if detection is None:
                detection = detect_radar_orientation(input_mf4)
            if detection:
                with _RADAR_DETECTION_CACHE_LOCK:
                    if len(_RADAR_DETECTION_CACHE) >= 128:
                        _RADAR_DETECTION_CACHE.pop(next(iter(_RADAR_DETECTION_CACHE)))
                    _RADAR_DETECTION_CACHE[cache_key] = copy.deepcopy(detection)
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
