"""Unified environment checks for local and cluster backends.

Both ``rsim check`` and the preflight inside ``rsim run`` / ``rsim cluster run``
go through here so a single source of truth describes what "ready to simulate"
means for each backend.

CheckItem carries a severity (error|warning|info) and category
(repo|selena|runtime|data|cluster|profile). CheckReport.ok is true only when no
error-severity item has failed; warnings do not block.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from core.cluster import CheckItem, check_cluster_environment as _check_cluster_environment
from core.config import resolve_selena_executable
from core.data import check_data_access
from core.profiles import apply_profile, resolve_selena_exe
from core.simulation import get_simulation_config


@dataclass
class CheckReport:
    """Aggregated environment check result for one backend+profile."""

    backend: str
    profile: str
    items: list[CheckItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" and not item.ok for item in self.items)

    @property
    def errors(self) -> list[CheckItem]:
        return [item for item in self.items if item.severity == "error" and not item.ok]

    @property
    def warnings(self) -> list[CheckItem]:
        return [item for item in self.items if item.severity == "warning" and not item.ok]

    @property
    def passed(self) -> list[CheckItem]:
        return [item for item in self.items if item.ok]

    def __iter__(self) -> Iterator[CheckItem]:
        # Transitional compatibility: let old code iterate the report directly.
        return iter(self.items)


def check_for_backend(config: dict[str, Any], backend: str = "", *, profile: str = "") -> CheckReport:
    """Run the environment checks appropriate for a backend.

    ``backend`` defaults to the profile's backend when empty. Returns a
    CheckReport (also iterable for backwards compatibility).
    """
    configured = apply_profile(config, profile)
    target = (backend or configured.get("active_backend") or "local").strip().lower()
    if target == "cluster":
        items = _check_cluster_environment(configured, profile=profile)
    else:
        items = check_local_environment(configured)
    return CheckReport(
        backend=target,
        profile=str(configured.get("active_profile") or profile or "default"),
        items=items,
    )


def check_local_environment(config: dict[str, Any]) -> list[CheckItem]:
    """Check local simulation prerequisites: repo, selena, assets, data."""
    from core.repo import check_repo_context
    from core.simulation import detect_radar_orientation

    items: list[CheckItem] = []
    sim = get_simulation_config(config)
    profile = _active_profile_dict(config)

    # Repo context (branch/submodule/working tree).
    items.extend(check_repo_context(config, allow_switch=True))

    # Selena executable.
    selena_exe = resolve_selena_exe(config, profile)
    if selena_exe and Path(selena_exe).exists():
        items.append(CheckItem("Selena executable", True, selena_exe, "info", "selena"))
        _check_selena_branch_freshness(config, selena_exe, items)
    else:
        items.append(CheckItem(
            "Selena executable", False,
            f"{selena_exe or '(not configured)'} — run 'rsim build selena' or set profile selena.exe",
            "error", "selena",
            repair_hint="Compile Selena from the configured branch, or switch to an existing selena.exe path.",
            auto_repairable=True,
            repair_action="build_selena",
        ))

    # Runtime assets.
    runtime_xml = str(sim.get("runtime_xml") or "")
    if runtime_xml:
        items.append(_path_item("Runtime XML", Path(runtime_xml), "runtime"))
    matfilefilter = str(sim.get("matfilefilter") or "")
    if matfilefilter:
        items.append(_path_item("MAT file filter", Path(matfilefilter), "runtime"))
    adapter_file = str(sim.get("adapter_file") or "")
    if adapter_file:
        items.append(_path_item("Adapter file", Path(adapter_file), "runtime"))

    # Data reachability + radar orientation for configured datasets.
    datasets = sim.get("datasets", []) or []
    if datasets:
        for dataset in datasets:
            name = dataset.get("name", "<unnamed>")
            input_dir = str(dataset.get("input_dir") or "")
            input_mf4 = str(dataset.get("input_mf4") or "")
            data_path = input_mf4 or input_dir
            if not data_path:
                continue
            access = check_data_access(data_path)
            items.append(CheckItem(
                f"Dataset '{name}' data", access.ok,
                f"{access.kind}: {data_path} — {access.detail}",
                "error" if not access.ok else "info", "data",
            ))
    else:
        items.append(CheckItem("Datasets", True, "no datasets configured (pass an MF4 path at run time)", "info", "data"))

    items.extend(_check_tcc(config))
    items.append(CheckItem("Profile", True, str(config.get("active_profile") or "default"), "info", "profile"))
    return items


def _check_tcc(config: dict[str, Any]) -> list[CheckItem]:
    """TCC / itc2 checks: manager present, required toolcollection installed, init.bat available."""
    from core.tcc import detect_itc2, detect_ito_share, check_toolcollection, read_required_toolcollection

    items: list[CheckItem] = []
    itc2 = detect_itc2(config)
    if itc2.installed:
        items.append(CheckItem(
            "TCC 管理器 (itc2.exe)", True,
            f"{itc2.exe_path} (v{itc2.version})", "info", "tcc",
        ))
        tc_name = read_required_toolcollection(config)
        if tc_name:
            tc = check_toolcollection(config, tc_name)
            items.append(CheckItem(
                f"编译工具集 ({tc_name})", tc.installed,
                tc.detail, "error" if not tc.installed else "info", "tcc",
                repair_hint=f"运行 itc2.exe install {tc_name}，或点击自动安装",
                auto_repairable=True,
                repair_action="install_toolcollection",
            ))
            if tc.installed and not tc.init_bat_present:
                items.append(CheckItem(
                    "init.bat 可执行", False,
                    f"未找到 {tc_name} 的 init.bat — 工具集可能不完整，尝试重装",
                    "warning", "tcc",
                    repair_hint=f"重装工具集：itc2.exe install {tc_name}",
                    auto_repairable=True,
                    repair_action="install_toolcollection",
                ))
        else:
            items.append(CheckItem(
                "编译工具集", True,
                "未配置 ip_if/tcc_toolversion_itc2.txt（旧项目，跳过 TCC 检查）", "info", "tcc",
            ))
        # Show derived dependencies from the build script (cmake_build.bat).
        from core.tcc import derive_dependencies_from_build_script
        deps = derive_dependencies_from_build_script(config)
        if deps:
            tc_names = [d["name"] for d in deps if d["kind"] == "toolcollection"]
            env_vars = [d["name"] for d in deps if d["kind"] == "env_var"]
            detail = f"toolcollection: {', '.join(tc_names) or '(none)'}; env: {', '.join(env_vars[:5]) or '(none)'}"
            items.append(CheckItem(
                "编译依赖（来自编译脚本）", True,
                detail, "info", "tcc",
            ))
    else:
        ito_ok, ito_share = detect_ito_share(config)
        hint = "点击自动从 ITO 共享盘安装 itc2" if ito_ok else "请连 Bosch 内网/VPN 后点击自动安装 itc2"
        items.append(CheckItem(
            "TCC 管理器 (itc2.exe)", False,
            f"未找到 {itc2.exe_path}" + ("" if ito_ok else "，且 ITO 共享盘不可达"),
            "error", "tcc",
            repair_hint=hint,
            auto_repairable=ito_ok,
            repair_action="bootstrap_itc2",
        ))
    return items


def _check_selena_branch_freshness(config: dict[str, Any], selena_exe: str, items: list[CheckItem]) -> None:
    """Warn if selena.exe looks older than the target branch's last commit."""
    target_branch = str(config.get("_profile_selena_branch") or config.get("build", {}).get("selena_branch") or "")
    if not target_branch:
        return
    repos = config.get("repos") or {}
    inner_repo = repos.get("inner_repo_root") or ""
    if not inner_repo or not Path(inner_repo).exists():
        return
    try:
        current = subprocess.run(
            ["git", "-C", inner_repo, "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
        if current.returncode == 0 and current.stdout.strip() != target_branch:
            items.append(CheckItem(
                "Selena branch match", False,
                f"inner repo on '{current.stdout.strip()}', profile expects '{target_branch}' — selena.exe may be from another branch",
                "warning", "selena",
            ))
        ref = Path(inner_repo) / ".git" / "refs" / "heads" / target_branch.replace("/", "-")
        # git may also pack refs; fall back to packed-refs lookup is complex, keep it simple.
        if ref.exists() and Path(selena_exe).exists():
            exe_mtime = Path(selena_exe).stat().st_mtime
            ref_mtime = ref.stat().st_mtime
            if exe_mtime < ref_mtime:
                items.append(CheckItem(
                    "Selena exe freshness", False,
                    f"selena.exe (built {int(exe_mtime)}) predates branch '{target_branch}' last commit ({int(ref_mtime)}) — consider 'rsim build selena'",
                    "warning", "selena",
                ))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def _active_profile_dict(config: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the active profile dict from the applied config."""
    name = str(config.get("active_profile") or "default")
    cluster = config.get("cluster") or {}
    sim = get_simulation_config(config)
    selena_branch = str(config.get("_profile_selena_branch") or "")
    return {
        "name": name,
        "selena": {
            "source": "path" if cluster.get("selena_exe") else "build",
            "exe": str(cluster.get("selena_exe") or ""),
            "selena_branch": selena_branch,
        },
        "runtime_xml": str(sim.get("runtime_xml") or ""),
    }


def _path_item(name: str, path: Path, category: str = "") -> CheckItem:
    if path.exists():
        return CheckItem(name, True, str(path), "info", category)
    hint = ""
    if category == "runtime":
        hint = "Set the correct path in the Configuration tab."
    elif category == "selena":
        hint = "Compile Selena or point at an existing selena.exe."
    return CheckItem(name, False, f"not found: {path}", "error", category, repair_hint=hint)
