"""Tests for core/web_control.py adapter (control-plane → BuildTask tail shape)."""

import time

import pytest

from core.control_service import ControlService
import core.web_control as wc


@pytest.fixture
def service(tmp_path):
    svc = ControlService(tmp_path / "_ctrl_test.db", now_fn=lambda: _CLOCK[0])
    wc.set_service(svc)
    yield svc
    wc.set_service(None)


_CLOCK = [1000.0]


def _advance(dt: float) -> None:
    _CLOCK[0] += dt


def test_start_and_tail_build_status_mapping(service):
    job_id = wc.start_build_via_control("ovrs25", mode="RelWithDebInfo", clean=True)

    # Job is queued, no logs yet.
    snap = wc.tail_via_control(job_id, since=0)
    assert snap["found"] is True
    assert snap["task_id"] == job_id
    assert snap["status"] == "queued"
    assert snap["lines"] == []
    assert snap["total_lines"] == 0
    # All 11 BuildTask fields present.
    for key in ["returncode", "errors", "exe_path", "current_file",
                "files_done", "files_total", "duration_sec"]:
        assert key in snap


def test_tail_status_succeeded_maps_to_success(service):
    job_id = wc.start_build_via_control("ovrs25")
    job = service.get_job(job_id)
    task_id = job["tasks"][0]["task_id"]

    _advance(5.0)
    service.append_logs(task_id, ["compiling...", "done"])
    service.submit_task_result(task_id, agent_id="agent_1", status="succeeded", returncode=0,
                               result={"exe_path": "C:/build/selena.exe"})

    snap = wc.tail_via_control(job_id, since=0)
    assert snap["status"] == "success"  # succeeded → success
    assert snap["returncode"] == 0
    assert snap["exe_path"] == "C:/build/selena.exe"
    assert snap["lines"] == ["compiling...", "done"]
    assert snap["duration_sec"] == 5.0


def test_tail_incremental_cursor_advances(service):
    job_id = wc.start_sim_via_control("ovrs25", backend="local", data_path="D:/x.MF4")
    job = service.get_job(job_id)
    task_id = job["tasks"][0]["task_id"]

    service.append_logs(task_id, ["line1", "line2"])
    snap1 = wc.tail_via_control(job_id, since=0)
    assert snap1["lines"] == ["line1", "line2"]
    cursor = snap1["total_lines"]
    assert cursor > 0

    service.append_logs(task_id, ["line3"])
    snap2 = wc.tail_via_control(job_id, since=cursor)
    assert snap2["lines"] == ["line3"]  # only the new one
    assert snap2["total_lines"] > cursor


def test_tail_failed_extracts_error(service):
    job_id = wc.start_tcc_via_control("ovrs25", "auto_repair_all")
    job = service.get_job(job_id)
    task_id = job["tasks"][0]["task_id"]

    service.submit_task_result(task_id, agent_id="a1", status="failed", returncode=1,
                               result={"error": "ITO unreachable"})

    snap = wc.tail_via_control(job_id, since=0)
    assert snap["status"] == "failed"
    assert "ITO unreachable" in snap["errors"]


def test_tail_unknown_job_not_found(service):
    snap = wc.tail_via_control("job_nonexistent", since=0)
    assert snap == {"found": False}


def test_cancel_marks_job(service):
    job_id = wc.start_build_via_control("ovrs25")
    ok = wc.cancel_via_control(job_id)
    assert ok is True
    job = service.get_job(job_id)
    assert job["cancel_requested"] is True


def test_cancel_unknown_returns_false(service):
    assert wc.cancel_via_control("job_nope") is False


def test_list_jobs_newest_first(service):
    j1 = wc.start_build_via_control("ovrs25")
    _advance(1.0)
    j2 = wc.start_sim_via_control("ovrs25", backend="local", data_path="x.MF4")
    jobs = wc.list_jobs_via_control(limit=10)
    assert [j["task_id"] for j in jobs] == [j2, j1]
    assert all("status" in j and "project" in j for j in jobs)


def test_list_agents_via_control(service):
    """list_agents_via_control returns registered agents in embedded mode."""
    service.register_agent(
        "win-01", agent_id="agent-a", hostname="winhost1",
        platform="Windows", capabilities=["local.check"],
    )
    agents = wc.list_agents_via_control()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "agent-a"
    assert agents[0]["hostname"] == "winhost1"
    assert agents[0]["capabilities"] == ["local.check"]


def test_tcc_action_maps_to_task_type(service):
    job_id = wc.start_tcc_via_control("ovrs25", "install_toolcollection", "IF:BTC-7.0.0")
    job = service.get_job(job_id)
    assert job["job_type"] == "tcc.install_toolcollection"
    assert job["tasks"][0]["payload"]["toolcollection"] == "IF:BTC-7.0.0"


def test_per_user_db_isolation_via_http(tmp_path, monkeypatch):
    """Two users hitting one multi-user server see only their own jobs."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from core.control_http import make_control_handler
    from core.control_service import ControlService
    import core.user as user_mod
    import core.web_control as web_control

    # Redirect RSIM_HOME so per-user DBs land in tmp.
    monkeypatch.setenv("RSIM_HOME", str(tmp_path))

    # Build the per-user service factory (same as cli/server.py serve).
    cache: dict = {}
    lock = threading.Lock()

    def factory(user):
        with lock:
            if user not in cache:
                cache[user] = ControlService(user_mod.control_db_path_for_user(user))
            return cache[user]

    handler = make_control_handler(factory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def post(path, payload, user):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{base}{path}", data=data,
                                     headers={"Content-Type": "application/json", "X-Rsim-User": user},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def get(path, user):
        req = urllib.request.Request(f"{base}{path}", headers={"X-Rsim-User": user})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            return None

    try:
        j1 = post("/api/jobs", {"job_type": "local.build_selena", "payload": {"project": "p1"}}, "alice")
        j2 = post("/api/jobs", {"job_type": "local.run_sim", "payload": {"project": "p2"}}, "bob")

        # alice cannot see bob's job
        assert get(f"/api/jobs/{j2['job_id']}", "alice") is None
        # bob cannot see alice's job
        assert get(f"/api/jobs/{j1['job_id']}", "bob") is None
        # each sees their own
        assert get(f"/api/jobs/{j1['job_id']}", "alice")["job_id"] == j1["job_id"]
        assert get(f"/api/jobs/{j2['job_id']}", "bob")["job_id"] == j2["job_id"]
    finally:
        server.shutdown()
        server.server_close()

