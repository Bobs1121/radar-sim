"""rsim web - lightweight local Web Console for local and Cluster simulation."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cli.cluster import _diagnose_wait_state
from core.cluster import (
    check_cluster_environment,
    detect_python2_candidates,
    fetch_cluster_job,
    get_cluster_web_status,
    inspect_cluster_job,
    list_cluster_jobs,
    list_cluster_profiles,
    package_to_dict,
    prepare_cluster_job,
    scan_cluster_data,
    submit_cluster_job,
)
from core.config import list_projects, load_config


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"


def register(subparsers):
    p = subparsers.add_parser("web", help="Start the local Radar Sim Web Console")
    p.add_argument("--host", default="127.0.0.1", help="Bind host")
    p.add_argument("--port", type=int, default=8765, help="Bind port")
    p.add_argument("--control-port", type=int, default=8877,
                   help="Port for the embedded control server (0 = random free port)")
    p.add_argument("--no-control", action="store_true",
                   help="Disable the embedded control server+agent (use legacy BuildTaskRegistry)")
    p.add_argument("--server-url", default="",
                   help="Remote control server URL (e.g. http://10.190.171.44:8877). "
                        "When set, web forwards task ops to the remote server instead of starting an embedded one.")
    p.add_argument("--user", default="",
                   help="User identity for the remote server (default: RSIM_USER env or OS user). "
                        "Jobs/logs are isolated per-user on the server.")


def run(args, config):
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8765))
    default_project = getattr(args, "project", None) or config.get("_meta", {}).get("project") or ""

    server_url = (getattr(args, "server_url", "") or "").strip()
    if server_url:
        # Remote mode: forward to a remote control server, no embedded server/agent.
        from core.remote_control import RemoteControlClient
        from core.user import current_user
        import core.web_control as web_control
        user = (getattr(args, "user", "") or "").strip() or current_user()
        client = RemoteControlClient(server_url, user)
        web_control.set_remote_client(client)
        print(f"Control plane: remote {server_url} (user={user})", flush=True)
        print("Tasks execute on the remote server's agent; this web instance only submits/polls.", flush=True)
    elif not getattr(args, "no_control", False):
        control_url = _start_embedded_control(getattr(args, "control_port", 8877))
        print(f"Control plane: {control_url} (embedded server + agent)", flush=True)
    else:
        print("Control plane: disabled (--no-control), using legacy BuildTaskRegistry", flush=True)

    handler = _make_handler(default_project)
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError:
        # Port in use (e.g. another user on the same machine) → fall back to a free port.
        server = ThreadingHTTPServer((host, 0), handler)
        port = server.server_address[1]
    print(f"Radar Sim Web Console: http://{host}:{port}/", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def _start_embedded_control(preferred_port: int) -> str:
    """Start an embedded control server + polling agent; return the server URL.

    The agent reuses ``cli.agent`` logic but polls this in-process server over
    HTTP so the same code path serves both embedded and standalone modes.
    """
    from core.control_http import make_control_handler
    from core.control_service import ControlService
    from core.user import control_db_path_for_user, current_user
    import core.web_control as web_control
    import threading as _t

    # Per-user service cache (embedded web is single-user, but this keeps the
    # path consistent with the standalone multi-user server).
    _cache: dict = {}
    _lock = _t.Lock()

    def factory(user: str) -> ControlService:
        with _lock:
            if user not in _cache:
                _cache[user] = ControlService(control_db_path_for_user(user))
            return _cache[user]

    # The web console's own adapter uses the current user's service.
    web_control.set_service(factory(current_user()))

    # Bind control server; fall back to a random free port if preferred is taken.
    handler = make_control_handler(factory)
    try:
        ctrl_server = ThreadingHTTPServer(("127.0.0.1", int(preferred_port or 8877)), handler)
    except OSError:
        ctrl_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    ctrl_port = ctrl_server.server_address[1]
    ctrl_url = f"http://127.0.0.1:{ctrl_port}"
    threading.Thread(target=ctrl_server.serve_forever, daemon=True).start()

    # Embedded agent: poll the control server and execute claimed tasks locally.
    _start_embedded_agent(ctrl_url)
    return ctrl_url


def _start_embedded_agent(server_url: str) -> None:
    """Run a polling agent loop in a daemon thread (reuses cli.agent internals)."""
    from cli.agent import _ControlClient, _run_task, DEFAULT_CAPABILITIES
    from core.user import current_user
    import os
    import platform as platform_mod
    import socket

    client = _ControlClient(server_url, timeout=30)
    user = current_user()
    # Unique per (user, pid) so two users / two web instances on one machine don't collide.
    agent_id = f"embedded-{user}-{os.getpid()}"
    try:
        agent = client.register_agent(
            name=f"{socket.gethostname()}-embedded",
            agent_id=agent_id,
            hostname=socket.gethostname(),
            platform=platform_mod.platform(),
            capabilities=list(DEFAULT_CAPABILITIES),
            metadata={"cwd": str(ROOT), "embedded": True, "user": user},
        )
        agent_id = agent["agent_id"]
        print(f"Embedded agent registered: {agent_id} (capabilities={len(DEFAULT_CAPABILITIES)})", flush=True)
    except Exception as exc:
        print(f"[WARN] embedded agent failed to register: {exc}", flush=True)
        return

    def loop():
        import time
        while True:
            try:
                claim = client.poll(agent_id)
                task = claim.get("task")
                if task:
                    _run_task(client, agent_id, task, heartbeat_interval=10.0)
                else:
                    time.sleep(3.0)
            except Exception as exc:
                print(f"[WARN] embedded agent poll error: {exc}", flush=True)
                time.sleep(5.0)

    threading.Thread(target=loop, daemon=True).start()


def _make_handler(default_project: str):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

        def guess_type(self, path):  # noqa: D401
            content_type = super().guess_type(path)
            if content_type in {"text/html", "text/css", "text/javascript", "application/javascript"}:
                return f"{content_type}; charset=utf-8"
            return content_type

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                return self._handle_api_get(parsed)
            if parsed.path == "/":
                self.path = "/index.html"
            return super().do_GET()

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                return self._handle_api_post(parsed)
            self.send_error(404)

        def _handle_api_get(self, parsed):
            query = parse_qs(parsed.query)
            project = query.get("project", [default_project or _default_project()])[0]
            if parsed.path == "/api/projects":
                return self._json({"projects": list_projects(), "default": project})
            if parsed.path == "/api/profiles":
                from core.profiles import list_profiles
                cfg = load_config(project)
                return self._json({"profiles": list_profiles(cfg)})
            if parsed.path == "/api/check":
                from core.environment import check_for_backend
                cfg = load_config(project)
                profile = query.get("profile", [""])[0]
                backend = query.get("backend", [""])[0]
                report = check_for_backend(cfg, backend, profile=profile)
                return self._json(_report_to_dict(report))
            if parsed.path == "/api/user-config":
                from core.config import get_user_config
                return self._json(get_user_config(project))
            if parsed.path == "/api/config/list-files":
                from core.config import get_projects_dir, get_data_root
                files = []
                seen = set()
                # In-repo local.yaml files (shared defaults).
                for local in sorted((get_projects_dir()).glob("*/local.yaml")):
                    files.append({"project": local.parent.name, "path": str(local)})
                    seen.add((local.parent.name,))
                # Per-user local.yaml files under RSIM_HOME (if set).
                user_projects = get_data_root() / "config" / "projects"
                if user_projects != get_projects_dir() and user_projects.exists():
                    for local in sorted(user_projects.glob("*/local.yaml")):
                        files.append({"project": local.parent.name, "path": str(local)})
                return self._json({"files": files})
            if parsed.path == "/api/config/load":
                import core.config as _cfgmod
                path = query.get("path", [""])[0]
                if not path:
                    return self._json({"error": "path required"}, 400)
                local_path = Path(path)
                try:
                    project = local_path.relative_to(_cfgmod.get_projects_dir()).parts[0]
                except ValueError:
                    return self._json({"error": "path must be under config/projects/"}, 400)
                return self._json({
                    "project": project,
                    "local_yaml_path": str(local_path),
                    "user_config": _cfgmod.get_user_config(project),
                    "effective_config": _safe_config(_cfgmod.load_config(project)),
                })
            if parsed.path == "/api/config/export":
                from core.config import local_yaml_path_for_project, get_projects_dir
                local_path = local_yaml_path_for_project(project)
                if not local_path.exists():
                    local_path = get_projects_dir() / project / "local.yaml"
                if not local_path.exists():
                    return self._json({"error": f"no local.yaml for {project}"}, 404)
                return self._json({
                    "project": project,
                    "filename": "local.yaml",
                    "yaml_content": local_path.read_text(encoding="utf-8"),
                })
            if parsed.path == "/api/build/check":
                from core.config import resolve_selena_executable
                cfg = load_config(project)
                exe = resolve_selena_executable(cfg)
                has_exe = bool(exe) and Path(exe).exists()
                return self._json({"has_exe": has_exe, "exe_path": exe, "stale": False})
            if parsed.path == "/api/tasks":
                import core.web_control as web_control
                limit = int(query.get("limit", ["20"])[0] or 20)
                tasks = web_control.list_jobs_via_control(limit)
                # Merge legacy registry tasks (tcc history from before control-plane migration).
                try:
                    from core.build_runner import get_registry
                    tasks = tasks + get_registry().list_tasks(limit)
                except Exception:
                    pass
                return self._json({"tasks": tasks})
            if parsed.path == "/api/agents":
                import core.web_control as web_control
                return self._json({"agents": web_control.list_agents_via_control()})
            if parsed.path == "/api/build/status":
                import core.web_control as web_control
                task_id = query.get("task_id", [""])[0]
                since = int(query.get("since", ["0"])[0] or 0)
                return self._json(_tail_task(task_id, since))
            if parsed.path == "/api/sim/status":
                import core.web_control as web_control
                task_id = query.get("task_id", [""])[0]
                since = int(query.get("since", ["0"])[0] or 0)
                return self._json(_tail_task(task_id, since))
            if parsed.path == "/api/config":
                return self._json(_safe_config(load_config(project)))
            if parsed.path == "/api/cluster/check":
                cfg = load_config(project)
                return self._json({"items": [item.__dict__ for item in check_cluster_environment(cfg, profile=query.get("profile", [""])[0])]})
            if parsed.path == "/api/cluster/profiles":
                cfg = load_config(project)
                return self._json({"profiles": list_cluster_profiles(cfg)})
            if parsed.path == "/api/cluster/python":
                cfg = load_config(project)
                configured = str((cfg.get("cluster") or {}).get("python_path") or "")
                return self._json({"items": [item.__dict__ for item in detect_python2_candidates(configured)]})
            if parsed.path == "/api/cluster/jobs":
                cfg = load_config(project)
                limit = int(query.get("limit", ["20"])[0])
                return self._json({"jobs": list_cluster_jobs(cfg, limit=limit)})
            if parsed.path == "/api/cluster/data":
                cfg = load_config(project)
                signals = query.get("required_signal", [])
                return self._json(
                    scan_cluster_data(
                        cfg,
                        input_path=query.get("input_path", [""])[0],
                        dataset=query.get("dataset", [""])[0],
                        profile=query.get("profile", [""])[0],
                        required_signals=signals or None,
                        limit=int(query.get("limit", ["20"])[0]),
                        max_read_mb=int(query.get("max_read_mb", ["8"])[0]),
                    )
                )
            if parsed.path == "/api/cluster/status":
                job_dir = query.get("job_dir", [""])[0]
                return self._json(inspect_cluster_job(job_dir))
            if parsed.path == "/api/cluster/web-status":
                cfg = load_config(project)
                job = query.get("job", query.get("job_dir", [""]))[0]
                return self._json(get_cluster_web_status(cfg, job))
            if parsed.path == "/api/cluster/wait":
                cfg = load_config(project)
                job = query.get("job", query.get("job_dir", [""]))[0]
                job_dir = query.get("job_dir", [""])[0]
                web_status = get_cluster_web_status(cfg, job)
                include_shared = str(query.get("shared", ["0"])[0]).lower() in {"1", "true", "yes"}
                shared_status = inspect_cluster_job(job_dir) if include_shared and job_dir else {}
                return self._json(
                    {
                        "web": web_status,
                        "shared": shared_status,
                        "diagnosis": _diagnose_wait_state(web_status, shared_status, max_minutes=0),
                    }
                )
            if parsed.path == "/api/local/check":
                return self._json(_run_rsim_check(project))
            self.send_error(404)

        def _handle_api_post(self, parsed):
            body = self.rfile.read(int(self.headers.get("content-length", "0") or 0))
            payload = json.loads(body.decode("utf-8") or "{}")
            project = payload.get("project") or default_project or _default_project()
            cfg = load_config(project)
            if parsed.path == "/api/user-config":
                from core.config import save_local_config
                local_path = save_local_config(project, {k: payload.get(k, "") for k in [
                    "source", "code_path", "env_build_script", "selena_build_script",
                    "selena_branch", "runtime_path", "adapter_path", "data_path", "selena_exe", "backend",
                ]})
                return self._json({"ok": True, "local_yaml_path": str(local_path)})
            if parsed.path == "/api/config/new":
                from core.config import get_projects_dir
                new_project = payload.get("project", "").strip()
                if not new_project:
                    return self._json({"error": "project required"}, 400)
                pdir = get_projects_dir() / new_project
                if pdir.exists():
                    return self._json({"error": f"project {new_project} already exists"}, 400)
                pdir.mkdir(parents=True)
                (pdir / "config.yaml").write_text(
                    f"project:\n  name: \"{new_project}\"\n  platform: \"gen5_selena\"\npaths:\n  project_root: \"\"\n",
                    encoding="utf-8",
                )
                return self._json({"ok": True, "project": new_project, "path": str(pdir / "local.yaml")})
            if parsed.path == "/api/config/import":
                from core.config import local_yaml_path_for_project
                import yaml as _yaml
                yaml_content = payload.get("yaml_content", "")
                mode = payload.get("mode", "replace")
                local_path = local_yaml_path_for_project(project)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    parsed_yaml = _yaml.safe_load(yaml_content) or {}
                except _yaml.YAMLError as exc:
                    return self._json({"error": f"invalid YAML: {exc}"}, 400)
                if mode == "replace":
                    if local_path.exists():
                        bak = local_path.with_suffix(".yaml.bak")
                        bak.write_text(local_path.read_text(encoding="utf-8"), encoding="utf-8")
                    local_path.write_text(yaml_content, encoding="utf-8")
                else:  # merge
                    from core.config import _load_yaml_file, _deep_merge
                    existing = _load_yaml_file(local_path) if local_path.exists() else {}
                    merged = _deep_merge(existing, parsed_yaml)
                    local_path.write_text(_yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
                return self._json({"ok": True, "local_yaml_path": str(local_path), "mode": mode})
            if parsed.path == "/api/build/selena":
                import core.web_control as web_control
                mode = payload.get("mode", "RelWithDebInfo")
                clean = bool(payload.get("clean", False))
                task_id = web_control.start_build_via_control(project, mode=mode, clean=clean)
                return self._json({"task_id": task_id})
            if parsed.path == "/api/build/cancel":
                import core.web_control as web_control
                ok = web_control.cancel_via_control(payload.get("task_id", ""))
                if not ok:
                    # Fall back to legacy registry for pre-migration task_ids.
                    from core.build_runner import get_registry
                    ok = get_registry().cancel(payload.get("task_id", ""))
                return self._json({"ok": ok})
            if parsed.path == "/api/repair":
                return self._json(_run_repair(payload))
            if parsed.path == "/api/sim/start":
                import core.web_control as web_control
                backend = payload.get("backend", "local")
                data_path = payload.get("data_path", "")
                dry_run = bool(payload.get("dry_run", False))
                # Pre-check environment; block if errors.
                from core.environment import check_for_backend
                profile = payload.get("profile", "")
                report = check_for_backend(cfg, backend, profile=profile)
                if not report.ok:
                    return self._json({"blocked": True, "items": _report_to_dict(report)})
                task_id = web_control.start_sim_via_control(project, backend=backend, data_path=data_path, dry_run=dry_run)
                return self._json({"task_id": task_id, "blocked": False})
            if parsed.path == "/api/cluster/prepare":
                package = prepare_cluster_job(
                    cfg,
                    input_path=payload.get("input_path", ""),
                    dataset=payload.get("dataset", ""),
                    profile=payload.get("profile", ""),
                    run_id=payload.get("run_id", ""),
                    copy_data=_optional_bool(payload, "copy_data"),
                    copy_selena=_optional_bool(payload, "copy_selena"),
                )
                return self._json(package_to_dict(package))
            if parsed.path == "/api/cluster/submit":
                result = submit_cluster_job(
                    payload.get("config_path", ""),
                    cfg,
                    dry_run=not bool(payload.get("execute", False)),
                )
                return self._json(result.__dict__)
            if parsed.path == "/api/cluster/run":
                # One-shot prepare + submit (non-blocking). Front-end polls
                # /api/cluster/wait?once=1 to track progress.
                package = prepare_cluster_job(
                    cfg,
                    input_path=payload.get("input_path", ""),
                    dataset=payload.get("dataset", ""),
                    profile=payload.get("profile", ""),
                    run_id=payload.get("run_id", ""),
                    copy_data=_optional_bool(payload, "copy_data"),
                    copy_selena=_optional_bool(payload, "copy_selena"),
                )
                execute = bool(payload.get("execute", False))
                if not execute:
                    return self._json({
                        "prepared": True, "submitted": False, "dry_run": True,
                        "package": package_to_dict(package),
                    })
                result = submit_cluster_job(package.config_path, cfg, dry_run=False)
                return self._json({
                    "prepared": True, "submitted": True, "dry_run": False,
                    "package": package_to_dict(package),
                    "submit": result.__dict__,
                })
            if parsed.path == "/api/cluster/fetch":
                result = fetch_cluster_job(
                    payload.get("job_dir", ""),
                    payload.get("dest", "") or str(ROOT / "results" / project / "cluster" / Path(payload.get("job_dir", "job")).name),
                    overwrite=bool(payload.get("overwrite", False)),
                )
                return self._json(result)
            if parsed.path == "/api/local/run":
                return self._json(
                    _run_rsim_run(
                        project,
                        input_mf4=payload.get("input_mf4", ""),
                        dataset=payload.get("dataset", ""),
                        output_mf4=payload.get("output_mf4", ""),
                        dry_run=not bool(payload.get("execute", False)),
                    )
                )
            self.send_error(404)

        def _json(self, payload, status=200):
            data = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _default_project() -> str:
    projects = list_projects()
    return projects[0] if projects else "default"


def _tail_task(task_id: str, since: int) -> dict:
    """Tail a task by id, trying the control plane first then the legacy registry.

    The control plane is the primary path (build/sim/tcc now route through it).
    The legacy BuildTaskRegistry fallback covers task_ids from prior sessions or
    when the embedded control server is disabled (--no-control).
    """
    import core.web_control as web_control
    snap = web_control.tail_via_control(task_id, since)
    if snap.get("found"):
        return snap
    # Legacy fallback for pre-migration task_ids.
    from core.build_runner import get_registry
    return get_registry().tail(task_id, since)


def _run_rsim_check(project: str) -> dict:
    cmd = [sys.executable, str(ROOT / "rsim.py"), "--project", project, "check", "--deps"]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "command": cmd}


def _run_rsim_run(project: str, *, input_mf4: str, dataset: str, output_mf4: str, dry_run: bool) -> dict:
    cmd = [sys.executable, str(ROOT / "rsim.py"), "--project", project, "run"]
    if input_mf4:
        cmd.append(input_mf4)
    if dataset:
        cmd.extend(["--dataset", dataset])
    if output_mf4:
        cmd.extend(["--output-mf4", output_mf4])
    if dry_run:
        cmd.append("--dry-run")
    if not input_mf4 and not dataset:
        return {"returncode": 1, "stdout": "", "stderr": "input_mf4 or dataset is required", "command": cmd}
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "command": cmd, "dry_run": dry_run}


def _safe_config(config: dict) -> dict:
    keys = ["project", "build", "paths", "assets", "simulation", "cluster", "environment"]
    return {key: config.get(key, {}) for key in keys}


def _optional_bool(payload: dict, key: str):
    """Return None when key absent (let profile adaptivity decide), else bool."""
    if key not in payload or payload.get(key) is None:
        return None
    return bool(payload[key])


def _report_to_dict(report) -> dict:
    return {
        "backend": report.backend,
        "profile": report.profile,
        "ok": report.ok,
        "items": [item.__dict__ for item in report.items],
        "errors": [item.__dict__ for item in report.errors],
        "warnings": [item.__dict__ for item in report.warnings],
    }


def _run_repair(payload: dict) -> dict:
    """Execute or guide a repair action for a failed check item."""
    action = payload.get("repair_action", "")
    project = payload.get("project", "")
    if action == "switch_branch":
        from core.repo import prepare_repo_context
        from core.config import load_config
        cfg = load_config(project)
        msg = prepare_repo_context(cfg)
        return {"ok": not msg, "message": msg or "switched to target branch"}
    if action == "build_selena":
        import core.web_control as web_control
        task_id = web_control.start_build_via_control(project)
        return {"ok": True, "repair_action": "build_selena", "task_id": task_id,
                "message": "Selena build started; poll /api/build/status"}
    if action == "bootstrap_itc2":
        import core.web_control as web_control
        task_id = web_control.start_tcc_via_control(project, "bootstrap_itc2")
        return {"ok": True, "repair_action": "bootstrap_itc2", "task_id": task_id,
                "message": "itc2 bootstrap started; poll /api/build/status"}
    if action == "install_toolcollection":
        import core.web_control as web_control
        from core.tcc import read_required_toolcollection
        from core.config import load_config
        tc = payload.get("toolcollection", "")
        if not tc:
            tc = read_required_toolcollection(load_config(project))
        if not tc:
            return {"ok": False, "guidance": "无法确定 toolcollection 名（缺少 ip_if/tcc_toolversion_itc2.txt）。请在配置 tab 填写本地代码路径。"}
        task_id = web_control.start_tcc_via_control(project, "install_toolcollection", tc)
        return {"ok": True, "repair_action": "install_toolcollection", "task_id": task_id,
                "toolcollection": tc, "message": f"正在安装工具集 {tc}，轮询 /api/build/status"}
    if action == "auto_repair_all":
        import core.web_control as web_control
        task_id = web_control.start_tcc_via_control(project, "auto_repair_all")
        return {"ok": True, "repair_action": "auto_repair_all", "task_id": task_id,
                "message": "一键修复启动：itc2 + toolcollection 自动检测安装，轮询 /api/build/status"}
    if action == "run_env_script":
        from core.config import load_config
        cfg = load_config(project)
        script = (cfg.get("build") or {}).get("env_build_script", "")
        if not script:
            return {"ok": False, "guidance": "No env_build_script configured. Set the software build script (e.g. cmake_build.bat) in the Configuration tab, then run it manually in a cmd window to initialize the TCC environment."}
        return {"ok": False, "guidance": f"Open a cmd window and run the env build script manually (it is interactive):\n  {script}\nThis initializes MATLAB/Boost/Qt/VS environment variables. Then re-run the environment check."}
    return {"ok": False, "guidance": f"Unknown repair action: {action}"}
