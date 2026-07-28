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


class RuntimeDataSignalContractError(ValueError):
    """A deterministic Runtime.xml-to-MF4 mismatch discovered before upload/run.

    The exception text deliberately contains a user-actionable explanation but
    never the physical source path.  It is used by the Windows Agent to stop a
    local-data upload before moving hundreds of megabytes to the control plane.
    """

    def __init__(self, code: str, detail: str, repair_hint: str = "") -> None:
        super().__init__(detail)
        self.code = str(code or "runtime_data_signal_unverified")
        self.detail = str(detail)
        self.repair_hint = str(repair_hint)


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


def _xml_local_name(element: ET.Element) -> str:
    """Return an XML tag name without a possible namespace."""
    return str(element.tag).split("}", 1)[-1].casefold()


def _connection_endpoints(connection: ET.Element) -> tuple[ET.Element | None, ET.Element | None]:
    """Return the direct output/input endpoint elements of one connection."""
    output: ET.Element | None = None
    input_: ET.Element | None = None
    for child in connection:
        tag = _xml_local_name(child)
        if tag in {"outport", "output", "port"} and output is None:
            output = child
        elif tag in {"inport", "input"} and input_ is None:
            input_ = child
    return output, input_


def runtime_active_runnables(runtime_xml_path: str) -> set[str]:
    """Return the runnable closure scheduled by a Runtime XML.

    A Runtime can declare/contain many optional runnables while scheduling only
    a subset through ``job``/``init``.  Starting with those scheduled nodes we
    include their upstream runnable dependencies.  ``DataPlayer`` and
    ``DataRecorder`` are transport endpoints, not algorithms, so they are not
    returned as active runnables.

    An older Runtime without any job/init declaration has no trustworthy
    execution scope.  The empty result deliberately makes callers retain the
    legacy full-Runtime contract instead of guessing that inputs are optional.
    """
    path = Path(str(runtime_xml_path or ""))
    if not path.is_file():
        return set()
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeDataSignalContractError(
            "runtime_xml_unreadable",
            "无法读取 Runtime XML，无法确认实际执行的 runnable。",
            "确认 Runtime XML 文件可读且与所选 Selena 产物匹配后重试。",
        ) from exc

    active: set[str] = set()
    for elem in root.iter():
        if _xml_local_name(elem) not in {"job", "init"}:
            continue
        for child in elem.iter():
            if _xml_local_name(child) != "runnable":
                continue
            name = str(child.get("name") or child.get("Name") or "").strip()
            if name and name.casefold() not in {"dataplayer", "datarecorder"}:
                active.add(name)

    if not active:
        return set()

    # A scheduled runnable may consume the output of another runnable which
    # is not explicitly present in a job block. Include that upstream node so
    # its own DataPlayer requirements cannot be accidentally skipped.
    connections = [elem for elem in root.iter() if _xml_local_name(elem) == "connection"]
    changed = True
    while changed:
        changed = False
        for connection in connections:
            output, input_ = _connection_endpoints(connection)
            if output is None or input_ is None:
                continue
            source = str(output.get("runnable") or output.get("Runnable") or "").strip()
            target = str(input_.get("runnable") or input_.get("Runnable") or "").strip()
            if not source or target not in active:
                continue
            if source.casefold() in {"dataplayer", "datarecorder"}:
                continue
            if source not in active:
                active.add(source)
                changed = True
    return active


