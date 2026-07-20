"""Unit tests for the pure wire/protocol/policy logic (no device, no network).

Exercises the parts that must be byte-faithful to 01-PROTOCOL.md: binary
framing, Snapshot/Selector/SettleConfig/Action (de)serialisation, grounding
selector choice + recovery, and config validation.
"""
import io
import json
import struct

import pytest

from a11y import grounding
from a11y.annotate import set_of_marks, tree_text
from a11y.device_session import Observation
from a11y.framing import parse_binary_frame
from a11y.protocol import (Action, Selector, SettleConfig, Snapshot,
                           configure_args)
from a11y.wire import PV, DeviceError, ErrorCode


# --- framing (01 §4) --------------------------------------------------------

def _build_frame(header: dict, payload: bytes) -> bytes:
    hb = json.dumps(header).encode("utf-8")
    return struct.pack(">I", len(hb)) + hb + payload


def test_parse_binary_frame_roundtrip():
    header = {"type": "screenshot", "correlationId": "r-2", "snapshotVersion": 1291,
              "format": "png", "width": 1080, "height": 2340, "reason": "bundled"}
    frame = parse_binary_frame(_build_frame(header, b"\x89PNGdata"))
    assert frame.snapshot_version == 1291
    assert frame.correlation_id == "r-2"
    assert frame.payload == b"\x89PNGdata"


def test_parse_binary_frame_truncated_raises():
    with pytest.raises(ValueError):
        parse_binary_frame(b"\x00\x00")  # < 4 bytes
    with pytest.raises(ValueError):
        parse_binary_frame(struct.pack(">I", 999) + b"short")  # header shorter than declared


# --- snapshot / node (01 §8) ------------------------------------------------

def _snap() -> Snapshot:
    return Snapshot.parse({
        "snapshotVersion": 1291, "pkg": "com.foo.app", "activity": ".Main",
        "screen": {"width": 1080, "height": 2340},
        "nodes": [
            {"ref": 1, "cls": "android.widget.Button", "viewId": "com.foo.app:id/ok",
             "text": "OK", "bounds": [0, 0, 100, 50], "flags": ["clickable", "enabled", "visible"]},
            {"ref": 2, "cls": "android.widget.TextView", "text": "OK",
             "bounds": [0, 60, 100, 90], "flags": ["visible"]},
            {"ref": 3, "cls": "android.widget.EditText", "viewId": "com.foo.app:id/dup",
             "bounds": [0, 100, 100, 150], "flags": ["editable", "focusable", "visible"]},
            {"ref": 4, "cls": "android.widget.EditText", "viewId": "com.foo.app:id/dup",
             "bounds": [0, 160, 100, 210], "flags": ["editable", "visible"]},
        ],
    })


def test_snapshot_lookup_and_helpers():
    s = _snap()
    assert s.by_ref(3).view_id == "com.foo.app:id/dup"
    with pytest.raises(KeyError):
        s.by_ref(99)
    assert {n.ref for n in s.actionable_nodes()} == {1, 3, 4}  # 2 is a bare TextView
    assert s.view_id_ambiguous("com.foo.app:id/dup") is True
    assert s.view_id_ambiguous("com.foo.app:id/ok") is False
    assert s.text_index(s.by_ref(2)) == 1  # second node with text "OK"


# --- selector (01 §10) ------------------------------------------------------

def test_selector_to_wire_ref_binding():
    w = Selector(ref=42, snapshot_version=1291).to_wire()
    assert w == {"ref": 42, "snapshotVersion": 1291}


def test_selector_to_wire_composite():
    w = Selector(view_id="id/x", text="Row 1").to_wire()
    assert w == {"viewId": "id/x", "text": "Row 1"}


# --- settle config (01 §9) --------------------------------------------------

