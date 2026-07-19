"""Wire constants and the device-error type (01-PROTOCOL.md §3.4, §7).

Kept dependency-free so the framing and protocol layers can import it without
pulling in asyncio or PIL.
"""
from __future__ import annotations

from dataclasses import dataclass

# Protocol version. Mirrors the device's ``Wire.PV`` (Envelope.kt). The device
# rejects a mismatched ``req.pv`` with a PROTOCOL_VERSION error, and emits its
# own ``pv`` on ``hello``; this controller pins to the same major and sends
# ``pv`` on every request (01 §3.4 — strict side).
PV = 1


class ErrorCode:
    """The closed error taxonomy (01 §7). Values are the exact wire strings."""

    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    STALE = "STALE"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    RATE_LIMITED = "RATE_LIMITED"
    SECURE_WINDOW = "SECURE_WINDOW"
    CANCELED = "CANCELED"
    TIMEOUT = "TIMEOUT"
    PROTOCOL_VERSION = "PROTOCOL_VERSION"
    PERMISSION = "PERMISSION"
    INTERNAL = "INTERNAL"


@dataclass
class DeviceError(Exception):
    """A ``res.ok:false`` from the device, carried as an exception.

    Raised by :meth:`DeviceSession.request` so callers can ``except
    DeviceError`` and route ``code`` through grounding recovery (03 §4). The
    grounding layer branches on :attr:`code` (an :class:`ErrorCode` string).
    """

    code: str
    message: str = ""
    retry_after_ms: int = 0

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"

    @classmethod
    def from_wire(cls, error: dict) -> "DeviceError":
        """Build from a wire ``error`` object (01 §3.2)."""
        return cls(
            code=error.get("code", ErrorCode.INTERNAL),
            message=error.get("message", ""),
            retry_after_ms=int(error.get("retryAfterMs", 0) or 0),
        )
