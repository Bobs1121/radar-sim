"""Focused Web/SDK/Connector identity contract checks."""

from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.user import current_user, stable_user_identity
from radar_sim_sdk import RadarSimClient
from cli.agent import _ControlClient


def test_stable_user_identity_is_lowercase_and_namespaced(monkeypatch):
    monkeypatch.setenv("RSIM_USER", "HOZ2WX")
    assert current_user() == "hoz2wx"
    assert stable_user_identity("HOZ2WX") == "user-hoz2wx"
    assert stable_user_identity("user-HOZ2WX") == "user-hoz2wx"


def test_web_stable_owner_matches_sdk_default_without_merging_legacy_labels(tmp_path, monkeypatch):
    monkeypatch.setattr("radar_sim_sdk.client.getpass.getuser", lambda: "HOZ2WX")
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    client = TestClient(create_app(api_service=ApiV1Service(control_service_factory=factory)))
    with RadarSimClient("http://testserver", client=client) as sdk:
        sdk.capabilities()

    # Web stores ``user-<lowercase NTID>`` and the SDK default emits the same
    # value.  Old generated labels remain separate until an explicit upgrade.
    assert "user-hoz2wx" in services
    client.get("/api/v1/capabilities", headers={"X-Rsim-User": "user-hoz2wx"})
    client.get("/api/v1/capabilities", headers={"X-Rsim-User": "user-HOZ2WX"})
    client.get("/api/v1/capabilities", headers={"X-Rsim-User": "web-0123456789abcdef0123456789abcdef"})
    assert "web-0123456789abcdef0123456789abcdef" in services
    assert len(services) == 2


def test_connector_download_binds_stable_owner_from_request(tmp_path):
    services = {}
    app = create_app(
        api_service=ApiV1Service(
            control_service_factory=lambda owner: services.setdefault(
                owner, ControlService(tmp_path / f"{owner}.db")
            )
        )
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/windows-connector/connect.cmd?mode=unified",
        headers={"X-Rsim-User": "user-hoz2wx"},
    )
    assert response.status_code == 200
    encoded = base64.b64encode(b"user-hoz2wx").decode("ascii")
    assert f"RSIM_OWNER_B64={encoded}" in response.text


def test_web_identity_source_drops_random_default_but_keeps_legacy_migration():
    source = Path("radar_sim_web/static/app.js").read_text(encoding="utf-8")
    assert "function browserUserId" not in source
    assert "rsimUserId" in source
    assert "rsimBrowserUserId" in source
    assert "更新 Connector" in source


def test_connector_installer_migrates_only_generated_legacy_owner():
    source = Path("scripts/bootstrap.ps1").read_text(encoding="utf-8")
    assert "existingOwner" in source
    assert "'^(web|sdk)-[0-9a-f]{24,64}$'" in source
    assert "Never silently" in source
    assert "replace an existing explicit owner" in source


def test_bare_connector_control_requests_use_same_stable_owner_as_sdk(monkeypatch):
    monkeypatch.setenv("RSIM_USER", "Alice")
    captured: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def open_request(request, timeout):
        captured["owner"] = request.headers["X-rsim-user"]
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    _ControlClient("http://control.invalid", timeout=1)._request("GET", "/api/agents")

    assert captured["owner"] == "user-alice"
