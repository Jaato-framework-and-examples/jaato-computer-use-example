#!/usr/bin/env python3
"""jaato-a11y-bridge controller — the daemon-side "mind".

Stands up the device-facing WS listener (a *registry* of advertised devices) and
connects to the jaato daemon with the ``a11y-controller`` profile (doubleword /
Qwen3-VL vision). ONE device-agnostic operator agent runs the whole session:

  - Devices advertise by dialing in and stay idle until chosen — several can be
    connected at once. The agent lists them (``list_devices``), the operator picks
    one, and the agent connects it (``connect_device``).
  - ``connect_device`` binds the chosen device, registers ITS platform-gated
    ``screen.*`` host tools, and returns — as the tool result — that platform's
    operating guide (a bundled asset of the selection tool) plus the first set-of-
    marks screen. So the agent learns how the device works and sees it, then drives.
  - Driving: every ``screen.*`` tool executes on the device (act -> settle ->
    recover -> re-observe) and returns the fresh set-of-marks screenshot as its own
    tool result, so the agent sees each effect and acts again within the turn. It
    can ``screen_observe`` to (re)look, and re-``connect_device`` to switch devices.

The observation rides in tool RESULTS (connect_device / screen_*), not an injected
per-turn user message — so the loop is plain conversation.

Interaction is **mid-run steering**: while a turn is running you can type a new
instruction or correction; it is injected INTO that turn (USER priority; the runner
drains it at the next action boundary), so the agent adapts without waiting for the
turn to end. While idle, your line starts the next turn. Type ``/quit`` to exit.

Config:
- LLM provider/model/key: ``.jaato/profiles/a11y-controller.yaml`` (a jaato profile)
- device listener + scope + screenshot policy: ``a11y-bridge.yaml`` (workspace root)

Usage:
    python run_controller.py ["initial task"] \
        [--scope com.android.settings] [--socket /tmp/jaato-a11y.sock] [--once]

The device app's Daemon URL must point at this listener (ws://<host>:8765/a11y).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
from typing import List, Optional

from prompt_toolkit import HTML

from jaato_sdk import ClientType, EventType, IPCClient

from a11y import config
from a11y.console import SteerConsole
from a11y.audit import AuditLog
from a11y.controller import Controller
from a11y.device_session import BridgeServer
from a11y.host_tools import build_selection_tools, build_tools

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
PROFILE = "a11y-controller"
# How long to wait for the device to dial back in after a drop before giving up
# and surfacing "device unavailable" (a network flap reconnects in seconds).
RECONNECT_TIMEOUT_S = 90.0
# One device-agnostic operator persona (system instructions): the platform-neutral
# operating discipline + the list_devices/connect_device selection flow. The
# platform-specific half of the grounding is delivered as DATA — connect_device
# returns the matching per-platform guide (a bundled asset of the selection tool,
# a11y/guides/) as its tool result — so the SAME agent adapts to any device.
PERSONA = "a11y-operator"


# --- operator input ---------------------------------------------------------

def _toolbar(turn_active: dict):
    """State-reflecting bottom bar text (re-evaluated on every render)."""
    if turn_active["on"]:
        return HTML(" <b>⏳ agent working</b> — type to steer → "
                    "<i>Enter injects into the running turn</i> ")
    return HTML(" <b>▶ idle</b> — type a goal (or <i>/quit</i>) ")


async def _route_lines(console: SteerConsole, client, steer_queue: "asyncio.Queue[str]",
                       turn_active: dict, quit_event: asyncio.Event) -> None:
    """Drain each line the console submits and route it by whether a turn is in
    flight (``turn_active["on"]``):

    - **turn in flight** -> ``client.inject_prompt(line, source_type="user")``:
      steered INTO the running turn (USER priority; the runner drains it at the
      next ``screen_*`` boundary), so the model reacts mid-run — the point of
      steering.
    - **idle** -> ``steer_queue``: an idle session does NOT run on an inject alone
      (the daemon queues it but nothing drives a turn), so an idle line instead
      unblocks the ``you>`` wait / folds into the next ``send_message``.
    """
    while not quit_event.is_set():
        line = await _next_line(console.line_queue, quit_event)
        if line is None:  # quit
            return
        if turn_active["on"]:
            try:
                await client.inject_prompt(line, source_type="user")
                console.write(f"[steering → folded into the running turn: {line!r}]")
            except Exception as exc:
                console.write(f"[steering failed: {exc}]")
        else:
            steer_queue.put_nowait(line)


async def _next_line(steer_queue: "asyncio.Queue[str]",
                     quit_event: asyncio.Event) -> Optional[str]:
    """Block for the next operator line, or return ``None`` if they quit."""
    getter = asyncio.ensure_future(steer_queue.get())
    quitter = asyncio.ensure_future(quit_event.wait())
    done, pending = await asyncio.wait({getter, quitter}, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
    return getter.result() if getter in done else None


def _drain(q: "asyncio.Queue[str]") -> List[str]:
    out: List[str] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# --- main loop --------------------------------------------------------------

async def run(initial_task: Optional[str], socket: str,
              scope: Optional[list], once: bool) -> int:
    cfg = config.load(WORKSPACE, scope_override=scope)

    steer_queue: "asyncio.Queue[str]" = asyncio.Queue()
    quit_event = asyncio.Event()
    # Flipped True only while a turn is in flight; the line router consults it to
    # route a typed line to live steering (inject) vs the idle you> path (send).
    turn_active = {"on": False}
    # Full-screen console (interactive only): output scrolls above a pinned you>
    # input + state toolbar. Started now so setup/"waiting for device" render live.
    # --once is non-interactive: no console, output falls back to plain print.
    console = SteerConsole(lambda: _toolbar(turn_active), quit_event) if not once else None
    app_task = asyncio.ensure_future(console.run()) if console is not None else None
    consumer_task: Optional["asyncio.Future"] = None

    def emit(text: str = "", end: str = "\n") -> None:
        """Route one line of UI output: into the console (interactive) or stdout
        (--once). Every user-facing message goes through here so there is a single
        output path regardless of mode."""
        if console is not None:
            console.write(text, end=end)
        else:
            print(text, end=end, flush=True)

    def on_registry_change(session, present: bool) -> None:
        # Devices advertise in the BACKGROUND (the loop no longer blocks on one), so
        # announce them LIVE as they connect/leave — otherwise a device that dials
        # in after startup is invisible to the operator until they happen to ask.
        if present:
            emit(f"[bridge] device available: {session.device_id} "
                 f"({session.platform or '<undeclared>'}) — say what to do and I'll connect it")
        else:
            emit(f"[bridge] device left: {session.device_id}")

    bridge = BridgeServer(cfg.host, cfg.port, cfg.path, cfg.token, cfg.unsafe_no_auth,
                          on_registry_change=on_registry_change)
    await bridge.start()

    if cfg.unsafe_no_auth:
        logging.getLogger("a11y").warning("running with unsafe_no_auth — dev/loopback only")
    emit(f"[bridge] listening on {cfg.listen_url} — point the device app's Daemon URL here")

    client = IPCClient(
        socket,
        client_type=ClientType.API,      # keeps signal_completion
        auto_start=True,
        env_file=os.path.join(WORKSPACE, ".env"),  # absent -> daemon uses profile only
        workspace_path=WORKSPACE,
    )

    exit_code = 0
    try:
        if not await client.connect(timeout=120.0):
            emit("could not connect/autostart the daemon — run jaato-doctor")
            return 1

        if console is not None:
            consumer_task = asyncio.ensure_future(
                _route_lines(console, client, steer_queue, turn_active, quit_event))

        # --- per-turn event state -------------------------------------------
        # ONE model session for the whole run. The observation now rides in TOOL
        # RESULTS (connect_device + screen_*), not an injected per-turn user
        # message, so this is a plain conversational loop.
        turn_done = asyncio.Event()
        # TURN_COMPLETED carries the model's finish_reason (01-PROTOCOL contract,
        # framework PR #544): "stop"/"tool_use" are clean; "max_tokens"/"safety"/
        # "error" mean the turn was cut short. The daemon also emits a source=
        # "system" banner for the abnormal ones (rendered by on_output), so we only
        # read it to keep the turn-boundary line honest (an "ended early" turn must
        # not report a cheerful "done").
        turn_finish = {"reason": "stop"}
        # Agent text streams in chunks; the first chunk of each turn is prefixed
        # "agent> " so replies are visually distinct from the prompt/status lines.
        turn_output = {"started": False}
        terminated: dict = {}
        # Surface a session-creation failure's REASON (the daemon emits an
        # ErrorEvent — e.g. a SecretResolutionError when a pass:// gpg key relocked
        # — but create_session swallows it and returns None), captured here.
        last_error: dict = {}
        # Set by connect_device (via connect_cb) when a device is freshly bound
        # THIS turn. Its platform-gated screen_* tools register mid-session, so the
        # agent can't call them until the NEXT turn — the loop reads this flag and
        # auto-continues so the agent drives straight away instead of waiting for
        # another operator line.
        just_connected = {"on": False}

        def on_turn_completed(ev):
            turn_finish["reason"] = getattr(ev, "finish_reason", "stop") or "stop"
            turn_done.set()

        def on_output(ev):
            # Show the agent's own voice AND framework SYSTEM notices. The daemon
            # streams the prompt back as source="user" (the observation tree the
            # model consumes) and tool output as source="tool" — telemetry, not
            # conversation — so the pane would otherwise interleave "Current screen:
            # … nodes=N" dumps with the model's words. Keep model text + thinking;
            # keep source="system" — the WHY a turn ended: abnormal-finish banners
            # (max_tokens/safety/error) and [Generation cancelled]. These are the
            # severe outcomes that must NOT be silently swallowed as a bare
            # "[turn done]" (the whole point of the surfacing fix). System flush
            # signals carry empty text and fall out at the `if not text` guard.
            src = getattr(ev, "source", "")
            text = getattr(ev, "text", "") or getattr(ev, "content", "")
            if not text:
                return
            if src == "system":
                if turn_output["started"]:
                    emit("")  # close the open agent> line before the notice
                    turn_output["started"] = False
                emit(f"[!] {text}")
                return
            if src not in ("model", "thinking"):
                return
            if not turn_output["started"]:
                emit("agent> ", end="")
                turn_output["started"] = True
            emit(text, end="")

        def on_terminated(ev):
            # A terminal error (provider 402/auth, rate cap) arrives as
            # SESSION_TERMINATED (reason="error") and KILLS the session; catching it
            # unblocks the turn wait instead of hanging on a TURN_COMPLETED that
            # never comes.
            terminated["reason"] = getattr(ev, "reason", None) or "natural"
            terminated["error_type"] = getattr(ev, "error_type", None)
            terminated["error_summary"] = getattr(ev, "error_summary", None)
            turn_done.set()

        client.subscribe(EventType.ERROR, lambda ev: last_error.update(
            error=getattr(ev, "error", "") or "", error_type=getattr(ev, "error_type", "") or ""))
        client.subscribe(EventType.TURN_COMPLETED, on_turn_completed)
        client.subscribe(EventType.AGENT_OUTPUT, on_output)
        client.subscribe(EventType.SESSION_TERMINATED, on_terminated)

        def report_terminated() -> int:
            """Print why a session ended; return the process exit code for it."""
            if terminated.get("reason") == "error":
                emit(f"[error] {terminated.get('error_type')}: "
                     f"{terminated.get('error_summary')}")
                return 1
            emit(f"[session ended: {terminated.get('reason')}]")
            return 0

        def prep_turn() -> None:
            turn_done.clear()
            turn_finish["reason"] = "stop"
            turn_output["started"] = False

        async def await_turn() -> str:
            """Wait out the in-flight turn. Returns 'quit'|'terminated'|'completed'.
            Operator lines steer INTO the turn while it runs (turn_active)."""
            turn_active["on"] = True
            try:
                await asyncio.wait(
                    {asyncio.ensure_future(turn_done.wait()),
                     asyncio.ensure_future(quit_event.wait())},
                    return_when=asyncio.FIRST_COMPLETED)
            finally:
                turn_active["on"] = False
            if turn_output["started"]:
                emit("")  # close the streamed agent> line
                turn_output["started"] = False
            if quit_event.is_set() and not turn_done.is_set():
                return "quit"
            if terminated:
                return "terminated"
            return "completed"

        # connect_device delegates the BINDING here (device/client wiring is a host
        # concern): bind the chosen device to a Controller, register ITS platform-
        # gated screen_* tools on this client (mid-session, so visible next turn),
        # and return the live controller. build_selection_tools then loads that
        # platform's guide (the tool's own asset) and returns guide + first screen.
        # Re-connecting to another device just rebinds: the new build_tools handlers
        # close over the new controller and register_client_tools replaces screen_*.
        async def connect_cb(device_id: str) -> Controller:
            session = bridge.get_device(device_id)
            audit = AuditLog(os.path.join(WORKSPACE, ".jaato", "logs", "a11y-audit.jsonl"),
                             device_id=device_id)
            # A pinned scope (non-empty package_scope) restricts authority to those
            # packages; an empty scope follows the foreground app (auto re-scope).
            follow_foreground = not cfg.package_scope

            async def reacquire(reason):
                # The active device dropped — wait for THIS id to dial back in and
                # adopt it. A DISCONNECT is announced distinctly from a bare flap;
                # both resume on reconnect.
                if reason == "user_disconnect":
                    emit("[bridge] operator disconnected the device — reconnect it to resume…")
                else:
                    emit("[bridge] device dropped — waiting for reconnect…")
                s = await bridge.wait_for_device(device_id=device_id, timeout=RECONNECT_TIMEOUT_S)
                emit(f"[bridge] device reconnected: {s.device_id}")
                return s

            controller = Controller(session, audit, cfg.package_scope,
                                    cfg.screenshot_defaults, cfg.redaction, cfg.settle_ceiling_s,
                                    follow_foreground=follow_foreground, reacquire=reacquire)
            await controller.configure()
            await controller.first_observation()
            await client.register_client_tools(build_tools(controller))
            just_connected["on"] = True
            emit(f"[bridge] connected: {device_id} (platform "
                 f"{controller.platform or '<undeclared>'}; scope "
                 f"{'follow-foreground' if follow_foreground else cfg.package_scope})")
            return controller

        # Selection tools first — registered BEFORE create_session so the buffer
        # seeds the first turn's schema. The screen_* tools register later, on the
        # first connect_device (mid-session → visible the turn after connecting).
        await client.register_client_tools(build_selection_tools(bridge, connect_cb))
        last_error.clear()
        try:
            sid = await client.create_session(profile=PROFILE, agent=PERSONA, timeout=60.0)
        except Exception as exc:  # a raised bootstrap/tool error must not crash the CLI
            sid = None
            last_error.setdefault("error", str(exc))
        if not sid:
            detail = last_error.get("error") or ""
            etype = last_error.get("error_type") or ""
            emit("[error] the model session failed to start"
                 + (f": {detail}" if detail else "")
                 + (f" [{etype}]" if etype else "") + ".")
            emit("  Most often the provider API key didn't resolve — if it's a pass://")
            emit("  secret, its gpg key is likely locked; unlock it once and relaunch:")
            emit("    pass show jaato/<provider>/api-key >/dev/null   (e.g. .../doubleword/api-key)")
            emit("  Otherwise check provider auth (jaato-doctor). Full detail in the daemon log.")
            return 1

        avail = bridge.list_devices()
        if avail:
            emit("[bridge] available now: "
                 + ", ".join(f"{d.device_id} ({d.platform or '?'})" for d in avail))
        emit("[bridge] say what you want to do and I'll connect a device "
             "(more may announce as they dial in; /quit to exit)")

        # --- conversational loop (one session; observation via tool results) ---
        pending: List[str] = [initial_task] if initial_task else []
        while not quit_event.is_set():
            if not pending:
                if once:
                    break  # --once: the single goal has run to a stop
                line = await _next_line(steer_queue, quit_event)
                if line is None:
                    break
                pending.append(line)
            pending.extend(_drain(steer_queue))  # fold any typed-ahead lines
            text = " ".join(pending)
            pending = []

            just_connected["on"] = False
            prep_turn()
            # No attachments: the agent pulls the screen through tool results
            # (connect_device / screen_*), so a turn is plain conversation. Steering
            # still injects mid-turn (await_turn keeps turn_active on).
            await client.send_message(text)
            status = await await_turn()
            if status == "quit":
                break
            if status == "terminated":
                exit_code = report_terminated()
                break

            if just_connected["on"]:
                # The device's screen_* tools just registered (visible next turn) —
                # auto-continue so the agent drives now, not after another operator
                # line. It already holds the task; this just unblocks it.
                pending = ["(connected — the device's tools are now available; go ahead)"]
                continue
            if once:
                break
            if turn_finish["reason"] not in ("stop", "tool_use"):
                emit("[turn ended early — your turn (type an instruction or /quit)]")
            else:
                emit("[turn done — your turn (type an instruction or /quit)]")
    finally:
        quit_event.set()
        if consumer_task is not None:
            consumer_task.cancel()
        if console is not None:
            console.stop()          # exit the full-screen app, restore the terminal
        if app_task is not None:
            app_task.cancel()
        await bridge.stop()
        await client.disconnect()
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser(description="jaato-a11y-bridge controller (mid-run steering)")
    ap.add_argument("goal", nargs="?", default=None,
                    help="optional initial task; you can also type it at the prompt after connect")
    ap.add_argument("--socket", default="/tmp/jaato-a11y.sock",
                    help="IPC socket for the client's own daemon (fresh path avoids other daemons)")
    ap.add_argument("--scope", nargs="*", default=None,
                    help="pin authority to these package(s). Omit to follow the "
                         "foreground app (the controller learns the on-screen "
                         "package after connect and auto-re-scopes).")
    ap.add_argument("--once", action="store_true",
                    help="run the single goal to completion and exit (no interactive steering)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.once and not args.goal:
        ap.error("--once requires a goal argument")
    # Engine telemetry (websockets / a11y.session / a11y.controller / jaato_sdk)
    # goes to a per-run file so the pane carries only the operator<->agent
    # conversation. `tail -f` the printed path to watch logs live.
    logdir = os.path.join(WORKSPACE, ".jaato", "logs")
    os.makedirs(logdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logpath = os.path.join(logdir, f"controller-{stamp}.log")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logpath)])
    print(f"logs → {logpath}")
    return asyncio.run(run(args.goal, args.socket, args.scope, args.once))


if __name__ == "__main__":
    raise SystemExit(main())
