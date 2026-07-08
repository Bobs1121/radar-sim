"""rsim open-vs — open the Selena VS solution."""

import os
import subprocess
import sys


def register(subparsers):
    p = subparsers.add_parser("open-vs", help="Open Selena VS solution")
    p.add_argument("--sln", help="Override .sln path")


def run(args, config):
    sln = args.sln or config.get("compile", {}).get("vs_sln", "")

    if not sln:
        build_output = config.get("paths", {}).get("build_output", "")
        sln = os.path.join(build_output, "selena.sln")

    if not os.path.exists(sln):
        print(f"Error: VS solution not found: {sln}")
        print()
        print("Make sure Selena build has been completed first.")
        print("Or specify the path with --sln.")
        return 1

    print(f"Opening Visual Studio: {sln}")

    try:
        # Try devenv (VS Command Prompt) or direct .sln open
        devenv = os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft Visual Studio\2019\Community\Common7\IDE\devenv.com")
        if os.path.exists(devenv):
            subprocess.Popen([devenv, sln])
        else:
            # Fallback: use os.startfile (Windows default)
            os.startfile(sln)
    except Exception as e:
        print(f"Error opening VS: {e}")
        return 1

    return 0