def test_settle_config_validation():
    with pytest.raises(ValueError):
        SettleConfig(event_mask=["NOT_A_REAL_EVENT"])
    with pytest.raises(ValueError):
        SettleConfig(mode="bogus")
    ok = SettleConfig(quiet_window_ms=800, package_scope=["com.foo.app"])
    assert ok.to_wire()["quietWindowMs"] == 800


# --- actions (01 §11) -------------------------------------------------------

def test_action_validation_and_merge():
    with pytest.raises(ValueError):
        Action("SET_TEXT")  # missing text
    with pytest.raises(ValueError):
        Action.global_("NOPE")
    args = Action.set_text("hello").merge_into({"target": {}})
    assert args["action"] == "SET_TEXT" and args["text"] == "hello"
    g = Action.gesture_tap(10, 20).merge_into({})
    assert g["gesture"]["type"] == "tap"


def test_configure_args_shape():
    a = configure_args(SettleConfig(package_scope=["p"]), ["p"],
                       {"format": "png"}, {"maskPasswordNodes": True})
    assert a["packageScope"] == ["p"]
    assert a["settle"]["packageScope"] == ["p"]
    assert a["redaction"]["maskPasswordNodes"] is True


# --- grounding (03 §4) ------------------------------------------------------

def test_to_selector_immediate_uses_ref():
    s = _snap()
    sel = grounding.to_selector(1, s, acting_immediately=True)
    assert sel.ref == 1 and sel.snapshot_version == 1291


def test_to_selector_durable_prefers_viewid_disambiguated():
    s = _snap()
    # ref 3 has an ambiguous viewId -> selector carries text disambiguator (None here) ...
    sel = grounding.to_selector(1, s, acting_immediately=False)
    assert sel.view_id == "com.foo.app:id/ok" and sel.text is None
    # ref 3's viewId is ambiguous; it has no text, so text stays None but viewId is chosen
    sel3 = grounding.to_selector(3, s, acting_immediately=False)
    assert sel3.view_id == "com.foo.app:id/dup"


def test_to_selector_text_then_desc_then_bounds():
    s = Snapshot.parse({
        "snapshotVersion": 5, "pkg": "p", "activity": "a", "screen": {"width": 100, "height": 100},
        "nodes": [
            {"ref": 1, "cls": "X", "text": "Hello", "bounds": [0, 0, 10, 10], "flags": ["clickable"]},
            {"ref": 2, "cls": "Y", "desc": "Close", "bounds": [0, 10, 10, 20], "flags": ["clickable"]},
            {"ref": 3, "cls": "Z", "bounds": [0, 20, 10, 30], "flags": ["clickable"]},
        ],
    })
    assert grounding.to_selector(1, s, acting_immediately=False).text == "Hello"
    assert grounding.to_selector(2, s, acting_immediately=False).desc == "Close"
    assert grounding.to_selector(3, s, acting_immediately=False).bounds == [0, 20, 10, 30]


def test_recovery_classification():
    assert grounding.describe_recovery(DeviceError(ErrorCode.STALE)).next == "reobserve"
    assert grounding.describe_recovery(DeviceError(ErrorCode.AMBIGUOUS)).next == "reobserve"
    # NOT_ACTIONABLE on a CLICK -> tap the visible-but-inert node by coordinate.
    r = grounding.describe_recovery(DeviceError(ErrorCode.NOT_ACTIONABLE),
                                    node_bounds=[0, 0, 40, 40], action=Action.click())
    assert r.next == "retry" and r.action.action == "GESTURE"
    assert r.selector.bounds == [0, 0, 40, 40]
    # NOT_ACTIONABLE on a SCROLL must NOT become a tap (that would launch the
    # element) — re-observe and steer the agent instead.
    s = grounding.describe_recovery(DeviceError(ErrorCode.NOT_ACTIONABLE),
                                    node_bounds=[0, 0, 40, 40], action=Action.scroll_dir("down"))
    assert s.next == "reobserve" and s.action is None
    # The device's own message (e.g. "advertised but at scroll extent" vs "does
    # not advertise SCROLL_DOWN") is a distinction only it can make — surface it
    # verbatim rather than flattening it to a generic hint.
    s2 = grounding.describe_recovery(
        DeviceError(ErrorCode.NOT_ACTIONABLE, "already at scroll extent"),
        node_bounds=[0, 0, 40, 40], action=Action.scroll_dir("down"))
    assert s2.reason == "already at scroll extent"
    # NOT_ACTIONABLE on IME_ENTER surfaces the device's reason verbatim (e.g. "not
    # focused") so the model knows to tap the field before submitting — a generic
    # hint would hide it. And no doubled `NOT_ACTIONABLE:` prefix (the ack adds it).
    i = grounding.describe_recovery(
        DeviceError(ErrorCode.NOT_ACTIONABLE,
                    "IME_ENTER advertised but returned false — field may not be focused"),
        action=Action.ime_enter())
    assert i.next == "reobserve" and "may not be focused" in i.reason
    assert not i.reason.startswith("NOT_ACTIONABLE")


