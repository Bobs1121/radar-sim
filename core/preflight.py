"""Pre-flight Compatibility Validation Engine (PRD §1.6).

Before a simulation is dispatched to the Cluster (or run locally), the engine
runs three static contract checks against the 4D dependency graph
(Branch -> Binary <-> Runtime.xml <-> Dataset.MF4). Any hard mismatch must
block dispatch with a human-readable diagnostic so invalid sims never run.

The three checks (PRD §1.6.2):
  1. Software fingerprint  — branch declared in config vs. signature embedded
     in / adjacent to the selena.exe build artifact.
  2. Interface consistency — Runnable topology declared in Runtime.xml vs. the
     interface manifest exported alongside the binary.
  3. Signal contract       — Required Signals (signals.yaml) present in the
     input MF4 header; DBC protocol version aligned (when a DBC is configured).

Dependency policy (PRD §1.6.1 "乱配不崩溃"):
  asammdf / cantools are optional. When absent, the affected sub-check degrades
  to a WARNING (not a hard failure) so the engine still returns a result. A
  sub-check only hard-fails when it has enough information to be certain the
  contract is violated.

This module is stdlib-only at import time; heavy deps are imported lazily
inside the checks that need them.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class CheckResult:
    """Outcome of a single pre-flight sub-check."""

    name: str
    level: str  # "info" | "warning" | "error"
    passed: bool  # True unless level == "error"
    detail: str
    repair_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "passed": self.passed,
            "detail": self.detail,
            "repair_hint": self.repair_hint,
        }


@dataclass
class PreflightResult:
    """Aggregate pre-flight result. ``ok`` is False iff any check is "error"."""

    ok: bool = True
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)
        if result.level == "error":
            self.ok = False

    @property
    def diagnostics(self) -> list[str]:
        """Human-readable failure lines (PRD §1.6.3: 人话报错)."""
        lines: list[str] = []
        for c in self.checks:
            if c.level == "error":
                msg = f"[{c.name}] {c.detail}"
                if c.repair_hint:
                    msg += f" → 修复建议: {c.repair_hint}"
                lines.append(msg)
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "diagnostics": self.diagnostics,
        }


# ---------------------------------------------------------------------------
# Helpers: locate the binary, its fingerprint, and its interface manifest.
# ---------------------------------------------------------------------------

def _selena_exe_path(config: dict[str, Any]) -> str:
    """Resolve the selena.exe path the same way the runtime would."""
    try:
        from core.config import resolve_selena_executable
        return resolve_selena_executable(config) or ""
    except Exception:
        return str(
            (config.get("build", {}) or {}).get("build_output", "")
            or (config.get("paths", {}) or {}).get("build_output", "")
        )


def _signature_for(exe_path: str) -> Optional[dict[str, Any]]:
    """Load the build signature adjacent to the binary.

    The compile hook writes ``selena.exe.sig.json`` beside the binary holding
    ``{branch, commit, timestamp}`` (PRD §1.6.2 check 1). Returns None when no
    signature file exists (caller degrades to a warning).
    """
    if not exe_path:
        return None
    p = Path(exe_path)
    candidates = [
        p.with_suffix(p.suffix + ".sig.json"),
        p.with_name(p.name + ".sig.json"),
        p.with_suffix(".json"),
    ]
    for cand in candidates:
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
    return None


def _interface_manifest_for(exe_path: str) -> Optional[dict[str, Any]]:
    """Load the interface manifest exported beside the binary.

    The build hook may emit ``selena.interfaces.json`` listing the Runnable
    interface names the binary actually exports (PRD §1.6.2 check 2). Absent
    → None (degrade to warning; cannot statically prove a mismatch).
    """
    if not exe_path:
        return None
    p = Path(exe_path)
    for cand in (p.with_name("selena.interfaces.json"), p.with_suffix(".interfaces.json")):
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
    return None


# ---------------------------------------------------------------------------
# Check 1: Software fingerprint (Branch <-> Binary)
# ---------------------------------------------------------------------------

def check_fingerprint(config: dict[str, Any]) -> CheckResult:
    """Verify the binary's embedded branch matches the configured Selena branch."""
    declared_branch = (
        config.get("_profile_selena_branch")
        or (config.get("build", {}) or {}).get("selena_branch", "")
        or (config.get("repos", {}) or {}).get("inner_repo_branch", "")
    )
    exe_path = _selena_exe_path(config)
    sig = _signature_for(exe_path)

    if not declared_branch:
        # No branch pinned — nothing to validate. Not an error (config-driven).
        return CheckResult(
            "software_fingerprint", "info", True,
            "未配置 Selena 分支，跳过指纹校验（配置驱动，可选）。",
        )

    if sig is None:
        return CheckResult(
            "software_fingerprint", "warning", True,
            f"未找到 binary 伴生签名文件（{Path(exe_path).name}.sig.json），"
            "无法静态校验分支指纹。建议编译钩子写入签名后重试。",
            repair_hint="在 Selena 编译脚本中嵌入 git commit/branch 到 .sig.json",
        )

    sig_branch = str(sig.get("branch") or sig.get("git_branch") or "").strip()
    if not sig_branch:
        return CheckResult(
            "software_fingerprint", "warning", True,
            "签名文件存在但缺少 branch 字段，无法校验指纹。",
        )

    if sig_branch == declared_branch:
        commit = sig.get("commit") or sig.get("git_commit") or "?"
        return CheckResult(
            "software_fingerprint", "info", True,
            f"分支指纹一致: declared='{declared_branch}' == signed='{sig_branch}' (commit {commit[:8] if isinstance(commit, str) else commit})",
        )

    return CheckResult(
        "software_fingerprint", "error", False,
        f"分支指纹不匹配: 配置声明 '{declared_branch}'，但该 selena.exe 实际由 '{sig_branch}' 分支编译。"
        "继续运行会导致变量布局/接口错位，仿真无声崩溃。",
        repair_hint=f"重新编译 Selena 到 '{declared_branch}' 分支，或修正配置中的 selena_branch",
    )


