#!/usr/bin/env python3
"""Build a single-file zipapp (.pyz) for the control-plane server.

The .pyz bundles the minimal server file set (stdlib-only: no PyYAML/asammdf)
so it can be copied to any Linux/Windows box with Python 3.9+ and run with
``python rsim_server.pyz server serve --host 0.0.0.0 --port 8877``.

Usage:
    python scripts/build_server_pyz.py [--out dist/rsim_server.pyz]
"""

from __future__ import annotations

import argparse
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Minimal file set for the control server (all stdlib-only).
SERVER_FILES = [
    "rsim.py",
    "core/__init__.py",
    "core/control_service.py",
    "core/control_http.py",
    "core/user.py",
    "cli/__init__.py",
    "cli/server.py",
]

# Dedicated entry point: rsim.py's dynamic cli/ glob scan doesn't work inside a
# zipapp (no filesystem to glob), so we register cli.server directly.
_MAIN_PY = '''\
"""Entry point for the rsim_server zipapp — registers the server command directly."""
import argparse, sys
from cli import server as server_cmd

def main():
    parser = argparse.ArgumentParser(prog="rsim_server")
    parser.add_argument("--project", default="")
    parser.add_argument("--config", default="")
    sub = parser.add_subparsers(dest="command")
    server_cmd.register(sub)
    args = parser.parse_args()
    if not args.command:
        parser.print_help(); return 0
    return server_cmd.run(args, {})

if __name__ == "__main__":
    sys.exit(main() or 0)
'''


def build(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for rel in SERVER_FILES:
            src = ROOT / rel
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        # Dedicated __main__.py so the zipapp doesn't rely on rsim.py's glob scan.
        (tmp_path / "__main__.py").write_text(_MAIN_PY, encoding="utf-8")
        zipapp.create_archive(tmp_path, target=out, main=None, compressed=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist" / "rsim_server.pyz"))
    args = ap.parse_args()
    out = build(Path(args.out))
    print(f"Built {out} ({out.stat().st_size} bytes)")
    print(f"Run with: python {out.name} server serve --host 0.0.0.0 --port 8877")
    print(f"(no 'rsim' prefix — the zipapp's main is the server entry)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
