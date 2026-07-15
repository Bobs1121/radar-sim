"""SSE parsing helpers for the SDK."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Iterator

from radar_sim_sdk.models import Event


@dataclass(frozen=True)
class SseMessage:
    id: str
    event: str
    data: str
    retry: int | None = None


def parse_sse_lines(lines: Iterable[str]) -> Iterator[SseMessage]:
    """Parse Server-Sent Events lines.

    Supports comments, blank-line dispatch, multi-line ``data:``, ``id:``,
    ``event:``, and ``retry:`` fields. A final unterminated event is dispatched
    to make short TestClient/MockTransport streams convenient.
    """
    event_id = ""
    event_type = "message"
    data_lines: list[str] = []
    retry: int | None = None

    def dispatch() -> SseMessage | None:
        nonlocal event_type, data_lines, retry
        if not data_lines and event_type == "message" and retry is None:
            return None
        msg = SseMessage(id=event_id, event=event_type or "message", data="\n".join(data_lines), retry=retry)
        event_type = "message"
        data_lines = []
        retry = None
        return msg

    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            msg = dispatch()
            if msg is not None:
                yield msg
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_type = value or "message"
        elif field == "id":
            event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None

    msg = dispatch()
    if msg is not None:
        yield msg


def event_from_sse(message: SseMessage) -> Event:
    try:
        payload = json.loads(message.data) if message.data else {}
    except json.JSONDecodeError:
        payload = {"message": message.data}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    payload.setdefault("event", message.event)
    if message.id and "id" not in payload:
        payload["id"] = message.id
    return Event.from_dict(payload)
