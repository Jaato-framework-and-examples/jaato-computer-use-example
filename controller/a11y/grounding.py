"""Selector grounding policy (03-DAEMON_DESIGN.md §4) — the crux, daemon-side.

The device only *resolves* selectors mechanically; **choosing** the selector is
the daemon's job. The agent points at a set-of-marks ``ref``; :func:`to_selector`
turns that ref into the tightest binding that will survive to action time, and
:func:`describe_recovery` classifies a :class:`DeviceError` into the next loop
step (STALE -> re-observe, AMBIGUOUS -> refine, …).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .protocol import Action, Selector, Snapshot
from .wire import DeviceError, ErrorCode


def to_selector(ref: int, snap: Snapshot, acting_immediately: bool = True) -> Selector:
    """Translate a set-of-marks ``ref`` into a Selector (§4).

    Acting immediately on a fresh observe, the tightest binding is
    ``{ref, snapshotVersion}`` — the device rejects it with STALE if the world
    moved, which is exactly the signal we want. Otherwise degrade to durable
    bindings: viewId (disambiguated by text when the id repeats), then text,
    then content-description, then bounds as the last resort.
    """
    node = snap.by_ref(ref)  # KeyError if the agent named a nonexistent mark
    if acting_immediately:
        return Selector(ref=node.ref, snapshot_version=snap.version)
    if node.view_id:
        return Selector(
            view_id=node.view_id,
            text=node.text if snap.view_id_ambiguous(node.view_id) else None,
        )
    if node.text:
        return Selector(text=node.text, index=snap.text_index(node))
    if node.desc:
        return Selector(desc=node.desc)
    return Selector(bounds=node.bounds)


@dataclass
class Recovery:
    """The daemon's decision after a failed ``act`` (§4).

    ``next`` is the loop directive: ``"reobserve"`` (re-snapshot and let the
    agent re-plan) or ``"retry"`` (a refined selector/action is supplied). When
    ``retry``, ``selector``/``action`` carry the replacement.
    """

    next: str  # "reobserve" | "retry"
    reason: str
    selector: Optional[Selector] = None
    action: Optional[Action] = None


def describe_recovery(err: DeviceError, node_bounds: Optional[list] = None,
                      action: Optional[Action] = None) -> Recovery:
    """Map a device error to a recovery step (§4, §10 resolution rules).

    ``node_bounds`` (the failed node's bounds, if known) and ``action`` (what was
    attempted) enable the NOT_ACTIONABLE fallback: a visible-but-inert *clickable*
    node is tapped by coordinate. That fallback is click-only — turning a refused
    SCROLL into a coordinate tap would tap (and launch) the element instead of
    scrolling. Codes with no local recovery surface as a re-observe so the agent
    sees the new reality and decides.
    """
    code = err.code
    if code == ErrorCode.STALE:
        return Recovery("reobserve", "world moved between observe and act")
    if code == ErrorCode.NOT_FOUND:
        return Recovery("reobserve", "selector matched no node — element gone or needs scroll")
    if code == ErrorCode.AMBIGUOUS:
        return Recovery("reobserve", "selector matched >1 node — re-observe to disambiguate by ref")
    if code == ErrorCode.NOT_ACTIONABLE:
        kind = getattr(action, "action", "") or ""
        if node_bounds and kind in ("CLICK", "LONG_CLICK"):
            cx = (node_bounds[0] + node_bounds[2]) // 2
            cy = (node_bounds[1] + node_bounds[3]) // 2
            return Recovery("retry", "semantic click refused — fall back to gesture tap",
                            selector=Selector(bounds=node_bounds),
                            action=Action.gesture_tap(cx, cy))
        if kind.startswith("SCROLL"):
            # The device's message distinguishes "node doesn't advertise this
            # scroll action" (-> target a different ref / gesture) from
            # "advertised but already at the scroll extent" (-> you've reached the
            # end) — different model decisions. Surface it verbatim; a generic
            # hint here would flatten a distinction only the device can make.
            return Recovery("reobserve", err.message
                            or "that ref can't scroll that way — target a scrollable "
                               "container's ref, or swipe with screen_gesture")
        return Recovery("reobserve", f"{code}: not actionable — re-observe and re-plan")
    if code == ErrorCode.RATE_LIMITED:
        return Recovery("reobserve", "screenshot rate-limited — re-observe applies cadence policy")
    return Recovery("reobserve", f"{code}: {err.message}")
