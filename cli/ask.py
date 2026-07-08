"""rsim ask — AI Q&A about analysis results."""


def register(subparsers):
    p = subparsers.add_parser("ask", help="Ask AI about analysis results")
    p.add_argument("question", help="Question to ask")
    p.add_argument("--results", help="Results directory to query (default: latest)")


def run(args, config):
    from core.config import get_latest_result_dir, load_signals
    from core.analysis_runner import AnalysisPlugin
    from platforms import get as get_platform

    project = args.project
    question = args.question

    # Find results directory
    if args.results:
        results_dir = args.results
    else:
        latest = get_latest_result_dir(project)
        if not latest:
            print(f"No analysis results found for project '{project}'.")
            print("Run 'rsim analyze <mf4>' first.")
            return 1
        results_dir = str(latest)

    # Load saved signals
    import json
    import yaml
    signals_path = __import__("os").path.join(results_dir, "signals.json")
    if not __import__("os").path.exists(signals_path):
        print(f"No signals.json found in {results_dir}")
        return 1

    # Reconstruct minimal SignalData from saved JSON
    with open(signals_path, encoding="utf-8") as f:
        raw_signals = yaml.safe_load(f)

    signals = {}
    for name, data in raw_signals.items():
        signals[name] = type("SignalData", (), {
            "name": name,
            "unit": data.get("unit", ""),
            "summary": data,
            "values": [],  # Not stored — summary only
        })()

    # Load AI QA plugin
    from plugins.analysis.ai_qa import AIQAPlugin
    from core.models import AnalysisContext
    from datetime import datetime

    plugin = AIQAPlugin()
    context = AnalysisContext(
        mf4_path="",
        project=project,
        platform=config.get("project", {}).get("platform", "gen5_selena"),
        timestamp=datetime.now(),
        signals_config=load_signals(project),
        rules_config=[],
        output_dir=results_dir,
    )

    print(f"Question: {question}")
    print()
    answer = plugin.ask(question, signals, context)
    print(answer)

    return 0