def test_scroll_dir_maps_to_directional_actions():
    assert Action.scroll_dir("down").action == "SCROLL_DOWN"
    assert Action.scroll_dir("UP").action == "SCROLL_UP"
    assert Action.scroll_dir("left").action == "SCROLL_LEFT"
    assert Action.scroll_dir("right").action == "SCROLL_RIGHT"
    # Orientation-agnostic FORWARD/BACKWARD stay on the wire (device keeps them).
    assert Action.scroll(True).action == "SCROLL_FORWARD"
    for bad in ("forward", "sideways", ""):
        try:
            Action.scroll_dir(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"scroll_dir({bad!r}) should have raised")


def test_ime_enter_action():
    a = Action.ime_enter()
    assert a.action == "IME_ENTER" and a.text is None and a.gesture is None
    assert Action("IME_ENTER").action == "IME_ENTER"  # accepted by Action() validation


# --- annotate ---------------------------------------------------------------

def test_tree_text_lists_refs():
    obs = Observation(snapshot=_snap(), image=None)
    txt = tree_text(obs)
    assert "[1]" in txt and "com.foo.app:id/ok" in txt and "v1291" in txt


def test_tree_text_collapses_scroll_axes():
    # The X-feed case: two scrollables read identically as bare `scrollable` until
    # the device exposes axes — a horizontal pager (left/right) and a vertical feed
    # (down/up). The tree must fold the axis tokens into `scrollable:<dirs>` so the
    # model can tell them apart, and must not leak the raw scrollable<Dir> tokens.
    snap = Snapshot.parse({
        "snapshotVersion": 7, "pkg": "com.x", "activity": ".Main",
        "screen": {"width": 1000, "height": 2000},
        "nodes": [
            {"ref": 0, "cls": "V", "bounds": [0, 0, 1000, 2000],
             "flags": ["scrollable", "scrollableLeft", "scrollableRight", "visible"]},
            {"ref": 1, "cls": "V", "bounds": [0, 0, 1000, 2000],
             "flags": ["scrollable", "scrollableDown", "scrollableUp", "visible"]},
        ],
    })
    txt = tree_text(Observation(snapshot=snap, image=None))
    assert "scrollable:left,right" in txt   # pager (ref 0)
    assert "scrollable:down,up" in txt      # feed (ref 1)
    assert "scrollableDown" not in txt      # raw axis tokens folded away
    assert "scrollableLeft" not in txt


