#!/usr/bin/env python3
"""End-to-end proof: the REAL gemini agent drives a MOCK device through the actual
run_controller loop. No physical hardware — a scripted 2-screen device stands in,
but everything else is real (jaato daemon, a11y-controller profile, pass:// key,
host tools, set-of-marks vision grounding, DeviceSession, Controller).

Scenario: screen 1 has a 'Go' button; tapping it advances to a 'Success' screen;
the agent should then call screen_done. Bounded by a wall-clock timeout so a
non-terminating agent can't run away on credits.

Run:  python tools/live_loop_smoke.py
"""
import asyncio
import io
import json
import os
import socket
import struct
import sys

import websockets
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_controller  # noqa: E402
from a11y.wire import PV  # noqa: E402

HOST, PATH = "127.0.0.1", "/a11y"


def _free_port() -> int:
    """Ask the OS for an unused loopback TCP port (bind :0, read it back). Avoids
    colliding with whatever else is on the box — e.g. the device-side e2e_daemon
    that normally owns the committed 8765."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _png(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (540, 1170), color).save(buf, format="PNG")
    return buf.getvalue()


def _frame(header: dict, payload: bytes) -> bytes:
    hb = json.dumps(header).encode("utf-8")
    return struct.pack(">I", len(hb)) + hb + payload


def _screen(state: str, version: int) -> dict:
    if state == "home":
        nodes = [{"ref": 1, "cls": "android.widget.Button", "viewId": "com.mock.app:id/go",
                  "text": "Go", "bounds": [100, 200, 500, 320],
                  "flags": ["clickable", "enabled", "visible"]}]
    else:  # "success"
        nodes = [{"ref": 1, "cls": "android.widget.TextView", "text": "Success!",
                  "bounds": [100, 200, 900, 320], "flags": ["visible"]}]
    return {"snapshotVersion": version, "pkg": "com.mock.app", "activity": f".{state}",
            "screen": {"width": 1080, "height": 2340}, "nodes": nodes}


async def mock_device(stop: asyncio.Event, port: int) -> None:
    st = {"state": "home", "v": 1000}
    # retry until run()'s listener is up
    for _ in range(50):
        try:
            ws = await websockets.connect(f"ws://{HOST}:{port}{PATH}")
            break
        except OSError:
            await asyncio.sleep(0.2)
    else:
        print("!! mock could not connect")
        return
    async with ws:
        await ws.send(json.dumps({"kind": "event", "event": "hello", "data": {
            "pv": PV, "deviceId": "mock-tab-01", "androidSdk": 34,
            "capabilities": {"takeScreenshot": True, "canPerformGestures": True,
                             "reportViewIds": True, "retrieveInteractiveWindows": True},
            "screen": {"width": 1080, "height": 2340, "density": 2.75}}}))
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                return
            req = json.loads(raw)
            rid, verb = req["id"], req["verb"]
            if verb == "configure":
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": {"applied": True}}))
            elif verb == "observe":
                v = st["v"]
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": _screen(st["state"], v)}))
                if req["args"].get("includeScreenshot"):
                    color = "white" if st["state"] == "home" else "#c8f7c5"
                    await ws.send(_frame({"type": "screenshot", "correlationId": rid,
                                          "snapshotVersion": v, "format": "png",
                                          "width": 540, "height": 1170, "reason": "bundled"}, _png(color)))
            elif verb == "act":
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": {
                    "resolved": True, "matchedRef": 1, "matchedBy": "ref", "settleAwaited": False}}))
                st["state"] = "success"  # the tap advanced the screen
                st["v"] += 1
                await asyncio.sleep(0.05)
                await ws.send(json.dumps({"kind": "event", "event": "settled", "data": {
                    "reason": "quiet", "snapshotVersion": st["v"], "pkg": "com.mock.app",
                    "hasBundledScreenshot": False}}))
            elif verb == "ping":
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": {"t": 1}}))
            else:
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": False,
                                          "error": {"code": "INTERNAL", "message": f"unhandled {verb}"}}))


async def main() -> int:
    port = _free_port()

    # Bind run()'s listener to the free loopback port without touching the
    # committed .jaato/a11y-bridge.yaml (which stays on the production 8765).
    # run() has no port parameter — it reads config.load() — so wrap that seam.
    _orig_load = run_controller.config.load

    def _load_on_free_port(*a, **kw):
        cfg = _orig_load(*a, **kw)
        cfg.host = HOST
        cfg.port = port  # listen_url is a derived property, recomputes from this
        return cfg

    run_controller.config.load = _load_on_free_port

    stop = asyncio.Event()
    dev = asyncio.create_task(mock_device(stop, port))
    task = ("Tap the Go button. When the screen shows 'Success!', call screen_done "
            "with a one-line summary.")
    try:
        code = await asyncio.wait_for(
            run_controller.run(task, socket="/tmp/jaato-a11y-liveloop.sock",
                               scope=["com.mock.app"], once=True),
            timeout=180.0)
        print(f"\n=== run() exit code: {code} ===")
        return code
    except asyncio.TimeoutError:
        print("\n!! FAIL: loop did not terminate within 180s")
        return 1
    finally:
        run_controller.config.load = _orig_load
        stop.set()
        dev.cancel()
        try:
            await dev
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