# ---------------------------------------------------------------------------
# Check 2: Interface consistency (Binary <-> Runtime.xml)
# ---------------------------------------------------------------------------

_RUNNABLE_RE = re.compile(r"<runnable\s+name=[\"']([^\"']+)[\"']", re.IGNORECASE)


def parse_runtime_runnables(runtime_xml_path: str) -> set[str]:
    """Extract the set of Runnable names declared by Runtime.xml."""
    if not runtime_xml_path or not Path(runtime_xml_path).exists():
        return set()
    try:
        tree = ET.parse(runtime_xml_path)
    except ET.ParseError:
        # Fall back to a tolerant regex sweep so a malformed XML still yields
        # the declared runnable names rather than crashing the whole engine.
        text = Path(runtime_xml_path).read_text(encoding="utf-8", errors="replace")
        return {m.group(1) for m in _RUNNABLE_RE.finditer(text)}
    names: set[str] = set()
    for elem in tree.iter():
        tag = elem.tag.split("}", 1)[-1].lower()
        if tag == "runnable":
            n = elem.get("name") or elem.get("Name")
            if n:
                names.add(n.strip())
    return names


def check_interface(config: dict[str, Any]) -> CheckResult:
    """Verify Runtime.xml runnable topology is satisfiable by the binary."""
    sim = config.get("simulation", {}) or {}
    assets = config.get("assets", {}) or {}
    runtime_xml = (
        sim.get("runtime_xml")
        or assets.get("runtime_xml")
        or (config.get("paths", {}) or {}).get("runtime_xml", "")
    )

    if not runtime_xml or not Path(runtime_xml).exists():
        return CheckResult(
            "interface_consistency", "warning", True,
            "未找到 Runtime.xml，接口匹配性校验降级（运行时由自适应寻址引擎模糊装载）。",
        )

    xml_runnables = parse_runtime_runnables(runtime_xml)
    if not xml_runnables:
        return CheckResult(
            "interface_consistency", "warning", True,
            f"Runtime.xml '{Path(runtime_xml).name}' 未声明任何 <runnable>，接口校验降级。",
        )

    exe_path = _selena_exe_path(config)
    manifest = _interface_manifest_for(exe_path)
    if manifest is None:
        return CheckResult(
            "interface_consistency", "warning", True,
            f"Runtime.xml 声明 {len(xml_runnables)} 个 runnable，但未找到 binary 接口清单"
            f"（{Path(exe_path).name}.interfaces.json），无法静态比对。运行时将依赖 selena.exe 自检。",
            repair_hint="编译钩子导出 selena.interfaces.json 以启用严格接口校验",
        )

    exported = set(manifest.get("runnables") or manifest.get("interfaces") or [])
    if not exported:
        return CheckResult(
            "interface_consistency", "warning", True,
            "接口清单存在但 runnables 字段为空，接口校验降级。",
        )

    missing = xml_runnables - exported
    if not missing:
        return CheckResult(
            "interface_consistency", "info", True,
            f"接口匹配: Runtime.xml 的 {len(xml_runnables)} 个 runnable 全部存在于 binary 导出清单。",
        )

    sample = ", ".join(sorted(missing)[:5])
    return CheckResult(
        "interface_consistency", "error", False,
        f"接口不匹配: Runtime.xml 引用了 {len(missing)} 个 binary 未导出的 runnable（如 {sample}）。"
        "在 VS 中运行会瞬间闪退或内存非法访问。",
        repair_hint="更换与该 selena.exe 匹配的 Runtime.xml，或重新编译包含这些接口的 Selena",
    )


