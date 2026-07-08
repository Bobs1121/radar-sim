"""Tests for core/remote_control.py and web_control remote mode."""

import json
import os
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from core.control_http import make_control_handler
from core.control_service import ControlService
from core.remote_control import RemoteControlClient, RemoteControlError
import core.web_control as web_control


@pytest.fixture
def remote_server(tmp_path):
    """A real multi-user control server on a random port, RSIM_HOME=tmp_path."""
    import os
    monkeypatch_env = {"RSIM_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    # Set env for the server process (same process, so os.environ).
    old = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)

    cache: dict = {}
    lock = threading.Lock()

    def factory(user):
        from core.user import control_db_path_for_user
        with lock:
            if user not in cache:
                cache[user] = ControlService(control_db_path_for_user(user))
            return cache[user]

    handler = make_control_handler(factory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield base, cache
    server.shutdown()
    server.server_close()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _client(base, user):
    return RemoteControlClient(base, user, timeout=10)


def test_create_and_get_job(remote_server):
    base, cache = remote_server
    c = _client(base, "alice")
    job = c.create_job("local.build_selena", payload={"project": "p1"})
    assert job["job_id"].startswith("job_")
    # get_job returns the same job
    fetched = c.get_job(job["job_id"])
    assert fetched["job_id"] == job["job_id"]
    assert fetched["job_type"] == "local.build_selena"


def test_user_isolation_remote(remote_server):
    base, cache = remote_server
    alice = _client(base, "alice")
    bob = _client(base, "bob")
    ja = alice.create_job("local.check", payload={"project": "p"})
    jb = bob.create_job("local.run_sim", payload={"project": "p"})
    # alice cannot fetch bob's job
    with pytest.raises(RemoteControlError) as exc:
        alice.get_job(jb["job_id"])
    assert exc.value.status == 404
    # bob cannot fetch alice's job
    with pytest.raises(RemoteControlError) as exc:
        bob.get_job(ja["job_id"])
    assert exc.value.status == 404


def test_get_logs_and_cancel(remote_server):
    base, cache = remote_server
    c = _client(base, "alice")
    job = c.create_job("local.build_selena", payload={"project": "p"})
    job_id = job["job_id"]
    # Agent-side: append logs via the alice service directly (simulating an agent).
    task_id = cache["alice"].get_job(job_id)["tasks"][0]["task_id"]
    cache["alice"].append_logs(task_id, ["line1", "line2"])

    logs = c.get_logs(job_id, since=0)
    assert [e["message"] for e in logs["entries"]] == ["line1", "line2"]
    assert logs["next_since"] > 0

    # Cancel
    cancelled = c.cancel_job(job_id)
    assert cancelled["cancel_requested"] is True


def test_list_jobs(remote_server):
    base, cache = remote_server
    c = _client(base, "alice")
    c.create_job("local.check", payload={"project": "p1"})
    c.create_job("local.run_sim", payload={"project": "p2"})
    jobs = c.list_jobs(limit=10)
    assert len(jobs) == 2
    assert all("job_id" in j and "job_type" in j for j in jobs)


def test_list_agents_remote(remote_server):
    """RemoteControlClient.list_agents reads GET /api/agents on the remote server."""
    import json
    import urllib.request

    from core.user import USER_HEADER

    base, cache = remote_server
    # Register an agent directly over HTTP (the client intentionally exposes
    # no register method — registration is the agent's job).
    req = urllib.request.Request(
        f"{base}/api/agents/register",
        data=json.dumps({
            "name": "win-01", "agent_id": "agent-a", "hostname": "winhost1",
            "platform": "Windows", "capabilities": ["local.check"],
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", USER_HEADER: "alice"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        json.loads(resp.read().decode("utf-8"))

    agents = _client(base, "alice").list_agents()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "agent-a"
    assert agents[0]["hostname"] == "winhost1"
    assert agents[0]["capabilities"] == ["local.check"]


def test_unreachable_server_raises():
    # Point at a closed port — connection refused.
    c = RemoteControlClient("http://127.0.0.1:1", "alice", timeout=2)
    with pytest.raises(RemoteControlError):
        c.get_job("job_x")


def test_web_control_remote_mode_round_trip(remote_server, monkeypatch):
    """web_control functions forward to the remote server when set_remote_client is used."""
    base, _ = remote_server
    client = _client(base, "alice")
    web_control.set_remote_client(client)
    try:
        # start_build_via_control → create_job on remote
        job_id = web_control.start_build_via_control("ovrs25", mode="RelWithDebInfo", clean=True)
        assert job_id.startswith("job_")

        # tail_via_control → get_job + get_logs
        snap = web_control.tail_via_control(job_id, since=0)
        assert snap["found"] is True
        assert snap["status"] == "queued"
        assert snap["task_id"] == job_id

        # cancel_via_control
        assert web_control.cancel_via_control(job_id) is True

        # list_jobs_via_control
        jobs = web_control.list_jobs_via_control(limit=10)
        assert any(j["task_id"] == job_id for j in jobs)

        # Unknown job → found: False (404 mapped)
        assert web_control.tail_via_control("job_nope", since=0) == {"found": False}
    finally:
        web_control.set_remote_client(None)


def test_web_control_remote_tail_status_mapping(remote_server, monkeypatch):
    """succeeded → success mapping in remote tail."""
    base, cache = remote_server
    client = _client(base, "alice")
    web_control.set_remote_client(client)
    try:
        job_id = web_control.start_build_via_control("ovrs25")
        # Simulate agent completing the task.
        task_id = cache["alice"].get_job(job_id)["tasks"][0]["task_id"]
        cache["alice"].append_logs(task_id, ["done"])
        cache["alice"].submit_task_result(task_id, agent_id="a1", status="succeeded",
                                          returncode=0, result={"exe_path": "C:/selena.exe"})
        snap = web_control.tail_via_control(job_id, since=0)
        assert snap["status"] == "success"
        assert snap["returncode"] == 0
        assert snap["exe_path"] == "C:/selena.exe"
        assert snap["lines"] == ["done"]
    finally:
        web_control.set_remote_client(None)
