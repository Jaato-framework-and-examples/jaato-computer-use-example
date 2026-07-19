"""STALE-retry with a version-independent selector (Controller, no device).

A churning screen (live clock/animation) bumps snapshotVersion between observe
and act, so the version-bound ref selector STALEs even though the target node is
unchanged. The controller must retry once with a version-independent selector
(viewId/text) that resolves in the current tree, so the tap/type lands.
"""
import asyncio

from a11y.controller import Controller
from a11y.device_session import Observation
from a11y.protocol import Action, Snapshot
from a11y.wire import DeviceError, ErrorCode


def _snap() -> Snapshot:
    return Snapshot.parse({
        "snapshotVersion": 5, "pkg": "com.x", "activity": "com.x/.Main",
        "screen": {"width": 100, "height": 100},
        "nodes": [{"ref": 1, "cls": "android.widget.TextView", "viewId": "com.x:id/foo",
                   "text": "Foo", "bounds": [0, 0, 10, 10],
                   "flags": ["clickable", "enabled", "visible"]}],
    })


class _NullAudit:
    def record(self, *a, **k):
        pass


class _StaleThenOkSession:
    """First act STALEs (version-bound selector); the second (stable selector)
    lands. await_settled times out (avoids constructing a Settled)."""

    def __init__(self, snap):
        self.current_snapshot = snap
        self.alive = True
        self.bye_reason = None
        self.acts = []

    async def act(self, selector, action, settle_override=None):
        self.acts.append(selector)
        if len(self.acts) == 1:
            raise DeviceError(ErrorCode.STALE, "world moved between observe and act")
        return {"resolved": True}

    async def await_settled(self, timeout):
        raise DeviceError(ErrorCode.TIMEOUT, "no settle")

    async def observe(self, screenshot=True, screenshot_params=None):
        return Observation(snapshot=self.current_snapshot, image=None)

    async def configure(self, *a, **k):
        pass


def test_stale_retries_with_stable_selector_and_lands():
    snap = _snap()
    sess = _StaleThenOkSession(snap)
    ctl = Controller(sess, audit=_NullAudit(), package_scope=["com.x"],
                     screenshot_defaults={}, redaction={}, follow_foreground=False)

    ack = asyncio.run(ctl.act_ref(1, Action.click()))

    assert len(sess.acts) == 2                          # STALE, then retried
    # first attempt was the version-bound ref selector; the retry was the stable
    # (viewId) one — version-independent.
    assert sess.acts[0].snapshot_version == 5 and sess.acts[0].ref == 1
    assert sess.acts[1].view_id == "com.x:id/foo" and sess.acts[1].ref is None
    assert "not applied" not in ack                     # it landed, not rejected
