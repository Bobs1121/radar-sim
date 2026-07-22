"""Internal, project-free Selena workspace recognition.

Users provide a code path and, optionally, a build script.  This compatibility
layer reads the existing ``config/projects`` adapters and returns an internal
adapter key without making project/profile part of the Web or SDK contract.
It is pure discovery: it never changes Git state or runs a build.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


class WorkspaceRecognitionError(ValueError):
    """Stable recognition failure."""


@dataclass(frozen=True)
class RecognitionResult:
    status: str
    adapter_key: str = ""
    internal_project: str = ""
    workspace_root: str = ""
    build_script: str = ""
    selena_build_script: str = ""
    package_build_script: str = ""
    output_dir: str = ""
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "ambiguous", "unresolved"}:
            raise WorkspaceRecognitionError("recognition status is invalid")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise WorkspaceRecognitionError("recognition confidence is invalid")

    def to_internal_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Return only path-free, adapter-free information for Web/SDK."""
        return {
            "status": self.status,
            "confidence": round(float(self.confidence), 4),
            "evidence": list(dict.fromkeys(self.evidence)),
            "candidate_count": len(self.candidates),
        }


@dataclass(frozen=True)
class _Adapter:
    key: str
    project: str
    workspace_roots: tuple[str, ...]
    build_script: str
    package_build_script: str
    output_dir: str


@dataclass(frozen=True)
class _Candidate:
    adapter: _Adapter
    confidence: float
    evidence: tuple[str, ...]


