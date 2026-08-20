#!/usr/bin/env python3
"""Build the Windows connector package served by Linux.

Only explicitly allow-listed product files are included, so dirty worktrees,
credentials, logs, outputs and developer notes can never leak into the public
download.  Python itself and actual Selena/simulation dependencies are not
redistributed; only the small connector scaffold wheelhouse is allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = ("rsim.py", "setup.py", "requirements.txt")
SOURCE_DIRS = (
    "cli",
    "core",
    "platforms",
    "plugins",
    "radar_sim_sdk",
    "radar_sim_web",
    "web",
    "config",
    "scripts",
    "vendor/windows-wheels",
)
ALLOWED_SUFFIXES = {".py", ".ps1", ".vbs", ".yaml", ".yml", ".json", ".html", ".css", ".js", ".txt", ".whl"}


def _files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    missing = [path.relative_to(ROOT).as_posix() for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError("connector package inputs are missing: " + ", ".join(missing))
    for directory in SOURCE_DIRS:
        files.extend(
            path for path in (ROOT / directory).rglob("*")
            if path.is_file()
            and path.suffix.lower() in ALLOWED_SUFFIXES
            and "__pycache__" not in path.parts
            and not path.name.endswith(".pyc")
        )
    selected = sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix())
    required = {
        "rsim.py",
        "setup.py",
        "cli/agent.py",
        "core/__init__.py",
        "core/agent_policy.py",
        "scripts/bootstrap.ps1",
        "scripts/run_hidden.vbs",
        "scripts/start_windows.ps1",
        "scripts/watch_windows_connector.ps1",
    }
    included = {path.relative_to(ROOT).as_posix() for path in selected}
    absent = sorted(required - included)
    if absent:
        raise FileNotFoundError("connector package runtime files are missing: " + ", ".join(absent))
    return selected


def build(out: Path) -> tuple[Path, dict[str, object]]:
    out.parent.mkdir(parents=True, exist_ok=True)
    files = _files()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in files:
            # Do not let local checkout mtimes or Windows file attributes alter
            # the public package hash.  Deterministic bytes make the Linux
            # download checksum a useful deployment invariant.
            relative = source.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    manifest: dict[str, object] = {
        "version": 1,
        "filename": out.name,
        "sha256": digest,
        "size": out.stat().st_size,
        "file_count": len(files),
    }
    out.with_suffix(out.suffix + ".json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return out, manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist" / "rsim-windows-connector.zip"))
    args = parser.parse_args()
    out, manifest = build(Path(args.out))
    print(f"Built {out} ({manifest['size']} bytes, {manifest['file_count']} files)")
    print(f"sha256:{manifest['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
