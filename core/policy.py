"""Unified run-policy derivation for all source × data × backend combinations.

Users choose three things: Selena source (build | path), data path (local | UNC),
and backend (local | cluster). Everything else — whether to copy/stage Selena,
whether to copy/stage data, where output goes — is derived here so the decision
is made in one place instead of being scattered across api/cluster/run.

The 8-combination matrix:

  source | data  | backend | copy_selena | copy_data | output_local
  -------|-------|---------|-------------|-----------|-------------
  build  | local | local   | False       | False     | True
  build  | UNC   | local   | False       | True      | True
  build  | local | cluster | True        | True      | False
  build  | UNC   | cluster | True        | False     | False
  path   | local | local   | False       | False     | True
  path   | UNC   | local   | False       | True      | True
  path   | local | cluster | True        | True      | False
  path   | UNC   | cluster | False*      | False     | False

  * path+cluster: copy_selena is True when selena_exe is a local-drive path
    (worker can't see it), False when selena_exe is UNC (worker can see it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunPolicy:
    """Derived decisions for one (source, data, backend) triple."""

    source: str            # "build" | "path"
    data_is_unc: bool      # data path is a UNC share
    backend: str           # "local" | "cluster"
    selena_exe: str        # source=path: user-provided exe; source=build: ""
    copy_selena: bool      # package/copy selena runtime to the execution host
    copy_data: bool        # copy/stage data to the execution host
    output_local: bool     # write output MF4 to local results/ (not beside input)
    rationale: str         # human-readable explanation for UI display


def derive_run_policy(
    *,
    source: str,
    data_path: str,
    backend: str,
    selena_exe: str = "",
) -> RunPolicy:
    """Derive copy/staging decisions from the user's three choices.

    Does NOT compute staging paths — those need a run_id / workspace root that
    the caller owns. Callers (cluster.prepare_cluster_job, run._validate_and_stage)
    keep their existing path logic; this function only centralizes the booleans.
    """
    source = str(source or "build").lower()
    backend = str(backend or "local").lower()
    data_is_unc = bool(data_path) and str(data_path).startswith("\\\\")
    selena_exe = str(selena_exe or "")
    selena_exe_is_local = bool(selena_exe) and not selena_exe.startswith("\\\\")

    # copy_selena: local backend never packages (selena runs where it is).
    # cluster + build: always package the local compiled runtime.
    # cluster + path: package only if the exe is on a local drive (worker can't see it).
    if backend == "local":
        copy_selena = False
    elif source == "build":
        copy_selena = True
    else:  # source == "path" + cluster
        copy_selena = selena_exe_is_local

    # copy_data: local backend downloads UNC data (slow read / can't write output
    # back to a read-only share). cluster backend migrates local data to the shared
    # workspace (worker can't see local D:\); UNC data is referenced in place.
    if backend == "local":
        copy_data = data_is_unc
    else:
        copy_data = not data_is_unc

    output_local = backend == "local"

    rationale = _rationale(source, data_is_unc, backend, copy_selena, copy_data, output_local, selena_exe_is_local)
    return RunPolicy(
        source=source,
        data_is_unc=data_is_unc,
        backend=backend,
        selena_exe=selena_exe,
        copy_selena=copy_selena,
        copy_data=copy_data,
        output_local=output_local,
        rationale=rationale,
    )


def _rationale(source, data_unc, backend, copy_selena, copy_data, output_local, exe_local) -> str:
    parts = []
    if backend == "local":
        parts.append("本地仿真")
        if copy_data:
            parts.append("UNC 数据下载到本地（避免服务器慢读/写失败）")
        else:
            parts.append("本地数据原地引用")
        parts.append("输出写本地 results/")
    else:
        parts.append("集群仿真")
        if copy_selena:
            parts.append("Selena 打包推送到共享盘" if source == "build" else "本地 Selena 打包推送")
        else:
            parts.append("Selena UNC 路径原地引用")
        if copy_data:
            parts.append("本地数据迁移到共享盘")
        else:
            parts.append("UNC 数据原地引用")
        parts.append("输出写共享盘 job 目录")
    return "；".join(parts)


def policy_from_config(config: dict[str, Any], data_path: str) -> RunPolicy:
    """Derive a policy from an applied config + data path.

    Reads source/backend/selena_exe from the config's active profile (set by
    apply_profile). Used by core.api._auto_copy_policy and submit_cluster.
    """
    from core.profiles import get_profile

    source = str(config.get("_profile_selena_source") or "build")
    selena_exe = str((config.get("cluster") or {}).get("selena_exe") or "")
    backend = str(config.get("active_backend") or "local")
    # active_backend (set by apply_profile) is authoritative; fall back to profile
    # only when active_backend is unset.
    try:
        active_name = str(config.get("active_profile") or "default")
        prof = get_profile(config, active_name)
        source = str((prof.get("selena") or {}).get("source") or source or "build")
        if not selena_exe:
            selena_exe = str((prof.get("selena") or {}).get("exe") or selena_exe)
        if not config.get("active_backend"):
            backend = str(prof.get("backend") or backend)
    except Exception:
        pass
    return derive_run_policy(source=source, data_path=data_path, backend=backend, selena_exe=selena_exe)
