"""rsim diff — compare two analysis results with interpretation."""

import json
import os
import sys


def register(subparsers):
    p = subparsers.add_parser("diff", help="Compare two analysis results")
    p.add_argument("base", help="Base result directory or MF4 path")
    p.add_argument("current", help="Current result directory or MF4 path")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--ai", action="store_true", help="Include AI interpretation")


def run(args, config):
    from core.tui import styled
    from core.config import load_config as load_cfg

    base_signals = _load_signals(args.base)
    current_signals = _load_signals(args.current)

    if not base_signals:
        print(f"Error: Could not load signals from {args.base}")
        return 1
    if not current_signals:
        print(f"Error: Could not load signals from {args.current}")
        return 1

    # Compare
    diffs = []
    common = set(base_signals.keys()) & set(current_signals.keys())

    for name in sorted(common):
        b = base_signals[name]
        c = current_signals[name]

        base_mean = float(b.get("mean", 0) or 0)
        curr_mean = float(c.get("mean", 0) or 0)
        base_min = float(b.get("min", 0) or 0)
        curr_min = float(c.get("min", 0) or 0)
        base_max = float(b.get("max", 0) or 0)
        curr_max = float(c.get("max", 0) or 0)

        change_pct = ((curr_mean - base_mean) / abs(base_mean) * 100) if base_mean else 0

        if abs(change_pct) < 1:
            interp = "基本一致"
        elif abs(change_pct) < 5:
            interp = "小幅变化"
        elif abs(change_pct) < 20:
            interp = "明显变化"
        else:
            interp = "重大变化"

        diffs.append({
            "signal": name,
            "base_mean": base_mean,
            "current_mean": curr_mean,
            "change_pct": change_pct,
            "base_min": base_min, "current_min": curr_min,
            "base_max": base_max, "current_max": curr_max,
            "interpretation": interp,
        })

    # Output
    if args.json:
        print(json.dumps(diffs, indent=2, ensure_ascii=False))
        return 0

    print()
    print(styled.title("Signal Comparison"))
    print(f"  Base:    {args.base}")
    print(f"  Current: {args.current}")
    print()

    # Summary
    major = sum(1 for d in diffs if "重大" in d["interpretation"])
    moderate = sum(1 for d in diffs if "明显" in d["interpretation"])
    minor = sum(1 for d in diffs if "小幅" in d["interpretation"])
    stable = sum(1 for d in diffs if "一致" in d["interpretation"])

    print(styled.status("Overall"))
    print(f"  Total signals: {len(diffs)}")
    print(f"  Stable:    {styled.stable(stable)}")
    print(f"  Minor:     {styled.warning(minor)}")
    print(f"  Moderate:  {styled.warning(moderate)}")
    if major:
        print(f"  Major:     {styled.error(major)}")
    print()

    # Detail table — only show changed signals by default
    changed = [d for d in diffs if abs(d["change_pct"]) >= 1]
    if changed:
        print(f"{'Signal':<30} {'Base':>10} {'Current':>10} {'Change':>10} {'Interpretation'}")
        print("-" * 80)
        for d in sorted(changed, key=lambda x: abs(x["change_pct"]), reverse=True):
            sign = "+" if d["change_pct"] > 0 else ""
            print(f"{d['signal']:<30} {d['base_mean']:>10.2f} {d['current_mean']:>10.2f} {sign}{d['change_pct']:>9.1f}% {d['interpretation']}")
    else:
        print("  All signals are stable — no significant changes.")

    # Signals only in base/current
    only_base = set(base_signals.keys()) - set(current_signals.keys())
    only_current = set(current_signals.keys()) - set(base_signals.keys())

    if only_base:
        print()
        print(styled.warning("Signals only in base:"))
        for name in only_base:
            print(f"  - {name}")

    if only_current:
        print()
        print(styled.info("Signals only in current:"))
        for name in only_current:
            print(f"  + {name}")

    # AI interpretation
    if args.ai and changed:
        print()
        print(styled.info("AI Interpretation"))
        _ai_interpret(diffs, changed, config)

    return 0


def _ai_interpret(diffs, changed, config):
    """Ask AI to interpret changes."""
    try:
        from plugins.analysis.ai_qa import AIQAPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        # Build signal context
        signal_descriptions = []
        for d in changed[:10]:  # Top 10 changes
            sign = "+" if d["change_pct"] > 0 else ""
            signal_descriptions.append(
                f"- {d['signal']}: {d['base_mean']:.2f} -> {d['current_mean']:.2f} "
                f"({sign}{d['change_pct']:.1f}%), {d['interpretation']}"
            )

        question = (
            "Analyze these signal changes between two simulation runs. "
            "What do these changes indicate? Any concerns?"
        )

        # Build minimal signals dict
        signals = {}
        for d in changed[:10]:
            signals[d["signal"]] = SignalData(
                name=d["signal"],
                values=[d["current_mean"]],
                timestamps=[0.0],
                summary=d,
            )

        plugin = AIQAPlugin()
        context = AnalysisContext(
            mf4_path="", project=config.get("project", {}).get("name", "unknown"),
            platform="gen5_selena", timestamp=datetime.now(),
            signals_config=[], rules_config=[],
        )

        answer = plugin.ask(question, signals, context)
        print(answer)

    except Exception as e:
        print(f"  AI unavailable: {e}")


def _load_signals(path):
    """Load signals from results directory or extract from MF4."""
    import yaml

    # Try as results directory
    signals_path = os.path.join(path, "signals.json")
    if os.path.exists(signals_path):
        with open(signals_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # Try as MF4 — extract directly
    from core.config import get_default_project, load_config, load_signals as ls

    cfg = load_config(get_default_project())
    signal_cfg = ls(get_default_project())
    signal_names = [s["name"] for s in signal_cfg]

    from platforms import get as get_platform
    platform = get_platform(cfg.get("project", {}).get("platform", "gen5_selena"), cfg)
    try:
        signals = platform.extract_signals(path, signal_names)
    except Exception:
        return {}

    # Convert to summary dict
    result = {}
    for name, sig in signals.items():
        result[name] = sig.summary or {
            "min": min(sig.values) if sig.values else 0,
            "max": max(sig.values) if sig.values else 0,
            "mean": sum(sig.values) / len(sig.values) if sig.values else 0,
        }
    return result
