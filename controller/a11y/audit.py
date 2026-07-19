"""Append-only action audit trail (03-DAEMON_DESIGN.md §9, 01 §13.4).

This protocol grants the LLM full device authority, so the trail is mandatory,
not optional. Each record captures *what was targeted, how, and the before/after
snapshot versions* — enough to replay or forensically reconstruct a session.
Written as JSONL so it is append-only and trivially greppable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .protocol import Action, Selector


class AuditLog:
    """JSONL sink for act records. One line per action, flushed immediately."""

    def __init__(self, path: str, device_id: str, clock=None) -> None:
        """``clock`` is an injected ``() -> str`` timestamp source (ISO-8601).
        Injected rather than calling the wall clock inline so tests are
        deterministic and the module has no hidden time dependency."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._device_id = device_id
        self._clock = clock

    def record(self, selector: Selector, action: Action,
               from_version: int, settled_version: int,
               settled_reason: str) -> None:
        rec = {
            "ts": self._clock() if self._clock else None,
            "device": self._device_id,
            "action": action.action,
            "selector": selector.describe(),
            "from_version": from_version,
            "settled_version": settled_version,
            "settled_reason": settled_reason,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