def runtime_data_player_ports(runtime_xml_path: str) -> list[str]:
    """Return the input names Runtime.xml asks the DataPlayer to provide.

    These ports are a stronger source of truth than a static project signal
    list: a Runtime.xml is tied to the selected Selena binary and may change
    between branches.  The result is intentionally de-duplicated and stable
    so it can travel as an internal Agent-stage payload without exposing a
    product adapter to the public YAML/API.
    """
    path = Path(str(runtime_xml_path or ""))
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeDataSignalContractError(
            "runtime_xml_unreadable",
            "无法读取 Runtime XML，无法确认数据输入端口。",
            "确认 Runtime XML 文件可读且与所选 Selena 产物匹配后重试。",
        ) from exc
    active = runtime_active_runnables(runtime_xml_path)
    ports: list[str] = []
    connection_ports_seen = False
    for connection in (elem for elem in root.iter() if _xml_local_name(elem) == "connection"):
        output, input_ = _connection_endpoints(connection)
        if output is None:
            continue
        runnable = str(output.get("runnable") or output.get("Runnable") or "").strip()
        if runnable.casefold() != "dataplayer":
            continue
        connection_ports_seen = True
        target = str(
            (input_.get("runnable") or input_.get("Runnable") or "")
            if input_ is not None else ""
        ).strip()
        # When scheduling is explicit, only the selected runnable closure is
        # a valid input contract.  With no schedule (or no target endpoint),
        # retain the conservative legacy behaviour and validate the port.
        if active and target and target not in active:
            continue
        port = str(output.get("port") or output.get("name") or output.get("Name") or "").strip()
        if port:
            ports.append(port)

    # Compatibility with legacy/minimal Runtime XMLs that place a bare
    # DataPlayer outport directly under <selena> without a <connection>.
    if not connection_ports_seen:
        for elem in root.iter():
            tag = _xml_local_name(elem)
            if tag not in {"outport", "output", "port"}:
                continue
            runnable = str(elem.get("runnable") or elem.get("Runnable") or "").strip()
            if runnable.casefold() != "dataplayer":
                continue
            port = str(elem.get("port") or elem.get("name") or elem.get("Name") or "").strip()
            if port:
                ports.append(port)
    return list(dict.fromkeys(ports))


@dataclass(frozen=True)
class RuntimeDataSignalMatch:
    """One safe Runtime DataPlayer port to MF4 channel comparison."""

    required: str
    method: str  # exact | normalized_unique | missing | ambiguous
    candidates: tuple[str, ...] = ()


def _runtime_port_signature(port: str) -> tuple[str, str] | None:
    """Extract ``runnable, port`` only from an unambiguous generated name."""
    runnable, marker, endpoint = str(port).partition("_m_")
    if not marker or not runnable or not endpoint:
        return None
    return runnable, endpoint


def _normalized_runtime_endpoint(channel: str, runnable: str, endpoint: str) -> str | None:
    """Map one MF4 metadata path to its logical Runtime input endpoint.

    Generated MF4 metadata represents a Runtime port such as
    ``g_A_m_input`` as ``_g_A.SomeType._._m_input.TBase...``.  This is not a
    substring search: the channel must start with the exact runnable name and
    contain the exact ``._m_<endpoint>`` member with a legal member boundary.
    All child fields of that member collapse to one logical endpoint.
    """
    value = str(channel).lstrip("_")
    if not value.startswith(runnable):
        return None
    suffix = value[len(runnable):]
    if not suffix.startswith("."):
        return None
    marker = f"._m_{endpoint}"
    index = suffix.find(marker)
    if index < 0:
        return None
    tail = suffix[index + len(marker):]
    if tail and not tail.startswith((".", "[")):
        return None
    return runnable + suffix[:index + len(marker)]


def match_runtime_data_player_port(required: str, channels: set[str]) -> RuntimeDataSignalMatch:
    """Match a Runtime input against MF4 metadata without fuzzy matching.

    Exact names remain authoritative.  The only compatibility form accepted
    is the deterministic generated-MF4 representation described above.  It
    must resolve to *one* logical endpoint; several candidates are reported
    as ambiguous rather than silently choosing one.
    """
    if required in channels:
        return RuntimeDataSignalMatch(required, "exact", (required,))
    signature = _runtime_port_signature(required)
    if signature is None:
        return RuntimeDataSignalMatch(required, "missing")
    runnable, endpoint = signature
    candidates = tuple(sorted({
        normalized
        for channel in channels
        if (normalized := _normalized_runtime_endpoint(channel, runnable, endpoint)) is not None
    }))
    if len(candidates) == 1:
        return RuntimeDataSignalMatch(required, "normalized_unique", candidates)
    if candidates:
        return RuntimeDataSignalMatch(required, "ambiguous", candidates)
    return RuntimeDataSignalMatch(required, "missing")


