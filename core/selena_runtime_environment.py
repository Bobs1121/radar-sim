"""Infer the runtime PATH of one built Selena without product adapters.

Selena binaries commonly load Qt, MATLAB and Boost libraries that are not
copied next to ``Selena.exe``.  CMake already records the exact installations
used for that binary, so the Connector can reconstruct the execution PATH from
the nearest ``CMakeCache.txt`` instead of selecting a business project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SelenaRuntimeEnvironment:
    cache_path: Path | None = None
    path_prefix: tuple[Path, ...] = ()


def infer_selena_runtime_environment(
    hints: Iterable[str | Path],
) -> SelenaRuntimeEnvironment:
    """Return existing runtime directories recorded by the closest build.

    Hints are ordered by authority.  An existing Selena/output path should be
    supplied before a broader workspace root.  The first ancestor cache wins;
    only when no ancestor cache exists do we perform a small, bounded build
    tree search.  Missing or stale cache entries are ignored.
    """

    normalized = _normalize_hints(hints)
    cache = next(
        (candidate for hint in normalized if (candidate := _ancestor_cache(hint))),
        None,
    )
    if cache is None:
        cache = _bounded_build_cache(normalized)
    if cache is None:
        return SelenaRuntimeEnvironment()
    return SelenaRuntimeEnvironment(cache, _runtime_directories(_read_cache(cache)))


def _normalize_hints(hints: Iterable[str | Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in hints:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            continue
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            continue
        key = os.path.normcase(os.path.normpath(str(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def _ancestor_cache(start: Path) -> Path | None:
    current = start
    # A normal Selena output is only a handful of levels below the CMake build
    # root.  The bound avoids walking an arbitrary parent chain supplied by a
    # malformed path while still reaching a drive/share root safely.
    for _ in range(16):
        candidate = current / "CMakeCache.txt"
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def _bounded_build_cache(hints: tuple[Path, ...]) -> Path | None:
    candidates: list[Path] = []
    for root in hints:
        search_roots = [root / "ip_dc" / "build", root / "build"]
        for search_root in search_roots:
            if not search_root.is_dir() or search_root.is_symlink():
                continue
            for current, dirs, files in os.walk(search_root):
                current_path = Path(current)
                try:
                    depth = len(current_path.relative_to(search_root).parts)
                except ValueError:
                    continue
                dirs[:] = [] if depth >= 3 else sorted(dirs, key=str.casefold)
                if "CMakeCache.txt" in files:
                    candidate = current_path / "CMakeCache.txt"
                    if candidate.is_file() and not candidate.is_symlink():
                        candidates.append(candidate)
                        if len(candidates) >= 32:
                            break
            if candidates:
                break
        if candidates:
            break
    if not candidates:
        return None
    # A just-built workspace may contain several variants.  The latest cache
    # is the best project-independent correlation available after the selected
    # build script has completed.
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path).casefold()))


def _read_cache(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        left, value = line.split("=", 1)
        key = left.split(":", 1)[0].strip()
        value = value.strip().strip('"')
        if key and value and not value.endswith("-NOTFOUND"):
            values[key] = value
    return values


def _runtime_directories(values: dict[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []

    matlab = values.get("Matlab_ROOT_DIR", "")
    if matlab:
        candidates.append(Path(matlab) / "bin" / "win64")

    for key, value in values.items():
        if key.startswith("Qt5") and key.endswith("_DIR"):
            qt_root = _parent_before(value, "lib")
            if qt_root is not None:
                candidates.extend((qt_root / "bin", qt_root / "lib"))

    for key in ("Boost_LIBRARY_DIR_RELEASE", "Boost_LIBRARY_DIR_DEBUG"):
        if values.get(key):
            candidates.append(Path(values[key]))

    for key in ("PYTHON_EXECUTABLE", "GIT_EXECUTABLE"):
        if values.get(key):
            candidates.append(Path(values[key]).parent)

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or resolved.is_symlink():
            continue
        key = os.path.normcase(os.path.normpath(str(resolved)))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return tuple(result)


def _parent_before(value: str, marker: str) -> Path | None:
    path = Path(value)
    marker = marker.casefold()
    for parent in (path, *path.parents):
        if parent.name.casefold() == marker:
            return parent.parent
    return None


__all__ = ["SelenaRuntimeEnvironment", "infer_selena_runtime_environment"]