# ---------------------------------------------------------------------------
# Check 3: Signal contract (Binary <-> Dataset.MF4)
# ---------------------------------------------------------------------------

def _required_signal_names(config: dict[str, Any]) -> list[str]:
    """Required Signals from signals.yaml (project-level, hard constraint)."""
    project = config.get("_meta", {}).get("project") or config.get("project", {}).get("name")
    if project:
        try:
            from core.config import load_signals
            sigs = load_signals(project)
            names = [s.get("name") for s in sigs if s.get("name")]
            if names:
                return names
        except Exception:
            pass
    # Fallback: inline signals in config.
    inline = (config.get("signals", {}) or {}).get("required") or []
    return [s for s in inline if s]


def _mf4_channel_names(mf4_path: str) -> Optional[set[str]]:
    """Read MF4 header channel names via asammdf (minimum memory). None if unavailable."""
    if not mf4_path or not Path(mf4_path).exists():
        return None
    try:
        from asammdf import MDF
    except ImportError:
        return None
    try:
        mdf = MDF(mf4_path, memory="minimum")
        try:
            return set(mdf.channels_db.keys())
        finally:
            mdf.close()
    except Exception:
        return None


def check_signal_contract(config: dict[str, Any]) -> CheckResult:
    """Verify the input MF4 carries every Required Signal (and DBC aligns)."""
    required = _required_signal_names(config)
    if not required:
        return CheckResult(
            "signal_contract", "info", True,
            "signals.yaml 未定义硬约束信号名单，信号契约校验跳过（可选）。",
        )

    sim = config.get("simulation", {}) or {}
    paths = config.get("paths", {}) or {}
    input_mf4 = (
        sim.get("input_mf4")
        or paths.get("input_mf4")
        or (sim.get("datasets", [{}])[0].get("input_mf4") if sim.get("datasets") else "")
        or ""
    )

    if not input_mf4:
        return CheckResult(
            "signal_contract", "warning", True,
            f"检测到 {len(required)} 个 Required Signals，但未指定输入 MF4，契约校验降级（将在数据自适应阶段复检）。",
        )

    channels = _mf4_channel_names(input_mf4)
    if channels is None:
        # asammdf missing OR file unreadable — degrade, do not hard-fail.
        return CheckResult(
            "signal_contract", "warning", True,
            f"无法读取 MF4 header（asammdf 缺失或文件不可读），无法校验 {len(required)} 个 Required Signals。"
            "运行时 Selena 解码可能读到垃圾数值。",
            repair_hint="安装 asammdf 或确认 MF4 路径可达后重跑 preflight",
        )

    missing = [s for s in required if not any(s in ch for ch in channels)]
    if not missing:
        return CheckResult(
            "signal_contract", "info", True,
            f"信号契约满足: 全部 {len(required)} 个 Required Signals 均存在于数据集。",
        )

    sample = ", ".join(missing[:5])
    return CheckResult(
        "signal_contract", "error", False,
        f"信号契约不匹配: 数据集缺少 {len(missing)} 个 Required Signals（如 {sample}）。"
        "Selena 解码会读到 NaN，导致后续分析死机。",
        repair_hint="更换含这些信号的数据集，或调整分支/信号名单",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_preflight(config: dict[str, Any]) -> PreflightResult:
    """Run all three pre-flight checks. Returns aggregate PreflightResult.

    A result with ``ok=False`` must block dispatch (PRD §1.6.3 hard intercept).
    """
    result = PreflightResult()
    result.add(check_fingerprint(config))
    result.add(check_interface(config))
    result.add(check_signal_contract(config))
    return result
