"""Project-independent MatFilter discovery on the source computer.

Linux never walks a user's repository.  The SDK or unified Windows Connector
calls this resolver only when ``simulation.mat_filter`` is empty.  An explicit
user path always wins.  Otherwise candidates are ranked deterministically from
the user-provided paths; equal candidates never block a mature simulation run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_PRUNED_DIRS = {
    ".git", ".svn", ".hg", ".venv", "__pycache__", "node_modules",
    "build", "builds", "out", "output", "dist",
}
_BAD_MARKERS = ("template", "sample", "test", "example", "empty")


class MatFilterResolutionError(ValueError):
    def __init__(self, code: str, message: str, *, candidates: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = str(code)
        self.candidates = tuple(str(item) for item in candidates)


@dataclass(frozen=True)
class MatFilterResolution:
    path: Path
    source: str
    repository_root: Path
    candidates: tuple[str, ...] = ()


def resolve_mat_filter(
    explicit_path: str = "",
    *,
    code_path: str = "",
    existing_path: str = "",
    selena_build_script: str = "",
    runtime_xml: str = "",
) -> MatFilterResolution:
    """Resolve one MatFilter without using a project name or product table."""

    explicit = str(explicit_path or "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve(strict=True)
        if not path.is_file():
            raise MatFilterResolutionError(
                "mat_filter_unavailable", "The user-selected MatFilter is not a file"
            )
        return MatFilterResolution(path, "user", _repository_root(path.parent) or path.parent)

    roots: list[Path] = []
    # Relevance order is deliberate.  The current code workspace is strongest;
    # an existing Selena folder is next because it belongs to the selected
    # artifact.  A build-script path is weaker evidence and may be a harmless
    # stale YAML field when source=existing.  Runtime XML is last.
    for raw in (code_path, existing_path, selena_build_script, runtime_xml):
        value = str(raw or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        try:
            candidate = candidate.resolve(strict=True)
        except OSError:
            continue
        seed = candidate if candidate.is_dir() else candidate.parent
        discovered = _repository_roots(seed)
        if not discovered and raw == code_path:
            discovered = (seed,)
        for root in discovered:
            if root not in roots:
                roots.append(root)
    if not roots:
        raise MatFilterResolutionError(
            "mat_filter_repository_unavailable",
            "MatFilter was not provided and a readable code repository could not be derived",
        )

    ranked_by_path: dict[str, tuple[int, str, Path, Path]] = {}
    for root in roots:
        for path in _candidate_files(root):
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            item = (_score(relative), relative.casefold(), path, root)
            key = str(path.resolve(strict=False)).casefold()
            previous = ranked_by_path.get(key)
            if previous is None or item[0] > previous[0]:
                ranked_by_path[key] = item
    ranked = list(ranked_by_path.values())
    ranked = [item for item in ranked if item[0] >= 40]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        raise MatFilterResolutionError(
            "mat_filter_not_found",
            "No high-confidence MatFilter was found in the derived code repository",
        )

    visible = tuple(item[2].relative_to(item[3]).as_posix() for item in ranked[:10])
    # Input roots retain the public-contract order: code_path first, followed
    # by existing Selena, build script and runtime.  Python's stable sort keeps
    # that root order when score and relative path are equal.  Selecting the
    # first ranked candidate therefore gives every user the same explainable
    # result without introducing a project table or rejecting the task before
    # Selena can validate the effective configuration itself.
    selected = ranked[0]
    return MatFilterResolution(
        selected[2].resolve(strict=True),
        "repository_inference",
        selected[3].resolve(strict=True),
        candidates=visible,
    )


def _repository_root(seed: Path) -> Path | None:
    roots = _repository_roots(seed)
    return roots[0] if roots else None


def _repository_roots(seed: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (seed, *seed.parents)
        if (candidate / ".git").exists()
    )


def _candidate_files(root: Path, *, max_files: int = 200_000) -> Iterable[Path]:
    seen = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = [item for item in dirs if item.casefold() not in _PRUNED_DIRS]
        for name in files:
            seen += 1
            if seen > max_files:
                raise MatFilterResolutionError(
                    "mat_filter_search_too_large",
                    "MatFilter discovery exceeded the bounded repository search limit",
                )
            lowered = name.casefold()
            if not lowered.endswith(".filter"):
                continue
            path = Path(current) / name
            if path.is_file() and path.stat().st_size > 0:
                yield path


def _score(relative_path: str) -> int:
    value = relative_path.replace("\\", "/").casefold()
    name = value.rsplit("/", 1)[-1]
    score = 0
    if name.endswith(".mdf.mat.filter"):
        score += 50
    elif name.endswith(".filter"):
        score += 5
    if "/tools/selena/" in f"/{value}":
        score += 35
    if "/matlab_transport_cfg/" in f"/{value}":
        score += 35
    if "/selena_filter/" in f"/{value}":
        score += 15
    if "plotreco" in name:
        score += 30
    if "swx" in name:
        score += 10
    if any(marker in value for marker in _BAD_MARKERS):
        score -= 100
    return score


__all__ = ["MatFilterResolution", "MatFilterResolutionError", "resolve_mat_filter"]
