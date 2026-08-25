"""Private, local active-profile state for Skill-driven simulation repeats.

The state store is deliberately outside the repository and outside the Linux
control plane.  It remembers only the canonical UserRunConfig paths and Job
references needed by a local Agent to interpret phrases such as "再仿刚刚的
数据".  It never stores file bodies, credentials, MF4 content, or runtime
binary bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Mapping

from core.user_config import UserRunConfig


class AgentSimulationStateError(ValueError):
    """The local active-profile state is invalid or unavailable."""

    code = "simulation_state_unavailable"


def default_simulation_state_path() -> Path:
    override = os.environ.get("RADAR_SIM_STATE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root_override = os.environ.get("RADAR_SIM_MCP_ROOT", "").strip()
    if root_override:
        return Path(root_override).expanduser().resolve() / "simulation-state.json"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip() or str(
            Path.home() / "AppData" / "Local"
        )
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or str(
            Path.home() / ".local" / "share"
        )
    return (Path(base) / "radar-sim-mcp" / "simulation-state.json").resolve()


def _normalize_context(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    # Do not resolve or stat user paths.  A disconnected UNC/share path is
    # still valid simulation state and should remain restorable offline.
    while "//" in text[1:]:
        text = text.replace("//", "/")
    return text.rstrip("/").casefold()


def _context_paths(config: UserRunConfig) -> tuple[str, ...]:
    values = (
        config.selena.code_path,
        config.selena.existing_path,
        config.data.path,
    )
    normalized = [_normalize_context(value) for value in values if str(value or "").strip()]
    return tuple(dict.fromkeys(normalized))


def _profile_key(config: UserRunConfig) -> str:
    # A profile represents a configured workspace/artifact context.  The
    # dataset is intentionally not part of the key: changing data for the
    # same configured Selena run should replace the active profile rather than
    # create a second profile that makes "刚刚的数据" ambiguous.
    material = _normalize_context(
        config.selena.code_path
        or config.selena.existing_path
        or config.data.path
    ) or config.fingerprint()
    return "profile:sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _is_context_match(profile: Mapping[str, Any], context_path: str) -> bool:
    needle = _normalize_context(context_path)
    if not needle:
        return False
    for raw in profile.get("context_paths") or ():
        value = _normalize_context(raw)
        if not value:
            continue
        if needle == value or needle.startswith(value + "/") or value.startswith(needle + "/"):
            return True
    return False


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part." + uuid.uuid4().hex)
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class AgentSimulationStateStore:
    """Small bounded local profile store, safe to use from MCP tool calls."""

    _lock = threading.RLock()
    _max_profiles = 32

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_simulation_state_path()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": "radar-sim.agent-state/1.0", "profiles": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentSimulationStateError("本机仿真 active profile 不可读") from exc
        if not isinstance(value, dict):
            raise AgentSimulationStateError("本机仿真 active profile 格式无效")
        profiles = value.get("profiles")
        if not isinstance(profiles, list):
            profiles = []
        return {
            "schema_version": "radar-sim.agent-state/1.0",
            "profiles": [dict(item) for item in profiles if isinstance(item, dict)],
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_write(self.path, value)

    def save(
        self,
        config: UserRunConfig | Mapping[str, Any],
        *,
        job_id: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        parsed = config if isinstance(config, UserRunConfig) else UserRunConfig.from_dict(dict(config))
        now = time.time()
        key = _profile_key(parsed)
        record = {
            "key": key,
            "context_paths": list(_context_paths(parsed)),
            "config": parsed.to_dict(),
            "config_fingerprint": parsed.fingerprint(),
            "last_job_id": str(job_id or "").strip(),
            "last_status": str(status or "").strip(),
            "updated_at": now,
        }
        with self._lock:
            state = self._read()
            profiles = [item for item in state["profiles"] if str(item.get("key") or "") != key]
            profiles.insert(0, record)
            state["profiles"] = profiles[: self._max_profiles]
            self._write(state)
        return self.public_record(record)

    def update_job(self, job_id: str, *, status: str = "") -> None:
        job = str(job_id or "").strip()
        if not job:
            return
        with self._lock:
            state = self._read()
            changed = False
            for profile in state["profiles"]:
                if str(profile.get("last_job_id") or "") == job:
                    profile["last_status"] = str(status or profile.get("last_status") or "").strip()
                    profile["updated_at"] = time.time()
                    changed = True
            if changed:
                state["profiles"].sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
                self._write(state)

    def get(
        self,
        *,
        context_path: str = "",
        data_path: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            profiles = self._read()["profiles"]
        selected = None
        for profile in profiles:
            if _is_context_match(profile, context_path) or _is_context_match(profile, data_path):
                selected = profile
                break
        if selected is None and profiles:
            # A repeat request with no discoverable cwd should still recover
            # the most recently confirmed profile for this local Agent.
            selected = max(profiles, key=lambda item: float(item.get("updated_at") or 0))
        if selected is None:
            return {"found": False, "profile": None}
        return {"found": True, "profile": self.public_record(selected)}

    @staticmethod
    def public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        """Return only the internal Agent-facing profile, never state paths."""

        config = record.get("config")
        return {
            "key": str(record.get("key") or ""),
            "config": dict(config) if isinstance(config, dict) else {},
            "config_fingerprint": str(record.get("config_fingerprint") or ""),
            "last_job_id": str(record.get("last_job_id") or ""),
            "last_status": str(record.get("last_status") or ""),
            "updated_at": float(record.get("updated_at") or 0),
        }


__all__ = [
    "AgentSimulationStateError",
    "AgentSimulationStateStore",
    "default_simulation_state_path",
]
