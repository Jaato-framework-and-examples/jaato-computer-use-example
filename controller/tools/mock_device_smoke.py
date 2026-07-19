#!/usr/bin/env python3
"""End-to-end smoke test of the bridge core against a MOCK device.

Exercises the full daemon-side stack — WS listener, hello/pv handshake,
DeviceSession req/res mux, binary-frame reunion, Controller act->settle->reobserve,
grounding, set-of-marks, audit — over a real loopback WebSocket, with a scripted
fake device standing in for the Kotlin AccessibilityService. No jaato daemon and
no LLM are involved; this proves the plumbing before the real hardware loop.

Run:  python tools/mock_device_smoke.py
Exit: 0 on success, non-zero on any assertion failure.
"""
import asyncio
import io
import json
import os
import struct
import sys
import tempfile

import websockets
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a11y import annotate  # noqa: E402
from a11y.audit import AuditLog  # noqa: E402
from a11y.controller import Controller  # noqa: E402
from a11y.device_session import BridgeServer  # noqa: E402
from a11y.protocol import Action  # noqa: E402
from a11y.wire import PV  # noqa: E402

HOST, PORT, PATH = "127.0.0.1", 8799, "/a11y"


def _png(w=540, h=1170) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _binary_frame(header: dict, payload: bytes) -> bytes:
    hb = json.dumps(header).encode("utf-8")
    return struct.pack(">I", len(hb)) + hb + payload


def _snapshot(version: int) -> dict:
    return {
        "snapshotVersion": version, "pkg": "com.mock.app", "activity": ".Main",
        "screen": {"width": 1080, "height": 2340},
        "nodes": [
            {"ref": 1, "cls": "android.widget.Button", "viewId": "com.mock.app:id/go",
             "text": "Go", "bounds": [100, 200, 500, 320],
             "flags": ["clickable", "enabled", "visible"]},
            {"ref": 2, "cls": "android.widget.EditText", "viewId": "com.mock.app:id/field",
             "bounds": [100, 400, 980, 520], "flags": ["editable", "focusable", "visible"]},
        ],
    }


async def mock_device(stop: asyncio.Event) -> None:
    """Scripted fake device: dials in, says hello, answers verbs like the real one."""
    version = {"v": 1000}
    async with websockets.connect(f"ws://{HOST}:{PORT}{PATH}") as ws:
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
            req = json.loads(raw)
            rid, verb = req["id"], req["verb"]
            assert req.get("pv") == PV, f"controller must send pv; got {req.get('pv')}"
            if verb == "configure":
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": {"applied": True}}))
            elif verb == "observe":
                v = version["v"]
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": _snapshot(v)}))
                if req["args"].get("includeScreenshot"):
                    await ws.send(_binary_frame(
                        {"type": "screenshot", "correlationId": rid, "snapshotVersion": v,
                         "format": "png", "width": 540, "height": 1170, "reason": "bundled"}, _png()))
            elif verb == "act":
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": {
                    "resolved": True, "matchedRef": 1, "matchedBy": "ref", "settleAwaited": False}}))
                version["v"] += 1  # the action moved the world
                await asyncio.sleep(0.05)
                await ws.send(json.dumps({"kind": "event", "event": "settled", "data": {
                    "reason": "quiet", "snapshotVersion": version["v"], "pkg": "com.mock.app",
                    "hasBundledScreenshot": False}}))
            elif verb == "ping":
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": True, "data": {"t": 1}}))
            else:
                await ws.send(json.dumps({"kind": "res", "id": rid, "ok": False,
                                          "error": {"code": "INTERNAL", "message": f"unhandled {verb}"}}))


async def main() -> int:
    stop = asyncio.Event()
    bridge = BridgeServer(HOST, PORT, PATH, token=None, unsafe_no_auth=True)
    await bridge.start()
    dev_task = asyncio.create_task(mock_device(stop))

    try:
        session = await bridge.wait_for_device(timeout=5.0)
        assert session.device_id == "mock-tab-01", session.device_id
        assert session.capabilities.get("takeScreenshot") is True
        print(f"✓ handshake: {session.device_id} sdk={session.hello['androidSdk']}")

        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditLog(os.path.join(tmp, "audit.jsonl"), device_id=session.device_id)
            ctl = Controller(session, audit, ["com.mock.app"],
                             {"format": "png", "quality": 80, "maxDimension": 1280, "crop": None},
                             {"maskPasswordNodes": True, "extraMaskSelectors": []})
            await ctl.configure()
            print("✓ configure applied")

            obs = await ctl.first_observation()
            assert obs.image is not None and obs.image[:8] == b"\x89PNG\r\n\x1a\n", "no bundled PNG"
            assert len(obs.snapshot.nodes) == 2
            v0 = obs.version
            print(f"✓ observe v{v0}: {len(obs.snapshot.nodes)} nodes + bundled screenshot reunited")

            marked = annotate.set_of_marks(obs)
            assert marked[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"✓ set-of-marks rendered ({len(marked)} bytes PNG)")

            ack = await ctl.act_ref(1, Action.click())
            assert "settled(quiet)" in ack, ack
            assert ctl.pending_observation.version > v0, "world version did not advance"
            print(f"✓ act->settle->reobserve: {ack!r}")

            # one-action-per-turn guard
            guard = await ctl.act_ref(2, Action.click())
            assert "already acted" in guard, guard
            print("✓ one-action-per-turn guard fired")
            ctl.begin_turn()

            # SET_TEXT path
            ack2 = await ctl.act_ref(2, Action.set_text("hello"))
            assert "SET_TEXT" in ack2, ack2
            print(f"✓ set_text: {ack2!r}")

            with open(os.path.join(tmp, "audit.jsonl")) as f:
                lines = [json.loads(x) for x in f if x.strip()]
            assert len(lines) == 2 and lines[0]["action"] == "CLICK", lines
            print(f"✓ audit trail: {len(lines)} records ({[l['action'] for l in lines]})")

        print("\nALL SMOKE CHECKS PASSED")
        return 0
    finally:
        stop.set()
        await bridge.stop()
        dev_task.cancel()
        try:
            await dev_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
