#!/usr/bin/env python3
"""
Minimal e2e daemon for jaato-a11y-bridge — NOT the real daemon (03), just enough of the
"mind" to exercise the wire end-to-end against a live device.

- Accepts the device's outbound WebSocket (plaintext ws:// for local testing over adb reverse).
- Logs `hello`, auto-sends a default `configure`, then gives you an interactive command line to
  drive the canonical loop (observe → act → settled → observe) by hand.
- Parses binary frames (PROTOCOL §4) and saves screenshots to ./captures/.

Requires: websockets (pip install "websockets>=10,<13").

Run:
    python3 tools/e2e_daemon.py --port 8765
    # then on the host:  adb reverse tcp:8765 tcp:8765
    # in the app:        Daemon URL = ws://localhost:8765/a11y , token = anything
    # enable the accessibility service

Commands (type `help`):
    scope <pkg> [pkg...]        set package allowlist (also settle scope) via configure
    observe | obs              observe with bundled screenshot
    shot                       standalone screenshot
    click <viewId>             act CLICK by viewId
    clicktext <text>           act CLICK by visible text
    settext <viewId> <value>   act SET_TEXT
    tap <x> <y>                act GESTURE tap
    swipe <x1> <y1> <x2> <y2>  act GESTURE swipe
    back | home | recents      act GLOBAL
    ping                       app-layer ping
    raw <json>                 send a raw req object (id auto-filled)
    quit
"""
import argparse
import asyncio
import json
import os
import struct
import sys
import time

import websockets

CAPTURES = os.path.join(os.getcwd(), "captures")
os.makedirs(CAPTURES, exist_ok=True)

state = {
    "ws": None,          # current device socket (single device assumed)
    "id": 0,
    "pending": {},       # req id -> verb (for nicer response logging)
    "scope": [],         # current package allowlist
    "screen": None,
}


def next_id():
    state["id"] += 1
    return f"r-{state['id']}"


async def send_req(verb, args=None):
    ws = state["ws"]
    if ws is None:
        print("!! no device connected")
        return None
    rid = next_id()
    frame = {"kind": "req", "id": rid, "verb": verb, "args": args or {}}
    state["pending"][rid] = verb
    await ws.send(json.dumps(frame))
    print(f">> {verb} {json.dumps(args or {})}  (id={rid})")
    return rid


def configure_args(scope):
    return {
        "settle": {
            "quietWindowMs": 500,
            "hardTimeoutMs": 5000,
            "eventMask": ["WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED"],
            "packageScope": scope,
            "mode": "quiet",
            "minEventCount": 1,
            "bundleScreenshotOnSettle": False,
        },
        "screenshotDefaults": {"format": "webp", "quality": 80, "maxDimension": 1280, "crop": None},
        "redaction": {"maskPasswordNodes": True, "extraMaskSelectors": []},
        "packageScope": scope,
    }


# ---------------------------------------------------------------------------
# Inbound frame handling
# ---------------------------------------------------------------------------

def handle_binary(data: bytes):
    if len(data) < 4:
        print("!! short binary frame")
        return
    (hlen,) = struct.unpack(">I", data[:4])
    header = json.loads(data[4:4 + hlen].decode("utf-8"))
    payload = data[4 + hlen:]
    ext = header.get("format", "bin")
    corr = header.get("correlationId", "unknown")
    ver = header.get("snapshotVersion", "na")
    fname = os.path.join(CAPTURES, f"{corr}-v{ver}.{ext}")
    with open(fname, "wb") as f:
        f.write(payload)
    print(f"<< [binary] {header.get('reason')} {header.get('width')}x{header.get('height')} "
          f"{len(payload)}B -> {fname}")


def print_snapshot(data):
    nodes = data.get("nodes", [])
    print(f"<< observe v{data.get('snapshotVersion')} pkg={data.get('pkg')} "
          f"activity={data.get('activity')} nodes={len(nodes)}")
    for n in nodes:
        flags = ",".join(n.get("flags", []))
        label = n.get("text") or n.get("desc") or ""
        print(f"   ref={n['ref']:>3} {n.get('viewId') or n['cls']}  "
              f"'{label}'  {n.get('bounds')}  [{flags}]")


def handle_text(msg: str):
    obj = json.loads(msg)
    kind = obj.get("kind")
    if kind == "res":
        verb = state["pending"].pop(obj.get("id"), "?")
        if obj.get("ok"):
            data = obj.get("data")
            if verb == "observe" and isinstance(data, dict):
                print_snapshot(data)
            else:
                print(f"<< res ok {verb} {json.dumps(data)}")
        else:
            print(f"<< res ERR {verb} {json.dumps(obj.get('error'))}")
    elif kind == "event":
        ev = obj.get("event")
        data = obj.get("data", {})
        if ev == "hello":
            state["screen"] = data.get("screen")
            print(f"<< HELLO device={data.get('deviceId')} sdk={data.get('androidSdk')} "
                  f"caps={data.get('capabilities')} screen={data.get('screen')}")
        else:
            print(f"<< event {ev} {json.dumps(data)}")
    else:
        print(f"<< ? {msg[:200]}")


