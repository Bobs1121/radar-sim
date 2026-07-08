"""
Selena Builder — 仿真环境编译.

Calls jenkins_selena_build.bat or R2D2.py to compile selena.exe.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

from core.models import BuildOptions, BuildResult

logger = logging.getLogger(__name__)


class SelenaBuilder:
    """Build selena.exe simulation environment."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.python3 = config.get("environment", {}).get("python3_path", "python3")
        self.r2d2 = config.get("paths", {}).get("r2d2_script", "")
        self.build_config = config.get("paths", {}).get("build_config", "")
        self.build_output = config.get("paths", {}).get("build_output", "")
        self.selena_build_script = config.get("selena_build_script", "")
        self.boost_root = config.get("boost_root", "") or config.get("environment", {}).get("boost_root", "")
        self.vs_postfix = config.get("vs_postfix", "")
        self.root = config.get("project_root", "")

    def build(self, options: BuildOptions) -> BuildResult:
        """Build selena.exe using R2D2.py."""
        from .builder import _resolve_config_path, _build_env_full, _detect_vs_postfix

        start = time.time()
        errors = []
        log_lines = []

        build_config = options.build_config or self.build_config
        build_mode = options.mode or self.config.get("selena", {}).get("build_mode", "RelWithDebInfo")

        if not build_config:
            return BuildResult(
                success=False, duration_sec=time.time() - start,
                errors=["No build config specified"],
            )

        config_path = _resolve_config_path(build_config, self.root)
        if not config_path or not os.path.exists(config_path):
            return BuildResult(
                success=False, duration_sec=time.time() - start,
                errors=[f"Build config not found: {config_path}"],
            )

        env = _build_env_full(self.config)

        cmd = [
            self.python3, self.r2d2,
            "-m", config_path,
        ]

        if options.clean:
            cmd.extend(["-clean"])

        cmd.extend([
            "-ghs_math", "-use_mat", "-notests",
            "-bm", build_mode,
        ])

        vs_postfix = self.vs_postfix or _detect_vs_postfix()
        if vs_postfix:
            cmd.extend(vs_postfix.split())

        try:
            logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=1800,
                env=env,
            )
            log_lines = result.stdout.split("\n") + result.stderr.split("\n")

            if result.returncode != 0:
                errors = self._extract_errors(log_lines)
                logger.error(f"Build failed with {len(errors)} errors")
            else:
                logger.info("Build succeeded")

        except subprocess.TimeoutExpired:
            errors = ["Build timed out after 1800s"]
        except FileNotFoundError:
            errors = [f"R2D2.py not found: {self.r2d2}"]

        exe_path = ""
        if not errors:
            exe_pattern = self.config.get("selena", {}).get("exe_pattern", "dc_tools/selena/core/{build_mode}/selena.exe")
            exe_dir = os.path.join(self.build_output, exe_pattern.format(build_mode=build_mode))
            exe_path = os.path.join(exe_dir, "selena.exe")

        duration = time.time() - start
        return BuildResult(
            success=len(errors) == 0,
            executable_path=exe_path,
            log_path="",
            duration_sec=duration,
            errors=errors,
            warnings=self._extract_warnings(log_lines),
        )

    def _extract_errors(self, lines: list[str]) -> list[str]:
        """Extract error lines from build log."""
        errors = []
        for line in lines:
            if any(kw in line.lower() for kw in ["error", "failed", "- ERROR -"]):
                errors.append(line.strip())
        return errors[:20]  # Cap at 20

    def _extract_warnings(self, lines: list[str]) -> list[str]:
        """Extract warning lines from build log."""
        warnings = []
        for line in lines:
            if "warning" in line.lower():
                warnings.append(line.strip())
        return warnings[:20]

    def check_environment(self) -> list[str]:
        """Check if build environment is ready."""
        issues = []
        if not self.r2d2 or not os.path.exists(self.r2d2):
            issues.append(f"R2D2.py not found: {self.r2d2}")
        if not self.build_config or not os.path.exists(self.build_config):
            issues.append(f"Build config not found: {self.build_config}")
        if not self.python3 or not os.path.exists(self.python3):
            # Try system python
            if not os.path.exists("python3"):
                pass  # Will work via PATH
        return issues

    def _build_env(self) -> dict[str, str]:
        """Assemble build environment variables."""
        env = os.environ.copy()
        if self.boost_root:
            env["BOOST_ROOT"] = self.boost_root
        # PATH assembly
        paths = list(self.config.get("environment", {}).get("path_prefix", []))
        if self.python3:
            paths.append(os.path.dirname(self.python3))
        if self.boost_root:
            paths.append(os.path.join(self.boost_root, "lib64-msvc-14.0"))
        env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
        return env
