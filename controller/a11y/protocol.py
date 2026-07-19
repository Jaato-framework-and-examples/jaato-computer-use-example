"""Typed mirrors of the wire schema (01-PROTOCOL.md §8-§11).

These dataclasses are the daemon-side representation of what crosses the socket.
They parse from / serialise to the exact JSON field names the device uses — the
device is the single source of truth for the wire, so nothing here renames a
field. Enum-ish string sets (actions, globals, flags) are validated against 01
so a bad value fails loud rather than reaching the device and erroring remotely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Snapshot & Node (§8)
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """One pruned, serialised accessibility node (§8).

    ``ref`` is ephemeral — unique **within one snapshotVersion only** — and is
    the token the agent points at (set-of-marks) and the daemon turns into a
    Selector (grounding §4). ``bounds`` is ``[left, top, right, bottom]`` in
    screen pixels. ``flags`` is a set drawn from the fixed vocabulary in §8;
    absent flags are false.
    """

    ref: int
    cls: str
    bounds: List[int]
    flags: List[str] = field(default_factory=list)
    view_id: Optional[str] = None
    text: Optional[str] = None
    desc: Optional[str] = None
    parent: Optional[int] = None

    @classmethod
    def parse(cls, d: dict) -> "Node":
        return cls(
            ref=int(d["ref"]),
            cls=d.get("cls", ""),
            bounds=list(d.get("bounds", [0, 0, 0, 0])),
            flags=list(d.get("flags", [])),
            view_id=d.get("viewId"),
            text=d.get("text"),
            desc=d.get("desc"),
            parent=d.get("parent"),
        )

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def actionable(self) -> bool:
        """True if the node advertises any actionable affordance (for set-of-marks
        marker placement and the ``actionable_nodes`` filter, §5)."""
        return any(
            f in self.flags
            for f in ("clickable", "longClickable", "scrollable", "editable", "checkable", "focusable")
        )

    @property
    def label(self) -> str:
        """Human/model-readable label: text, else content-description, else class."""
        return self.text or self.desc or self.cls


@dataclass
class Snapshot:
    """A pruned tree returned by ``observe`` (and referenced by ``settled``), §8."""

    version: int
    pkg: str
    activity: Optional[str]
    screen: dict  # {"width": int, "height": int}
    nodes: List[Node]
    screenshot_ref: Optional[str] = None

    @classmethod
    def parse(cls, d: dict) -> "Snapshot":
        return cls(
            version=int(d["snapshotVersion"]),
            pkg=d.get("pkg", ""),
            activity=d.get("activity"),
            screen=d.get("screen", {}),
            nodes=[Node.parse(n) for n in d.get("nodes", [])],
            screenshot_ref=d.get("screenshotRef"),
        )

    def by_ref(self, ref: int) -> Node:
        """Look up a node by ref; raise KeyError if the ref isn't in this version
        (the agent hallucinated a mark, or acted against a stale snapshot)."""
        for n in self.nodes:
            if n.ref == ref:
                return n
        raise KeyError(f"ref {ref} not in snapshot v{self.version}")

    def actionable_nodes(self) -> List[Node]:
        return [n for n in self.nodes if n.actionable]

    def view_id_ambiguous(self, view_id: str) -> bool:
        return sum(1 for n in self.nodes if n.view_id == view_id) > 1

    def text_index(self, node: Node) -> int:
        """Index of ``node`` among nodes sharing its visible text (disambiguator
        for a ``{text, index}`` selector, §10)."""
        same = [n for n in self.nodes if n.text == node.text]
        return same.index(node)


# ---------------------------------------------------------------------------
# Selector (§10)
# ---------------------------------------------------------------------------


@dataclass
class Selector:
    """Names a target for ``act``; resolved mechanically device-side (§10).

    Exactly the fields set here cross the wire. The daemon chooses the tightest
    binding it can (grounding §4): ``{ref, snapshot_version}`` when acting
    immediately, degrading to ``view_id`` / ``text`` / ``desc`` / ``bounds``.
    """

    view_id: Optional[str] = None
    text: Optional[str] = None
    index: Optional[int] = None
    desc: Optional[str] = None
    ref: Optional[int] = None
    snapshot_version: Optional[int] = None
    bounds: Optional[List[int]] = None

    def to_wire(self) -> dict:
        out: dict = {}
        if self.ref is not None:
            out["ref"] = self.ref
            out["snapshotVersion"] = self.snapshot_version
        if self.view_id is not None:
            out["viewId"] = self.view_id
        if self.text is not None:
            out["text"] = self.text
        if self.index is not None:
            out["index"] = self.index
        if self.desc is not None:
            out["desc"] = self.desc
        if self.bounds is not None:
            out["bounds"] = self.bounds
        return out

    def describe(self) -> str:
        """Compact human string for the audit trail (§9)."""
        return ",".join(f"{k}={v}" for k, v in self.to_wire().items()) or "<empty>"


# ---------------------------------------------------------------------------
# SettleConfig (§9)
# ---------------------------------------------------------------------------

_EVENT_TYPES = frozenset(
    {"WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED", "VIEW_SCROLLED",
     "VIEW_TEXT_CHANGED", "VIEW_FOCUSED"}
)
_SETTLE_MODES = frozenset({"quiet", "minEventsThenQuiet"})


@dataclass
class SettleConfig:
    """Parameters for the device's on-device debounce detector (§9).

    Authored entirely by the daemon (settle_policy §6) — the device runs it but
    owns none of it. ``quiet_window_ms`` is the primary knob; ``hard_timeout_ms``
    is the mandatory upper bound; ``event_mask`` selects which event types reset
    the quiet timer (the highest-value knob for looping-animation screens).
    """

    quiet_window_ms: int = 500
    hard_timeout_ms: int = 5000
    event_mask: List[str] = field(
        default_factory=lambda: ["WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED"])
    package_scope: List[str] = field(default_factory=list)
    mode: str = "quiet"
    min_event_count: int = 1
    bundle_screenshot_on_settle: bool = False

    def __post_init__(self) -> None:
        bad = [e for e in self.event_mask if e not in _EVENT_TYPES]
        if bad:
            raise ValueError(f"unknown eventMask entries: {bad}")
        if self.mode not in _SETTLE_MODES:
            raise ValueError(f"unknown settle mode: {self.mode!r}")

    def to_wire(self) -> dict:
        return {
            "quietWindowMs": self.quiet_window_ms,
            "hardTimeoutMs": self.hard_timeout_ms,
            "eventMask": list(self.event_mask),
            "packageScope": list(self.package_scope),
            "mode": self.mode,
            "minEventCount": self.min_event_count,
            "bundleScreenshotOnSettle": self.bundle_screenshot_on_settle,
        }


# ---------------------------------------------------------------------------
# Actions (§11)
# ---------------------------------------------------------------------------

_ACTIONS = frozenset(
    {"CLICK", "LONG_CLICK", "SET_TEXT",
     # Orientation-agnostic scroll along the container's own axis (kept: many
     # nodes advertise only these), plus the axis-explicit directional actions
     # (ACTION_SCROLL_DOWN/UP/LEFT/RIGHT, API 23+). The daemon exposes only the
     # directional four to the model — which of the mechanism's actions the model
     # MAY pick is daemon policy, not a reason to amputate the wire (01-PROTOCOL).
     "SCROLL_FORWARD", "SCROLL_BACKWARD",
     "SCROLL_DOWN", "SCROLL_UP", "SCROLL_LEFT", "SCROLL_RIGHT",
     "FOCUS", "GESTURE", "GLOBAL"}
)
_SCROLL_DIRECTIONS = {
    "down": "SCROLL_DOWN", "up": "SCROLL_UP",
    "left": "SCROLL_LEFT", "right": "SCROLL_RIGHT",
}
_GLOBALS = frozenset(
    {"BACK", "HOME", "RECENTS", "NOTIFICATIONS", "QUICK_SETTINGS", "LOCK_SCREEN"}
)


@dataclass
class Action:
    """An ``act`` payload's action portion (§11).

    Exactly one shape applies: ``SET_TEXT`` carries ``text``; ``GESTURE`` carries
    a ``gesture`` dict; ``GLOBAL`` carries a ``global`` name; the rest are bare.
    """

    action: str
    text: Optional[str] = None
    gesture: Optional[dict] = None
    global_action: Optional[str] = None

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError(f"unknown action: {self.action!r}")
        if self.action == "SET_TEXT" and self.text is None:
            raise ValueError("SET_TEXT requires text")
        if self.action == "GESTURE" and not self.gesture:
            raise ValueError("GESTURE requires a gesture")
        if self.action == "GLOBAL":
            if self.global_action not in _GLOBALS:
                raise ValueError(f"unknown global: {self.global_action!r}")

    # Convenience constructors ------------------------------------------------
    @classmethod
    def click(cls) -> "Action":
        return cls("CLICK")

    @classmethod
    def set_text(cls, text: str) -> "Action":
        return cls("SET_TEXT", text=text)

    @classmethod
    def scroll(cls, forward: bool) -> "Action":
        return cls("SCROLL_FORWARD" if forward else "SCROLL_BACKWARD")

    @classmethod
    def scroll_dir(cls, direction: str) -> "Action":
        """Axis-explicit scroll -> the directional AccessibilityActions
        ``ACTION_SCROLL_{DOWN,UP,LEFT,RIGHT}`` (API 23+). Unlike
        SCROLL_FORWARD/BACKWARD — which advance along the container's *own* axis,
        so on a horizontal pager 'forward' pages sideways — these name the axis
        explicitly and target the node, so they scroll the intended direction and
        survive layout shift. ``direction`` is one of down/up/left/right."""
        try:
            return cls(_SCROLL_DIRECTIONS[direction.lower()])
        except KeyError:
            raise ValueError(f"unknown scroll direction: {direction!r} "
                             f"(expected one of {sorted(_SCROLL_DIRECTIONS)})")

    @classmethod
    def global_(cls, name: str) -> "Action":
        return cls("GLOBAL", global_action=name)

    @classmethod
    def gesture_tap(cls, x: int, y: int, duration_ms: int = 60) -> "Action":
        return cls("GESTURE", gesture={"type": "tap", "path": [[x, y]], "durationMs": duration_ms})

    @classmethod
    def gesture_swipe(cls, path: List[List[int]], duration_ms: int = 300) -> "Action":
        return cls("GESTURE", gesture={"type": "swipe", "path": path, "durationMs": duration_ms})

    def merge_into(self, args: dict) -> dict:
        """Merge this action's fields into an ``act`` args dict (§5.3)."""
        args["action"] = self.action
        if self.text is not None:
            args["text"] = self.text
        if self.gesture is not None:
            args["gesture"] = self.gesture
        if self.global_action is not None:
            args["global"] = self.global_action
        return args


def configure_args(settle: SettleConfig, package_scope: List[str],
                   screenshot_defaults: dict, redaction: dict) -> dict:
    """Build the ``configure`` args (§5.1). ``package_scope`` scopes both
    observation and action; passing an empty list keeps the device fail-closed
    (§13.3)."""
    return {
        "settle": settle.to_wire(),
        "screenshotDefaults": screenshot_defaults,
        "redaction": redaction,
        "packageScope": list(package_scope),
    }