class WorkspaceRecognizer:
    """Recognize one internal adapter from an authorized code workspace."""

    SCRIPT_NAMES = ("jenkins_selena_build.bat", "build_selena.bat")

    def __init__(self, projects_dir: str | Path | None = None) -> None:
        root = Path(projects_dir) if projects_dir is not None else Path(__file__).parents[1] / "config" / "projects"
        self._adapters = tuple(_load_adapters(root))

    def recognize(
        self,
        code_path: str,
        build_script: str = "",
        *,
        selena_build_script: str = "",
        package_build_script: str = "",
    ) -> RecognitionResult:
        raw_root = str(code_path or "").strip()
        if not raw_root:
            return RecognitionResult(status="unresolved", evidence=("code_path_required",))
        root = _normalize_path(raw_root)
        if not _is_absolute(root):
            return RecognitionResult(status="unresolved", evidence=("code_path_must_be_absolute",))

        explicit_selena = _normalize_path(str(selena_build_script or build_script or "").strip())
        explicit_package = _normalize_path(str(package_build_script or "").strip())
        if explicit_selena and not _is_within(root, explicit_selena):
            return RecognitionResult(
                status="unresolved",
                workspace_root=root,
                evidence=("build_script_outside_workspace",),
            )
        if explicit_package and not _is_within(root, explicit_package):
            return RecognitionResult(
                status="unresolved",
                workspace_root=root,
                evidence=("package_build_script_outside_workspace",),
            )

        discovered = ""
        selected_script = explicit_selena
        candidates = [
            item
            for adapter in self._adapters
            if (item := _score_adapter(adapter, root, explicit_selena, explicit_package)) is not None
        ]

        # Filesystem discovery is a fallback only.  Config matches avoid an
        # expensive walk through a large real Selena workspace.
        if not candidates and not explicit_selena:
            discovered = self._discover_script(raw_root)
            selected_script = discovered
            if discovered:
                candidates = [
                    item
                    for adapter in self._adapters
                    if (item := _score_adapter(adapter, root, discovered, explicit_package)) is not None
                ]
        if not candidates and (explicit_selena or explicit_package or discovered):
            generic_selena = explicit_selena or discovered
            generic = _Adapter(
                key="generic:selena-script",
                # ``project`` is a local authorization namespace, not a
                # business-project choice.  Unknown products still need a
                # stable, path-free token so the Agent can bind the workspace
                # and later stages can refer to the resulting artifacts.
                project=_generic_internal_project(root, generic_selena, explicit_package),
                workspace_roots=(root,),
                build_script=generic_selena,
                package_build_script=explicit_package,
                output_dir="",
            )
            candidates.append(
                _Candidate(
                    generic,
                    0.65 if (explicit_selena or explicit_package) else 0.55,
                    ("explicit_build_script_only",) if (explicit_selena or explicit_package) else ("build_script_discovered",),
                )
            )

        if not candidates:
            return RecognitionResult(
                status="unresolved",
                workspace_root=root,
                build_script=selected_script,
                evidence=("adapter_not_recognized",),
            )

        best_score = max(item.confidence for item in candidates)
        best = [item for item in candidates if item.confidence == best_score]
        if len(best) != 1:
            return RecognitionResult(
                status="ambiguous",
                workspace_root=root,
                build_script=selected_script,
                confidence=best_score,
                evidence=("multiple_internal_adapters_match",),
                candidates=tuple(sorted(item.adapter.key for item in best)),
            )

        winner = best[0]
        script = selected_script or winner.adapter.build_script
        if script and not _is_within(root, script):
            return RecognitionResult(
                status="unresolved",
                workspace_root=root,
                confidence=winner.confidence,
                evidence=("configured_build_script_outside_workspace",),
            )
        # The user-selected Selena script is the source of truth for its build
        # directory.  Project config paths are only a fallback and may point at
        # another checkout.  This is especially important for ovrs25, whose
        # adapter intentionally has no static build_output.
        derived_output = ""
        if script:
            try:
                from core.config import derive_project_context_from_selena_script

                derived_output = _normalize_path(
                    str(
                        derive_project_context_from_selena_script(script).get(
                            "build_output"
                        )
                        or ""
                    )
                )
            except (OSError, TypeError, ValueError):
                derived_output = ""
        output_dir = _rebase_to_workspace(
            derived_output or winner.adapter.output_dir,
            winner.adapter.workspace_roots,
            root,
        )
        return RecognitionResult(
            status="resolved",
            adapter_key=winner.adapter.key,
            internal_project=winner.adapter.project,
            workspace_root=root,
            build_script=script,
            selena_build_script=explicit_selena or script,
            package_build_script=explicit_package or winner.adapter.package_build_script,
            output_dir=output_dir,
            confidence=winner.confidence,
            evidence=(
                winner.evidence
                + (("explicit_build_script",) if explicit_selena else ())
                + (("explicit_package_build_script",) if explicit_package else ())
                + (("build_script_discovered",) if discovered else ())
            ),
        )

    def _discover_script(self, code_path: str) -> str:
        root = Path(code_path).expanduser()
        if not root.is_dir():
            return ""
        ignored = {".git", ".svn", "build", "node_modules", "__pycache__"}
        candidates: list[Path] = []
        wanted = {name.casefold() for name in self.SCRIPT_NAMES}
        try:
            for current, dirs, files in os.walk(root):
                current_path = Path(current)
                depth = len(current_path.relative_to(root).parts)
                dirs[:] = [] if depth >= 6 else sorted(
                    (name for name in dirs if name.casefold() not in ignored),
                    key=str.casefold,
                )
                for name in sorted(files, key=str.casefold):
                    if name.casefold() in wanted:
                        candidates.append(current_path / name)
                        if len(candidates) > 1:
                            return ""
        except (OSError, ValueError):
            return ""
        if len(candidates) != 1:
            return ""
        value = _normalize_path(str(candidates[0]))
        return value if _is_within(_normalize_path(str(root)), value) else ""


def recognize_workspace(
    code_path: str,
    build_script: str = "",
    *,
    selena_build_script: str = "",
    package_build_script: str = "",
    projects_dir: str | Path | None = None,
) -> RecognitionResult:
    return WorkspaceRecognizer(projects_dir).recognize(
        code_path,
        build_script,
        selena_build_script=selena_build_script,
        package_build_script=package_build_script,
    )


def _load_adapters(projects_dir: Path) -> Iterable[_Adapter]:
    if not projects_dir.is_dir():
        return ()
    adapters: list[_Adapter] = []
    for config_path in sorted(projects_dir.glob("*/config.yaml"), key=lambda path: path.as_posix().casefold()):
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue
        build = payload.get("build") if isinstance(payload.get("build"), dict) else {}
        repos = payload.get("repos") if isinstance(payload.get("repos"), dict) else {}
        roots = tuple(
            dict.fromkeys(
                _normalize_path(str(value))
                for value in (repos.get("outer_repo_root"), repos.get("inner_repo_root"))
                if str(value or "").strip()
            )
        )
        script = _normalize_path(str(build.get("selena_build_script") or ""))
        if not roots and script:
            roots = (_common_workspace_root(script),)
        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        recipe = str(project.get("recipe") or "").strip()
        # Unique internal identity: platform alone is not sufficient because
        # multiple business adapters share gen5_selena.
        key = f"recipe:{recipe}" if recipe else f"project:{config_path.parent.name}"
        adapters.append(
            _Adapter(
                key=key,
                project=config_path.parent.name,
                workspace_roots=tuple(item for item in roots if item),
                build_script=script,
                package_build_script=_normalize_path(
                    str(build.get("env_build_script") or build.get("hex_build_script") or "")
                ),
                output_dir=_normalize_path(str(build.get("build_output") or "")),
            )
        )
    return tuple(adapters)


