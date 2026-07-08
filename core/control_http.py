"""Small stdlib HTTP adapter for the minimal control-plane service."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from core.control_service import ControlService


class RequestError(ValueError):
    """Structured request validation or routing error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def split_path(path: str) -> list[str]:
    """Return URL path parts without empty segments."""
    return [part for part in urlparse(path).path.split("/") if part]


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Read a JSON request body, returning an empty dict for an empty payload."""
    length = int(handler.headers.get("content-length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def write_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    """Write a JSON response."""
    data = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def write_error_json(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    """Write a JSON error response."""
    write_json(handler, {"error": message}, status=status)


def require_string(payload: dict[str, Any], key: str, *, default: str = "") -> str:
    """Read an optional string field from a JSON object."""
    if key not in payload or payload[key] is None:
        return default
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def optional_string(payload: dict[str, Any], key: str) -> str | None:
    """Read an optional string field, preserving missing values as None."""
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def require_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Read an optional object field from a JSON object."""
    if key not in payload or payload[key] is None:
        return {}
    value = payload[key]
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def require_string_list(payload: dict[str, Any], key: str, *, allow_scalar: bool = False) -> list[str]:
    """Read an optional string array from a JSON object."""
    if key not in payload or payload[key] is None:
        return []
    value = payload[key]
    if allow_scalar and isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{key}[{index}] must be a string")
        result.append(item)
    return result


def require_task_specs(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Read an optional tasks array from a JSON object."""
    if key not in payload or payload[key] is None:
        return []
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{key}[{index}] must be an object")
        spec = dict(item)
        if "payload" in spec and spec["payload"] is not None and not isinstance(spec["payload"], dict):
            raise ValueError(f"{key}[{index}].payload must be an object")
        specs.append(spec)
    return specs


def require_int(value: Any, label: str, *, default: int | None = None) -> int | None:
    """Parse an optional integer value."""
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def make_control_handler(service, allowed_task_types=None):
    """Create a request handler bound to a control service.

    ``service`` is either a ``ControlService`` instance (single-user, backward
    compatible) or a callable ``(user: str) -> ControlService`` that returns the
    per-user service (multi-user). The user is read from the ``X-Rsim-User``
    request header, defaulting to ``current_user()`` on the server.

    ``allowed_task_types`` is an optional collection of task_type strings that
    the server will accept on ``POST /api/jobs``. When ``None`` (default) or
    empty, all task types are accepted — this is the Mode B (full local+cluster)
    behavior. When set (e.g. ``{"cluster.run"}``), the server rejects any job
    whose ``job_type`` or any ``tasks[].task_type`` is not in the set with HTTP
    400 — this is the Mode A (Linux cluster-only service) behavior.
    """
    from core.user import USER_HEADER, current_user

    allowed = set(allowed_task_types) if allowed_task_types else None

    def resolve(handler):
        if callable(service) and not isinstance(service, ControlService):
            user = handler.headers.get(USER_HEADER, "").strip() or current_user()
            return service(user), user
        return service, current_user()

    def _check_allowed(task_type: str) -> None:
        if allowed and task_type not in allowed:
            raise RequestError(
                400,
                f"task_type {task_type!r} not allowed on this server "
                f"(allowed: {', '.join(sorted(allowed))})",
            )

    class Handler(BaseHTTPRequestHandler):
        server_version = "RadarSimControl/1.0"

        def do_GET(self):  # noqa: N802
            self._dispatch(self._handle_get)

        def do_POST(self):  # noqa: N802
            self._dispatch(self._handle_post)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _dispatch(self, func: Callable[[], None]) -> None:
            try:
                self._svc, self._user = resolve(self)
                func()
            except json.JSONDecodeError as exc:
                write_error_json(self, 400, f"invalid JSON: {exc}")
            except RequestError as exc:
                write_error_json(self, exc.status, str(exc))
            except KeyError as exc:
                write_error_json(self, 404, str(exc))
            except (TypeError, ValueError) as exc:
                write_error_json(self, 400, str(exc))

        def _handle_get(self) -> None:
            parsed = urlparse(self.path)
            parts = split_path(parsed.path)
            query = parse_qs(parsed.query)
            if parts == ["health"]:
                write_json(self, {"ok": True})
                return
            if parts == ["api", "agents"]:
                write_json(self, {"agents": self._svc.list_agents()})
                return
            if parts == ["api", "jobs"]:
                limit = require_int(query.get("limit", ["20"])[0], "limit", default=20) or 20
                write_json(self, {"jobs": self._svc.list_jobs(limit=limit)})
                return
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                write_json(self, self._svc.get_job(parts[2]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "logs":
                since = require_int(query.get("since", ["0"])[0], "since", default=0) or 0
                limit = require_int(query.get("limit", ["200"])[0], "limit", default=200) or 200
                write_json(self, self._svc.get_logs(job_id=parts[2], since=since, limit=limit))
                return
            raise RequestError(404, f"route not found: {parsed.path}")

        def _handle_post(self) -> None:
            parts = split_path(self.path)
            payload = read_json_body(self)
            if parts == ["api", "agents", "register"]:
                write_json(
                    self,
                    self._svc.register_agent(
                        require_string(payload, "name"),
                        agent_id=require_string(payload, "agent_id"),
                        platform=require_string(payload, "platform"),
                        hostname=require_string(payload, "hostname"),
                        capabilities=require_string_list(payload, "capabilities"),
                        metadata=require_object(payload, "metadata"),
                    ),
                    201,
                )
                return
            if parts == ["api", "jobs"]:
                job_type = require_string(payload, "job_type")
                _check_allowed(job_type)
                tasks = require_task_specs(payload, "tasks")
                for spec in tasks:
                    _check_allowed(str(spec.get("task_type") or job_type))
                write_json(
                    self,
                    self._svc.create_job(
                        job_type,
                        payload=require_object(payload, "payload"),
                        tasks=tasks,
                        metadata=require_object(payload, "metadata"),
                        assigned_agent_id=require_string(payload, "assigned_agent_id"),
                    ),
                    201,
                )
                return
            if parts == ["api", "agents", "poll"]:
                task = self._svc.claim_next_task(require_string(payload, "agent_id"))
                write_json(self, {"task": task})
                return
            if parts == ["api", "agents", "heartbeat"]:
                write_json(
                    self,
                    self._svc.heartbeat(
                        require_string(payload, "agent_id"),
                        status=require_string(payload, "status"),
                        current_task_id=optional_string(payload, "current_task_id"),
                        metadata=require_object(payload, "metadata"),
                    ),
                )
                return
            if parts == ["api", "tasks", "logs"]:
                write_json(
                    self,
                    self._svc.append_logs(
                        require_string(payload, "task_id"),
                        require_string_list(payload, "lines", allow_scalar=True),
                        stream=require_string(payload, "stream", default="stdout") or "stdout",
                    ),
                )
                return
            if parts == ["api", "tasks", "result"]:
                write_json(
                    self,
                    self._svc.submit_task_result(
                        require_string(payload, "task_id"),
                        agent_id=require_string(payload, "agent_id"),
                        status=require_string(payload, "status"),
                        returncode=require_int(payload.get("returncode"), "returncode"),
                        result=require_object(payload, "result"),
                        error=require_string(payload, "error"),
                    ),
                )
                return
            if parts == ["api", "jobs", "cancel"]:
                write_json(self, self._svc.cancel_job(require_string(payload, "job_id")))
                return
            raise RequestError(404, f"route not found: {urlparse(self.path).path}")

    return Handler
