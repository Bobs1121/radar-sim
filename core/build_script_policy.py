"""Generic safety policy for Windows build wrapper scripts.

The public run-config contract owns the build entry script, but the framework
still owns the safety policy around that entry point.  In particular, a
framework-managed build is incremental by default: an embedded ``clean``
command must not silently delete a previously built workspace.

This module deliberately recognizes command semantics rather than project
names or directory layouts.  It is bounded to the selected script and its
continuation lines; it never executes or scans an entire repository.
"""

from __future__ import annotations

import os
import re
import locale
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


class BuildScriptPolicyError(ValueError):
    """The selected build script cannot be safely adapted."""


@dataclass(frozen=True)
class IncrementalBuildScriptAdaptation:
    """Evidence about clean commands found in one selected build script."""

    changed: bool
    clean_command_lines: tuple[int, ...] = ()
    suppressed_lines: tuple[int, ...] = ()
    existing_build_detected: bool = False
    explicit_clean_requested: bool = False


_COMMENT_RE = re.compile(r"^\s*(?:@?rem(?:\s|$)|::|#)", re.IGNORECASE)
_ECHO_OR_ASSIGNMENT_RE = re.compile(r"^\s*(?:@?echo|set)(?:\s|$)", re.IGNORECASE)
_R2D2_CLEAN_RE = re.compile(
    r"\bR2D2\.py\b.*(?<![\w-])--?clean(?=$|[\s\"'&|])", re.IGNORECASE
)
_CMAKE_CLEAN_RE = re.compile(
    r"\bcmake\b.*(?:--clean-first|--target\s*[=\s]\s*clean\b|--target\s+clean\b)",
    re.IGNORECASE,
)
_MSBUILD_CLEAN_RE = re.compile(
    r"\b(?:msbuild|devenv)\b.*(?:[/\-](?:t|target)\s*[:=]\s*clean\b|/target\s*[:=]\s*clean\b)",
    re.IGNORECASE,
)
_KNOWN_BUILD_TOOL_RE = re.compile(
    r"\b(?:cmake|msbuild|devenv|ninja|make|mingw32-make|gmake|gradle|mvn|bazel|"
    r"python(?:3)?|py|powershell|pwsh)\b",
    re.IGNORECASE,
)
_CLEAN_FLAG_RE = re.compile(r"(?<![\w-])--?clean(?=$|[\s\"'&|])", re.IGNORECASE)
_CLEAN_TOKEN_RE = re.compile(r"(?<![\w-])clean(?=$|[\s\"'&|])", re.IGNORECASE)
_CLEAN_SCRIPT_RE = re.compile(
    r"(?:^|[\s\"'/\\])clean\.(?:bat|cmd|py|ps1|sh)\b", re.IGNORECASE
)
_GIT_CLEAN_RE = re.compile(r"\bgit\s+clean\b", re.IGNORECASE)
_DESTRUCTIVE_OUTPUT_CLEAN_RE = re.compile(
    r"(?:\b(?:rmdir|rd|del|erase|remove-item|rm)\b)"
    r"(?=.*(?:build|out(?:put)?|target|cmake|selena|artifact|generated|bin))",
    re.IGNORECASE,
)
_SUPPRESSION_TEXT = (
    "radar-sim: clean command disabled; use explicit clean=true to allow a clean build | "
)
_BATCH_SUPPRESSION_PREFIX = ("rem " + _SUPPRESSION_TEXT).casefold()
_HASH_SUPPRESSION_PREFIX = ("# " + _SUPPRESSION_TEXT).casefold()
_SUPPRESSION_PREFIXES = (_BATCH_SUPPRESSION_PREFIX, _HASH_SUPPRESSION_PREFIX)


