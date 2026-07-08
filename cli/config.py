"""rsim config — configuration helpers.

  rsim config show   Print the effective merged config for the current project
  rsim config init   Create a local.yaml from local.example.yaml
  rsim config diff   Show which keys local.yaml overrides vs config.yaml

Configuration is layered: default.yaml → platforms → recipes →
projects/<name>/config.yaml → projects/<name>/local.yaml (gitignored, user
private). Users override their own paths/branches/datasets/profiles in
local.yaml without touching the shared config.yaml.
"""

from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from core.config import get_projects_dir, load_config


def register(subparsers):
    p = subparsers.add_parser("config", help="Configuration helpers (show/init/diff)")
    sub = p.add_subparsers(dest="config_command")
    sub.add_parser("show", help="Print the effective merged config for the current project")
    init = sub.add_parser("init", help="Create local.yaml from local.example.yaml")
    init.add_argument("--force", action="store_true", help="Overwrite an existing local.yaml")
    sub.add_parser("diff", help="Show which keys local.yaml overrides vs config.yaml")


def run(args, config):
    command = getattr(args, "config_command", "") or ""
    if command == "show":
        return _run_show(args, config)
    if command == "init":
        return _run_init(args, config)
    if command == "diff":
        return _run_diff(args, config)
    print("Missing config command. Use: rsim config show|init|diff")
    return 1


def _run_show(args, config):
    project = getattr(args, "project", None) or config.get("_meta", {}).get("project") or "default"
    print(f"# Effective config for project '{project}'")
    print(f"# config.yaml: {config.get('_meta', {}).get('config_path', '?')}")
    local_path = config.get("_meta", {}).get("local_config_path", "")
    if local_path:
        print(f"# local.yaml:  {local_path} (applied)")
    print()
    safe = _safe_config(config)
    print(yaml.safe_dump(safe, sort_keys=False, allow_unicode=True).rstrip())
    return 0


def _run_init(args, config):
    project = getattr(args, "project", None) or config.get("_meta", {}).get("project") or "default"
    project_dir = get_projects_dir() / project
    example = project_dir / "local.example.yaml"
    local = project_dir / "local.yaml"
    if not example.exists():
        print(f"[ERROR] local.example.yaml not found: {example}")
        return 1
    if local.exists() and not getattr(args, "force", False):
        print(f"[ERROR] local.yaml already exists: {local}")
        print("  Use --force to overwrite, or edit it directly.")
        return 1
    shutil.copy2(example, local)
    print(f"[OK] Created {local} from {example}")
    print("  Edit it to override your paths/branches/datasets/profiles, then 'rsim config show' to verify.")
    return 0


def _run_diff(args, config):
    project = getattr(args, "project", None) or config.get("_meta", {}).get("project") or "default"
    project_dir = get_projects_dir() / project
    config_path = project_dir / "config.yaml"
    local_path = project_dir / "local.yaml"
    if not local_path.exists():
        print(f"[INFO] No local.yaml at {local_path} — nothing to diff.")
        print("  Run 'rsim config init' to create one from local.example.yaml.")
        return 0

    base_yaml = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    local_yaml = local_path.read_text(encoding="utf-8")
    base = yaml.safe_load(base_yaml) or {}
    local = yaml.safe_load(local_yaml) or {}

    overrides = _collect_overrides(base, local)
    if not overrides:
        print(f"[INFO] local.yaml does not override any keys from config.yaml for project '{project}'.")
        return 0

    print(f"local.yaml overrides {len(overrides)} key(s) in project '{project}':")
    for path, (base_val, local_val) in overrides.items():
        print(f"  {path}:")
        print(f"    config.yaml: {base_val!r}")
        print(f"    local.yaml:  {local_val!r}")
    return 0


def _collect_overrides(base: dict, local: dict, prefix: str = "") -> dict[str, tuple[Any, Any]]:
    """Return {dotted.key.path: (base_value, local_value)} for keys local sets differently."""
    result: dict[str, tuple[Any, Any]] = {}
    for key, local_val in local.items():
        path = f"{prefix}.{key}" if prefix else key
        base_val = base.get(key) if isinstance(base, dict) else None
        if isinstance(local_val, dict) and isinstance(base_val, dict):
            result.update(_collect_overrides(base_val, local_val, path))
        else:
            base_has = isinstance(base, dict) and key in base
            if not base_has or base_val != local_val:
                result[path] = (base_val if base_has else "<missing>", local_val)
    return result


def _safe_config(config: dict) -> dict:
    """Return a serializable subset of config for display."""
    keys = ["project", "repos", "build", "paths", "assets", "simulation", "cluster", "environment", "profiles"]
    safe = {}
    for key in keys:
        value = config.get(key, {})
        if isinstance(value, dict) and value:
            safe[key] = value
    safe["active_profile"] = config.get("active_profile", "default")
    safe["active_backend"] = config.get("active_backend", "local")
    return safe
