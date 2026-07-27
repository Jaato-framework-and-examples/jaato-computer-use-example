"""Device-selection tools (list_devices / connect_device) for the single agent.

connect_device validates the id then delegates to a host-provided ``connect_cb``
that binds the device and returns the platform guide + first screen as the tool
result. These tests pin that delegation and the unknown-id guard.
"""
import asyncio
from types import SimpleNamespace

from a11y.host_tools import build_selection_tools, devices_text


class _StubBridge:
    """Minimal BridgeServer stand-in: a fixed registry of (id, platform)."""

    def __init__(self, devices):
        self._devs = [SimpleNamespace(device_id=i, platform=p) for i, p in devices]

    def list_devices(self):
        return list(self._devs)

    def get_device(self, device_id):
        return next((d for d in self._devs if d.device_id == device_id), None)


def _tool(specs, name):
    return next(s["handler"] for s in specs if s["name"] == name)


def test_list_devices_lists_ids_and_platforms():
    bridge = _StubBridge([("phone-1", "android"), ("DESKTOP-DN", "windows")])
    specs = build_selection_tools(bridge, connect_cb=None)
    out = asyncio.run(_tool(specs, "list_devices")({}))["result"]
    assert "phone-1" in out and "android" in out
    assert "DESKTOP-DN" in out and "windows" in out


def test_connect_device_delegates_then_returns_platform_guide():
    bridge = _StubBridge([("phone-1", "android")])
    seen = {}

    async def connect_cb(device_id):
        seen["device_id"] = device_id
        # connect_cb returns the bound controller; the tool loads the guide itself.
        # pending_observation=None → screen_result degrades to {"result": guide}.
        return SimpleNamespace(platform="android", pending_observation=None)

    specs = build_selection_tools(bridge, connect_cb)
    res = asyncio.run(_tool(specs, "connect_device")({"device_id": "phone-1"}))
    assert seen == {"device_id": "phone-1"}          # delegated with the chosen id
    assert "Android" in res["result"]                # the tool loaded the android guide


def test_connect_device_missing_guide_is_reported():
    bridge = _StubBridge([("odd-1", "symbian")])

    async def connect_cb(device_id):
        return SimpleNamespace(platform="symbian", pending_observation=None)

    specs = build_selection_tools(bridge, connect_cb)
    res = asyncio.run(_tool(specs, "connect_device")({"device_id": "odd-1"}))
    assert "symbian" in res["result"] and "can't drive" in res["result"]


def test_connect_device_unknown_id_is_guarded():
    bridge = _StubBridge([("phone-1", "android")])
    called = {"n": 0}

    async def connect_cb(device_id):
        called["n"] += 1
        return SimpleNamespace(platform="android", pending_observation=None)

    specs = build_selection_tools(bridge, connect_cb)
    res = asyncio.run(_tool(specs, "connect_device")({"device_id": "ghost"}))
    assert called["n"] == 0                            # connect_cb never invoked
    assert "ghost" in res["result"] and "list_devices" in res["result"]


def test_devices_text_empty_guides_operator():
    out = devices_text([])
    assert "No devices" in out and "advertis" in out.lower()
