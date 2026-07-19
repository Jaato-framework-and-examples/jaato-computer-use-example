"""Settle policy (03-DAEMON_DESIGN.md §6) — authoring the device's debounce.

The daemon owns the ``SettleConfig`` the device runs, both as a session default
and as a per-action override, and adapts it from observed ``settled`` outcomes.
Package profiles hold the per-app knobs; the feedback loop widens the quiet
window when a package keeps timing out.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Deque, Dict, List, Optional

from .protocol import SettleConfig


@dataclass(frozen=True)
class PackageProfile:
    """Per-package settle knobs. ``DEFAULT_PROFILE`` covers unknown packages."""

    quiet_window_ms: int = 500
    hard_timeout_ms: int = 5000
    event_mask: tuple = ("WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED")
    mode: str = "quiet"


DEFAULT_PROFILE = PackageProfile()

#: Explicit per-package overrides. Empty by default — the daemon tunes at runtime
#: from ``settled`` telemetry rather than shipping guesses.
PACKAGE_PROFILES: Dict[str, PackageProfile] = {}


def session_default(pkg: str) -> SettleConfig:
    """The session-default ``SettleConfig`` for ``pkg`` (§6)."""
    prof = PACKAGE_PROFILES.get(pkg, DEFAULT_PROFILE)
    return SettleConfig(
        quiet_window_ms=prof.quiet_window_ms,
        hard_timeout_ms=prof.hard_timeout_ms,
        event_mask=list(prof.event_mask),
        package_scope=[pkg] if pkg else [],
        mode=prof.mode,
    )


class SettleTuner:
    """Adaptive settle tuning from ``settled.reason`` telemetry (§6 feedback loop).

    Three consecutive ``timeout``s on a package -> widen the quiet window and
    narrow the mask to state changes only, and signal a reconfigure. This is a
    decision only the daemon can make (no LLM/telemetry context on the device).
    """

    def __init__(self, base: SettleConfig) -> None:
        self._base = base
        self._history: Dict[str, Deque[str]] = defaultdict(lambda: deque(maxlen=3))

    def observe(self, pkg: str, reason: str) -> Optional[SettleConfig]:
        """Record a ``settled`` outcome; return a widened config to push if the
        package has timed out three times running, else ``None``."""
        h = self._history[pkg]
        h.append(reason)
        if len(h) == 3 and all(r == "timeout" for r in h):
            h.clear()
            self._base = replace(
                self._base,
                quiet_window_ms=min(self._base.quiet_window_ms * 2, 4000),
                event_mask=["WINDOW_STATE_CHANGED"],
            )
            return self._base
        return None
