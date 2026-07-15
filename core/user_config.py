"""Project-free user configuration contract shared by Web and SDK.

Internal project adapters, Runtime Bundle identifiers, Cluster endpoints,
credentials, mount mappings and scheduler details are intentionally absent.
The public contract only describes the user-selected workspace or existing
Selena folder; internal packaging and adapter recognition are Stage concerns.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from core.spec.yaml_codec import dump_yaml, load_yaml_mapping


def _path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("//"):
        return "//" + re.sub(r"/+", "/", text[2:])
    if "://" in text:
        scheme, rest = text.split("://", 1)
        return scheme + "://" + re.sub(r"/+", "/", rest)
    return re.sub(r"/+", "/", text)


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
            if not self.package_build_script:
                raise ValueError("selena.package_build_script is required for local build")
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
            build_fields = (
                self.code_path,
                self.branch,
                self.selena_build_script,
                self.package_build_script,
            )
            if any(build_fields):
                raise ValueError("existing Selena must not contain build workspace fields")
        return self


class UserDataConfig(_Frozen):
    path: str

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> Any:
        return _path(value) if isinstance(value, str) else value

    @field_validator("path")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("data.path must not be empty")
        return value


class UserSimulationConfig(_Frozen):
    target: Literal["auto", "local", "cluster"] = "auto"
    adapter_file: str = ""
    mat_filter: str

    @field_validator("adapter_file", "mat_filter", mode="before")
    @classmethod
    def _normalize_paths(cls, value: Any) -> Any:
        return _path(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _required_assets(self) -> "UserSimulationConfig":
        if not self.mat_filter:
            raise ValueError("simulation.mat_filter is required")
        return self


class UserRunConfig(_Frozen):
    schema_version: Literal["2.0"] = "2.0"
    selena: UserSelenaConfig
    data: UserDataConfig
    simulation: UserSimulationConfig

    @staticmethod
    def _migrate_legacy(value: dict[str, Any]) -> dict[str, Any]:
        """Map legacy YAML selena build fields onto the current contract.

        ``build_script`` (the single legacy Selena build entry point) maps to
        ``selena_build_script``; ``build_mode`` is dropped entirely.  Both
        legacy keys are removed so the strict ``extra="forbid"`` model accepts
        the migrated payload and the new fields never leak back on export.

        Legacy data.limit, simulation.timeout_minutes, and result block are
        silently dropped.
        """
        if not isinstance(value, dict):
            return value
        migrated = copy.deepcopy(value)
        selena = migrated.get("selena")
        if isinstance(selena, dict):
            legacy_build_script = selena.pop("build_script", None)
            if "selena_build_script" not in selena and legacy_build_script:
                selena["selena_build_script"] = legacy_build_script
            selena.pop("build_mode", None)
            # Runtime Bundle identifiers were an internal implementation detail
            # leaked by the old public contract.  Drop them on import; an old
            # bundle-only existing config still fails the required folder and
            # Runtime validators.
            selena.pop("bundle", None)
            selena.pop("executable", None)
        data = migrated.get("data")
        if isinstance(data, dict):
            data.pop("limit", None)
        simulation = migrated.get("simulation")
        if isinstance(simulation, dict):
            simulation.pop("timeout_minutes", None)
        migrated.pop("result", None)
        return migrated

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UserRunConfig":
        return cls.model_validate(cls._migrate_legacy(value))

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
                "package_build_script": self.selena.package_build_script,
                "runtime_xml": self.selena.runtime_xml,
            }
        else:
            selena = {
                "source": "existing",
                "existing_path": self.selena.existing_path,
                "runtime_xml": self.selena.runtime_xml,
            }
        return {
            "schema_version": self.schema_version,
            "selena": selena,
            "data": {"path": self.data.path},
            "simulation": {
                "target": self.simulation.target,
                "adapter_file": self.simulation.adapter_file,
                "mat_filter": self.simulation.mat_filter,
            },
        }

    def to_yaml(self) -> str:
        return dump_yaml(self.to_dict())

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()


__all__ = [
    "UserDataConfig",
    "UserRunConfig",
    "UserSelenaConfig",
    "UserSimulationConfig",
]
