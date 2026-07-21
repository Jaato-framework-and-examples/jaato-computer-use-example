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
    """Fake device session that flags overlapping acts and records act order."""

    def __init__(self):
        self.current_snapshot = _snap(1)
        self.alive = True
        self.in_act = False
        self.overlaps = 0
        self.order = []
        self._v = 1

    async def act(self, selector, action):
        if self.in_act:
            self.overlaps += 1          # two acts in flight => serialization broke
        self.in_act = True
        self.order.append(action.text or action.global_action or action.action)
        await asyncio.sleep(0.01)        # simulate device work; yields the loop
        self.in_act = False

    async def await_settled(self, timeout):
        self._v += 1
        return _Settled("quiet", self._v)

    async def observe(self, screenshot=True, screenshot_params=None):
        self.current_snapshot = _snap(self._v)
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
