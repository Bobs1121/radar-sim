"""SDK error types."""

from __future__ import annotations

from typing import Any


class RadarSimError(RuntimeError):
    """Base SDK error."""


class RadarSimTransportError(RadarSimError):
    """Raised for HTTP transport/timeouts before a valid API response exists."""


class RadarSimTransferCancelledError(RadarSimError):
    """Raised when the caller explicitly cancels a source-to-target transfer."""

    code = "transfer_cancelled"

    def __init__(self, message: str = "transfer cancelled") -> None:
        self.message = str(message or "transfer cancelled")
        super().__init__(f"{self.code}: {self.message}")


class RadarSimIntegrityError(RadarSimError):
    """Raised when downloaded content fails its advertised checksum.

    The error carries the stable machine-readable ``code`` and the affected
    resource reference so automation can decide how to retry without parsing
    human text.
    """

    def __init__(self, code: str, message: str, *, resource: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.resource = resource


class RadarSimApiError(RadarSimError):
    """Raised for `/api/v1` error envelopes."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        detail: Any = None,
        actions: list[dict[str, Any]] | None = None,
        request_id: str = "",
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = int(status_code)
        self.detail = detail if detail is not None else {}
        self.actions = list(actions or [])
        self.request_id = request_id

    @classmethod
    def from_envelope(cls, payload: dict[str, Any], *, status_code: int, request_id: str = "") -> "RadarSimApiError":
        return cls(
            str(payload.get("code") or "http_error"),
            str(payload.get("message") or "HTTP error"),
            status_code=status_code,
            detail=payload.get("detail") if isinstance(payload, dict) else {},
            actions=payload.get("actions") if isinstance(payload.get("actions"), list) else [],
            request_id=str(payload.get("request_id") or request_id or ""),
        )
