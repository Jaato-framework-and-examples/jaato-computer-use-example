"""Reconnect recovery (Controller, no device/network).

On a dropped session the controller waits for the device to dial back in and
adopts the newest bridge session (re-configured to the current scope), so a flap
is a brief pause, not a stuck 'device offline'. An operator DISCONNECT
(``bye`` reason ``user_disconnect``) is announced distinctly to ``reacquire`` but
is still resumable — reconnect and the new session is adopted.
"""
import asyncio

import pytest

from a11y.controller import Controller
from a11y.device_session import Observation
from a11y.protocol import Snapshot
from a11y.wire import DeviceError, ErrorCode


def _snap(version: int) -> Snapshot:
    return Snapshot.parse({"snapshotVersion": version, "pkg": "com.x",
                           "activity": "com.x/.Main", "screen": {"width": 100, "height": 100},
                           "nodes": []})


class _DeadSession:
    """A dropped session: observe raises 'device offline' and reports not-alive."""

    def __init__(self, bye_reason=None):
        self.alive = False
        self.bye_reason = bye_reason
        self.device_id = "dead-1"
        self.current_snapshot = None

    async def observe(self, screenshot=True, screenshot_params=None):
        raise DeviceError(ErrorCode.INTERNAL, "device offline")

    async def configure(self, *a, **k):
        pass


class _LiveSession:
    def __init__(self, snap):
        self.alive = True
        self.bye_reason = None
        self.device_id = "live-2"
        self.current_snapshot = None
        self._snap = snap
        self.configured = []

    async def observe(self, screenshot=True, screenshot_params=None):
        self.current_snapshot = self._snap
        return Observation(snapshot=self._snap, image=None)

    async def configure(self, settle, scope, shot, redaction):
        self.configured.append(list(scope))


def _controller(session, reacquire, scope=("com.x",)):
    # follow_foreground=False keeps _observe_once to a single observe (no windows
    # verb), isolating the recovery behaviour.
    return Controller(session, audit=None, package_scope=list(scope),
                      screenshot_defaults={}, redaction={},
                      follow_foreground=False, reacquire=reacquire)


def test_adopts_new_session_on_drop():
    live = _LiveSession(_snap(11))
    seen = {}

    async def reacquire(reason):
        seen["reason"] = reason
        return live

    ctl = _controller(_DeadSession(), reacquire)
    obs = asyncio.run(ctl.first_observation())

    assert seen["reason"] is None              # a bare network drop, no bye reason
    assert obs.snapshot.version == 11          # observed on the newly-adopted session
    assert live.configured == [["com.x"]]      # re-configured to the current scope
    assert ctl._session is live                # swapped over


def test_user_disconnect_is_announced_but_resumes():
    live = _LiveSession(_snap(12))
    seen = {}

    async def reacquire(reason):
        seen["reason"] = reason
        return live

    ctl = _controller(_DeadSession(bye_reason="user_disconnect"), reacquire)
    obs = asyncio.run(ctl.first_observation())

    assert seen["reason"] == "user_disconnect"  # announced distinctly to the caller
    assert ctl._session is live                 # ...but resumed on reconnect, not stranded
    assert obs.snapshot.version == 12


def test_unavailable_when_no_session_returns():
    async def reacquire(reason):
        return None                             # nothing came back

    ctl = _controller(_DeadSession(), reacquire)
    with pytest.raises(DeviceError) as exc:
        asyncio.run(ctl.first_observation())
    assert "unavailable" in str(exc.value)
