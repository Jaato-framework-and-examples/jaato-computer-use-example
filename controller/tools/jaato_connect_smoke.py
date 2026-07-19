#!/usr/bin/env python3
"""Verify the jaato half of the controller: autostart a fresh daemon, resolve the
pass:// key daemon-side via the a11y-controller profile, register a host tool,
and confirm the model can call it. No Android device involved.

Run:  python tools/jaato_connect_smoke.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jaato_sdk import ClientType, EventType, IPCClient  # noqa: E402

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCKET = "/tmp/jaato-a11y-smoke.sock"


async def main() -> int:
    called = asyncio.Event()

    async def probe_ping(args):
        print(f"\n[host tool] probe_ping({args!r}) fired")
        called.set()
        return {"ok": True, "echo": args.get("msg", "")}

    client = IPCClient(SOCKET, client_type=ClientType.API, auto_start=True,
                       env_file=os.path.join(WORKSPACE, ".env"), workspace_path=WORKSPACE)
    if not await client.connect(timeout=120.0):
        print("FAIL: could not connect/autostart daemon")
        return 1
    print("✓ connected (fresh daemon, my HOME)")

    await client.register_client_tools([{
        "name": "probe_ping",
        "description": "Echo a short message back. Call this once to acknowledge.",
        "parameters": {"type": "object", "properties": {"msg": {"type": "string"}},
                       "required": ["msg"]},
        "handler": probe_ping,
    }])

    sid = await client.create_session(profile="a11y-controller", timeout=60.0)
    if not sid:
        print("FAIL: create_session — check provider auth / daemon log")
        await client.disconnect()
        return 1
    print(f"✓ session created with profile a11y-controller (pass:// key resolved daemon-side): {sid}")

    turn_done = asyncio.Event()
    client.subscribe(EventType.TURN_COMPLETED, lambda ev: turn_done.set())
    await client.send_message("Call the probe_ping tool with msg='hi'.", parallel_tools=False)
    try:
        await asyncio.wait_for(turn_done.wait(), timeout=90.0)
    except asyncio.TimeoutError:
        print("FAIL: turn did not complete in 90s")
        await client.disconnect()
        return 1

    ok = called.is_set()
    print(f"\n{'✓ host tool was invoked by the model' if ok else 'FAIL: model did not call the host tool'}")
    await client.disconnect()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
