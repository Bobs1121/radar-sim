#!/usr/bin/env python3
"""Read-only discovery of radar-sim configuration candidates.

The script intentionally reports names, paths, and small metadata only. It
does not read source, Runtime XML, MF4, Selena, or result file contents and it
never mutates the repository.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any


BUILD_SCRIPT_NAMES = {
    "jenkins_selena_build.bat",
    "build_selena.bat",
    "selena_build.bat",
    "jenkins_selena_build.cmd",
    "build_selena.cmd",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
GENERATED_DATA_DIRS = {"outputs", "results", "result", "logs", "tmp", "temp"}


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _git(root: Path, *, include_dirty: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "root": str(root), "branch": "", "dirty": None}
    try:
        git_root = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if git_root.returncode != 0:
            return result
        resolved_root = Path(git_root.stdout.strip()).resolve()
        result["available"] = True
        result["root"] = str(resolved_root)
        try:
            branch = subprocess.run(
                ["git", "-C", str(resolved_root), "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if branch.returncode == 0:
                result["branch"] = branch.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        if include_dirty:
            try:
                status = subprocess.run(
                    ["git", "-C", str(resolved_root), "status", "--porcelain=v1", "--untracked-files=no"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if status.returncode == 0:
                    result["dirty"] = bool(status.stdout)
            except (OSError, subprocess.SubprocessError):
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _walk(
    root: Path,
    *,
    max_depth: int,
    max_entries: int,
    exclude_generated_data: bool = False,
) -> tuple[list[Path], bool]:
    items: list[Path] = []
    truncated = False
    root_depth = len(root.parts)
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        directories[:] = [
            item
            for item in directories
            if item not in SKIP_DIRS
            and not (
                exclude_generated_data
                and (item.casefold() in GENERATED_DATA_DIRS or item.casefold().startswith("job_"))
            )
        ]
        if depth >= max_depth:
            directories[:] = []
        for name in sorted(filenames, key=str.casefold):
            items.append(current_path / name)
            if len(items) >= max_entries:
                truncated = True
                return items, truncated
    return items, truncated


def _priority_files(root: Path, *, max_depth: int, max_directories: int) -> tuple[list[Path], bool]:
    """Find high-value build outputs without counting every source file first."""

    priority_names = {
        "build",
        "out",
        "output",
        "bin",
        "release",
        "debug",
        "relwithdebinfo",
        "selena",
    }
    candidates: list[Path] = []
    visited = 0
    root_depth = len(root.parts)
    truncated = False
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        visited += 1
        if visited > max_directories:
            truncated = True
            break
        depth = len(current_path.parts) - root_depth
        directories[:] = [item for item in directories if item not in SKIP_DIRS]
        if depth >= max_depth:
            directories[:] = []
        parts = {item.casefold() for item in current_path.parts}
        if current_path.name.casefold() in priority_names or "selena" in parts:
            for name in filenames:
                if name.casefold() == "selena.exe":
                    candidates.append(current_path / name)
    return candidates, truncated


def _priority_named_files(root: Path, *, max_depth: int, max_directories: int) -> tuple[list[Path], bool]:
    """Find named scripts and Runtime XML independently of source-file volume."""

    candidates: list[Path] = []
    visited = 0
    root_depth = len(root.parts)
    truncated = False
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        visited += 1
        if visited > max_directories:
            truncated = True
            break
        depth = len(current_path.parts) - root_depth
        directories[:] = [item for item in directories if item not in SKIP_DIRS]
        if depth >= max_depth:
            directories[:] = []
        for name in filenames:
            lowered = name.casefold()
            if lowered in BUILD_SCRIPT_NAMES or (
                lowered.endswith(".xml") and ("runtime" in lowered or lowered.startswith("rt_"))
            ):
                candidates.append(current_path / name)
    return candidates, truncated


def _nested_git_repositories(root: Path, *, max_directories: int) -> list[Path]:
    """Return nested Git worktrees when *root* itself is not a repository."""

    repositories: list[Path] = []
    visited = 0
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        visited += 1
        if visited > max_directories:
            break
        directories[:] = [item for item in directories if item not in SKIP_DIRS]
        candidate = Path(current)
        git_marker = candidate / ".git"
        if ".git" in filenames or ".git" in directories or git_marker.is_dir() or git_marker.is_file():
            if candidate.resolve() != root.resolve():
                repositories.append(candidate)
                directories[:] = []
    return repositories


def _file_info(path: Path, root: Path, *, kind: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {"path": str(path), "relative_path": _relative(path, root), "kind": kind, "size": size}


def discover(root: Path, data_root: Path | None, *, max_depth: int, max_entries: int) -> dict[str, Any]:
    root = root.expanduser().resolve()
    files, truncated = _walk(root, max_depth=max_depth, max_entries=max_entries)
    priority, priority_truncated = _priority_files(
        root,
        max_depth=max_depth,
        max_directories=max_entries,
    )
    files.extend(item for item in priority if item not in files)
    truncated = truncated or priority_truncated
    named, named_truncated = _priority_named_files(
        root,
        max_depth=max_depth,
        max_directories=max_entries,
    )
    files.extend(item for item in named if item not in files)
    truncated = truncated or named_truncated
    git = _git(root)
    nested = _nested_git_repositories(root, max_directories=max_entries)
    git["repositories"] = [
        {"path": str(item), **_git(item, include_dirty=False)}
        for item in nested
    ]
    build_scripts: list[dict[str, Any]] = []
    runtime_xml: list[dict[str, Any]] = []
    selena: dict[str, dict[str, Any]] = {}
    for path in files:
        lowered = path.name.casefold()
        if lowered in BUILD_SCRIPT_NAMES:
            build_scripts.append(_file_info(path, root, kind="selena_build_script"))
        if path.suffix.casefold() == ".xml" and ("runtime" in lowered or lowered.startswith("rt_")):
            runtime_xml.append(_file_info(path, root, kind="runtime_xml"))
        if lowered == "selena.exe":
            parent_key = os.path.normcase(str(path.parent.resolve()))
            try:
                dll_count = sum(
                    1
                    for sibling in path.parent.iterdir()
                    if sibling.is_file() and sibling.suffix.casefold() == ".dll"
                )
            except OSError:
                dll_count = None
            item = selena.setdefault(
                parent_key,
                {
                    "path": str(path.parent),
                    "relative_path": _relative(path.parent, root),
                    "executable": str(path),
                    "dll_count": dll_count,
                },
            )
    data_candidates: list[dict[str, Any]] = []
    if data_root is not None:
        data_root = data_root.expanduser().resolve()
        data_files, data_truncated = _walk(
            data_root,
            max_depth=max_depth,
            max_entries=max_entries,
            exclude_generated_data=True,
        )
        truncated = truncated or data_truncated
        direct_counts: dict[Path, int] = {}
        for path in data_files:
            if path.suffix.casefold() != ".mf4" or path.name.casefold().endswith("out.mf4"):
                continue
            data_candidates.append(_file_info(path, data_root, kind="mf4_file"))
            direct_counts[path.parent] = direct_counts.get(path.parent, 0) + 1
        for directory, count in sorted(direct_counts.items(), key=lambda item: str(item[0]).casefold()):
            data_candidates.append(
                {
                    "path": str(directory),
                    "relative_path": _relative(directory, data_root),
                    "kind": "mf4_directory",
                    "mf4_count": count,
                }
            )
    return {
        "root": str(root),
        "git": git,
        "build_scripts": sorted(build_scripts, key=lambda item: item["path"].casefold()),
        "runtime_xml": sorted(runtime_xml, key=lambda item: item["path"].casefold()),
        "selena_outputs": sorted(selena.values(), key=lambda item: item["path"].casefold()),
        "data_candidates": data_candidates,
        "truncated": truncated,
        "warnings": ["discovery_bound_reached; unresolved candidates require user confirmation"] if truncated else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Code/repository root to inspect")
    parser.add_argument("--data-root", default="", help="Optional data root to inspect for MF4 candidates")
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-entries", type=int, default=50000)
    args = parser.parse_args()
    if args.max_depth < 0 or args.max_entries < 1:
        parser.error("max-depth must be non-negative and max-entries must be positive")
    result = discover(
        Path(args.root),
        Path(args.data_root) if args.data_root else None,
        max_depth=args.max_depth,
        max_entries=args.max_entries,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
