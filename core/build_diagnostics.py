"""Stable extraction of actionable compiler/build failures from noisy logs."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable


_STRONG_ERROR = re.compile(
    r"(?:fatal error|\berror\s+(?:C|LNK|MSB)\d+\b|\bFAILED:|undefined reference|"
    r"cannot open (?:file|include)|no such file|could not find any instance of\s+Visual Studio)",
    re.IGNORECASE,
)
_GENERIC_ERROR = re.compile(r"(?:\bR2D2 execution failed\b|\bFailed to run (?:make|cmake)\b)", re.IGNORECASE)

_SOURCE_EXCEPTION_SPEC = re.compile(
    r"\berror\s+C2382\b.*(?:redefinition|exception specifications)", re.IGNORECASE
)
_MISSING_INCLUDE = re.compile(r"(?:fatal error\s+C1083|cannot open include file|no such file)", re.IGNORECASE)
_MISSING_LIBRARY = re.compile(r"(?:fatal error\s+LNK1104|cannot open file).*\.(?:lib|dll)\b", re.IGNORECASE)
_LINKER = re.compile(r"\b(?:fatal error\s+)?LNK\d+\b|undefined reference", re.IGNORECASE)
_TOOLCHAIN = re.compile(
    r"(?:not recognized as an internal or external command|command not found|"
    r"could not find (?:cmake|compiler|toolchain)|could not find any instance of\s+Visual Studio|MSB8020)",
    re.IGNORECASE,
)
_VISUAL_STUDIO = re.compile(
    r"(?:could not find any instance of\s+Visual Studio|Visual Studio\s+(?:14|15|16|17|2015|2017|2019|2022).*not found)",
    re.IGNORECASE,
)
_GENERATED_HEADER = re.compile(
    r"(?:cannot open include file|no such file).*?(?:_gen|_generated)\.h\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BuildDiagnostic:
    """Stable, UI-safe classification for one failed build.

    ``code`` is intended for Web/SDK branching.  The detail is the first real
    compiler/linker error, never the often misleading outer R2D2 wrapper.
    """

    code: str
    category: str
    summary: str
    action: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def extract_actionable_build_errors(lines: Iterable[str], *, limit: int = 20) -> list[str]:
    """Return de-duplicated compiler/linker errors before generic wrappers."""
    strong: list[str] = []
    generic: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        line = str(raw or "").strip()
        if not line:
            continue
        target = strong if _STRONG_ERROR.search(line) else generic if _GENERIC_ERROR.search(line) else None
        if target is None:
            continue
        canonical = re.sub(r"^.*?\[R2D2 \(make\)\]\s*", "", line).strip()
        if canonical.casefold() in seen:
            continue
        seen.add(canonical.casefold())
        target.append(canonical)
    return (strong + generic)[: max(1, int(limit))]


def classify_build_failure(lines: Iterable[str]) -> BuildDiagnostic:
    """Classify a noisy failed build without blaming the user environment.

    The order is deliberate: concrete source/compiler errors win over generic
    wrappers.  Unknown failures remain actionable by asking for the preserved
    log, rather than inventing a missing dependency.
    """
    errors = extract_actionable_build_errors(lines)
    detail = errors[0] if errors else "Build exited without an actionable compiler or linker error"
    if _VISUAL_STUDIO.search(detail):
        return BuildDiagnostic(
            code="VISUAL_STUDIO_UNAVAILABLE",
            category="environment",
            summary="The Selena build script selected a Visual Studio version that is not installed",
            action="Let the Windows Agent adapt the Selena script to the installed Visual Studio version, then retry",
            detail=detail,
        )
    if _SOURCE_EXCEPTION_SPEC.search(detail):
        return BuildDiagnostic(
            code="SOURCE_EXCEPTION_SPEC_MISMATCH",
            category="source",
            summary="Selena source declarations and definitions use different exception specifications",
            action="Fix the reported declaration/definition mismatch in the Selena branch, then rebuild",
            detail=detail,
        )
    if _GENERATED_HEADER.search(detail):
        return BuildDiagnostic(
            code="GENERATED_SOURCE_MISSING",
            category="generated_dependency",
            summary="A generated source header required by Selena is missing",
            action="Run the code-generation step discovered from the software-package build scripts, then retry",
            detail=detail,
        )
    if _MISSING_INCLUDE.search(detail):
        return BuildDiagnostic(
            code="SOURCE_OR_INCLUDE_DEPENDENCY_MISSING",
            category="source_or_dependency",
            summary="A required source header could not be opened",
            action="Check the reported include and the branch/toolcollection dependency mapping",
            detail=detail,
        )
    if _MISSING_LIBRARY.search(detail):
        return BuildDiagnostic(
            code="LINK_LIBRARY_MISSING",
            category="dependency",
            summary="A required link library could not be opened",
            action="Check the reported library and the selected Selena toolcollection",
            detail=detail,
        )
    if _LINKER.search(detail):
        return BuildDiagnostic(
            code="LINK_FAILED",
            category="source_or_dependency",
            summary="Selena linking failed",
            action="Inspect the first linker error and verify the branch's linked components",
            detail=detail,
        )
    if _TOOLCHAIN.search(detail):
        return BuildDiagnostic(
            code="TOOLCHAIN_UNAVAILABLE",
            category="environment",
            summary="The configured Selena toolchain is unavailable",
            action="Run environment repair for the reported tool, then retry",
            detail=detail,
        )
    return BuildDiagnostic(
        code="BUILD_FAILED",
        category="unknown",
        summary="Selena build failed",
        action="Open the preserved build log and fix the first reported failure before retrying",
        detail=detail,
    )


__all__ = ["BuildDiagnostic", "classify_build_failure", "extract_actionable_build_errors"]
