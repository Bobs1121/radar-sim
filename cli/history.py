"""rsim history — list and search analysis results."""

import os
from datetime import datetime


def register(subparsers):
    p = subparsers.add_parser("history", help="List/search analysis results")
    p.add_argument("--limit", type=int, default=10, help="Max results to show")
    p.add_argument("--search", help="Search in analysis summary")
    p.add_argument("--project", default=None, help="Filter by project")
    p.add_argument("--json", action="store_true", help="Output as JSON")


def run(args, config):
    from core.config import get_results_base_dir
    from core.tui import styled
    import yaml
    import json

    results_base = get_results_base_dir()
    if not os.path.exists(results_base):
        print("No analysis results found.")
        return 0

    # Collect all result directories
    all_results = []
    for project_dir in os.listdir(results_base):
        proj_path = os.path.join(results_base, project_dir)
        if not os.path.isdir(proj_path):
            continue

        for run_dir in os.listdir(proj_path):
            run_path = os.path.join(proj_path, run_dir)
            if not os.path.isdir(run_path):
                continue

            # Load analysis.json for metadata
            analysis_file = os.path.join(run_path, "analysis.json")
            if not os.path.exists(analysis_file):
                continue

            with open(analysis_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            # Filter by project if specified
            proj_filter = args.project
            if proj_filter and data.get("project") != proj_filter:
                continue

            # Filter by search term
            search = args.search
            if search:
                summary = data.get("plugins", [])
                text = json.dumps(summary, ensure_ascii=False).lower()
                if search.lower() not in text:
                    continue

            all_results.append({
                "project": data.get("project", project_dir),
                "run_dir": run_dir,
                "path": run_path,
                "mf4": data.get("mf4_path", ""),
                "plugins": len(data.get("plugins", [])),
                "timestamp": data.get("timestamp", run_dir),
            })

    # Sort by timestamp (newest first)
    all_results.sort(key=lambda r: r["timestamp"], reverse=True)

    # Limit
    all_results = all_results[:args.limit]

    if args.json:
        print(json.dumps(all_results, indent=2, ensure_ascii=False))
        return 0

    # Print table
    if not all_results:
        print("No results found.")
        return 0

    print()
    print(styled.title("Analysis History"))
    print(f"  {'Time':<20} {'Project':<15} {'Plugins':<10} {'MF4'}")
    print(f"  {'-'*18}  {'-'*13}  {'-'*8}  {'-'*40}")

    for r in all_results:
        ts = r["timestamp"][:19] if len(r["timestamp"]) > 19 else r["timestamp"]
        mf4_short = os.path.basename(r.get("mf4", ""))
        print(f"  {ts:<20} {r['project']:<15} {r['plugins']:<10} {mf4_short}")

    print(f"\n  Total: {len(all_results)} results shown")
    return 0
