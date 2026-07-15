"""Tests for wizard REST endpoints (cli/web.py)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cli.web import _make_handler


@pytest.fixture()
def web_server(tmp_path, monkeypatch):
    """Spin up a test web server with a temp project config dir."""
    project_dir = tmp_path / "projects" / "test"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text(
        "project:\n  name: test\n  platform: gen5_selena\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("core.config.get_default_project", lambda: "test")
    monkeypatch.setattr("core.config.list_projects", lambda: ["test"])
    monkeypatch.setattr("core.config.local_yaml_path_for_project",
                        lambda project: tmp_path / "projects" / project / "local.yaml")

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


class TestWizardValidateEndpoint:
    def test_valid_fields(self, web_server):
        data = _get(f"{web_server}/api/wizard/validate?project_name=unique_wiz_proj&outer_repo_root=C:/src")
        assert data["ok"] is True

    def test_missing_required_fields(self, web_server):
        data = _get(f"{web_server}/api/wizard/validate?project_name=&outer_repo_root=")
        assert data["ok"] is False
        assert len(data["errors"]) > 0


class TestWizardAgentStatusEndpoint:
    def test_returns_shape(self, web_server):
        data = _get(f"{web_server}/api/wizard/agent-status")
        assert "has_build_agent" in data
        assert "agents" in data
        assert "mode" in data


class TestWizardInitEndpoint:
    def test_create_project(self, web_server):
        data = _post(f"{web_server}/api/wizard/init", {
            "project_name": "wiz_web_test",
            "outer_repo_root": "C:/src/repo",
            "selena_branch": "develop",
        })
        assert data["ok"] is True
        assert "config_yaml_path" in data

    def test_missing_fields_returns_400(self, web_server):
        payload = json.dumps({"project_name": "", "outer_repo_root": ""}).encode("utf-8")
        req = urllib.request.Request(
            f"{web_server}/api/wizard/init",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 400
