"""Tests for cli/web.py REST endpoints (contract + new endpoints)."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cli.web import _make_handler


@pytest.fixture
def web_server(tmp_path, monkeypatch):
    """Spin up a web server on a random port with a temp project config."""
    # Build a minimal project config dir
    project_dir = tmp_path / "projects" / "test"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text(
        "project:\n  name: test\n  platform: gen5_selena\n"
        "paths:\n  project_root: '" + str(tmp_path).replace("\\", "/") + "'\n"
        "  build_output: '" + str(tmp_path / "build").replace("\\", "/") + "'\n"
        "selena:\n  exe_pattern: 'dc_tools/selena/core/{build_mode}/selena.exe'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("core.config.get_default_project", lambda: "test")
    monkeypatch.setattr("core.config.list_projects", lambda: ["test"])

    handler = _make_handler("test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=2)


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def test_api_profiles_endpoint(web_server):
    data = _get(f"{web_server}/api/profiles?project=test")
    assert "profiles" in data
    assert data["profiles"][0]["name"] == "default"
    assert "backend" in data["profiles"][0]


def test_api_check_endpoint(web_server):
    data = _get(f"{web_server}/api/check?project=test&backend=local")
    assert "backend" in data
    assert data["backend"] == "local"
    assert "items" in data
    assert "ok" in data
    assert "errors" in data
    assert "warnings" in data


def test_api_cluster_prepare_optional_bool(web_server, monkeypatch):
    """prepare must pass None (not False) for unset copy_data/copy_selena."""
    captured = {}

    def fake_prepare(cfg, *, input_path="", dataset="", run_id="", profile="", copy_data=None, copy_selena=None):
        captured["copy_data"] = copy_data
        captured["copy_selena"] = copy_selena
        from core.cluster import ClusterJobPackage
        return ClusterJobPackage(
            run_id=run_id or "t", profile=profile or "default",
            job_dir=".", config_path=".", simulation_script=".", manifest_path=".",
            datafile_path="", output_hint="", submit_command=[], warnings=[],
        )

    monkeypatch.setattr("cli.web.prepare_cluster_job", fake_prepare)
    _post(f"{web_server}/api/cluster/prepare", {"project": "test", "input_path": "x.MF4"})
    # Unset → None (lets profile adaptivity decide), NOT False.
    assert captured["copy_data"] is None
    assert captured["copy_selena"] is None


def test_api_cluster_run_dry(web_server, monkeypatch):
    def fake_prepare(cfg, *, input_path="", dataset="", run_id="", profile="", copy_data=None, copy_selena=None):
        from core.cluster import ClusterJobPackage
        return ClusterJobPackage(
            run_id=run_id or "t", profile=profile or "default",
            job_dir=".", config_path=".", simulation_script=".", manifest_path=".",
            datafile_path="", output_hint="", submit_command=[], warnings=[],
        )
    monkeypatch.setattr("cli.web.prepare_cluster_job", fake_prepare)
    data = _post(f"{web_server}/api/cluster/run", {"project": "test", "input_path": "x.MF4"})
    assert data["prepared"] is True
    assert data["submitted"] is False  # dry-run (no execute)
    assert "package" in data


def test_api_user_config_get(web_server):
    data = _get(f"{web_server}/api/user-config?project=test")
    # Flat shape with the 9 user-facing fields.
    for key in ["source", "code_path", "env_build_script", "selena_build_script",
                "selena_branch", "runtime_path", "data_path", "selena_exe", "backend"]:
        assert key in data


def test_api_user_config_save_round_trip(web_server):
    payload = {
        "project": "test", "source": "path", "code_path": "C:/repo",
        "env_build_script": "C:/cmake.bat", "selena_build_script": "C:/jenkins.bat",
        "selena_branch": "feature/x", "runtime_path": "C:/rt.xml",
        "data_path": "D:/data", "selena_exe": "C:/selena.exe", "backend": "cluster",
    }
    saved = _post(f"{web_server}/api/user-config", payload)
    assert saved["ok"] is True
    assert "local_yaml_path" in saved
    # Read back via the same endpoint.
    read = _get(f"{web_server}/api/user-config?project=test")
    assert read["source"] == "path"
    assert read["code_path"] == "C:/repo"
    assert read["selena_branch"] == "feature/x"
    assert read["env_build_script"] == "C:/cmake.bat"
    assert read["selena_exe"] == "C:/selena.exe"
    assert read["backend"] == "cluster"


def test_api_build_check(web_server):
    data = _get(f"{web_server}/api/build/check?project=test")
    assert "has_exe" in data
    assert "exe_path" in data


def test_api_repair_unknown_action(web_server):
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "bogus"})
    assert data["ok"] is False
    assert "guidance" in data


def test_api_repair_run_env_script_no_script(web_server):
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "run_env_script"})
    assert data["ok"] is False
    assert "env_build_script" in data["guidance"] or "cmake_build.bat" in data["guidance"]


def test_api_repair_switch_branch_refuses_git(web_server, monkeypatch):
    def fail_prepare(*args, **kwargs):
        raise AssertionError("prepare_repo_context must not be called")

    monkeypatch.setattr("core.repo.prepare_repo_context", fail_prepare)
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "switch_branch"})
    assert data["ok"] is False
    assert data["repair_action"] == "switch_branch"
    assert "disabled" in data["guidance"]
    assert "worktree" in data["guidance"]


def test_api_repair_bootstrap_itc2_returns_task(web_server):
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "bootstrap_itc2"})
    assert data["ok"] is True
    assert "task_id" in data
    assert data["repair_action"] == "bootstrap_itc2"


def test_api_repair_install_toolcollection_no_tc(web_server):
    # No toolcollection configured and none in payload → guidance, not a task.
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "install_toolcollection"})
    assert data["ok"] is False
    assert "toolcollection" in data["guidance"] or "代码路径" in data["guidance"]


def test_api_repair_install_toolcollection_with_tc(web_server, monkeypatch):
    def fake_install(config, tc, log=None):
        from core.tcc import InstallResult
        return InstallResult(ok=True, returncode=0, stdout="installed", stderr="", detail="ok")
    monkeypatch.setattr("core.tcc.install_toolcollection", fake_install)
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "install_toolcollection", "toolcollection": "IF:BTC-7.0.0"})
    assert data["ok"] is True
    assert data["toolcollection"] == "IF:BTC-7.0.0"
    assert "task_id" in data


def test_api_repair_auto_repair_all(web_server):
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "auto_repair_all"})
    assert data["ok"] is True
    assert data["repair_action"] == "auto_repair_all"
    assert "task_id" in data


def test_api_tasks_list(web_server):
    data = _get(f"{web_server}/api/tasks?limit=5")
    assert "tasks" in data
    assert isinstance(data["tasks"], list)


def test_api_build_status_falls_back_to_store(web_server, tmp_path, monkeypatch):
    """A task not in memory should be found via SQLite fallback."""
    from core.task_store import TaskStore
    from dataclasses import dataclass, field
    @dataclass
    class T:
        task_id: str = "hist_task"; project: str = "test"; kind: str = "build"
        status: str = "success"; started_at: float = 1000.0; finished_at: float = 1010.0
        stdout_lines: list = field(default_factory=lambda: ["survived"])
        returncode: object = 0; errors: list = field(default_factory=list)
        exe_path: str = ""; current_file: str = ""; files_done: int = 0; files_total: int = 0
    store = TaskStore(db_path=tmp_path / "t.db")
    store.save_task(T(), new_lines=["survived"])
    monkeypatch.setattr("core.task_store.get_store", lambda: store)
    data = _get(f"{web_server}/api/build/status?task_id=hist_task&since=0")
    assert data["found"] is True
    assert data["status"] == "success"
    assert data["lines"] == ["survived"]


def test_api_config_list_files(web_server):
    data = _get(f"{web_server}/api/config/list-files")
    assert "files" in data
    # web_server fixture creates a "test" project; list-files may or may not have local.yaml
    assert isinstance(data["files"], list)


def test_api_config_export_local_yaml(web_server):
    # First save a local.yaml, then export it.
    _post(f"{web_server}/api/user-config", {"project": "test", "source": "build", "code_path": "C:/x"})
    data = _get(f"{web_server}/api/config/export?project=test")
    assert data["project"] == "test"
    assert "yaml_content" in data
    assert "local-build" in data["yaml_content"] or "user" in data["yaml_content"]


def test_api_config_import_replace(web_server):
    yaml_text = "repos:\n  inner_repo_root: C:/imported\nprofiles:\n  - name: local-build\n    backend: local\n    selena:\n      source: build\nactive_profile: local-build\n"
    data = _post(f"{web_server}/api/config/import", {"project": "test", "yaml_content": yaml_text, "mode": "replace"})
    assert data["ok"] is True
    # Verify it loaded
    uc = _get(f"{web_server}/api/user-config?project=test")
    assert uc["code_path"] == "C:/imported"


def test_api_config_import_invalid_yaml(web_server):
    # Truly malformed YAML (tab indentation error).
    try:
        data = _post(f"{web_server}/api/config/import", {"project": "test", "yaml_content": "foo: bar\n\tbaz: bad", "mode": "replace"})
        assert "error" in data or data.get("ok") is False
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_api_config_new_duplicate_rejected(web_server, tmp_path, monkeypatch):
    (tmp_path / "projects" / "dup").mkdir(parents=True)
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    try:
        data = _post(f"{web_server}/api/config/new", {"project": "dup"})
        assert "error" in data
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_api_config_new_project(web_server, tmp_path, monkeypatch):
    # Point projects dir at tmp_path so the new project is isolated.
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    data = _post(f"{web_server}/api/config/new", {"project": "brandnew"})
    assert data["ok"] is True
    assert (tmp_path / "projects" / "brandnew" / "config.yaml").exists()


def test_api_build_status_not_found(web_server):
    data = _get(f"{web_server}/api/build/status?task_id=nonexistent&since=0")
    assert data["found"] is False


def test_save_local_config_round_trip(tmp_path, monkeypatch):
    """save_local_config writes local.yaml and get_user_config reads it back."""
    from core.config import save_local_config, get_user_config

    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text(
        "project:\n  name: demo\n  platform: gen5_selena\n"
        "paths:\n  project_root: '" + str(tmp_path).replace("\\", "/") + "'\n"
        "  build_output: '" + str(tmp_path / "build").replace("\\", "/") + "'\n"
        "selena:\n  exe_pattern: 'dc_tools/selena/core/{build_mode}/selena.exe'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("core.config.get_default_project", lambda: "demo")

    user_input = {
        "source": "path", "code_path": "C:/repo", "env_build_script": "C:/cmake.bat",
        "selena_build_script": "C:/jenkins.bat", "selena_branch": "dev",
        "runtime_path": "C:/rt.xml", "data_path": "D:/data",
        "selena_exe": "C:/selena.exe", "backend": "local",
    }
    local_path = save_local_config("demo", user_input)
    assert local_path.exists()
    assert local_path.name == "local.yaml"

    read = get_user_config("demo")
    assert read["source"] == "path"
    assert read["code_path"] == "C:/repo"
    assert read["env_build_script"] == "C:/cmake.bat"
    assert read["selena_branch"] == "dev"
    assert read["selena_exe"] == "C:/selena.exe"


def test_auto_copy_policy_decides_by_source_and_path(tmp_path):
    from core.api import _auto_copy_policy

    # source=build + cluster backend → copy_selena True; local data → copy_data True
    cfg = {"active_profile": "default", "active_backend": "cluster",
           "profiles": [{"name": "default", "selena": {"source": "build"}, "backend": "cluster"}]}
    p = _auto_copy_policy(cfg, "D:/data/file.MF4")
    assert p["copy_selena"] is True
    assert p["copy_data"] is True

    # UNC data → copy_data False
    p2 = _auto_copy_policy(cfg, r"\\share\data\file.MF4")
    assert p2["copy_data"] is False

    # source=path + cluster + UNC exe → copy_selena False (worker sees UNC); UNC data → copy_data False
    cfg2 = {"active_profile": "user", "active_backend": "cluster",
            "profiles": [{"name": "user", "selena": {"source": "path", "exe": r"\\share\selena.exe"}, "backend": "cluster"}]}
    p3 = _auto_copy_policy(cfg2, r"\\share\data\file.MF4")
    assert p3["copy_selena"] is False
    assert p3["copy_data"] is False


def test_api_build_selena_routes_to_control_plane(web_server, tmp_path, monkeypatch):
    """POST /api/build/selena creates a control-plane job (not a legacy registry task)."""
    from core.control_service import ControlService
    import core.web_control as web_control

    svc = ControlService(tmp_path / "_ctrl_web_test.db")
    monkeypatch.setattr(web_control, "_SERVICE", svc)
    data = _post(f"{web_server}/api/build/selena", {"project": "test"})
    assert "task_id" in data
    assert data["task_id"].startswith("job_")
    # The job exists in the control service.
    job = svc.get_job(data["task_id"])
    assert job["job_type"] == "local.build_selena"
    assert job["status"] == "queued"
    web_control.set_service(None)


def test_api_build_status_reads_control_plane(web_server, tmp_path, monkeypatch):
    """GET /api/build/status tails the control-plane job and maps succeeded→success."""
    from core.control_service import ControlService
    import core.web_control as web_control

    svc = ControlService(tmp_path / "_ctrl_web_test2.db")
    monkeypatch.setattr(web_control, "_SERVICE", svc)
    job = svc.create_job("local.build_selena", payload={"project": "test"})
    job_id = job["job_id"]
    task_id = job["tasks"][0]["task_id"]
    svc.append_logs(task_id, ["building...", "done"])
    svc.submit_task_result(task_id, agent_id="a1", status="succeeded", returncode=0,
                           result={"exe_path": "C:/selena.exe"})

    data = _get(f"{web_server}/api/build/status?task_id={job_id}&since=0")
    assert data["found"] is True
    assert data["status"] == "success"  # succeeded → success
    assert data["returncode"] == 0
    assert data["exe_path"] == "C:/selena.exe"
    assert data["lines"] == ["building...", "done"]
    web_control.set_service(None)


def test_api_repair_bootstrap_itc2_routes_to_control(web_server, tmp_path, monkeypatch):
    """repair bootstrap_itc2 creates a tcc.bootstrap_itc2 control-plane job."""
    from core.control_service import ControlService
    import core.web_control as web_control

    svc = ControlService(tmp_path / "_ctrl_web_test3.db")
    monkeypatch.setattr(web_control, "_SERVICE", svc)
    data = _post(f"{web_server}/api/repair", {"project": "test", "repair_action": "bootstrap_itc2"})
    assert data["ok"] is True
    assert data["task_id"].startswith("job_")
    job = svc.get_job(data["task_id"])
    assert job["job_type"] == "tcc.bootstrap_itc2"
    web_control.set_service(None)


def test_api_build_cancel_routes_to_control(web_server, tmp_path, monkeypatch):
    from core.control_service import ControlService
    import core.web_control as web_control

    svc = ControlService(tmp_path / "_ctrl_web_test4.db")
    monkeypatch.setattr(web_control, "_SERVICE", svc)
    job = svc.create_job("local.build_selena", payload={"project": "test"})
    data = _post(f"{web_server}/api/build/cancel", {"task_id": job["job_id"]})
    assert data["ok"] is True
    assert svc.get_job(job["job_id"])["cancel_requested"] is True
    web_control.set_service(None)