def test_tree_text_marks_submittable_field():
    # imeEnter (the field advertises ACTION_IME_ENTER) folds into `editable:submit`
    # so the model knows which field to screen_submit after typing — vs a plain
    # editable that must be submitted some other way.
    snap = Snapshot.parse({
        "snapshotVersion": 3, "pkg": "com.x", "activity": ".Main",
        "screen": {"width": 1000, "height": 2000},
        "nodes": [
            {"ref": 0, "cls": "E", "bounds": [0, 0, 500, 80],
             "flags": ["editable", "imeEnter", "focusable", "visible"]},
            {"ref": 1, "cls": "E", "bounds": [0, 90, 500, 170],
             "flags": ["editable", "visible"]},
        ],
    })
    txt = tree_text(Observation(snapshot=snap, image=None))
    assert "editable:submit" in txt   # ref 0 advertises submit
    assert "imeEnter" not in txt       # raw token folded away
    assert "[1] E '' [0, 90, 500, 170] <editable,visible>" in txt  # ref 1 plain editable


def test_set_of_marks_draws_on_real_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (540, 1170), "white").save(buf, format="PNG")
    obs = Observation(snapshot=_snap(), image=buf.getvalue())
    out = set_of_marks(obs)
    assert out[:8] == b"\x89PNG\r\n\x1a\n"  # a valid PNG came back
    with pytest.raises(ValueError):
        set_of_marks(Observation(snapshot=_snap(), image=None))


# --- wire -------------------------------------------------------------------

def test_device_error_from_wire():
    e = DeviceError.from_wire({"code": "RATE_LIMITED", "message": "slow down", "retryAfterMs": 640})
    assert e.code == "RATE_LIMITED" and e.retry_after_ms == 640
    assert PV == 1


def test_windows_text_lists_and_marks_foreground():
    """windows_text renders the `windows` verb result, one line per window, with
    exactly the foreground window marked and UWP windows shown by AUMID."""
    from a11y.host_tools import windows_text
    data = {"windows": [
        {"id": 66, "title": "Notepad", "exePath": r"C:\Windows\notepad.exe",
         "foreground": False},
        {"id": 77, "title": "ssh dan@box",
         "exePath": r"C:\Program Files\Mobatek\MobaXterm\MobaXterm.exe",
         "foreground": True},
        {"id": 88, "title": "Calculator",
         "aumid": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "foreground": False},
    ]}
    out = windows_text(data)
    assert "notepad.exe" in out                       # Win32 -> exe basename
    assert "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" in out  # UWP -> aumid
    fg = [ln for ln in out.splitlines() if "[FOREGROUND]" in ln]
    assert len(fg) == 1 and "MobaXterm.exe" in fg[0]  # exactly the foreground one
    assert "no top-level windows" in windows_text({"windows": []})


def test_build_tools_gates_nav_by_platform():
    """Android system-nav tools (back/home/recents) are Android-only; screen_windows
    is Windows-only. A tool mapping to a global the platform lacks would only ever
    return NOT_ACTIONABLE, so it must not be offered there."""
    from a11y.host_tools import build_tools

    class _Ctl:
        def __init__(self, platform):
            self.platform = platform

    common = {"screen_tap", "screen_type", "screen_scroll", "screen_submit",
              "screen_gesture", "screen_wait", "screen_done"}
    win = {s["name"] for s in build_tools(_Ctl("windows"))}
    andr = {s["name"] for s in build_tools(_Ctl("android"))}

    assert "screen_windows" in win
    assert not ({"screen_home", "screen_recents", "screen_back"} & win)
    assert {"screen_home", "screen_recents", "screen_back"} <= andr
    assert "screen_windows" not in andr
    assert common <= win and common <= andr


def test_start_menu_global_and_windows_tool():
    """START_MENU is a valid wire global (Windows key -> Start w/ search focused);
    screen_start_menu is exposed only on Windows."""
    from a11y.protocol import Action
    assert Action.global_("START_MENU").global_action == "START_MENU"
    from a11y.host_tools import build_tools

    class _Ctl:
        def __init__(self, p): self.platform = p

    assert "screen_start_menu" in {s["name"] for s in build_tools(_Ctl("windows"))}
    assert "screen_start_menu" not in {s["name"] for s in build_tools(_Ctl("android"))}
