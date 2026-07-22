"""Derive and prepare Windows build dependencies from user-selected scripts.

The control plane must not maintain a product whitelist for tools such as
Perl.  This module inspects a bounded, workspace-local set of build
descriptors around the supplied Selena/package entry scripts, discovers an
already installed tool, and returns a process-local environment overlay.
Nothing in the user's source tree or machine-wide environment is modified.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


class WindowsBuildDependencyError(ValueError):
    """A script-derived build dependency is not available on this machine."""


@dataclass(frozen=True)
class WindowsBuildEnvironment:
    dependencies: tuple[str, ...] = ()
    environment: dict[str, str] | None = None
    evidence: tuple[str, ...] = ()
    perl_executable: str = ""

    @property
    def prepared(self) -> bool:
        return bool(self.dependencies)


_BUILD_SUFFIXES = {".bat", ".cmd", ".cmake", ".config", ".ps1", ".py"}
_BUILD_NAME_RE = re.compile(r"build|cmake|generate|generator|install|setup|init|tool", re.I)
_PERL_RE = re.compile(r"(?:\bperl(?:\.exe)?\b|pad_generator\.pl)", re.I)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _scan_roots(entry_scripts: Iterable[Path], workspace: Path) -> list[Path]:
    """Return small script-adjacent roots inferred from the supplied entries."""
    roots: list[Path] = []
    for script in entry_scripts:
        for candidate in (script.parent, script.parent.parent):
            if candidate.is_dir() and _is_within(candidate, workspace) and candidate not in roots:
                roots.append(candidate)
    return roots


def _dependency_sources(
    entry_scripts: Iterable[Path],
    workspace: Path,
    *,
    max_files: int = 400,
    max_bytes: int = 2 * 1024 * 1024,
) -> list[tuple[Path, str]]:
    """Read a bounded set of build descriptors without executing user code.

    Entry scripts are always inspected.  Their adjacent build subtree is used
    as a generic approximation of the script/include graph; this covers batch
    wrappers that enter CMake indirectly while avoiding an expensive scan of
    the whole repository.
    """
    entries = [item.resolve(strict=False) for item in entry_scripts if item.is_file()]
    candidates: list[Path] = list(entries)
    for root in _scan_roots(entries, workspace):
        try:
            for item in root.rglob("*"):
                if len(candidates) >= max_files:
                    break
                if (
                    item.is_file()
                    and item.suffix.casefold() in _BUILD_SUFFIXES
                    and _BUILD_NAME_RE.search(item.name)
                ):
                    candidates.append(item.resolve(strict=False))
        except OSError:
            continue

    sources: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or len(sources) >= max_files or not _is_within(path, workspace):
            continue
        seen.add(path)
        try:
            if path.stat().st_size > max_bytes:
                continue
            sources.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return sources


def _env_value(env: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in env.items():
        if str(key).casefold() == wanted:
            return str(value or "")
    return ""


def _candidate_perl_paths(env: Mapping[str, str], sources: Iterable[tuple[Path, str]]) -> list[Path]:
    candidates: list[Path] = []
    configured = _env_value(env, "TCCPATH_perl").strip().strip('"')
    if configured:
        base = Path(configured)
        candidates.extend((base / "perl" / "bin" / "perl.exe", base / "bin" / "perl.exe"))

    found = shutil.which("perl", path=_env_value(env, "PATH") or None)
    if found:
        candidates.append(Path(found))

    # Script literals are stronger than generic machine fallbacks.
    literal_re = re.compile(r"([A-Za-z]:[\\/][^\r\n\"']*?perl\.exe)", re.I)
    for _path, text in sources:
        candidates.extend(Path(raw.replace("/", os.sep)) for raw in literal_re.findall(text))

    candidates.append(Path(r"C:\Perl\bin\perl.exe"))
    tcc_root = Path(r"C:\TCC\Tools\perl")
    if tcc_root.is_dir():
        try:
            candidates.extend(tcc_root.glob("*/perl/bin/perl.exe"))
            candidates.extend(tcc_root.glob("*/bin/perl.exe"))
        except OSError:
            pass

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve(strict=False)).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _perl_environment(perl: Path, base_env: Mapping[str, str]) -> dict[str, str]:
    env = dict(base_env)
    tool_root = perl.parents[2] if len(perl.parents) > 2 else perl.parent
    path_entries = [str(perl.parent)]
    strawberry_bin = tool_root / "c" / "bin"
    if strawberry_bin.is_dir():
        path_entries.insert(0, str(strawberry_bin))
    current_path = _env_value(env, "PATH")
    if current_path:
        path_entries.append(current_path)
    env["PATH"] = os.pathsep.join(path_entries)
    if not _env_value(env, "TCCPATH_perl"):
        env["TCCPATH_perl"] = str(tool_root)
    return env


def prepare_windows_build_environment(
    *,
    workspace_root: str | os.PathLike[str],
    selena_build_script: str | os.PathLike[str] | None = None,
    package_build_script: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> WindowsBuildEnvironment:
    """Prepare a process-local environment for script-derived dependencies."""
    workspace = Path(workspace_root).resolve(strict=False)
    entries = [
        Path(value).resolve(strict=False)
        for value in (selena_build_script, package_build_script)
        if value
    ]
    sources = _dependency_sources(entries, workspace)
    perl_evidence = [path for path, text in sources if _PERL_RE.search(text)]
    base_env = dict(os.environ if env is None else env)
    if not perl_evidence:
        return WindowsBuildEnvironment(environment=base_env)

    perl = next((path.resolve(strict=False) for path in _candidate_perl_paths(base_env, sources) if path.is_file()), None)
    if perl is None:
        raise WindowsBuildDependencyError(
            "The supplied build scripts require Perl, but no usable Perl installation was found. "
            "Install the software-package tool collection and retry."
        )
    evidence = tuple(
        str(path.relative_to(workspace)).replace("\\", "/")
        for path in perl_evidence[:8]
        if _is_within(path, workspace)
    )
    return WindowsBuildEnvironment(
        dependencies=("perl",),
        environment=_perl_environment(perl, base_env),
        evidence=evidence,
        perl_executable=str(perl),
    )


__all__ = [
    "WindowsBuildDependencyError",
    "WindowsBuildEnvironment",
    "prepare_windows_build_environment",
]
