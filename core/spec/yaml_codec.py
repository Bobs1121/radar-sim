"""YAML import/export helpers for SimulationSpec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml_mapping(source: str | Path) -> dict[str, Any]:
    """Load a YAML mapping from text or an existing file path."""
    text_or_path = str(source)
    path = Path(text_or_path)
    try:
        is_file = path.exists() and path.is_file()
    except OSError:
        is_file = False
    if is_file:
        text = path.read_text(encoding="utf-8")
    else:
        text = text_or_path
    loaded = yaml.safe_load(text)
    if loaded is None:
        raise ValueError("SimulationSpec YAML must be a mapping, got empty document")
    if not isinstance(loaded, dict):
        raise ValueError(f"SimulationSpec YAML must be a mapping, got {type(loaded).__name__}")
    return loaded


def dump_yaml(data: dict[str, Any]) -> str:
    """Dump YAML with stable field order and Unicode preserved."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
