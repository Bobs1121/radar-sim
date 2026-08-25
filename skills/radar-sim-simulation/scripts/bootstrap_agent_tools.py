#!/usr/bin/env python3
"""Bootstrap the source-free radar-sim MCP from the provider service.

This file is intentionally standard-library only.  A Skill can therefore use
it before the radar-sim MCP has been registered in the Agent.  The downloaded
server-side installer performs Manifest/SHA-256 verification and installs the
SDK, MCP and Skill bundle side-by-side without downloading a source checkout.

The script prints one JSON result and never prints a token or file contents.
It is safe to run repeatedly: the server installer returns ``already_current``
when the content-addressed local release is already active.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
import uuid


MAX_INSTALLER_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120.0


class BootstrapFailure(RuntimeError):
    """A stable, user-actionable bootstrap failure."""

    code = "agent_tools_bootstrap_failed"


def _default_root() -> Path:
    override = os.environ.get("RADAR_SIM_MCP_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip() or str(
            Path.home() / "AppData" / "Local"
        )
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or str(
            Path.home() / ".local" / "share"
        )
    return (Path(base) / "radar-sim-mcp").resolve()


def _internal_log(root: Path, message: str) -> None:
    """Write silent bootstrap diagnostics to the local hidden terminal log."""

    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "agent-tools.log").open("a", encoding="utf-8") as handle:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        return


def _stable_user() -> str:
    configured = os.environ.get("RADAR_SIM_USER", "").strip()
    if configured:
        return configured
    try:
        login = getpass.getuser().strip().casefold()
    except Exception:
        login = ""
    return "user-" + (login or "default")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapFailure("本机仿真服务状态文件不可读") from exc
    if not isinstance(value, dict):
        raise BootstrapFailure("本机仿真服务状态文件格式无效")
    return dict(value)


def _configured_local_url() -> str:
    config_path = _default_root() / "mcp-config.json"
    if not config_path.is_file():
        return ""
    value = _read_json(config_path)
    env = value.get("env")
    return str(env.get("RADAR_SIM_BASE_URL") or "") if isinstance(env, dict) else ""


def _profile_urls() -> list[str]:
    profile_path = Path(__file__).resolve().parents[1] / "references" / "service-profile.json"
    if not profile_path.is_file():
        return []
    value = _read_json(profile_path)
    candidates: list[str] = []
    single = str(value.get("service_url") or "").strip()
    if single:
        candidates.append(single)
    multiple = value.get("service_urls")
    if isinstance(multiple, list):
        candidates.extend(str(item or "").strip() for item in multiple if str(item or "").strip())
    return list(dict.fromkeys(candidates))


def _normalize_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise BootstrapFailure("仿真服务地址必须使用 http 或 https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BootstrapFailure("仿真服务地址不能包含账号、密码、查询参数或片段")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def resolve_server_url(explicit: str = "") -> tuple[str, str]:
    """Resolve the provider URL without asking the user for infrastructure data."""

    candidates: list[tuple[str, str]] = [
        (str(explicit or "").strip(), "argument"),
        (os.environ.get("RADAR_SIM_SERVICE_URL", "").strip(), "RADAR_SIM_SERVICE_URL"),
        (os.environ.get("RADAR_SIM_BASE_URL", "").strip(), "RADAR_SIM_BASE_URL"),
        (_configured_local_url(), "local_installation"),
    ]
    candidates.extend((item, "skill_profile") for item in _profile_urls())
    for raw, source in candidates:
        if raw:
            return _normalize_url(raw), source
    raise BootstrapFailure("Skill 未包含可用的仿真服务地址")


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "text/plain, application/json"}
    token = os.environ.get("RADAR_SIM_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    user = os.environ.get("RADAR_SIM_USER", "").strip() or _stable_user()
    if user:
        headers["X-Rsim-User"] = user
    return headers


def _download_installer(url: str, destination: Path, timeout_seconds: float) -> None:
    request = Request(
        url + "/api/v1/agent-tools/install.py",
        headers=_headers(),
        method="GET",
    )
    try:
        with urlopen(request, timeout=max(10.0, float(timeout_seconds))) as response:
            advertised = str(response.headers.get("Content-Length") or "").strip()
            if advertised:
                try:
                    if int(advertised) > MAX_INSTALLER_BYTES:
                        raise BootstrapFailure("仿真服务安装引导文件过大")
                except ValueError:
                    pass
            total = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise BootstrapFailure("仿真服务安装引导文件过大")
                    handle.write(chunk)
    except BootstrapFailure:
        raise
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise BootstrapFailure("仿真服务需要有效的登录凭据") from exc
        if exc.code == 503:
            raise BootstrapFailure("仿真服务尚未发布可用的 Agent Tools 版本") from exc
        raise BootstrapFailure(f"仿真服务暂时不可达（HTTP {exc.code}）") from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise BootstrapFailure("无法连接仿真服务，请稍后重试") from exc


def _acquire_lock(root: Path, timeout_seconds: float) -> tuple[int, Path]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "bootstrap.lock"
    deadline = time.monotonic() + max(10.0, float(timeout_seconds))
    while True:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii", errors="ignore"))
            return descriptor, lock_path
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise BootstrapFailure("已有仿真服务安装正在进行，请稍后重试")
            time.sleep(0.25)
        except OSError as exc:
            raise BootstrapFailure("无法创建仿真服务安装锁") from exc


def _release_lock(descriptor: int, lock_path: Path) -> None:
    try:
        os.close(descriptor)
    finally:
        lock_path.unlink(missing_ok=True)


def bootstrap(server_url: str = "", *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    url, _url_source = resolve_server_url(server_url)
    root = _default_root()
    descriptor, lock_path = _acquire_lock(root, timeout_seconds)
    try:
        _internal_log(root, "bootstrap started")
        with tempfile.TemporaryDirectory(prefix="radar-sim-skill-bootstrap-") as temporary:
            installer = Path(temporary) / "install_agent_tools.py"
            _download_installer(url, installer, timeout_seconds)
            environment = dict(os.environ)
            environment.setdefault("RADAR_SIM_USER", _stable_user())
            completed = subprocess.run(
                [sys.executable, str(installer), "--server-url", url],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=max(30.0, float(timeout_seconds) + 30.0),
            )
            if completed.returncode != 0:
                raise BootstrapFailure("仿真服务组件安装失败，之前的安装保持不变")
            output = str(completed.stdout or "").strip().splitlines()
            result: dict[str, Any] = {}
            if output:
                try:
                    parsed = json.loads(output[-1])
                    if isinstance(parsed, dict):
                        result = dict(parsed)
                except (ValueError, json.JSONDecodeError):
                    pass
            if not result:
                result = {"status": "installed"}
            result["mcp_config_path"] = str(result.get("mcp_config_path") or root / "mcp-config.json")
            result["next_action"] = (
                "register_or_reload_stdio_mcp"
                if result.get("restart_required", True)
                else "use_registered_radar_sim_mcp"
            )
            _internal_log(root, f"bootstrap completed: {result.get('status', 'unknown')}")
            return result
    except subprocess.TimeoutExpired as exc:
        _internal_log(root, "bootstrap timed out")
        raise BootstrapFailure("仿真服务组件安装超时，之前的安装保持不变") from exc
    finally:
        _release_lock(descriptor, lock_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default="")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        result = bootstrap(args.server_url, timeout_seconds=args.timeout_seconds)
    except BootstrapFailure as exc:
        print(json.dumps({"status": "failed", "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
