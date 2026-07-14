"""
WorkspaceRecognizer - user-facing "no project concept" workspace recognition.

This is an independently auditable slice: the only job of this module is to
take a user's ``code_path`` (the source workspace they want to build) and an
optional ``build_script``, and produce a :class:`RecognitionResult` describing
which Selena build adapter applies and where the build/output roots live.

Design rules (see PRD.md S6.3, S10.1 and CLAUDE.md):
  * The user never names a "project". We infer the adapter by matching the
    workspace path against the per-project ``config/projects/*/config.yaml``
    repositories and build-script paths.
  * Explicit (user-supplied) and auto-discovered build scripts must never
    *escape* the ``code_path``: the resolved script must live at or below
    ``code_path`` after path normalization. Escaping scripts are rejected.
  * When two or more candidates tie on confidence, the result is ``ambiguous``
    rather than silently picking one.
  * When nothing matches, the result is ``unresolved`` - we never fabricate a
    project.
  * The public view (:meth:`RecognitionResult.public_dict`) must never expose
    absolute filesystem paths, the internal project name, or any profile name.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Optional

import yaml

# Status values are stable strings; callers (tests, API) rely on them.
STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNRESOLVED = "unresolved"

# Filename(s) we look for when auto-discovering a Selena build entry point.
# Keep this list small and explicit - discovery is best-effort only.
_SELENA_BUILD_SCRIPT_NAMES = (
    "jenkins_selena_build.bat",
)

# Adapter keys are derived from the platform/recipe fields in each project
# config. The fallback key when a project has no platform is the generic
# selena key - but we still never expose the project name in public_dict.
_DEFAULT_ADAPTER_KEY = "selena"


@dataclass(frozen=True)
class RecognitionResult:
    """Outcome of recognizing a user workspace.

    Attributes:
        adapter_key: Stable adapter identifier (e.g. ``gen5_selena``). Empty
            when ``status`` is ``unresolved``.
        workspace_root: Normalized absolute path of the recognized workspace.
            Internal only.
        build_script: Normalized absolute path of the Selena build script.
            Internal only.
        output_dir: Normalized absolute path of the build output directory, if
            the matched config declares ``build.build_output``. Internal only.
        confidence: 0.0-1.0. Higher means stronger evidence.
        evidence: Ordered evidence strings explaining the match. These MUST NOT
            contain absolute paths when surfaced publicly.
        status: One of ``resolved`` / ``ambiguous`` / ``unresolved``.
        candidates: When ``ambiguous``, the list of tied candidate adapter
            keys (internal diagnostic only).
    """

    adapter_key: str = ""
    workspace_root: str = ""
    build_script: str = ""
    output_dir: str = ""
    confidence: float = 0.0
    evidence: tuple = field(default_factory=tuple)
    status: str = STATUS_UNRESOLVED
    candidates: tuple = field(default_factory=tuple)

    def public_dict(self) -> dict:
        """Return a safe, externally-shareable view of this result.

        Strips every absolute filesystem path, the internal project/adapter
        origin, and profile names. The public contract is intentionally tiny:
        callers learn whether the workspace was recognized and a sanitized
        evidence trail, nothing about where files live on disk. Duplicate
        evidence strings are collapsed so the public view stays readable.
        """
        seen = set()
        deduped = []
        for e in self.evidence:
            if e not in seen:
                seen.add(e)
                deduped.append(e)
        return {
            "status": self.status,
            "confidence": round(self.confidence, 4),
            "evidence": deduped,
            "candidates": list(self.candidates),
        }

    def as_dict(self) -> dict:
        """Full internal representation (includes paths). For logs/manifests."""
        return asdict(self)


# --------------------------------------------------------------------------- #
# Path helpers - Windows-casing- and slash-stable, cross-platform testable.
# --------------------------------------------------------------------------- #

def _normalize_path(p: str) -> str:
    """Normalize a path string to absolute forward-slash lower-cased form.

    Forward slashes make the value portable across Windows/Linux and stable
    in tests. Lower-casing makes Windows drive letters and directory names
    compare correctly despite the filesystem being case-insensitive there.

    On a POSIX host the input may still be a Windows-style path
    (``D:/bydod25fr/byd``) coming from a project config authored on Windows -
    we therefore use :class:`PureWindowsPath` for normalization so the same
    config matches regardless of the host running the tests.
    """
    if not p:
        return ""
    norm = PureWindowsPath(p)
    s = str(norm).replace("\\", "/")
    return s.lower()


def _is_within(parent: str, child: str) -> bool:
    """True if ``child`` is ``parent`` itself or lives below it.

    Both inputs must be pre-normalized via :func:`_normalize_path`. A child
    that resolves to a sibling or ancestor (path escape) returns False.
    """
    if not parent or not child:
        return False
    if child == parent:
        return True
    # Ensure prefix match is on a path-segment boundary, not a substring of
    # a directory name (``/foo`` must not contain ``/foobar``).
    return child.startswith(parent + "/")


def _script_escapes(code_path: str, script: str) -> bool:
    """True if ``script`` does NOT live at or below ``code_path``."""
    ncode = _normalize_path(code_path)
    nscript = _normalize_path(script)
    return not _is_within(ncode, nscript)


# --------------------------------------------------------------------------- #
# Project config loading.
# --------------------------------------------------------------------------- #

def _projects_dir() -> Path:
    """config/projects/ relative to this file (radar-sim root)."""
    return Path(__file__).resolve().parent.parent / "config" / "projects"


def _load_project_configs(projects_dir: Optional[Path] = None) -> list:
    """Load every ``config/projects/*/config.yaml`` as (dirname, dict)."""
    base = Path(projects_dir) if projects_dir else _projects_dir()
    out = []
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        cfg = entry / "config.yaml"
        if not cfg.is_file():
            continue
        try:
            with cfg.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            continue
        out.append((entry.name, data))
    return out


def _adapter_key_for(project_dirname: str, cfg: dict) -> str:
    """Derive a stable adapter key from a project config."""
    project = cfg.get("project") or {}
    platform = project.get("platform")
    if platform:
        return str(platform)
    recipe = project.get("recipe")
    if recipe:
        return str(recipe)
    return _DEFAULT_ADAPTER_KEY


def _candidate_paths_from_cfg(cfg: dict) -> list:
    """Collect path strings from a project config we match code_path against.

    These are the repo roots and the configured build-script path. Matching is
    prefix-based: if the user's code_path is at or below one of these roots,
    this project is a candidate.
    """
    paths = []
    repos = cfg.get("repos") or {}
    for key in ("outer_repo_root", "inner_repo_root"):
        v = repos.get(key)
        if v:
            paths.append(str(v))
    build = cfg.get("build") or {}
    script = build.get("selena_build_script")
    if script:
        paths.append(str(script))
    return paths


def _build_output_from_cfg(cfg: dict) -> str:
    return str((cfg.get("build") or {}).get("build_output") or "")


def _build_script_from_cfg(cfg: dict) -> str:
    return str((cfg.get("build") or {}).get("selena_build_script") or "")


# --------------------------------------------------------------------------- #
# The recognizer.
# --------------------------------------------------------------------------- #

class WorkspaceRecognizer:
    """Recognize a Selena build adapter from a user's workspace path.

    The recognizer is stateless aside from the loaded project configs; it never
    runs a real compile and never writes to the user's workspace.
    """

    # Confidence levels assigned to each evidence source. Tuned so that an
    # explicit build_script beats config path matching beats auto-discovery,
    # and a tie between two equally-strong sources yields ``ambiguous``.
    CONF_EXPLICIT_SCRIPT_AGAINST_CFG = 0.95
    CONF_CONFIG_PATH_MATCH = 0.8
    CONF_AUTO_DISCOVERED_SCRIPT = 0.6

    def __init__(self, projects_dir: Optional[Path] = None):
        self._projects_dir = Path(projects_dir) if projects_dir else None
        self._configs = _load_project_configs(self._projects_dir)

    def recognize(self, code_path: str, build_script: Optional[str] = None) -> RecognitionResult:
        """Recognize the adapter for ``code_path``.

        Args:
            code_path: Absolute path to the user's source workspace.
            build_script: Optional explicit build script path. If supplied it
                MUST live within ``code_path``; an escaping script is rejected
                and recorded as evidence.

        Returns:
            A :class:`RecognitionResult`.
        """
        if not code_path:
            return RecognitionResult(
                status=STATUS_UNRESOLVED,
                evidence=("code_path is empty",),
            )
        ncode = _normalize_path(code_path)
        evidence: list = []

        # ---- 1. Validate an explicit build script, if any. -------------- #
        explicit_script = ""
        if build_script:
            if _script_escapes(code_path, build_script):
                evidence.append(
                    "explicit build_script rejected: escapes code_path boundary"
                )
            else:
                explicit_script = build_script
                evidence.append("explicit build_script accepted (within code_path)")

        # ---- 2. Match against project configs by path prefix. ----------- #
        candidates = self._match_configs(ncode, explicit_script)

        # ---- 3. Auto-discover a build script inside code_path. ---------- #
        discovered_script = ""
        if not explicit_script:
            discovered_script = self._discover_script(code_path)
            if discovered_script:
                evidence.append("auto-discovered build script inside code_path")
                if candidates:
                    for c in candidates:
                        if c["confidence"] < self.CONF_AUTO_DISCOVERED_SCRIPT:
                            c["confidence"] = self.CONF_AUTO_DISCOVERED_SCRIPT
                            c["evidence_extra"] = c.get("evidence_extra", ()) + (
                                "confidence lifted by auto-discovered build script",
                            )
                else:
                    candidates.append({
                        "adapter_key": _DEFAULT_ADAPTER_KEY,
                        "workspace_root": code_path,
                        "build_script": discovered_script,
                        "output_dir": "",
                        "confidence": self.CONF_AUTO_DISCOVERED_SCRIPT,
                        "evidence_extra": ("auto-discovered build script only",),
                    })

        # ---- 4. Decide. ------------------------------------------------- #
        if not candidates:
            return RecognitionResult(
                status=STATUS_UNRESOLVED,
                workspace_root=_normalize_path(code_path),
                build_script=_normalize_path(explicit_script or discovered_script),
                confidence=0.0,
                evidence=tuple(evidence) or ("no adapter matched code_path",),
            )

        top_conf = max(c["confidence"] for c in candidates)
        top = [c for c in candidates if c["confidence"] == top_conf]

        if len(top) > 1:
            keys = tuple(sorted(c["adapter_key"] for c in top))
            return RecognitionResult(
                status=STATUS_AMBIGUOUS,
                workspace_root=_normalize_path(code_path),
                build_script=_normalize_path(
                    explicit_script or discovered_script or top[0]["build_script"]
                ),
                output_dir="",
                confidence=top_conf,
                evidence=tuple(evidence) + (
                    "multiple adapters tied at top confidence: "
                    + ", ".join(keys),
                ),
                candidates=keys,
            )

        c = top[0]
        all_evidence = tuple(evidence) + c.get("evidence_extra", ())
        return RecognitionResult(
            status=STATUS_RESOLVED,
            adapter_key=c["adapter_key"],
            workspace_root=c["workspace_root"],
            build_script=_normalize_path(
                explicit_script or discovered_script or c["build_script"]
            ),
            output_dir=c["output_dir"],
            confidence=c["confidence"],
            evidence=all_evidence,
        )

    def _match_configs(self, ncode: str, explicit_script: str) -> list:
        """Return candidate matches from project configs for normalized code path."""
        out = []
        for dirname, cfg in self._configs:
            adapter = _adapter_key_for(dirname, cfg)
            paths = _candidate_paths_from_cfg(cfg)
            if not paths:
                continue
            matched_on = []
            conf = 0.0
            for raw in paths:
                nraw = _normalize_path(raw)
                if not nraw:
                    continue
                # code_path at/below a configured root, OR a configured root
                # at/below code_path (user pointed higher than the repo root).
                if _is_within(nraw, ncode) or _is_within(ncode, nraw):
                    matched_on.append("configured repo/script path prefix")
                    conf = max(conf, self.CONF_CONFIG_PATH_MATCH)
            if not matched_on:
                continue
            cfg_script = _build_script_from_cfg(cfg)
            if explicit_script and cfg_script:
                if _normalize_path(explicit_script) == _normalize_path(cfg_script):
                    conf = self.CONF_EXPLICIT_SCRIPT_AGAINST_CFG
                    matched_on.append("explicit script matches configured script")
            out.append({
                "adapter_key": adapter,
                "workspace_root": _restore_slashes(ncode),
                "build_script": cfg_script,
                "output_dir": _normalize_path(_build_output_from_cfg(cfg)),
                "confidence": conf,
                "evidence_extra": tuple(matched_on),
            })
        return out

    def _discover_script(self, code_path: str) -> str:
        """Look for a known Selena build script name inside code_path.

        Discovery is deliberately bounded: we only descend a few levels and
        skip common build-output / VCS directories. This keeps recognition
        fast even when the user points at a large repo root, and it never
        touches the filesystem on non-existent (e.g. cross-platform Windows)
        paths.
        """
        base = Path(code_path)
        if not base.is_dir():
            return ""
        # Directories we never descend into during discovery.
        skip_dirs = {".git", "build", ".svn", "__pycache__", "node_modules"}
        max_depth = 6

        def _search(root: Path, depth: int) -> str:
            if depth > max_depth:
                return ""
            try:
                entries = list(root.iterdir())
            except (OSError, PermissionError):
                return ""
            for entry in entries:
                if entry.is_file() and entry.name in _SELENA_BUILD_SCRIPT_NAMES:
                    if not _script_escapes(code_path, str(entry)):
                        return str(entry)
            for entry in entries:
                if entry.is_dir() and entry.name.lower() not in skip_dirs:
                    found = _search(entry, depth + 1)
                    if found:
                        return found
            return ""

        return _search(base, 0)


def _restore_slashes(normalized_lower: str) -> str:
    """Return the normalized key as the internal workspace_root representation.

    We keep the lowercased forward-slash form: it is stable across platforms
    and casing. Callers needing the original case should keep their own copy.
    """
    return normalized_lower


def recognize(code_path: str, build_script: Optional[str] = None,
              projects_dir: Optional[Path] = None) -> RecognitionResult:
    """Module-level convenience wrapper around :class:`WorkspaceRecognizer`."""
    return WorkspaceRecognizer(projects_dir=projects_dir).recognize(
        code_path, build_script=build_script
    )
