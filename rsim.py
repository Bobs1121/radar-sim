#!/usr/bin/env python3
"""radar-sim (rsim) — 仿真辅助与数据分析工具.

Usage:
    python rsim.py build [hex|selena|all]
    python rsim.py run <input.mf4> [--output-dir DIR]
    python rsim.py analyze <mf4>
    python rsim.py diff <base> <current>
    python rsim.py ask "问题"
    python rsim.py history [--search term]
    python rsim.py init [project_name]
    python rsim.py open-vs
    python rsim.py check
    python rsim.py prepare-sim
"""

import argparse
import logging
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="rsim",
        description="AI-Assisted Radar Simulation Verification Platform",
    )

    # Global options
    parser.add_argument("--project", help="Project name (default: from config)")
    parser.add_argument("--config", help="Path to local.yaml (path-driven config, overrides --project)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Register CLI modules
    _register_cli_modules(subparsers)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Disable color if requested
    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    # Load config (skip for control-plane commands that don't need project config)
    cmd_module = _COMMANDS.get(args.command)
    if cmd_module is not None and getattr(cmd_module, "NO_CONFIG", False):
        config = {}
        args.project = getattr(args, "project", "") or ""
    else:
        try:
            from core.config import load_config, load_config_from_path, get_default_project, list_projects

            config_path = getattr(args, "config", None)
            if config_path:
                config = load_config_from_path(config_path)
                project = config.get("_meta", {}).get("project", "") or "default"
            else:
                project = args.project or get_default_project()
                config = load_config(project)

            # Merge CLI overrides
            overrides = {}
            for item in getattr(args, "param", []):
                k, v = item.split("=", 1)
                overrides[k] = v
            if overrides:
                from core.config import merge_cli_overrides
                config = merge_cli_overrides(config, overrides)

            # Store project in args
            args.project = project

        except FileNotFoundError as e:
            print(f"Config error: {e}")
            print(f"Available projects: {', '.join(list_projects()) or 'none (create config.yaml)'}")
            return 1

    # Dispatch to command
    if not cmd_module:
        parser.print_help()
        return 1

    try:
        return cmd_module.run(args, config)
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted.")
        return 130
    except Exception as e:
        if args.verbose:
            import traceback
            traceback.print_exc()
        print(f"Error: {e}", file=sys.stderr)
        return 1


# Command registry
_COMMANDS = {}


def _register_cli_modules(subparsers):
    """Dynamically register CLI modules from cli/ directory."""
    cli_dir = Path(__file__).parent / "cli"
    if not cli_dir.exists():
        return

    for py_file in sorted(cli_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        module_name = f"cli.{py_file.stem}"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                if hasattr(module, "register"):
                    module.register(subparsers)
                    cmd_name = py_file.stem.replace("_", "-")
                    _COMMANDS[cmd_name] = module
        except Exception as e:
            logging.warning(f"Failed to load CLI module {py_file}: {e}")


if __name__ == "__main__":
    sys.exit(main() or 0)
