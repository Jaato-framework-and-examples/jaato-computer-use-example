"""Multi-device registry in BridgeServer (real loopback sockets).

Devices advertise by dialing in; the bridge holds them all as *available*
(no first-wins rejection), exposes them via ``list_devices`` / ``get_device``,
drops one from the registry when its socket closes, and lets recovery block for a
specific device to (re)appear via ``wait_for_device(device_id=…)``.
"""
import asyncio
import json
import socket

import pytest
import websockets

from a11y.device_session import BridgeServer
from a11y.wire import PV


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hello(device_id: str, platform: str = "android") -> str:
    return json.dumps({"kind": "event", "event": "hello", "data": {
        "pv": PV, "deviceId": device_id, "platform": platform, "androidSdk": 34,
        "capabilities": {"takeScreenshot": True},
        "screen": {"width": 1080, "height": 2340, "density": 2.0}}})


async def _advertise(url: str, bridge: BridgeServer, device_id: str, platform="android"):
    """Dial in, send hello, and wait until the bridge has registered the id."""
    ws = await websockets.connect(url)
    await ws.send(_hello(device_id, platform))
    for _ in range(100):
        if bridge.get_device(device_id) is not None:
            break
        await asyncio.sleep(0.02)
    return ws


def test_two_devices_both_advertised():
    async def main():
        port = _free_port()
        bridge = BridgeServer("127.0.0.1", port, "/a11y", token=None, unsafe_no_auth=True)
        await bridge.start()
        url = f"ws://127.0.0.1:{port}/a11y"
        try:
            ws1 = await _advertise(url, bridge, "dev-1", "android")
            ws2 = await _advertise(url, bridge, "dev-2", "windows")

            # Both are held — no first-wins rejection.
            ids = {d.device_id for d in bridge.list_devices()}
            assert ids == {"dev-1", "dev-2"}
            assert bridge.get_device("dev-1").platform == "android"
            assert bridge.get_device("dev-2").platform == "windows"
            assert bridge.get_device("nope") is None

            # dev-1 leaves -> dropped from the registry; dev-2 unaffected.
            await ws1.close()
            for _ in range(100):
                if bridge.get_device("dev-1") is None:
                    break
                await asyncio.sleep(0.02)
            assert bridge.get_device("dev-1") is None
            assert {d.device_id for d in bridge.list_devices()} == {"dev-2"}
            await ws2.close()
        finally:
            await bridge.stop()

    asyncio.run(main())


def test_wait_for_specific_device_reconnect():
    async def main():
        port = _free_port()
        bridge = BridgeServer("127.0.0.1", port, "/a11y", token=None, unsafe_no_auth=True)
        await bridge.start()
        url = f"ws://127.0.0.1:{port}/a11y"
        try:
            ws1 = await _advertise(url, bridge, "dev-1")
            first = bridge.get_device("dev-1")

            await ws1.close()               # the held session drops
            for _ in range(100):
                if bridge.get_device("dev-1") is None:
                    break
                await asyncio.sleep(0.02)
            assert bridge.get_device("dev-1") is None

            # With the device gone, recovery blocks on THIS id until it re-appears.
            waiter = asyncio.ensure_future(
                bridge.wait_for_device(device_id="dev-1", timeout=5.0))
            await asyncio.sleep(0.05)       # let the waiter reach its wait
            assert not waiter.done()
            ws2 = await _advertise(url, bridge, "dev-1")   # same id dials back in
            reacquired = await asyncio.wait_for(waiter, timeout=5.0)
            assert reacquired.device_id == "dev-1"
            assert reacquired is not first   # a fresh session object
            assert bridge.get_device("dev-1") is reacquired
            await ws2.close()
        finally:
            await bridge.stop()

    asyncio.run(main())


def test_registry_change_callback_fires_on_add_and_remove():
    async def main():
        events = []
        port = _free_port()
        bridge = BridgeServer("127.0.0.1", port, "/a11y", token=None, unsafe_no_auth=True,
                              on_registry_change=lambda s, present: events.append((s.device_id, present)))
        await bridge.start()
        url = f"ws://127.0.0.1:{port}/a11y"
        try:
            ws = await _advertise(url, bridge, "dev-1")
            assert ("dev-1", True) in events            # announced on advertise
            await ws.close()
            for _ in range(100):
                if ("dev-1", False) in events:
                    break
                await asyncio.sleep(0.02)
            assert ("dev-1", False) in events           # announced on leave
        finally:
            await bridge.stop()

    asyncio.run(main())


def test_wait_for_any_device():
    async def main():
        port = _free_port()
        bridge = BridgeServer("127.0.0.1", port, "/a11y", token=None, unsafe_no_auth=True)
        await bridge.start()
        url = f"ws://127.0.0.1:{port}/a11y"
        try:
            # No device yet: a bare wait (device_id=None) blocks until one appears.
            waiter = asyncio.ensure_future(bridge.wait_for_device(timeout=5.0))
            ws1 = await _advertise(url, bridge, "dev-1")
            got = await asyncio.wait_for(waiter, timeout=5.0)
            assert got.device_id == "dev-1"
            await ws1.close()
        finally:
            await bridge.stop()

    asyncio.run(main())
