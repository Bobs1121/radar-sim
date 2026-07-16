"""Windows compiler detection and narrow Selena batch-script adaptation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class WindowsToolchainError(RuntimeError):
    """The selected Selena script cannot be matched to an installed VS."""


@dataclass(frozen=True)
class VisualStudioInstallation:
    tag: str
    year: str
    toolset: str


@dataclass(frozen=True)
class VisualStudioScriptAdaptation:
    installation: VisualStudioInstallation
    changed: bool
    requested_tag: str


_VS_META = {
    "vs14": ("2015", "v140"),
    "vs15": ("2017", "v141"),
    "vs16": ("2019", "v142"),
    "vs17": ("2022", "v143"),
}
_VS_ARG_RE = re.compile(r"(?i)(?<!\S)-vs\s+(vs(?:14|15|16|17))\b")
_VS_POSTFIX_TAG_RE = re.compile(r"(?i)-vs\s+(vs(?:14|15|16|17))\b")
_R2D2_RE = re.compile(r"(?i)\bR2D2\.py\b")
_COMMENT_RE = re.compile(r"^\s*(?:@?rem(?:\s|$)|::)", re.IGNORECASE)
_POSTFIX_ASSIGN_RE = re.compile(r"^(\s*)set\s+\"?VS_POSTFIX\s*=.*$", re.IGNORECASE)
_POSTFIX_CONDITIONAL_RE = re.compile(r"^\s*if\s+exist\b.*VS_POSTFIX\s*=", re.IGNORECASE)


def _has_newer_compiler(root: Path) -> bool:
    patterns = (
        "*/VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe",
        "*/VC/Tools/MSVC/*/bin/Hostx86/x86/cl.exe",
    )
    return any(next(root.glob(pattern), None) is not None for pattern in patterns)


def detect_visual_studio_installations(
    *,
    program_files_x86: str | os.PathLike[str] | None = None,
    program_files: str | os.PathLike[str] | None = None,
) -> tuple[VisualStudioInstallation, ...]:
    """Detect usable C++ installations; Visual Studio itself remains user-managed."""
    pf86 = Path(program_files_x86 or os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    pf = Path(program_files or os.environ.get("ProgramFiles") or r"C:\Program Files")
    found: list[VisualStudioInstallation] = []
    vs2015 = pf86 / "Microsoft Visual Studio 14.0"
    if (vs2015 / "VC" / "bin" / "amd64" / "cl.exe").is_file():
        found.append(VisualStudioInstallation("vs14", "2015", "v140"))
    for tag, year in (("vs15", "2017"), ("vs16", "2019")):
        root = pf86 / "Microsoft Visual Studio" / year
        if root.is_dir() and _has_newer_compiler(root):
            found.append(VisualStudioInstallation(tag, year, _VS_META[tag][1]))
    root2022 = pf / "Microsoft Visual Studio" / "2022"
    if root2022.is_dir() and _has_newer_compiler(root2022):
        found.append(VisualStudioInstallation("vs17", "2022", "v143"))
    return tuple(found)


def _active_r2d2_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if _R2D2_RE.search(line) and not _COMMENT_RE.match(line)]


def requested_visual_studio_tag(text: str) -> str:
    """Return the explicit R2D2 VS tag, or vs14 when the script uses its default."""
    matches = [match.group(1).lower() for line in _active_r2d2_lines(text) for match in _VS_ARG_RE.finditer(line)]
    if matches:
        return matches[-1]
    postfix_matches = [
        match.group(1).lower()
        for line in text.splitlines()
        if not _COMMENT_RE.match(line) and "VS_POSTFIX" in line.upper()
        for match in _VS_POSTFIX_TAG_RE.finditer(line)
    ]
    return postfix_matches[-1] if postfix_matches else "vs14"


def _choose_installation(
    requested: str,
    installations: Iterable[VisualStudioInstallation],
) -> VisualStudioInstallation:
    available = tuple(installations)
    if not available:
        raise WindowsToolchainError(
            "No supported Visual Studio C++ compiler was found. Install Visual Studio 2015, 2017, 2019 or 2022 and retry."
        )
    exact = next((item for item in available if item.tag == requested), None)
    if exact is not None:
        return exact
    # Prefer the oldest usable compiler because legacy Selena R2D2 defaults to
    # v140 and newer VS installations do not necessarily include older toolsets.
    order = {"vs14": 0, "vs15": 1, "vs16": 2, "vs17": 3}
    return min(available, key=lambda item: order[item.tag])


def adapt_selena_script_visual_studio(
    script_path: str | os.PathLike[str],
    *,
    installations: Iterable[VisualStudioInstallation] | None = None,
) -> VisualStudioScriptAdaptation:
    """Adapt only R2D2 ``-vs``/``VS_POSTFIX`` tokens to the local compiler.

    The edit is intentionally persistent and visible in the user's current
    workspace, as requested by the product contract. It is idempotent and does
    not touch source, branch state, clean flags, build mode, or any other option.
    """
    path = Path(script_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WindowsToolchainError("The Selena build script is unavailable.") from exc
    encoding = "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = raw.decode(encoding)
    requested = requested_visual_studio_tag(text)
    selected = _choose_installation(
        requested,
        detect_visual_studio_installations() if installations is None else installations,
    )
    postfix = "" if selected.tag == "vs14" else f"-vs {selected.tag}"
    newline = "\r\n" if "\r\n" in text else "\n"
    output: list[str] = []
    base_assignment_seen = False
    for line in text.splitlines():
        if _POSTFIX_CONDITIONAL_RE.match(line):
            output.append("rem radar-sim: VS selection is validated and adapted by the Windows Agent")
            continue
        assignment = _POSTFIX_ASSIGN_RE.match(line)
        if assignment:
            if not base_assignment_seen:
                output.append(f'{assignment.group(1)}SET "VS_POSTFIX={postfix}"')
                base_assignment_seen = True
            else:
                output.append("rem radar-sim: duplicate VS_POSTFIX assignment removed")
            continue
        if _R2D2_RE.search(line) and not _COMMENT_RE.match(line):
            adapted = _VS_ARG_RE.sub("", line).rstrip()
            if "%VS_POSTFIX%" not in adapted.upper() and postfix:
                adapted += f" {postfix}"
            output.append(adapted)
            continue
        output.append(line)
    adapted_text = newline.join(output) + (newline if text.endswith(("\n", "\r")) else "")
    changed = adapted_text != text
    if changed:
        try:
            path.write_bytes(adapted_text.encode(encoding))
        except OSError as exc:
            raise WindowsToolchainError("The Selena build script could not be adapted.") from exc
    return VisualStudioScriptAdaptation(selected, changed, requested)


__all__ = [
    "VisualStudioInstallation",
    "VisualStudioScriptAdaptation",
    "WindowsToolchainError",
    "adapt_selena_script_visual_studio",
    "detect_visual_studio_installations",
    "requested_visual_studio_tag",
]
