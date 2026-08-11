"""Business-facing projection of the internal simulation Stage DAG.

The scheduler may keep a detailed fixed DAG for recovery and audit.  Web, SDK
and future AI adapters should not force users to understand that implementation
detail, so this module projects it into four stable business steps.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


_STEP_DEFINITIONS = (
    (
        "resolve_inputs",
        "解析输入与执行环境",
        ("resolve_spec", "environment_check", "prepare_source"),
    ),
    (
        "prepare_execution",
        "编译与准备仿真输入",
        ("build_selena", "register_artifact", "prepare_data"),
    ),
    (
        "run_simulation",
        "执行仿真",
        ("preflight", "run_simulation"),
    ),
    (
        "collect_results",
        "收集并整理结果",
        ("collect_results", "finalize_manifest"),
    ),
)

_STATUS_PRIORITY = {
    "failed": 80,
    "cancelled": 70,
    "cancel_requested": 60,
    "running": 50,
    "blocked": 40,
    "queued": 30,
    "succeeded": 20,
    "skipped": 10,
}


def business_steps(stages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return four stable, path-free business progress records.

    Unknown/internal future stages remain in the scheduler audit view but do not
    leak into the public business flow until they are intentionally assigned to
    a product step.
    """

    by_type: dict[str, Mapping[str, Any]] = {}
    for stage in stages:
        stage_type = str(stage.get("stage_type") or stage.get("task_type") or "")
        if stage_type:
            by_type[stage_type] = stage

    result: list[dict[str, Any]] = []
    for step_id, label, stage_types in _STEP_DEFINITIONS:
        members = [by_type[item] for item in stage_types if item in by_type]
        if not members:
            continue
        status = _aggregate_status(members)
        progress = _aggregate_progress(members)
        item: dict[str, Any] = {
            "id": step_id,
            "label": label,
            "status": status,
            "progress": progress,
            "stage_types": [
                str(member.get("stage_type") or member.get("task_type") or "")
                for member in members
            ],
        }
        problem = next(
            (
                dict(member.get("error") or {})
                for member in members
                if str(member.get("status") or "") in {"failed", "blocked"}
                and isinstance(member.get("error"), Mapping)
            ),
            {},
        )
        if problem:
            item["error"] = problem
        result.append(item)
    return result


def _aggregate_status(stages: list[Mapping[str, Any]]) -> str:
    statuses = [str(stage.get("status") or "queued") for stage in stages]
    if statuses and all(value == "skipped" for value in statuses):
        return "skipped"
    if statuses and all(value in {"succeeded", "skipped"} for value in statuses):
        return "succeeded"
    return max(statuses or ["queued"], key=lambda value: _STATUS_PRIORITY.get(value, 0))


def _aggregate_progress(stages: list[Mapping[str, Any]]) -> float:
    values: list[float] = []
    for stage in stages:
        status = str(stage.get("status") or "")
        if status in {"succeeded", "skipped"}:
            values.append(1.0)
            continue
        try:
            value = float(stage.get("progress") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        values.append(min(max(value, 0.0), 1.0))
    return round(sum(values) / len(values), 4) if values else 0.0


__all__ = ["business_steps"]
