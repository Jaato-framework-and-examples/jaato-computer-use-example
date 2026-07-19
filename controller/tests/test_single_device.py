"""First-wins single-device enforcement in BridgeServer (real loopback sockets).

A second device that dials in while one is connected is rejected (close 1013);
the first keeps the session. After the first disconnects, the slot frees so a
fresh device can take over.
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


def _hello(device_id: str) -> str:
    return json.dumps({"kind": "event", "event": "hello", "data": {
        "pv": PV, "deviceId": device_id, "androidSdk": 34,
        "capabilities": {"takeScreenshot": True},
        "screen": {"width": 1080, "height": 2340, "density": 2.0}}})


def test_second_device_rejected_first_keeps_session():
    async def main():
        port = _free_port()
        bridge = BridgeServer("127.0.0.1", port, "/a11y", token=None, unsafe_no_auth=True)
        await bridge.start()
        url = f"ws://127.0.0.1:{port}/a11y"
        try:
            ws1 = await websockets.connect(url)
            await ws1.send(_hello("dev-1"))
            session = await asyncio.wait_for(bridge.wait_for_device(timeout=5.0), timeout=5.0)
            assert session.device_id == "dev-1"

            # Second device dials in while dev-1 holds the slot -> server rejects it.
            ws2 = await websockets.connect(url)
            with pytest.raises(websockets.ConnectionClosed) as exc:
                await asyncio.wait_for(ws2.recv(), timeout=3.0)
            assert exc.value.rcvd is not None and exc.value.rcvd.code == 1013

            # dev-1 is untouched: still the active session.
            assert bridge.session is not None and bridge.session.device_id == "dev-1"

            # dev-1 leaves -> slot frees -> a fresh device can take over.
            await ws1.close()
            for _ in range(50):
                if not bridge._connected:
                    break
                await asyncio.sleep(0.05)
            assert bridge._connected is False and bridge.session is None

            ws3 = await websockets.connect(url)
            await ws3.send(_hello("dev-3"))
            session3 = await asyncio.wait_for(bridge.wait_for_device(timeout=5.0), timeout=5.0)
            assert session3.device_id == "dev-3"
            await ws3.close()
        finally:
            await bridge.stop()

    asyncio.run(main())
