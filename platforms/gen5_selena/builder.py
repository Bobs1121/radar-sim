"""
Gen5 Builder — 统一构建入口.

Supports two build modes:
- selena: Compile simulation environment (R2D2.py / jenkins_selena_build.bat)
- hex: Compile firmware hex (testbuild_BaseC0S_SINGLE.bat)
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from core.models import BuildOptions, BuildResult


class Gen5Builder:
    """Unified builder — delegates to SelenaBuilder or HexBuilder."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def build(self, options: BuildOptions) -> BuildResult:
        """Build selena simulation environment (default)."""
        from .selena_builder import SelenaBuilder
        builder = SelenaBuilder(self.config)
        return builder.build(options)

    def build_hex(self, options: BuildOptions) -> BuildResult:
        """Build HEX firmware."""
        from .hex_builder import HexBuilder
        builder = HexBuilder(self.config)
        return builder.build(options)

    # ------------------------------------------------------------------
    # Environment checks (B1-B5)
    # ------------------------------------------------------------------

    def check_environment(self) -> list[str]:
        """Check build prerequisites."""
        issues: list[str] = []

        # B2: BOOST_ROOT
        boost_root = self._get_boost_root()
        if not boost_root or (not _is_deferred_env_path(boost_root) and not Path(boost_root).exists()):
            issues.append(f"B2: BOOST_ROOT not found or invalid: {boost_root}")

        # B3: python3
        py3 = self.config.get("environment", {}).get("python3_path", "")
        if py3 and not Path(py3).exists():
            issues.append(f"B3: python3.exe not found: {py3}")

        # R2D2 script
        r2d2 = self.config.get("paths", {}).get("r2d2_script", "")
        if r2d2 and not Path(r2d2).exists():
            issues.append(f"Build: R2D2.py not found: {r2d2}")

        # Build config
        bc = self.config.get("paths", {}).get("build_config", "")
        project_root = self.config.get("project_root", "")
        resolved_bc = _resolve_config_path(bc, project_root) if bc else ""
        if bc and not Path(resolved_bc).exists():
            issues.append(f"Build: build_config not found: {bc}")

        # Git submodule check
        sub_issues = self._check_submodules()
        issues.extend(sub_issues)

        return issues

    def _check_cmake_generator(self) -> Optional[str]:
        """B1: Check CMakeCache.txt generator consistency."""
        build_output = self.config.get("paths", {}).get("build_output", "")
        cache = Path(build_output) / "CMakeCache.txt"
        if not cache.exists():
            return None
        try:
            text = cache.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("CMAKE_GENERATOR:"):
                    gen = line.split(":", 1)[1].strip()
                    if "Visual Studio 14" in gen or "Visual Studio 12" in gen:
                        return f"Old CMake generator: {gen}. Clean build dir."
                    break
        except OSError:
            pass
        return None

    def _check_submodules(self) -> list[str]:
        """Check if submodules are properly initialized."""
        issues = []
        root = self.config.get("project_root", "")
        if not root:
            return issues
        try:
            result = subprocess.run(
                ["git", "-C", root, "submodule", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("-"):
                        name = line.split()[1] if len(line.split()) > 1 else "?"
                        issues.append(f"Git: Submodule '{name}' not initialized")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return issues

    def find_selena_exe(
        self, build_output: Optional[str] = None, build_mode: str = "RelWithDebInfo"
    ) -> Optional[str]:
        """Locate the selena.exe compilation artifact."""
        if build_output is None:
            build_output = self.config["paths"]["build_output"]
        pattern = self.config.get("selena", {}).get(
            "exe_pattern", "dc_tools/selena/core/{build_mode}/selena.exe"
        )
        exe_name = self.config.get("selena", {}).get("executable_name", "selena.exe")
        exe_dir = os.path.join(build_output, pattern.format(build_mode=build_mode))
        return os.path.join(exe_dir, exe_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_boost_root(self) -> str:
        return self.config.get("environment", {}).get("boost_root", "")


# ------------------------------------------------------------------
# Shared helpers used by selena_builder and cli/build
# ------------------------------------------------------------------

def _build_env_full(config: dict) -> dict[str, str]:
    """Assemble full environment for R2D2/selena subprocess."""
    import shutil
    env = os.environ.copy()
    environment = config.get("environment", {})

    boost = config.get("boost_root", "") or environment.get("boost_root", "")
    if boost:
        env["BOOST_ROOT"] = boost

    py3 = environment.get("python3_path", "")
    selena_env = environment.get("selena_env_path", "")
    qt_path = environment.get("qt_path", "")
    matlab_root = environment.get("matlab_root", "")

    path_parts: list[str] = []
    if py3:
        path_parts.append(os.path.dirname(py3))
    if selena_env:
        path_parts.append(os.path.join(selena_env, "MSYS", "mingw64", "bin"))
    if qt_path:
        path_parts.append(os.path.join(qt_path, "bin"))
        path_parts.append(os.path.join(qt_path, "lib"))
    if matlab_root:
        path_parts.append(os.path.join(matlab_root, "bin", "win64"))
    if boost:
        path_parts.append(os.path.join(boost, "lib64-msvc-14.0"))

    existing_path = env.get("PATH", "")
    path_parts.append(existing_path)
    seen = set()
    deduped = []
    for p in path_parts:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            deduped.append(p)
    env["PATH"] = os.pathsep.join(deduped)
    return env


def _resolve_config_path(build_config: str, project_root: str) -> str:
    """Resolve build_config to a full .config file path."""
    if os.path.isabs(build_config) and os.path.exists(build_config):
        return build_config

    config_name = build_config
    if config_name.endswith(".config"):
        config_name = config_name[:-7]

    candidates = [
        os.path.join(project_root, "apl", "byd", "selena", "cmake_build_cfg", f"{config_name}.config"),
        os.path.join(project_root, "apl", "byd", "selena", "config", "cmake", f"{config_name}.config"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    if os.path.isabs(build_config):
        return build_config

    return candidates[0]


def _is_deferred_env_path(value: str) -> bool:
    """Return True for paths resolved by project init scripts at runtime."""
    return "%" in str(value) or "$(" in str(value)


def _detect_vs_postfix() -> str:
    """Auto-detect VS postfix from installed Visual Studio version."""
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2019"):
        return "-vs vs16"
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2022"):
        return "-vs vs17"
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2017"):
        return "-vs vs15"
    return ""
