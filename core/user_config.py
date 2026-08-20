"""Project-free user configuration contract shared by Web and SDK.

Internal project adapters, Runtime Bundle identifiers, Cluster endpoints,
credentials, mount mappings and scheduler details are intentionally absent.
The public contract only describes the user-selected workspace or existing
Selena folder; internal packaging and adapter recognition are Stage concerns.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.spec.yaml_codec import dump_yaml, load_yaml_mapping
from core.path_normalization import normalize_path_text


def _path(value: str) -> str:
    return normalize_path_text(value)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class UserSelenaConfig(_Frozen):
    source: Literal["build", "existing"]
    code_path: str = ""
    branch: str = ""
    selena_build_script: str = ""
    package_build_script: str = ""
    runtime_xml: str = ""
    existing_path: str = ""

    @field_validator("code_path", "selena_build_script", "package_build_script", "runtime_xml", "existing_path", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return _path(value) if isinstance(value, str) else value

    @field_validator("branch", mode="before")
    @classmethod
    def _trim(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _source_contract(self) -> "UserSelenaConfig":
        if self.source == "build":
            if not self.code_path:
                raise ValueError("selena.code_path is required for local build")
            if not self.selena_build_script:
                raise ValueError("selena.selena_build_script is required for local build")
            if not self.runtime_xml:
                raise ValueError("selena.runtime_xml is required and bound to the build output")
            if self.existing_path:
                raise ValueError("built Selena cannot also select an existing Selena folder")
        else:
            # existing mode: require existing_path + runtime_xml (public contract)
            if not self.existing_path:
                raise ValueError("selena.existing_path is required for existing Selena")
            if not self.runtime_xml:
                raise ValueError("selena.runtime_xml is required for existing Selena")
            if (
                self.branch
                or self.selena_build_script
                or self.package_build_script
            ) and not self.code_path:
                raise ValueError(
                    "selena.code_path is required when existing Selena includes "
                    "repository or build-script evidence"
                )
        return self


class UserDataConfig(_Frozen):
    path: str

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        if value is None:
            return ""
        return _path(value) if isinstance(value, str) else value

    @field_validator("path")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("data.path must not be empty")
        return value


class UserResultConfig(_Frozen):
    """User-visible result delivery preference.

    ``path`` is the receiver-side result root. Execution delivery places each
    Job below ``<path>/<job_id>`` alongside its directly consumable files and
    Manifest. An empty value deliberately means ``auto``; it must not be
    replaced with a Linux service path while a configuration is canonicalized
    or exported. ZIP retention is a parallel result-catalog concern.
    """

    path: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        if value is None:
            return ""
        return _path(value) if isinstance(value, str) else value


class UserSimulationConfig(_Frozen):
    target: Literal["auto", "local", "cluster"] = "auto"
    source: str = ""
    adapter_file: str = ""
    mat_filter: str = ""

    @field_validator("adapter_file", "mat_filter", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return _path(value) if isinstance(value, str) else value

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_radar_source(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text or text.casefold() == "auto":
            return ""
        aliases = {
            "fc": "RadarFC", "radarfc": "RadarFC",
            "fl": "RadarFL", "radarfl": "RadarFL",
            "fr": "RadarFR", "radarfr": "RadarFR",
            "rl": "RadarRL", "radarrl": "RadarRL",
            "rr": "RadarRR", "radarrr": "RadarRR",
        }
        normalized = aliases.get(text.casefold())
        if not normalized:
            raise ValueError("simulation.source must be empty or one of RadarFC/RadarFL/RadarFR/RadarRL/RadarRR")
        return normalized



class UserRunConfig(_Frozen):
    schema_version: Literal["2.0"] = "2.0"
    selena: UserSelenaConfig
    data: UserDataConfig
    simulation: UserSimulationConfig
    result: UserResultConfig = Field(default_factory=UserResultConfig)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UserRunConfig":
        return cls.model_validate(value)

    @classmethod
    def from_yaml(cls, source: str | Path) -> "UserRunConfig":
        return cls.from_dict(load_yaml_mapping(source))

    def to_dict(self) -> dict[str, Any]:
        selena: dict[str, Any]
        if self.selena.source == "build":
            selena = {
                "source": "build",
                "code_path": self.selena.code_path,
                "branch": self.selena.branch,
                "selena_build_script": self.selena.selena_build_script,
                "runtime_xml": self.selena.runtime_xml,
            }
            if self.selena.package_build_script:
                selena["package_build_script"] = self.selena.package_build_script
        else:
            selena = {
                "source": "existing",
                "existing_path": self.selena.existing_path,
                "runtime_xml": self.selena.runtime_xml,
            }
            optional_evidence = {
                "code_path": self.selena.code_path,
                "branch": self.selena.branch,
                "selena_build_script": self.selena.selena_build_script,
                "package_build_script": self.selena.package_build_script,
            }
            selena.update(
                {
                    key: value
                    for key, value in optional_evidence.items()
                    if value
                }
            )
        return {
            "schema_version": self.schema_version,
            "selena": selena,
            "data": {"path": self.data.path},
            "simulation": {
                "target": self.simulation.target,
                "source": self.simulation.source,
                "adapter_file": self.simulation.adapter_file,
                "mat_filter": self.simulation.mat_filter,
            },
            "result": {"path": self.result.path},
        }

    def to_yaml(self) -> str:
        return dump_yaml(self.to_dict())

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()


class PartialUserSelenaConfig(_Frozen):
    """A YAML draft section; completeness is checked only at submit/validate."""

    source: Literal["build", "existing"] | None = None
    code_path: str | None = None
    branch: str | None = None
    selena_build_script: str | None = None
    package_build_script: str | None = None
    runtime_xml: str | None = None
    existing_path: str | None = None

    @field_validator(
        "code_path",
        "selena_build_script",
        "package_build_script",
        "runtime_xml",
        "existing_path",
        mode="before",
    )
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        if value is None:
            return None
        return _path(value) if isinstance(value, str) else value

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: Any) -> Any:
        text = str(value or "").strip().lower()
        if not text:
            return None
        if text not in {"build", "existing"}:
            raise ValueError("selena.source must be build or existing")
        return text

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch(cls, value: Any) -> Any:
        return None if value is None else (value.strip() if isinstance(value, str) else value)


class PartialUserDataConfig(_Frozen):
    path: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        if value is None:
            return None
        return _path(value) if isinstance(value, str) else value


class PartialUserSimulationConfig(_Frozen):
    target: Literal["auto", "local", "cluster"] | None = None
    source: str | None = None
    adapter_file: str | None = None
    mat_filter: str | None = None

    @field_validator("target", mode="before")
    @classmethod
    def _normalize_target(cls, value: Any) -> Any:
        text = str(value or "").strip().lower()
        return None if not text else text

    @field_validator("adapter_file", "mat_filter", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        if value is None:
            return None
        return _path(value) if isinstance(value, str) else value

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_radar_source(cls, value: Any) -> Any:
        text = str(value or "").strip()
        if not text or text.casefold() == "auto":
            return None
        aliases = {
            "fc": "RadarFC", "radarfc": "RadarFC",
            "fl": "RadarFL", "radarfl": "RadarFL",
            "fr": "RadarFR", "radarfr": "RadarFR",
            "rl": "RadarRL", "radarrl": "RadarRL",
            "rr": "RadarRR", "radarrr": "RadarRR",
        }
        normalized = aliases.get(text.casefold())
        if not normalized:
            raise ValueError(
                "simulation.source must be empty or one of RadarFC/RadarFL/RadarFR/RadarRL/RadarRR"
            )
        return normalized


class PartialUserResultConfig(_Frozen):
    path: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        if value is None:
            return None
        return _path(value) if isinstance(value, str) else value


class PartialUserRunConfig(_Frozen):
    """Partial YAML document used only by import/export draft operations.

    It intentionally has no cross-field completeness validator.  The strict
    :class:`UserRunConfig` remains the only model accepted by validate/submit.
    """

    schema_version: Literal["2.0"] | None = "2.0"
    selena: PartialUserSelenaConfig | None = None
    data: PartialUserDataConfig | None = None
    simulation: PartialUserSimulationConfig | None = None
    result: PartialUserResultConfig | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartialUserRunConfig":
        return cls.model_validate(value)

    @classmethod
    def from_yaml(cls, source: str | Path) -> "PartialUserRunConfig":
        return cls.from_dict(load_yaml_mapping(source))

    def to_dict(self) -> dict[str, Any]:
        value = self.model_dump(
            mode="python",
            exclude_unset=True,
            exclude_none=True,
        )
        # Keep the public contract marker even when the user omitted it from
        # a draft; this makes an exported partial document self-describing.
        schema_version = value.pop("schema_version", None) or "2.0"
        return {"schema_version": schema_version, **value}

    def to_yaml(self) -> str:
        return dump_yaml(self.to_dict())


def missing_user_run_config_fields(value: dict[str, Any]) -> list[str]:
    """Return deterministic fields required to turn a draft into a runnable config."""

    payload = dict(value or {})
    missing: list[str] = []
    selena = payload.get("selena")
    if not isinstance(selena, dict):
        missing.append("selena")
        source = ""
    else:
        source = str(selena.get("source") or "").strip().lower()
        if not source:
            missing.append("selena.source")
        elif source == "build":
            for field in ("code_path", "selena_build_script", "runtime_xml"):
                if not str(selena.get(field) or "").strip():
                    missing.append(f"selena.{field}")
        elif source == "existing":
            for field in ("existing_path", "runtime_xml"):
                if not str(selena.get(field) or "").strip():
                    missing.append(f"selena.{field}")
    data = payload.get("data")
    if not isinstance(data, dict) or not str(data.get("path") or "").strip():
        missing.append("data.path")
    if not isinstance(payload.get("simulation"), dict):
        missing.append("simulation")
    return missing


def partial_user_run_config_status(
    payload: dict[str, Any],
) -> tuple[UserRunConfig | None, list[str]]:
    """Return strict completeness status without rejecting a valid draft."""

    try:
        return UserRunConfig.from_dict(payload), []
    except ValidationError as exc:
        errors: list[str] = []
        for item in exc.errors():
            location = ".".join(str(value) for value in item.get("loc") or ())
            message = str(item.get("msg") or "invalid value")
            errors.append(f"{location}: {message}" if location else message)
        return None, errors


__all__ = [
    "PartialUserDataConfig",
    "PartialUserResultConfig",
    "PartialUserRunConfig",
    "PartialUserSelenaConfig",
    "PartialUserSimulationConfig",
    "UserDataConfig",
    "UserResultConfig",
    "UserRunConfig",
    "UserSelenaConfig",
    "UserSimulationConfig",
    "missing_user_run_config_fields",
    "partial_user_run_config_status",
]
