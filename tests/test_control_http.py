"""Focused tests for the control-plane HTTP handler helpers and routes."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from core.control_http import make_control_handler, split_path
from core.control_service import ControlService


@pytest.fixture
def control_server(tmp_path):
    service = ControlService(db_path=tmp_path / "control.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_control_handler(service))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=2)


def _post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _read_http_error(excinfo):
    return json.loads(excinfo.value.read().decode("utf-8"))


def test_split_path_normalizes_segments():
    assert split_path("/api/jobs/job_123/logs?since=1") == ["api", "jobs", "job_123", "logs"]


def test_control_http_end_to_end(control_server):
    agent = _post(
        f"{control_server}/api/agents/register",
        {"name": "win-agent", "capabilities": ["local.check"]},
    )
    assert agent["agent_id"].startswith("agent_")

    job = _post(
        f"{control_server}/api/jobs",
        {"job_type": "local.check", "payload": {"project": "ovrs25", "backend": "local"}},
    )
    assert job["status"] == "queued"

    claim = _post(f"{control_server}/api/agents/poll", {"agent_id": agent["agent_id"]})
    task = claim["task"]
    assert task["status"] == "running"
    assert task["task_type"] == "local.check"

    _post(f"{control_server}/api/tasks/logs", {"task_id": task["task_id"], "lines": ["hello", "world"]})
    logs = _get(f"{control_server}/api/jobs/{job['job_id']}/logs?since=0")
    assert [entry["message"] for entry in logs["entries"]] == ["hello", "world"]

    finished = _post(
        f"{control_server}/api/tasks/result",
        {
            "task_id": task["task_id"],
            "agent_id": agent["agent_id"],
            "returncode": 0,
            "result": {"note": "done"},
        },
    )
    assert finished["status"] == "succeeded"

    fetched = _get(f"{control_server}/api/jobs/{job['job_id']}")
    assert fetched["result"]["task_results"][0]["result"]["note"] == "done"


def test_control_http_cancel_visible_to_heartbeat(control_server):
    agent = _post(
        f"{control_server}/api/agents/register",
        {"name": "builder", "capabilities": ["local.build_selena"]},
    )
    job = _post(
        f"{control_server}/api/jobs",
        {"job_type": "local.build_selena", "payload": {"project": "ovrs25"}},
    )
    task = _post(f"{control_server}/api/agents/poll", {"agent_id": agent["agent_id"]})["task"]
    cancelled = _post(f"{control_server}/api/jobs/cancel", {"job_id": job["job_id"]})
    assert cancelled["status"] == "cancel_requested"

    heartbeat = _post(
        f"{control_server}/api/agents/heartbeat",
        {"agent_id": agent["agent_id"], "status": "busy", "current_task_id": task["task_id"]},
    )
    assert heartbeat["cancel_requested"] is True


def test_control_http_heartbeat_without_task_keeps_current_assignment(control_server):
    agent = _post(
        f"{control_server}/api/agents/register",
        {"name": "checker", "capabilities": ["local.check"]},
    )
    _post(
        f"{control_server}/api/jobs",
        {"job_type": "local.check", "payload": {"project": "ovrs25"}},
    )
    task = _post(f"{control_server}/api/agents/poll", {"agent_id": agent["agent_id"]})["task"]

    heartbeat = _post(
        f"{control_server}/api/agents/heartbeat",
        {"agent_id": agent["agent_id"], "status": "busy"},
    )
    assert heartbeat["agent"]["current_task_id"] == task["task_id"]


def test_control_http_list_agents(control_server):
    """GET /api/agents returns all registered agents with full shape."""
    # Register two agents with distinct capabilities/hostnames.
    a1 = _post(
        f"{control_server}/api/agents/register",
        {"name": "win-01", "agent_id": "agent-a", "hostname": "winhost1",
         "platform": "Windows", "capabilities": ["local.check", "local.run_sim"]},
    )
    a2 = _post(
        f"{control_server}/api/agents/register",
        {"name": "win-02", "agent_id": "agent-b", "hostname": "winhost2",
         "platform": "Windows", "capabilities": ["local.build_selena"]},
    )

    result = _get(f"{control_server}/api/agents")
    assert "agents" in result
    by_id = {a["agent_id"]: a for a in result["agents"]}
    assert set(by_id) == {"agent-a", "agent-b"}

    # Shape must include the fields operators need to verify registration.
    a = by_id["agent-a"]
    assert a["name"] == "win-01"
    assert a["hostname"] == "winhost1"
    assert a["platform"] == "Windows"
    assert a["status"] == "idle"
    assert a["capabilities"] == ["local.check", "local.run_sim"]
    assert "registered_at" in a and "last_heartbeat" in a
    assert a["current_task_id"] == ""

    # Assigning a task and heartbeating must reflect in list_agents.
    _post(f"{control_server}/api/jobs", {"job_type": "local.check", "payload": {"project": "ovrs25"}})
    task = _post(f"{control_server}/api/agents/poll", {"agent_id": "agent-a"})["task"]
    _post(
        f"{control_server}/api/agents/heartbeat",
        {"agent_id": "agent-a", "status": "busy", "current_task_id": task["task_id"]},
    )
    result = _get(f"{control_server}/api/agents")
    by_id = {a["agent_id"]: a for a in result["agents"]}
    assert by_id["agent-a"]["status"] == "busy"
    assert by_id["agent-a"]["current_task_id"] == task["task_id"]


def test_control_http_rejects_invalid_json(control_server):
    request = urllib.request.Request(
        f"{control_server}/api/jobs",
        data=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=15)
    assert excinfo.value.code == 400
    body = _read_http_error(excinfo)
    assert body["error"].startswith("invalid JSON:")


def test_control_http_returns_json_for_unknown_route(control_server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{control_server}/api/nope", timeout=15)
    assert excinfo.value.code == 404
    body = _read_http_error(excinfo)
    assert body == {"error": "route not found: /api/nope"}


def test_control_http_rejects_invalid_payload_shape(control_server):
    request = urllib.request.Request(
        f"{control_server}/api/agents/register",
        data=json.dumps({"name": "win-agent", "capabilities": "local.check"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=15)
    assert excinfo.value.code == 400
    body = _read_http_error(excinfo)
    assert body == {"error": "capabilities must be an array of strings"}


def test_control_http_rejects_result_from_wrong_agent(control_server):
    agent = _post(
        f"{control_server}/api/agents/register",
        {"name": "checker", "capabilities": ["local.check"]},
    )
    job = _post(
        f"{control_server}/api/jobs",
        {"job_type": "local.check", "payload": {"project": "ovrs25"}},
    )
    task = _post(f"{control_server}/api/agents/poll", {"agent_id": agent["agent_id"]})["task"]
    other_agent = _post(
        f"{control_server}/api/agents/register",
        {"name": "other", "capabilities": ["local.check"]},
    )

    request = urllib.request.Request(
        f"{control_server}/api/tasks/result",
        data=json.dumps(
            {
                "task_id": task["task_id"],
                "agent_id": other_agent["agent_id"],
                "returncode": 0,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=15)
    assert excinfo.value.code == 400
    body = _read_http_error(excinfo)
    assert "assigned to" in body["error"]

    fetched = _get(f"{control_server}/api/jobs/{job['job_id']}")
    assert fetched["status"] == "running"


# --- task_type whitelist (Mode A: cluster-only server) ---


@pytest.fixture
def cluster_only_server(tmp_path):
    """A control server restricted to cluster.run (Mode A)."""
    service = ControlService(db_path=tmp_path / "control.db")
    handler = make_control_handler(service, allowed_task_types={"cluster.run"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=2)


def test_cluster_only_server_rejects_local_task(cluster_only_server):
    """Mode A: server with --allowed-task-types cluster.run rejects local.* with 400."""
    request = urllib.request.Request(
        f"{cluster_only_server}/api/jobs",
        data=json.dumps({"job_type": "local.run_sim", "payload": {"project": "ovrs25"}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=15)
    assert excinfo.value.code == 400
    body = _read_http_error(excinfo)
    assert "not allowed" in body["error"]
    assert "local.run_sim" in body["error"]


def test_cluster_only_server_accepts_cluster_run(cluster_only_server):
    """Mode A: cluster.run is accepted (201) on a cluster-only server."""
    job = _post(
        f"{cluster_only_server}/api/jobs",
        {"job_type": "cluster.run", "payload": {"project": "ovrs25", "dataset": "smoke"}},
    )
    assert job["status"] == "queued"
    assert job["job_type"] == "cluster.run"


def test_cluster_only_server_rejects_disallowed_task_in_tasks_array(cluster_only_server):
    """Mode A: a disallowed task_type inside the tasks[] array is also rejected."""
    request = urllib.request.Request(
        f"{cluster_only_server}/api/jobs",
        data=json.dumps({
            "job_type": "cluster.run",
            "tasks": [{"task_type": "local.build_selena"}, {"task_type": "cluster.run"}],
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=15)
    assert excinfo.value.code == 400
    body = _read_http_error(excinfo)
    assert "local.build_selena" in body["error"]


def test_default_server_allows_all_task_types(control_server):
    """Mode B: a server with no whitelist (default) accepts local.* task types."""
    job = _post(
        f"{control_server}/api/jobs",
        {"job_type": "local.check", "payload": {"project": "ovrs25"}},
    )
    assert job["status"] == "queued"
    assert job["job_type"] == "local.check"
