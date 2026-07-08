"""Lightweight HTTP client for the control plane (web → remote server).

The web console in ``--server-url`` mode forwards task operations to a remote
control server over HTTP instead of calling an in-process ``ControlService``.
This client mirrors the operations the web needs (create/get/list jobs, logs,
cancel, list agents for the observability panel) and injects the
``X-Rsim-User`` header so the server routes to the caller's per-user DB.

It deliberately does NOT include agent execution operations (register/poll/
heartbeat/append_logs/submit_result) — those live in ``cli.agent._ControlClient``.
``list_agents`` is a read-only observability query, so it is exposed here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from core.user import USER_HEADER


class RemoteControlError(RuntimeError):
    """Raised when the remote control server returns an error or is unreachable."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class RemoteControlClient:
    """HTTP client for a remote control server, scoped to one user."""

    def __init__(self, server_url: str, user: str, *, timeout: int = 30) -> None:
        self._base = server_url.rstrip("/")
        self._user = user
        self._timeout = timeout

    @property
    def server_url(self) -> str:
        return self._base

    @property
    def user(self) -> str:
        return self._user

    def create_job(self, job_type: str, *, payload: Optional[dict] = None,
                   metadata: Optional[dict] = None) -> dict[str, Any]:
        return self._request("POST", "/api/jobs", {
            "job_type": job_type,
            "payload": dict(payload or {}),
            "metadata": dict(metadata or {}),
        })

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/jobs/{urllib.parse.quote(job_id)}")

    def get_logs(self, job_id: str, *, since: int = 0, limit: int = 500) -> dict[str, Any]:
        qs = urllib.parse.urlencode({"since": int(since or 0), "limit": int(limit or 500)})
        return self._request("GET", f"/api/jobs/{urllib.parse.quote(job_id)}/logs?{qs}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", "/api/jobs/cancel", {"job_id": job_id})

    def list_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode({"limit": int(limit or 20)})
        data = self._request("GET", f"/api/jobs?{qs}")
        return data.get("jobs", []) if isinstance(data, dict) else []

    def list_agents(self) -> list[dict[str, Any]]:
        """Return all registered agents (read-only observability query)."""
        data = self._request("GET", "/api/agents")
        return data.get("agents", []) if isinstance(data, dict) else []

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json", USER_HEADER: self._user}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self._base + path
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RemoteControlError(f"{method} {path} failed: {exc.code} {body}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise RemoteControlError(f"{method} {path} unreachable: {exc.reason}") from exc
