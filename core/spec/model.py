"""SimulationSpec v1 Pydantic model.

This is the user-facing business contract shared by Web and SDK. It must not
contain environment, Agent, toolchain, Cluster, repo, or scheduler fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.spec.yaml_codec import dump_yaml, load_yaml_mapping

SelenaMode = Literal["auto", "current_workspace", "branch", "existing"]
SimulationTarget = Literal["auto", "local", "cluster"]


def _normalize_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    scheme_sep = "://"
    if scheme_sep in text:
        scheme, rest = text.split(scheme_sep, 1)
        return f"{scheme}{scheme_sep}{re.sub('/+', '/', rest)}"
    is_unc = text.startswith("//")
    collapsed = re.sub("/+", "/", text)
    return f"/{collapsed}" if is_unc and not collapsed.startswith("//") else collapsed


def _non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    return text


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SelenaSpec(_FrozenModel):
    mode: SelenaMode = "auto"
    branch: str = ""
    artifact: str = ""
    publish_path: str = ""
    auto_build: bool = True
    build_mode: str = "Release"

    @field_validator("branch", "artifact", "publish_path", mode="before")
    @classmethod
    def _trim_optional_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("build_mode")
    @classmethod
    def _validate_build_mode(cls, value: str) -> str:
        return _non_empty(value, "selena.build_mode")

    @field_validator("publish_path")
    @classmethod
    def _validate_publish_path(cls, value: str) -> str:
        text = value.strip().replace("\\", "/")
        if not text:
            return ""
        if text.startswith("/") or "://" in text or (len(text) >= 2 and text[1] == ":"):
            raise ValueError("selena.publish_path must be a project-relative path")
        parts = text.split("/")
        if any(not part or part in {".", ".."} or ".." in part for part in parts):
            raise ValueError("selena.publish_path must not contain traversal or empty segments")
        return "/".join(parts)

    @model_validator(mode="before")
    @classmethod
    def _default_auto_build(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        mode = values.get("mode", "auto")
        if "auto_build" not in values:
            values["auto_build"] = False if mode == "existing" else True
        return values

    @model_validator(mode="after")
    def _validate_mode_contract(self) -> "SelenaSpec":
        if self.mode == "branch":
            if not self.branch:
                raise ValueError("selena.branch is required when selena.mode is 'branch'")
            if self.auto_build is not True:
                raise ValueError("selena.auto_build must be true when selena.mode is 'branch'")
        if self.mode == "current_workspace" and self.auto_build is not True:
            raise ValueError("selena.auto_build must be true when selena.mode is 'current_workspace'")
        if self.mode == "existing" and self.auto_build is not False:
            raise ValueError("selena.auto_build must be false when selena.mode is 'existing'")
        return self


class DataSpec(_FrozenModel):
    path: str
    limit: int = Field(default=0, ge=0)
    required_signals: tuple[str, ...] = ()

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_data_path(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _normalize_path(value)
        return value

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _non_empty(value, "data.path")

    @field_validator("required_signals", mode="before")
    @classmethod
    def _normalize_required_signals(cls, value: Any) -> Any:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            return value
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                result.append(item)
                continue
            text = item.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return tuple(result)


class SimulationRunSpec(_FrozenModel):
    target: SimulationTarget = "auto"
    profile: str = "default"
    timeout_minutes: int = Field(default=0, ge=0)

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, value: str) -> str:
        return _non_empty(value, "simulation.profile")


class ResultSpec(_FrozenModel):
    name: str = ""
    retain_days: int = Field(default=30, ge=1)

    @field_validator("name", mode="before")
    @classmethod
    def _trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SimulationSpec(_FrozenModel):
    schema_version: Literal["1.0"] = "1.0"
    project: str
    data: DataSpec
    selena: SelenaSpec = Field(default_factory=SelenaSpec)
    simulation: SimulationRunSpec = Field(default_factory=SimulationRunSpec)
    result: ResultSpec = Field(default_factory=ResultSpec)

    @field_validator("project")
    @classmethod
    def _validate_project(cls, value: str) -> str:
        return _non_empty(value, "project")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SimulationSpec":
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_yaml(cls, source: str | Path) -> "SimulationSpec":
        return cls.from_dict(load_yaml_mapping(source))

    def to_yaml(self) -> str:
        return dump_yaml(self.to_dict())

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()


__all__ = [
    "DataSpec",
    "ResultSpec",
    "SelenaMode",
    "SelenaSpec",
    "SimulationSpec",
    "SimulationTarget",
    "SimulationRunSpec",
    "ValidationError",
]