def _decode_script(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", raw.decode("utf-8-sig")
    try:
        return "utf-8", raw.decode("utf-8")
    except UnicodeDecodeError:
        # Windows build wrappers are frequently saved by an editor using the
        # active Chinese code page.  Try the current locale and GB18030 before
        # the Western fallback; otherwise a harmless non-ASCII comment can
        # make the policy rewrite fail or corrupt the script.
        encodings = [
            locale.getpreferredencoding(False),
            "gb18030",
            "cp936",
            "cp1252",
        ]
        for encoding in dict.fromkeys(item for item in encodings if item):
            try:
                return encoding, raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return "latin-1", raw.decode("latin-1")


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\r", "\n")):
        return line[:-1], line[-1]
    return line, ""


def _active_command_text(text: str) -> str:
    stripped = text.lstrip()
    if not stripped or _COMMENT_RE.match(stripped):
        return ""
    # ``echo Cleaning ...`` and ``set CLEAN=...`` are not destructive by
    # themselves.  Keeping them out avoids false positives in generic scripts.
    if _ECHO_OR_ASSIGNMENT_RE.match(stripped.lstrip("@")):
        return ""
    return stripped


def is_clean_command(text: str) -> bool:
    """Return whether an active batch command has high-confidence clean semantics."""

    active = _active_command_text(text)
    if not active:
        return False
    if _R2D2_CLEAN_RE.search(active):
        return True
    if _CMAKE_CLEAN_RE.search(active) or _MSBUILD_CLEAN_RE.search(active):
        return True
    if (
        _CLEAN_SCRIPT_RE.search(active)
        or _GIT_CLEAN_RE.search(active)
        or _DESTRUCTIVE_OUTPUT_CLEAN_RE.search(active)
    ):
        return True
    # Generic build tools use a standalone ``clean`` target/argument.  This
    # catches arbitrary projects while avoiding names such as ``cleanup.py``.
    return bool(
        _KNOWN_BUILD_TOOL_RE.search(active)
        and (_CLEAN_FLAG_RE.search(active) or _CLEAN_TOKEN_RE.search(active))
    )


def _logical_command_blocks(lines: list[str]) -> list[tuple[tuple[int, ...], str]]:
    blocks: list[tuple[tuple[int, ...], str]] = []
    index = 0
    while index < len(lines):
        indices = [index]
        parts = [_split_line_ending(lines[index])[0]]
        while True:
            body, _newline = _split_line_ending(lines[indices[-1]])
            if not body.rstrip().endswith("^") or indices[-1] + 1 >= len(lines):
                break
            next_index = indices[-1] + 1
            indices.append(next_index)
            parts.append(_split_line_ending(lines[next_index])[0])
        blocks.append((tuple(item + 1 for item in indices), " ".join(parts)))
        index = indices[-1] + 1
    return blocks


def _comment_line(line: str, script_path: Path) -> str:
    body, newline = _split_line_ending(line)
    indent = body[: len(body) - len(body.lstrip())]
    prefix = (
        "# "
        if script_path.suffix.casefold() in {
            ".ps1", ".py", ".sh", ".bash", ".zsh", ".fish", ".mk", ".cmake",
        }
        else "rem "
    )
    return (
        indent
        + (prefix + _SUPPRESSION_TEXT)
        + body.lstrip()
        + newline
    )


def _restore_suppressed_line(line: str) -> str:
    """Restore one line previously disabled by this policy.

    Explicit ``clean=true`` must be reversible.  Without this restoration a
    developer would have to manually edit the build wrapper after the first
    incremental run, which is exactly the kind of hidden state that causes
    deployment-only surprises.
    """

    body, newline = _split_line_ending(line)
    indent = body[: len(body) - len(body.lstrip())]
    candidate = body.lstrip()
    folded = candidate.casefold()
    for prefix in _SUPPRESSION_PREFIXES:
        if folded.startswith(prefix):
            original = candidate[len(prefix):]
            return indent + original + newline
    return line


def has_existing_build_artifact(
    artifact_path: str | os.PathLike[str] | None,
    output_roots: Iterable[str | os.PathLike[str]] = (),
    *,
    max_candidates: int = 512,
) -> bool:
    """Detect an existing non-empty Selena artifact without a repo-wide scan."""

    candidates: list[Path] = []
    if artifact_path:
        candidates.append(Path(artifact_path))
    candidates.extend(Path(value) for value in output_roots if value)
    seen_roots: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=False)
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
                return True
            if not path.is_dir() or path.is_symlink():
                continue
            root_key = os.path.normcase(str(path))
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            inspected = 0
            for item in path.rglob("*"):
                inspected += 1
                if inspected > max_candidates:
                    break
                try:
                    if (
                        item.name.casefold() == "selena.exe"
                        and item.is_file()
                        and not item.is_symlink()
                        and item.stat().st_size > 0
                    ):
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def adapt_build_script_for_incremental(
    script_path: str | os.PathLike[str],
    *,
    existing_build: bool = False,
    allow_clean: bool = False,
) -> IncrementalBuildScriptAdaptation:
    """Disable active clean commands unless the caller explicitly allows clean.

    The ``existing_build`` evidence is retained in the result for observability
    and policy diagnostics.  The default is deliberately safe even when the
    expected executable is not yet present: an accidental clean command should
    never be able to erase a workspace merely because artifact discovery was
    incomplete.  ``allow_clean`` is only true for an explicit clean request.
    """

    path = Path(script_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuildScriptPolicyError("The Selena build script is unavailable for incremental policy inspection.") from exc
    encoding, text = _decode_script(raw)
    lines = text.splitlines(keepends=True)

    if allow_clean:
        restored = [_restore_suppressed_line(line) for line in lines]
        restored_text = "".join(restored)
        if restored_text != text:
            try:
                path.write_bytes(restored_text.encode(encoding))
            except OSError as exc:
                raise BuildScriptPolicyError(
                    "The Selena build script could not restore its explicit clean command."
                ) from exc
            text = restored_text
            lines = restored
            restored_changed = True
        else:
            restored_changed = False
    else:
        restored_changed = False

    found: list[int] = []
    for line_numbers, command in _logical_command_blocks(lines):
        if is_clean_command(command):
            found.extend(line_numbers)

    found_tuple = tuple(sorted(set(found)))
    if not found_tuple or allow_clean:
        return IncrementalBuildScriptAdaptation(
            changed=restored_changed,
            clean_command_lines=found_tuple,
            existing_build_detected=bool(existing_build),
            explicit_clean_requested=bool(allow_clean),
        )

    changed_lines = set(found_tuple)
    adapted = [
        _comment_line(line, path) if index + 1 in changed_lines else line
        for index, line in enumerate(lines)
    ]
    adapted_text = "".join(adapted)
    if adapted_text == text:
        return IncrementalBuildScriptAdaptation(
            changed=False,
            clean_command_lines=found_tuple,
            existing_build_detected=bool(existing_build),
            explicit_clean_requested=False,
        )
    try:
        path.write_bytes(adapted_text.encode(encoding))
    except OSError as exc:
        raise BuildScriptPolicyError(
            "The Selena build script could not be adapted to incremental mode."
        ) from exc
    return IncrementalBuildScriptAdaptation(
        changed=True,
        clean_command_lines=found_tuple,
        suppressed_lines=found_tuple,
        existing_build_detected=bool(existing_build),
        explicit_clean_requested=False,
    )


def adapt_configured_selena_build_script(
    config: Mapping[str, Any],
    *,
    mode: str,
    allow_clean: bool = False,
) -> IncrementalBuildScriptAdaptation | None:
    """Apply the same policy to a legacy/config-driven Selena build entry.

    This helper intentionally consumes semantic config keys only.  It does not
    know any product name, checkout path, or Jenkins filename, so the legacy
    CLI and background build registry cannot silently bypass the policy for a
    different project.
    """

    build = dict(config.get("build") or {})
    script = str(build.get("selena_build_script") or config.get("selena_build_script") or "").strip()
    if not script:
        return None
    path = Path(script)
    if not path.is_file() or path.is_symlink():
        return None

    artifact_path: str = ""
    try:
        from core.config import resolve_selena_executable

        artifact_path = str(resolve_selena_executable(dict(config), build_mode=mode) or "")
    except Exception:
        # Artifact resolution is evidence only.  The clean suppression remains
        # fail-safe even when a project has not produced its first executable.
        artifact_path = ""

    output_roots: list[str | os.PathLike[str]] = []
    semantic_keys = (
        "output_root",
        "output_roots",
        "build_output",
        "build_output_root",
        "build_output_dir",
        "selena_build_dir",
        "build_dir",
    )
    for container in (config, build, dict(config.get("paths") or {})):
        for key in semantic_keys:
            value = container.get(key)
            if isinstance(value, (list, tuple, set)):
                output_roots.extend(item for item in value if item)
            elif value:
                output_roots.append(value)
    existing = has_existing_build_artifact(artifact_path, output_roots)
    return adapt_build_script_for_incremental(
        path,
        existing_build=existing,
        allow_clean=bool(allow_clean),
    )


__all__ = [
    "BuildScriptPolicyError",
    "IncrementalBuildScriptAdaptation",
    "adapt_build_script_for_incremental",
    "adapt_configured_selena_build_script",
    "has_existing_build_artifact",
    "is_clean_command",
]
