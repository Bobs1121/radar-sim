"""Build the no-source SDK/MCP/Skill distribution bundle.

Example:

    python scripts/build_agent_tools_bundle.py \
      --wheel-dir dist/wheels \
      --skill-dir skills/radar-sim-simulation \
      --output dist/agent-tools.zip \
      --release-version 4.0.0-agent.1 \
      --sdk-version 4.0.0 \
      --mcp-version 0.1.0 \
      --skill-version 0.1.0 \
      --connector-contract-version 16

The sidecar manifest is written beside the ZIP as ``agent-tools.json``.  The
deployment passes both paths to ``create_app``; the public API exposes only
path-free metadata and the same-origin download URLs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zipfile


def build(
    *,
    wheel_dir: Path,
    skill_dir: Path,
    output: Path,
    release_version: str,
    sdk_version: str,
    mcp_version: str,
    skill_version: str,
    connector_contract_version: str,
    mcp_tool_contract_version: str = "1.0",
    mcp_dependency_version: str = "",
    python_requires: str = ">=3.10",
    launcher_path: Path | None = None,
) -> tuple[Path, Path]:
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise ValueError("wheel-dir does not contain any wheel")
    if not (skill_dir / "SKILL.md").is_file():
        raise ValueError("skill-dir must contain SKILL.md")
    launcher = (launcher_path or Path(__file__).with_name("agent_mcp_launcher.py")).expanduser().resolve()
    if not launcher.is_file():
        raise ValueError("launcher_path is unavailable")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="radar-sim-agent-tools-") as temporary:
        root = Path(temporary)
        wheel_root = root / "wheels"
        skill_root = root / "skill" / skill_dir.name
        wheel_root.mkdir(parents=True)
        skill_root.parent.mkdir(parents=True)
        for wheel in wheels:
            shutil.copy2(wheel, wheel_root / wheel.name)
        shutil.copytree(skill_dir, skill_root)
        runtime_root = root / "runtime"
        runtime_root.mkdir(parents=True)
        shutil.copy2(launcher, runtime_root / "agent_mcp_launcher.py")
        embedded_manifest = {
            "schema_version": "radar-sim.agent-tools/1.0",
            "release_version": release_version,
            "sdk_version": sdk_version,
            "mcp_version": mcp_version,
            "skill_version": skill_version,
            "mcp_tool_contract_version": mcp_tool_contract_version,
            "mcp_dependency_version": mcp_dependency_version,
            "api_version": "v1",
            "config_schema_version": "2.0",
            "connector_contract_version": connector_contract_version,
            "python_requires": python_requires,
        }
        (root / "manifest.json").write_text(
            json.dumps(embedded_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_zip = root / "agent-tools.zip"
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path == temporary_zip or not path.is_file():
                    continue
                archive.write(path, path.relative_to(root).as_posix())
        shutil.copy2(temporary_zip, output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {
        **embedded_manifest,
        "bundle_size": output.stat().st_size,
        "bundle_sha256": digest,
    }
    manifest_path = output.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--mcp-version", required=True)
    parser.add_argument("--skill-version", required=True)
    parser.add_argument("--connector-contract-version", required=True)
    parser.add_argument("--mcp-tool-contract-version", default="1.0")
    parser.add_argument("--mcp-dependency-version", default="")
    parser.add_argument("--python-requires", default=">=3.10")
    args = parser.parse_args()
    output, manifest = build(**vars(args))
    print(json.dumps({"bundle": str(output), "manifest": str(manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
