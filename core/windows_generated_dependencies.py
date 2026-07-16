"""Safe preparation of generated headers declared by package build scripts."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


class GeneratedDependencyError(RuntimeError):
    """A recognized workspace-local generator could not be prepared or run."""


@dataclass(frozen=True)
class GeneratedDependencyResult:
    generator: str = ""
    generated_targets: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.generated_targets)


_PAD_COMMAND_RE = re.compile(
    r"^\s*set\s+\"?PAD_COMMOND=.*?-I\s+(\S+)\s+(\S*pad_generator\.pl)\s+-p",
    re.IGNORECASE | re.MULTILINE,
)
_PAD_TARGET_RE = re.compile(
    r"^\s*%PAD_COMMOND%\s+(\S+)\s+-b\s+(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _within_workspace(raw: str, *, script_dir: Path, workspace: Path) -> Path:
    expanded = re.sub(r"%cp%", lambda _match: str(script_dir) + os.sep, raw.strip().strip('"'), flags=re.I)
    if "%" in expanded or "!" in expanded:
        raise GeneratedDependencyError("The package generator contains an unresolved path variable.")
    path = Path(expanded.replace("/", os.sep))
    if not path.is_absolute():
        path = script_dir / path
    path = path.resolve(strict=False)
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise GeneratedDependencyError("The package generator points outside the authorized workspace.") from exc
    return path


def _find_named_script(directory: Path, name: str) -> Path | None:
    try:
        return next((item for item in directory.iterdir() if item.is_file() and item.name.casefold() == name.casefold()), None)
    except OSError:
        return None


def _find_perl(env: Mapping[str, str]) -> Path | None:
    candidates: list[Path] = []
    configured = str(env.get("TCCPATH_perl") or env.get("TCCPATH_PERL") or "").strip().strip('"')
    if configured:
        base = Path(configured)
        candidates.extend((base / "perl" / "bin" / "perl.exe", base / "bin" / "perl.exe"))
    candidates.append(Path(r"C:\Perl\bin\perl.exe"))
    tcc_root = Path(r"C:\TCC\Tools\perl")
    if tcc_root.is_dir():
        candidates.extend(sorted(tcc_root.glob("*/perl/bin/perl.exe"), reverse=True))
    return next((path.resolve(strict=False) for path in candidates if path.is_file()), None)


def prepare_package_generated_dependencies(
    package_script_path: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str],
    *,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> GeneratedDependencyResult:
    """Run a recognized non-interactive PAD generator only when outputs are absent.

    The selected package script remains the source of truth.  We only recognize
    its sibling ``GEN_PAD_PARAMS.bat`` contract and execute the workspace-local
    Perl generator directly, avoiding the interactive package build wrapper and
    its legacy hard-coded ``C:\\Perl`` path.
    """
    package_script = Path(package_script_path).resolve(strict=False)
    workspace = Path(workspace_root).resolve(strict=False)
    generator_script = _find_named_script(package_script.parent, "GEN_PAD_PARAMS.bat")
    if generator_script is None:
        return GeneratedDependencyResult()
    try:
        text = generator_script.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GeneratedDependencyError("The package code-generation script could not be read.") from exc
    command_match = _PAD_COMMAND_RE.search(text)
    targets = _PAD_TARGET_RE.findall(text)
    if command_match is None or not targets:
        return GeneratedDependencyResult()

    script_dir = generator_script.parent
    include_dir = _within_workspace(command_match.group(1), script_dir=script_dir, workspace=workspace)
    generator = _within_workspace(command_match.group(2), script_dir=script_dir, workspace=workspace)
    if not include_dir.is_dir() or not generator.is_file():
        raise GeneratedDependencyError("The package PAD generator dependency is missing from the workspace.")

    resolved_targets: list[tuple[Path, Path]] = []
    for target_raw, xml_raw in targets:
        target = _within_workspace(target_raw, script_dir=script_dir, workspace=workspace)
        xml = _within_workspace(xml_raw, script_dir=script_dir, workspace=workspace)
        if target.is_dir() and xml.is_file() and not any(target.glob("*_gen.h")):
            resolved_targets.append((target, xml))
    if not resolved_targets:
        return GeneratedDependencyResult(generator=str(generator))

    child_env = dict(os.environ if env is None else env)
    perl = _find_perl(child_env)
    if perl is None:
        raise GeneratedDependencyError(
            "The package requires Perl for generated headers, but no usable C:\\Perl or TCC Perl installation was found."
        )
    strawberry_root = perl.parents[2] if len(perl.parents) > 2 and perl.parent.name.casefold() == "bin" else perl.parent
    path_entries = [str(strawberry_root / "c" / "bin"), str(perl.parent), str(child_env.get("PATH") or "")]
    child_env["PATH"] = os.pathsep.join(item for item in path_entries if item)

    generated: list[str] = []
    for target, xml in resolved_targets:
        completed = runner(
            [str(perl), "-I", str(include_dir), str(generator), "-p", str(target), "-b", str(xml)],
            cwd=str(workspace),
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if int(completed.returncode) != 0:
            detail = str(completed.stderr or completed.stdout or "").strip().splitlines()
            raise GeneratedDependencyError(detail[-1] if detail else "Package code generation failed.")
        generated.append(str(target.relative_to(workspace)).replace("\\", "/"))
    return GeneratedDependencyResult(generator=str(generator), generated_targets=tuple(generated))


__all__ = [
    "GeneratedDependencyError",
    "GeneratedDependencyResult",
    "prepare_package_generated_dependencies",
]