def _runtime_data_signal_check(input_path: str | Path, required: list[str]) -> tuple[CheckResult, str]:
    """Check all MF4 headers for Runtime DataPlayer ports without leaking paths."""
    required = list(dict.fromkeys(str(item).strip() for item in required if str(item).strip()))
    if not required:
        return (
            CheckResult(
                "runtime_data_signal_contract", "info", True,
                "Runtime XML 未声明 DataPlayer 数据端口，跳过运行时数据契约校验。",
            ),
            "",
        )
    try:
        from core.data import iter_mf4_inputs

        files = list(iter_mf4_inputs(Path(input_path), limit=0))
    except (OSError, ValueError):
        files = []
    if not files:
        return (
            CheckResult(
                "runtime_data_signal_contract", "warning", True,
                "未找到可用于校验的 MF4 数据，未验证 Runtime XML 的数据输入；任务仍会继续。",
                repair_hint="选择包含原始 MF4 的数据文件或文件夹后重试。",
            ),
            "runtime_data_unavailable",
        )

    # ``asammdf`` reads the MF4 metadata/channel database, not the signal
    # samples. A bounded byte scan is not sufficient here: an absent name in
    # head/tail segments is inconclusive and must never permit an expensive
    # upload or Cluster run.  Do not stop at the first unreadable input: a
    # directory often contains multiple recordings, and an invalid/empty MF4
    # header must not be reported as a verified missing DataPlayer signal.
    readable_count = 0
    unverified_count = 0
    missing_by_file: list[list[str]] = []
    ambiguous_by_file: list[list[RuntimeDataSignalMatch]] = []
    exact_matches = 0
    normalized_matches = 0
    for mf4 in files:
        channels = _mf4_channel_names(str(mf4))
        # A successfully constructed MDF with an empty catalog is not enough
        # evidence either. It may be an incomplete/corrupt header and cannot
        # prove that every Runtime input is absent.
        if not channels:
            unverified_count += 1
            continue
        readable_count += 1
        matches = [match_runtime_data_player_port(signal, channels) for signal in required]
        exact_matches += sum(match.method == "exact" for match in matches)
        normalized_matches += sum(match.method == "normalized_unique" for match in matches)
        missing = [match.required for match in matches if match.method == "missing"]
        ambiguous = [match for match in matches if match.method == "ambiguous"]
        if missing:
            missing_by_file.append(missing)
        if ambiguous:
            ambiguous_by_file.append(ambiguous)

    if missing_by_file or ambiguous_by_file:
        unique_missing = list(dict.fromkeys(
            signal for missing in missing_by_file for signal in missing
        ))
        coverage = (
            f"已验证读取 {readable_count} 个 MF4，其中 {len(missing_by_file)} 个"
            f"缺少所需输入，{len(ambiguous_by_file)} 个存在歧义端口"
        )
        if unverified_count:
            coverage += f"；另有 {unverified_count} 个 MF4 未能读取有效通道目录"
        if ambiguous_by_file and not unique_missing:
            unique_ambiguous = {
                match.required: len(match.candidates)
                for matches in ambiguous_by_file for match in matches
            }
            sample = ", ".join(
                f"{signal}（{count} 个候选）"
                for signal, count in list(unique_ambiguous.items())[:3]
            )
            return (
                CheckResult(
                    "runtime_data_signal_contract", "warning", True,
                    f"数据与 Runtime XML 无法唯一匹配：{coverage}；"
                    f"如 {sample}。这是一条兼容性告警，任务仍会继续。",
                    repair_hint="提供与该 Runtime XML 配套的 MF4，或修正 Runtime 端口映射后重试。",
                ),
                "runtime_data_signal_ambiguous",
            )
        sample = ", ".join(unique_missing[:3])
        return (
            CheckResult(
                "runtime_data_signal_contract", "warning", True,
                f"数据与 Runtime XML 不匹配：{coverage}；已验证缺少 {len(unique_missing)} 个 "
                f"DataPlayer 输入（如 {sample}）。这是一条兼容性告警，任务仍会继续。",
                repair_hint="更换与该 Runtime XML 匹配的 MF4 数据，或更换匹配数据的 Runtime XML。",
            ),
            "runtime_data_signal_missing",
        )
    if unverified_count:
        return (
            CheckResult(
                "runtime_data_signal_contract", "warning", True,
                f"无法验证 Runtime XML 数据输入：{unverified_count} 个 MF4 未能读取有效通道目录/文件头；任务仍会继续。",
                repair_hint="确认 MF4 文件完整且格式可读，并在 Windows 连接组件中安装/修复 asammdf 后重试。",
            ),
            "runtime_data_signal_unverified",
        )
    return (
        CheckResult(
            "runtime_data_signal_contract", "info", True,
            f"运行时数据契约满足：已验证读取 {readable_count} 个 MF4，包含 {len(required)} 个 DataPlayer 输入；"
            f"端口-文件验证 {exact_matches + normalized_matches} 次（精确 {exact_matches}，"
            f"规范化唯一 {normalized_matches}；每个规范化端口均只有 1 个逻辑候选）。",
        ),
        "",
    )