# ---------------------------------------------------------------------------
# Connection + command loop
# ---------------------------------------------------------------------------

async def handler(ws, path):
    auth = ws.request_headers.get("Authorization", "<none>")
    print(f"\n== device connected path={path} auth={auth}")
    state["ws"] = ws
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                handle_binary(msg)
            else:
                handle_text(msg)
                # Auto-configure right after hello so the operator can act immediately.
                if '"event":"hello"' in msg or '"event": "hello"' in msg:
                    await send_req("configure", configure_args(state["scope"]))
    except websockets.ConnectionClosed:
        pass
    finally:
        print("== device disconnected")
        if state["ws"] is ws:
            state["ws"] = None


async def handle_command(line: str):
    if not line:
        return
    parts = line.split()
    cmd, rest = parts[0], parts[1:]

    if cmd in ("quit", "exit"):
        os._exit(0)
    elif cmd == "help":
        print(__doc__)
    elif cmd == "scope":
        state["scope"] = rest
        await send_req("configure", configure_args(rest))
    elif cmd in ("observe", "obs"):
        await send_req("observe", {"includeScreenshot": True})
    elif cmd == "shot":
        await send_req("screenshot", {"format": "webp", "quality": 80, "maxDimension": 1280})
    elif cmd == "click" and rest:
        await send_req("act", {"target": {"viewId": rest[0]}, "action": "CLICK"})
    elif cmd == "clicktext" and rest:
        await send_req("act", {"target": {"text": " ".join(rest)}, "action": "CLICK"})
    elif cmd == "settext" and len(rest) >= 2:
        await send_req("act", {"target": {"viewId": rest[0]}, "action": "SET_TEXT",
                               "text": " ".join(rest[1:])})
    elif cmd == "tap" and len(rest) == 2:
        x, y = int(rest[0]), int(rest[1])
        await send_req("act", {"target": {"bounds": [x, y, x, y]}, "action": "GESTURE",
                               "gesture": {"type": "tap", "path": [[x, y]], "durationMs": 60}})
    elif cmd == "swipe" and len(rest) == 4:
        x1, y1, x2, y2 = map(int, rest)
        await send_req("act", {"target": {"bounds": [x1, y1, x2, y2]}, "action": "GESTURE",
                               "gesture": {"type": "swipe", "path": [[x1, y1], [x2, y2]],
                                           "durationMs": 300}})
    elif cmd in ("back", "home", "recents"):
        await send_req("act", {"target": {}, "action": "GLOBAL", "global": cmd.upper()})
    elif cmd == "ping":
        await send_req("ping")
    elif cmd == "raw" and rest:
        obj = json.loads(" ".join(rest))
        await send_req(obj["verb"], obj.get("args", {}))
    else:
        print(f"?? unknown command: {line}  (type 'help')")


async def stdin_loop():
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        try:
            await handle_command(line.strip())
        except Exception as e:  # keep the REPL alive on bad input
            print(f"!! {e}")


async def cmdfile_loop(path):
    """Tail a command file and run each appended line — a control channel for headless runs."""
    open(path, "a").close()
    offset = os.path.getsize(path)  # start at EOF; only act on new lines
    print(f"(watching command file: {path})")
    while True:
        await asyncio.sleep(0.3)
        size = os.path.getsize(path)
        if size < offset:  # truncated/rotated
            offset = 0
        if size > offset:
            with open(path) as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                print(f"[cmd] {line}")
                try:
                    await handle_command(line)
                except Exception as e:
                    print(f"!! {e}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cmd-file", default=None,
                    help="tail this file for commands (headless control channel)")
    args = ap.parse_args()

    print(f"e2e daemon listening on ws://{args.host}:{args.port}/a11y")
    print("captures -> ", CAPTURES)
    print("waiting for device... (type 'help' for commands)\n")

    aux = []
    if args.cmd_file:
        aux.append(asyncio.create_task(cmdfile_loop(args.cmd_file)))

    async with websockets.serve(handler, args.host, args.port, max_size=16 * 1024 * 1024):
        if sys.stdin.isatty():
            await stdin_loop()
        elif aux:
            await asyncio.gather(*aux)
        else:
            print("(non-interactive stdin: REPL disabled; server stays up.)")
            await asyncio.Future()  # keep serving forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
