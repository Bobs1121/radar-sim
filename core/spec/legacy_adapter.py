"""Read-only adapter from legacy effective config to v5 spec layers.

The adapter intentionally keeps the three v5 configuration layers separate:
``SimulationSpec`` contains exportable business intent, ``ProjectCatalog`` is
read-only project metadata, and ``UserBindings`` keeps local machine paths.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from core.profiles import DEFAULT_PROFILE_NAME, get_profile, list_profiles
from core.spec.model import SimulationSpec


class LegacyConfigAdapterError(ValueError):
    """Raised when an effective legacy config cannot be mapped safely."""


@dataclass(frozen=True)
class ProjectProfile:
    name: str
    description: str
    target: str
    selena_source: str
    required_signals: tuple[str, ...]
    timeout_minutes: int


@dataclass(frozen=True)
class ProjectCatalog:
    project: str
    display_name: str
    platform: str
    default_profile: str
    selected_profile: str
    default_build_mode: str
    profiles: tuple[ProjectProfile, ...]
    adapter: str = "gen5_selena"
    revision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExistingSelenaBinding:
    profile: str
    artifact_id: str
    executable_path: str


@dataclass(frozen=True)
class UserBindings:
    project: str
    workspace_path: str
    selena_build_script: str
    environment_build_script: str
    existing_selena: tuple[ExistingSelenaBinding, ...]
    # Central control planes may authorize a logical Agent binding without
    # ever receiving its Windows path. ``None`` preserves legacy path-based
    # authorization; a tuple is an explicit path-free allowlist.
    authorized_workspace_binding_ids: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyConfigBundle:
    spec: SimulationSpec
    project_catalog: ProjectCatalog
    user_bindings: UserBindings


def adapt_legacy_config(
    config: Mapping[str, Any],
    *,
    project: str | None = None,
    profile: str | None = None,
    data_path: str | None = None,
) -> LegacyConfigBundle:
    """Map an effective legacy config dict to immutable v5 configuration layers.

    ``config`` is treated as read-only. Profile selection is delegated to
    ``core.profiles`` so legacy overlay and normalization behavior stays in one
    place.
    """

    effective = copy.deepcopy(dict(config))
    project_id = _project_id(effective, explicit=project)
    selected_profile_name = _selected_profile_name(effective, explicit=profile)

    try:
        profiles = list_profiles(effective)
        selected = get_profile(effective, selected_profile_name)
    except ValueError as exc:
        raise LegacyConfigAdapterError(f"Unknown legacy profile '{selected_profile_name}' for project '{project_id}'") from exc

    build_mode = _default_build_mode(effective)
    spec = SimulationSpec.from_dict(
        {
            "schema_version": "1.0",
            "project": project_id,
            "selena": _selena_spec_dict(effective, selected, project_id, selected_profile_name, build_mode),
            "data": {
                "path": _data_path(effective, explicit=data_path),
                "limit": 0,
                "required_signals": _required_signals(effective, selected),
            },
            "simulation": {
                "target": _target(selected),
                "profile": selected_profile_name,
                "timeout_minutes": _timeout_minutes(effective, selected),
            },
            "result": {"name": "", "retain_days": 30},
        }
    )
    catalog = _project_catalog(effective, profiles, project_id, selected_profile_name, build_mode)
    bindings = _user_bindings(effective, profiles, project_id)
    return LegacyConfigBundle(spec=spec, project_catalog=catalog, user_bindings=bindings)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _project_id(config: Mapping[str, Any], *, explicit: str | None) -> str:
    project_section = config.get("project") if isinstance(config.get("project"), Mapping) else {}
    meta = config.get("_meta") if isinstance(config.get("_meta"), Mapping) else {}
    project_id = _clean(explicit) or _clean(meta.get("project")) or _clean(project_section.get("name"))
    if not project_id:
        raise LegacyConfigAdapterError("Legacy config adapter requires a project id")
    return project_id


def _selected_profile_name(config: Mapping[str, Any], *, explicit: str | None) -> str:
    return _clean(explicit) or _clean(config.get("active_profile")) or DEFAULT_PROFILE_NAME


def _default_build_mode(config: Mapping[str, Any]) -> str:
    build = config.get("build") if isinstance(config.get("build"), Mapping) else {}
    selena = config.get("selena") if isinstance(config.get("selena"), Mapping) else {}
    return _clean(build.get("build_mode")) or _clean(selena.get("build_mode")) or "Release"


def _selena_spec_dict(
    config: Mapping[str, Any],
    profile: Mapping[str, Any],
    project_id: str,
    profile_name: str,
    build_mode: str,
) -> dict[str, Any]:
    selena = profile.get("selena") if isinstance(profile.get("selena"), Mapping) else {}
    source = _clean(selena.get("source")).lower() or "build"
    branch = _selena_branch(config, selena)

    if source == "build":
        if branch:
            return {
                "mode": "branch",
                "branch": branch,
                "artifact": "",
                "auto_build": True,
                "build_mode": build_mode,
            }
        return {
            "mode": "current_workspace",
            "branch": "",
            "artifact": "",
            "auto_build": True,
            "build_mode": build_mode,
        }

    if source in {"path", "existing"}:
        artifact = _artifact_id(project_id, profile_name) if _existing_exe_path(profile) else ""
        return {
            "mode": "existing",
            "branch": "",
            "artifact": artifact,
            "auto_build": False,
            "build_mode": build_mode,
        }

    raise LegacyConfigAdapterError(
        f"Unsupported legacy Selena source '{source}' in profile '{profile_name}'. Expected build, path, or existing."
    )


def _selena_branch(config: Mapping[str, Any], selena: Mapping[str, Any]) -> str:
    build = config.get("build") if isinstance(config.get("build"), Mapping) else {}
    repos = config.get("repos") if isinstance(config.get("repos"), Mapping) else {}
    return _clean(selena.get("selena_branch")) or _clean(build.get("selena_branch")) or _clean(repos.get("inner_repo_branch"))


def _data_path(config: Mapping[str, Any], *, explicit: str | None) -> str:
    if _clean(explicit):
        return _clean(explicit)
    simulation = config.get("simulation") if isinstance(config.get("simulation"), Mapping) else {}
    datasets = simulation.get("datasets") if isinstance(simulation.get("datasets"), list) else []
    first = datasets[0] if datasets and isinstance(datasets[0], Mapping) else {}
    path = _clean(first.get("input_mf4")) or _clean(first.get("input_dir"))
    if not path:
        raise LegacyConfigAdapterError(
            "Legacy config adapter requires data.path; pass data_path or configure "
            "simulation.datasets[0].input_mf4/input_dir"
        )
    return path


def _required_signals(config: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[str, ...]:
    data = profile.get("data") if isinstance(profile.get("data"), Mapping) else {}
    signals = data.get("required_signals") if data.get("required_signals") is not None else None
    if signals is None:
        cluster = config.get("cluster") if isinstance(config.get("cluster"), Mapping) else {}
        signals = cluster.get("required_input_signals") or []
    return _normalize_signals(signals)


def _normalize_signals(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _target(profile: Mapping[str, Any]) -> str:
    backend = _clean(profile.get("backend")).lower() or "local"
    if backend not in {"local", "cluster"}:
        raise LegacyConfigAdapterError(f"Unsupported legacy backend '{backend}'. Expected local or cluster.")
    return backend


def _timeout_minutes(config: Mapping[str, Any], profile: Mapping[str, Any]) -> int:
    if _target(profile) != "cluster":
        return 0
    profile_cluster = profile.get("cluster") if isinstance(profile.get("cluster"), Mapping) else {}
    cluster = config.get("cluster") if isinstance(config.get("cluster"), Mapping) else {}
    return _non_negative_int(profile_cluster.get("timeout_min") or profile.get("timeout_min") or cluster.get("timeout_min") or 0)


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LegacyConfigAdapterError(f"Expected non-negative integer, got {value!r}") from exc
    if parsed < 0:
        raise LegacyConfigAdapterError(f"Expected non-negative integer, got {value!r}")
    return parsed


def _project_catalog(
    config: Mapping[str, Any],
    profiles: list[dict[str, Any]],
    project_id: str,
    selected_profile_name: str,
    build_mode: str,
) -> ProjectCatalog:
    project_section = config.get("project") if isinstance(config.get("project"), Mapping) else {}
    machine = config.get("machine") if isinstance(config.get("machine"), Mapping) else {}
    entries = tuple(_catalog_profile(config, profile) for profile in profiles)
    display_name = _clean(project_section.get("name")) or project_id
    platform = _clean(project_section.get("platform")) or _clean(machine.get("platform")) or "gen5_selena"
    adapter = _clean(project_section.get("recipe")) or platform
    revision_payload = {
        "project": project_id,
        "display_name": display_name,
        "platform": platform,
        "default_profile": DEFAULT_PROFILE_NAME,
        "selected_profile": selected_profile_name,
        "default_build_mode": build_mode,
        "profiles": [asdict(entry) for entry in entries],
        "adapter": adapter,
    }
    revision = "sha256:" + hashlib.sha256(
        json.dumps(revision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ProjectCatalog(
        project=project_id,
        display_name=display_name,
        platform=platform,
        default_profile=DEFAULT_PROFILE_NAME,
        selected_profile=selected_profile_name,
        default_build_mode=build_mode,
        profiles=entries,
        adapter=adapter,
        revision=revision,
    )


def _catalog_profile(config: Mapping[str, Any], profile: Mapping[str, Any]) -> ProjectProfile:
    selena = profile.get("selena") if isinstance(profile.get("selena"), Mapping) else {}
    return ProjectProfile(
        name=_clean(profile.get("name")) or DEFAULT_PROFILE_NAME,
        description=_clean(profile.get("description")),
        target=_target(profile),
        selena_source=_clean(selena.get("source")).lower() or "build",
        required_signals=_required_signals(config, profile),
        timeout_minutes=_timeout_minutes(config, profile),
    )


def _user_bindings(config: Mapping[str, Any], profiles: list[dict[str, Any]], project_id: str) -> UserBindings:
    build = config.get("build") if isinstance(config.get("build"), Mapping) else {}
    repos = config.get("repos") if isinstance(config.get("repos"), Mapping) else {}
    workspace_path = (
        _clean(repos.get("inner_repo_root"))
        or _clean(repos.get("outer_repo_root"))
        or _clean(config.get("project_root"))
    )
    return UserBindings(
        project=project_id,
        workspace_path=workspace_path,
        selena_build_script=_clean(build.get("selena_build_script")),
        environment_build_script=_clean(build.get("env_build_script")),
        existing_selena=tuple(_existing_bindings(profiles, project_id)),
    )


def _existing_bindings(profiles: list[dict[str, Any]], project_id: str) -> list[ExistingSelenaBinding]:
    bindings: list[ExistingSelenaBinding] = []
    for profile in profiles:
        name = _clean(profile.get("name")) or DEFAULT_PROFILE_NAME
        exe = _existing_exe_path(profile)
        if not exe:
            continue
        bindings.append(
            ExistingSelenaBinding(
                profile=name,
                artifact_id=_artifact_id(project_id, name),
                executable_path=exe,
            )
        )
    return bindings


def _existing_exe_path(profile: Mapping[str, Any]) -> str:
    selena = profile.get("selena") if isinstance(profile.get("selena"), Mapping) else {}
    source = _clean(selena.get("source")).lower()
    if source not in {"path", "existing"}:
        return ""
    return _clean(selena.get("exe"))


def _artifact_id(project_id: str, profile_name: str) -> str:
    return f"legacy:{_logical_segment(project_id)}:{_logical_segment(profile_name)}"


def _logical_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-") or "default"


__all__ = [
    "ExistingSelenaBinding",
    "LegacyConfigAdapterError",
    "LegacyConfigBundle",
    "ProjectCatalog",
    "ProjectProfile",
    "UserBindings",
    "adapt_legacy_config",
]
