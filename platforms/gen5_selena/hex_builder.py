"""
Hex Builder — HEX 固件编译.

Calls testbuild_BaseC0S_SINGLE.bat to compile the hex firmware.
Used when code changes need full firmware rebuild before simulation.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

from core.models import BuildOptions, BuildResult

logger = logging.getLogger(__name__)


class HexBuilder:
    """Build HEX firmware using testbuild script."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.hex_build_script = config.get("hex_build_script", "")
        self.root = config.get("project_root", "")
        self.binding = config.get("binding", "ovrs25")

    def build(self, options: BuildOptions) -> BuildResult:
        """Build HEX firmware."""
        start = time.time()
        errors = []
        log_lines = []

        # Use testbuild script
        build_script = self.hex_build_script or self._find_testbuild()
        if not build_script or not os.path.exists(build_script):
            return BuildResult(
                success=False, duration_sec=time.time() - start,
                errors=[f"HEX build script not found: {build_script}"],
            )

        # Build environment
        env = os.environ.copy()
        # Set selena environment Python path
        py3 = self.config.get("environment", {}).get("python3_path", "")
        if py3:
            env["PYTHON3"] = py3
        # Boost
        boost = self.config.get("boost_root", "") or self.config.get("environment", {}).get("boost_root", "")
        if boost:
            env["BOOST_ROOT"] = boost

        # Determine clean mode
        args = ["-clean"] if options.clean else ["-no-clean"]

        try:
            cmd = ["cmd", "/c", build_script] + args
            logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=1800,
                env=env,
            )
            log_lines = result.stdout.split("\n") + result.stderr.split("\n")

            if result.returncode != 0:
                errors = self._extract_errors(log_lines)
                logger.error(f"HEX build failed with {len(errors)} errors")
            else:
                logger.info("HEX build succeeded")

        except subprocess.TimeoutExpired:
            errors = ["HEX build timed out after 1800s"]
        except FileNotFoundError:
            errors = [f"Build script not found: {build_script}"]

        # Find hex output
        hex_path = ""
        if not errors:
            hex_path = self._find_hex_output()

        duration = time.time() - start
        return BuildResult(
            success=len(errors) == 0,
            executable_path=hex_path,
            log_path="",
            duration_sec=duration,
            errors=errors,
            warnings=self._extract_warnings(log_lines),
        )

    def _find_testbuild(self) -> str:
        """Auto-find testbuild script."""
        default = os.path.join(
            self.root, "apl", "byd", "bindings", self.binding,
            "buildscripts", "testbuild_BaseC0S_SINGLE.bat",
        )
        return default

    def _find_hex_output(self) -> str:
        """Find compiled hex output file."""
        # HEX output is typically in build directory
        build_dir = os.path.join(self.root, "ip_dc", "build")
        if os.path.isdir(build_dir):
            import glob
            hex_files = glob.glob(os.path.join(build_dir, "**", "*.hex"), recursive=True)
            if hex_files:
                # Return the most recently modified
                hex_files.sort(key=os.path.getmtime, reverse=True)
                return hex_files[0]
        return ""

    def _extract_errors(self, lines: list[str]) -> list[str]:
        """Extract error lines from build log."""
        errors = []
        for line in lines:
            if any(kw in line.lower() for kw in ["error", "failed"]):
                errors.append(line.strip())
        return errors[:20]

    def _extract_warnings(self, lines: list[str]) -> list[str]:
        """Extract warning lines from build log."""
        warnings = []
        for line in lines:
            if "warning" in line.lower():
                warnings.append(line.strip())
        return warnings[:20]