def _score_adapter(
    adapter: _Adapter,
    root: str,
    selena_script: str,
    package_script: str = "",
) -> _Candidate | None:
    scores: list[tuple[float, str]] = []
    for configured in adapter.workspace_roots:
        if root == configured:
            scores.append((0.9, "workspace_exact_match"))
        elif _is_within(configured, root):
            scores.append((0.85, "workspace_child_match"))
        elif _is_within(root, configured):
            scores.append((0.7, "workspace_parent_match"))
    if selena_script and adapter.build_script and selena_script == adapter.build_script:
        scores.append((1.0, "build_script_exact_match"))
    elif selena_script and adapter.build_script and _relative_suffix(selena_script, adapter.build_script) >= 3:
        scores.append((0.95, "build_script_suffix_match"))
    if package_script and adapter.package_build_script and package_script == adapter.package_build_script:
        scores.append((1.0, "package_build_script_exact_match"))
    elif package_script and adapter.package_build_script and _relative_suffix(package_script, adapter.package_build_script) >= 3:
        scores.append((0.95, "package_build_script_suffix_match"))
    if not scores:
        return None
    score, evidence = max(scores, key=lambda item: item[0])
    return _Candidate(adapter=adapter, confidence=score, evidence=(evidence,))


def _normalize_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    windows = bool(re.match(r"^[A-Za-z]:/", text) or text.startswith("//"))
    normalized = (ntpath.normpath(text).replace("\\", "/") if windows else posixpath.normpath(text))
    return normalized.casefold() if windows else normalized


def _is_absolute(value: str) -> bool:
    return bool(re.match(r"^[a-z]:/", value) or value.startswith("//") or value.startswith("/"))


def _is_within(parent: str, child: str) -> bool:
    parent_parts = tuple(part for part in _normalize_path(parent).split("/") if part)
    child_parts = tuple(part for part in _normalize_path(child).split("/") if part)
    return bool(parent_parts) and child_parts[: len(parent_parts)] == parent_parts


def _common_workspace_root(script: str) -> str:
    marker = "/apl/"
    lowered = script.casefold()
    return script[: lowered.index(marker)] if marker in lowered else posixpath.dirname(script)


def _relative_suffix(left: str, right: str) -> int:
    left_parts = [part for part in left.split("/") if part]
    right_parts = [part for part in right.split("/") if part]
    count = 0
    for a, b in zip(reversed(left_parts), reversed(right_parts)):
        if a.casefold() != b.casefold():
            break
        count += 1
    return count


def _generic_internal_project(root: str, selena_script: str, package_script: str) -> str:
    """Return a stable logical namespace for an unregistered workspace.

    The value is deliberately opaque and path-free.  All inputs are already
    canonicalized by the recognizer, so equivalent Windows path spellings
    produce the same identity while a different checkout or script pair gets
    an independent authorization namespace.
    """
    payload = "\0".join(
        (
            _normalize_path(root),
            _normalize_path(selena_script),
            _normalize_path(package_script),
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"workspace-{digest}"


def _rebase_to_workspace(path: str, configured_roots: tuple[str, ...], actual_root: str) -> str:
    """Map a configured project output into the user-selected checkout."""
    normalized = _normalize_path(path)
    if not normalized:
        return ""
    for configured in configured_roots:
        base = _normalize_path(configured)
        if not _is_within(base, normalized):
            continue
        base_parts = [part for part in base.split("/") if part]
        value_parts = [part for part in normalized.split("/") if part]
        relative = value_parts[len(base_parts):]
        return posixpath.join(actual_root, *relative) if relative else actual_root
    return normalized


__all__ = [
    "RecognitionResult",
    "WorkspaceRecognitionError",
    "WorkspaceRecognizer",
    "recognize_workspace",
]
