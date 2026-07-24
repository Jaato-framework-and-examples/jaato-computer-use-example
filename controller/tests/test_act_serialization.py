"""Batched-tool serialization (Controller._act_and_settle lock).

With parallel_tools=True the model can emit several primitives in one turn, and
the SDK dispatches each host-tool call as its own asyncio task — so they'd race
on the device. The controller serializes them through one FIFO lock: this test
fires N acts concurrently (as the SDK would) and asserts (a) no two acts overlap
on the device and (b) they execute in the order they were submitted.
"""
import asyncio

from a11y.controller import Controller
from a11y.device_session import Observation
from a11y.protocol import Snapshot


def _snap(v):
    return Snapshot.parse({"snapshotVersion": v, "pkg": "x", "activity": None,
                           "screen": {"width": 100, "height": 100}, "nodes": []})


class _Settled:
    def __init__(self, reason, version):
        self.reason, self.version = reason, version


class _RecordingSession:
    """Fake device session that flags overlap across the WHOLE act op (send AND
    settle AND reobserve) and records act order. ``in_op`` is held from the act
    frame through the settle wait until reobserve, so if the lock released after
    send — letting act2 fire during act1's settle (the type-before-focus race) —
    it would be caught as an overlap. ``await_settled`` sleeps to widen that
    window, so a lock that stopped at 'send' would fail this test."""

    def __init__(self):
        self.current_snapshot = _snap(1)
        self.alive = True
        self.in_op = False
        self.overlaps = 0
        self.order = []
        self.settle_overrides = []       # settle_override seen per act (None = default)
        self._v = 1

    async def act(self, selector, action, settle_override=None):
        if self.in_op:                   # a prior op's send/settle still in flight
            self.overlaps += 1
        self.in_op = True
        self.order.append(action.text or action.global_action or action.action)
        self.settle_overrides.append(settle_override)
        await asyncio.sleep(0.005)        # simulate the send actuating

    async def await_settled(self, timeout):
        await asyncio.sleep(0.01)         # settle wait — must still be under the lock
        self._v += 1
        return _Settled("quiet", self._v)

    async def observe(self, screenshot=True, screenshot_params=None):
        self.current_snapshot = _snap(self._v)
        self.in_op = False               # reobserve is the last step under the lock
        return Observation(snapshot=self.current_snapshot, image=None)

    async def configure(self, *a, **k):
        pass


class _Audit:
    def record(self, *a, **k):
        pass


def test_batched_acts_serialize_in_emission_order():
    sess = _RecordingSession()
    ctl = Controller(sess, audit=_Audit(), package_scope=["x"],
                     screenshot_defaults={}, redaction={}, follow_foreground=False)

    async def go():
        # Mirror the SDK firing a batch: each tool call is its own task.
        tasks = [asyncio.ensure_future(ctl.type_text(t)) for t in ["a", "b", "c", "d"]]
        await asyncio.gather(*tasks)

    asyncio.run(go())

    assert sess.overlaps == 0                       # never two device acts at once
    assert sess.order == ["a", "b", "c", "d"]       # FIFO: emitted order preserved


def test_global_act_gets_focus_settle_override():
    """A GLOBAL act (whose effect is in another window) is sent with the desktop-
    wide VIEW_FOCUSED settle override, so the device settles on the focus change
    instead of timing out on a subtree that can't see it. Non-global acts get no
    override (None = session default)."""
    sess = _RecordingSession()
    ctl = Controller(sess, audit=_Audit(), package_scope=["x"],
                     screenshot_defaults={}, redaction={}, follow_foreground=False)
    asyncio.run(ctl.global_action("START_MENU"))
    asyncio.run(ctl.type_text("hello"))   # focus-directed, not global

    ov_global = sess.settle_overrides[0]
    assert ov_global is not None
    assert "VIEW_FOCUSED" in ov_global.event_mask       # settles on the focus change
    assert ov_global.package_scope == []                # desktop-wide (other window)
    assert ov_global.hard_timeout_ms <= 2000            # short bound, not the 12s ceiling
    assert ov_global.mode == "quiet"
    assert sess.settle_overrides[1] is None             # type_text uses the default