def assert_runtime_data_signal_contract(input_path: str | Path, required_signals: list[str]) -> CheckResult:
    """Return a non-blocking Runtime/MF4 compatibility diagnostic.

    The historical name is retained for Windows Agent compatibility. Runtime
    port inspection is evidence for troubleshooting, not authorization to
    cancel an otherwise runnable simulation; final success is determined from
    Selena/Cluster execution and result collection.
    """
    result, code = _runtime_data_signal_check(input_path, required_signals)
    del code
    return result


def check_runtime_data_signal_contract(config: dict[str, Any]) -> CheckResult:
    """Strictly validate Runtime.xml DataPlayer inputs against the selected MF4s."""
    simulation = config.get("simulation", {}) or {}
    assets = config.get("assets", {}) or {}
    paths = config.get("paths", {}) or {}
    runtime_xml = str(
        simulation.get("runtime_xml")
        or assets.get("runtime_xml")
        or paths.get("runtime_xml")
        or ""
    )
    input_mf4 = str(
        simulation.get("input_mf4")
        or paths.get("input_mf4")
        or (simulation.get("datasets", [{}])[0].get("input_mf4") if simulation.get("datasets") else "")
        or ""
    )
    try:
        required = runtime_data_player_ports(runtime_xml)
    except RuntimeDataSignalContractError as exc:
        return CheckResult(
            "runtime_data_signal_contract", "warning", True,
            f"{exc.detail} 运行时数据兼容性未验证，任务仍会继续。", exc.repair_hint
        )
    if not runtime_xml:
        return CheckResult(
            "runtime_data_signal_contract", "error", False,
            "未提供 Runtime XML，无法确认仿真数据是否匹配。",
            repair_hint="选择与 Selena 产物匹配的 Runtime XML 后重试。",
        )
    return _runtime_data_signal_check(input_mf4, required)[0]


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

def run_preflight(
    config: dict[str, Any],
    *,
    strict_runtime_data_signals: bool = False,
) -> PreflightResult:
    """Run compatibility checks and, when requested, the Runtime/MF4 contract.

    A result with ``ok=False`` must block dispatch (PRD §1.6.3 hard intercept).
    ``strict_runtime_data_signals`` is used by product v2 execution paths. It
    enables Runtime/MF4 evidence collection for task diagnostics. The evidence
    never cancels a simulation by itself: measurements may use a generated
    metadata representation which static inspection cannot fully model. Legacy
    CLI callers retain the historical best-effort three-check behaviour unless
    they opt in.
    """
    result = PreflightResult()
    result.add(check_fingerprint(config))
    result.add(check_interface(config))
    result.add(check_signal_contract(config))
    if strict_runtime_data_signals or bool(config.get("_strict_runtime_data_signals")):
        result.add(check_runtime_data_signal_contract(config))
    return result
