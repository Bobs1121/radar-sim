"""rsim analyze — analyze MF4 simulation output."""

import sys


def register(subparsers):
    p = subparsers.add_parser("analyze", help="Analyze MF4 simulation output")
    p.add_argument("mf4", help="Path to the output MF4 file")
    p.add_argument("--plugin", help="Comma-separated plugin names (default: signal_summary,rule_check,default_report)")
    p.add_argument("--no-ai", action="store_true", help="Disable AI analysis")
    p.add_argument("--context", help="User context (e.g., code changes description)")
    p.add_argument("--log", help="Path to simulation log file")


def run(args, config):
    from core.analysis_runner import AnalysisRunner

    mf4_path = args.mf4
    if not __import__("os").path.exists(mf4_path):
        print(f"Error: MF4 file not found: {mf4_path}")
        return 1

    # Determine plugins
    plugins = None
    if args.plugin:
        plugins = [p.strip() for p in args.plugin.split(",")]
    elif args.no_ai:
        plugins = ["signal_summary", "rule_check", "default_report"]

    runner = AnalysisRunner(args.project, config)
    result = runner.run(
        mf4_path=mf4_path,
        plugins=plugins,
        user_context=args.context,
        log_path=args.log,
    )

    return 0 if result.success else 1
