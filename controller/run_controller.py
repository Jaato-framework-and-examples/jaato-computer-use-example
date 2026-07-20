#!/usr/bin/env python3
"""jaato-a11y-bridge controller — the daemon-side "mind".

Stands up the device-facing WS listener, connects to the jaato daemon with the
``a11y-controller`` profile (doubleword / Qwen3-VL vision), registers the
``screen.*`` host tools, and runs the computer-use loop: each turn it pushes a
set-of-marks screenshot + pruned tree to the agent, and the agent drives a full
multi-action sequence — every ``screen.*`` tool executes on the device (act ->
settle -> recover -> re-observe) and returns the fresh set-of-marks screenshot as
its own tool result, so the model sees each effect and acts again within the turn.

Interaction is **mid-run steering**: the agent drives autonomously toward the
standing task, but you can type a new instruction or correction at any time —
while a turn is running it is injected INTO that turn (USER priority; the runner
drains it at the next action boundary), so the agent adapts without waiting for
the turn to end; while idle, your line starts the next turn. When the agent
finishes (``screen_done``) or stalls (a turn with no action), control returns to
your prompt. Type ``/quit`` to exit.

Config:
- LLM provider/model/key: ``.jaato/profiles/a11y-controller.yaml`` (a jaato profile)
- device listener + scope + screenshot policy: ``.jaato/a11y-bridge.yaml``

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
from contextlib import ExitStack
from typing import List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from jaato_sdk import ClientType, EventType, IPCClient

from a11y import annotate, config
from a11y.audit import AuditLog
from a11y.controller import Controller
from a11y.device_session import BridgeServer
from a11y.host_tools import build_tools

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
PROFILE = "a11y-controller"
# How long to wait for the device to dial back in after a drop before giving up
# and surfacing "device unavailable" (a network flap reconnects in seconds).
RECONNECT_TIMEOUT_S = 90.0
# The operator persona (tools + when-to-act judgment) lives in
# .jaato/agents/a11y-operator.md and is loaded as the session's *system*
# instructions — not injected into the first user turn.
AGENT = "a11y-operator"


def _observation_message(controller: Controller, first: bool,
                         steer_lines: List[str]) -> tuple[str, list]:
    """Build the per-turn user message (text + marked-image attachment), folding
    in any operator steering typed since the last turn."""
    obs = controller.pending_observation
    assert obs is not None
    tree = annotate.tree_text(obs)
    marked_png = annotate.set_of_marks(obs)
    # Provide context only — the operator's message + the current screen. Whether
    # this warrants a screen.* tool call is the agent's judgment (see the persona),
    # so no "take an action" imperative and no "TASK:" framing that would coerce it.
    parts: List[str] = []
    if steer_lines:
        parts.append("USER: " + " ".join(steer_lines))
    parts.append(("Current screen:" if first else "Updated screen:") + "\n" + tree)
    attachment = {"mime_type": "image/png", "data": marked_png,
                  "display_name": f"screen_v{obs.version}.png"}
    return "\n\n".join(parts), [attachment]


# --- operator input (concurrent stdin) --------------------------------------

async def _operator_input_loop(session, client, steer_queue: "asyncio.Queue[str]",
                               turn_active: dict, quit_event: asyncio.Event) -> None:
    """Own stdin via a prompt_toolkit ``PromptSession`` so a single persistent
    ``you>`` line stays pinned at the bottom while agent output scrolls above it
    (the caller wraps the run in ``patch_stdout``). One always-waiting prompt is
    what makes steering visible: the operator can type at ANY time, mid-turn or
    idle — not only when the loop happens to hand back control.

    Each submitted line is routed by whether a turn is in flight
    (``turn_active["on"]``):

    - **turn in flight** -> ``client.inject_prompt(line, source_type="user")``:
      steered INTO the running turn (USER priority; the runner drains it at the
      next ``screen_*`` boundary), so the model reacts mid-run. This is the point
      of steering.
    - **idle** -> ``steer_queue``: an idle session does NOT run on an inject alone
      (the daemon queues it but nothing drives a turn), so an idle line instead
      unblocks the ``you>`` wait / folds into the next ``send_message``.

    EOF (Ctrl-D), interrupt, or ``/quit``/``/exit`` set ``quit_event`` and end.
    """
    while not quit_event.is_set():
        try:
            line = await session.prompt_async("you> ")
        except (EOFError, KeyboardInterrupt):
            quit_event.set()
            return
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit"):
            quit_event.set()
            return
        if turn_active["on"]:
            try:
                await client.inject_prompt(line, source_type="user")
                print(f"[steering → folded into the running turn: {line!r}]")
            except Exception as exc:
                print(f"[steering failed: {exc}]")
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

    bridge = BridgeServer(cfg.host, cfg.port, cfg.path, cfg.token, cfg.unsafe_no_auth)
    await bridge.start()
    print(f"[bridge] listening on {cfg.listen_url} — point the device app's Daemon URL here")
    if cfg.unsafe_no_auth:
        logging.getLogger("a11y").warning("running with unsafe_no_auth — dev/loopback only")

    client = IPCClient(
        socket,
        client_type=ClientType.API,      # keeps signal_completion
        auto_start=True,
        env_file=os.path.join(WORKSPACE, ".env"),  # absent -> daemon uses profile only
        workspace_path=WORKSPACE,
    )
    if not await client.connect(timeout=120.0):
        print("could not connect/autostart the daemon — run jaato-doctor")
        return 1

    print("[bridge] waiting for the device to dial in…")
    session = await bridge.wait_for_device(timeout=None)
    print(f"[bridge] device connected: {session.device_id} (sdk {session.hello.get('androidSdk')})")

    audit = AuditLog(os.path.join(WORKSPACE, ".jaato", "logs", "a11y-audit.jsonl"),
                     device_id=session.device_id)
    # A pinned scope (non-empty package_scope) restricts authority to those
    # packages; an empty scope follows the foreground app (auto re-scope).
    follow_foreground = not cfg.package_scope
    print(f"[bridge] scope: {'follow-foreground (auto re-scope)' if follow_foreground else 'pinned ' + str(cfg.package_scope)}")

    async def reacquire(reason) -> Optional["object"]:
        # The held session dropped — wait for the device to dial back in and hand
        # the controller the newest bridge session (first-wins freed the slot).
        # An operator DISCONNECT is announced distinctly from a bare network flap,
        # but both resume by adopting the reconnected session. Bounded so a truly-
        # gone device surfaces instead of blocking forever.
        if reason == "user_disconnect":
            print("\n[bridge] operator disconnected the device — reconnect it to resume…",
                  flush=True)
        else:
            print("\n[bridge] device dropped — waiting for reconnect…", flush=True)
        s = await bridge.wait_for_device(timeout=RECONNECT_TIMEOUT_S)
        print(f"[bridge] device reconnected: {s.device_id}", flush=True)
        return s

    controller = Controller(session, audit, cfg.package_scope,
                            cfg.screenshot_defaults, cfg.redaction, cfg.settle_ceiling_s,
                            follow_foreground=follow_foreground, reacquire=reacquire)
    await controller.configure()
    await controller.first_observation()

    await client.register_client_tools(build_tools(controller))

    sid = await client.create_session(profile=PROFILE, agent=AGENT, timeout=60.0)
    if not sid:
        print("session.new failed — check provider auth (jaato-doctor) / the daemon log")
        await client.disconnect()
        return 1

    turn_done = asyncio.Event()
    client.subscribe(EventType.TURN_COMPLETED, lambda ev: turn_done.set())

    # Agent text streams in chunks; prefix the first chunk of each turn with
    # "agent> " (symmetric to the "you> " operator prompt) so its replies are
    # visually distinct from the prompt and status lines.
    turn_output = {"started": False}

    def on_output(ev):
        # Print only the agent's own voice. The daemon streams the prompt back
        # as source="user" (the observation tree the model consumes) and emits
        # tool/system chatter too; those are telemetry, not conversation, so the
        # pane would otherwise interleave "Current screen: … nodes=N" dumps with
        # the model's words. Keep model text + thinking; drop the rest.
        if getattr(ev, "source", "") not in ("model", "thinking"):
            return
        text = getattr(ev, "text", "") or getattr(ev, "content", "")
        if not text:
            return
        if not turn_output["started"]:
            print("agent> ", end="", flush=True)
            turn_output["started"] = True
        print(text, end="", flush=True)
    client.subscribe(EventType.AGENT_OUTPUT, on_output)

    # Terminal detection, per the scaffold client template (_client_templates.py):
    # this profile doesn't signal_completion, so a normal turn emits only
    # TURN_COMPLETED and the session stays alive (IDLE). A terminal error — a
    # provider 402/auth failure, a rate cap — arrives as SESSION_TERMINATED
    # (reason="error", with error_type/error_summary) and KILLS the session.
    # Subscribing to it too means the error unblocks the wait and is surfaced,
    # instead of hanging on a TURN_COMPLETED that will never come.
    terminated: dict = {}

    def on_terminated(ev):
        terminated["reason"] = getattr(ev, "reason", None) or "natural"
        terminated["error_type"] = getattr(ev, "error_type", None)
        terminated["error_summary"] = getattr(ev, "error_summary", None)
        turn_done.set()
    client.subscribe(EventType.SESSION_TERMINATED, on_terminated)

    steer_queue: "asyncio.Queue[str]" = asyncio.Queue()
    quit_event = asyncio.Event()
    # Flipped True only while a turn is in flight; the input loop consults it to
    # route a typed line to live steering (inject) vs the idle you> path (send).
    turn_active = {"on": False}
    input_task: Optional["asyncio.Future"] = None

    pending_steer: List[str] = [initial_task] if initial_task else []
    first = True
    idle = False
    exit_code = 0

    # patch_stdout keeps the persistent you> prompt pinned while agent output and
    # status lines scroll above it — so the operator can type/steer at any time
    # without their keystrokes tangling into the streamed model text. Interactive
    # only: --once has no prompt and no patched stdout.
    stack = ExitStack()
    if not once:
        stack.enter_context(patch_stdout())
        session = PromptSession()
        input_task = asyncio.ensure_future(
            _operator_input_loop(session, client, steer_queue, turn_active, quit_event))

    try:
        for step in range(cfg.max_steps):
            # Return control to the operator when there's nothing in flight:
            # before the first turn with no task, after completion, or after a stall.
            need_user = (first and not pending_steer) or controller.done or idle
            if need_user:
                if once:
                    if controller.done:
                        exit_code = 0
                    break
                # No prompt print here: the PromptSession keeps a persistent you>
                # line pinned at the bottom. An idle line arrives via steer_queue.
                line = await _next_line(steer_queue, quit_event)
                if line is None:
                    break
                pending_steer.append(line)
                controller.done = False
                idle = False
                # The operator is a co-actor: they may have changed the device
                # screen (opened an app, the app drawer) since the agent last
                # looked. Re-observe so this turn acts on the CURRENT screen, not
                # a frozen snapshot. (Autonomous turns already re-observe after
                # each action; this covers operator-driven screen changes.)
                try:
                    await controller.first_observation()
                except Exception as exc:
                    print(f"[bridge] couldn't refresh the screen: {exc}")

            pending_steer.extend(_drain(steer_queue))  # fold any typed-ahead lines
            text, attachments = _observation_message(controller, first, pending_steer)
            pending_steer = []

            turn_done.clear()
            turn_output["started"] = False
            controller.begin_turn()
            await client.send_message(text, attachments=attachments, parallel_tools=False)

            turn_active["on"] = True   # operator lines now steer INTO this turn
            try:
                done, _ = await asyncio.wait(
                    {asyncio.ensure_future(turn_done.wait()),
                     asyncio.ensure_future(quit_event.wait())},
                    return_when=asyncio.FIRST_COMPLETED)
            finally:
                turn_active["on"] = False  # back to the idle you> path
            print()
            if quit_event.is_set() and not turn_done.is_set():
                break

            if terminated:
                # The session ended — a terminal error (e.g. insufficient credits)
                # or a natural completion. It can't take another message, so
                # surface it and exit rather than hang or loop on a dead session.
                if terminated.get("reason") == "error":
                    print(f"[error] {terminated.get('error_type')}: "
                          f"{terminated.get('error_summary')}")
                    exit_code = 1
                else:
                    print(f"[session ended: {terminated.get('reason')}]")
                break

            first = False
            if controller.done:
                print(f"[done] {controller.done_summary}")
                if once:
                    break
                continue
            # Computer-use: the model drove a full multi-action loop this turn
            # (each action fed back the fresh screenshot as a tool result), so a
            # completed turn hands back to the operator for the next instruction.
            if not controller.acted_this_turn:
                print("[turn done — your turn (type an instruction or /quit)]")
            else:
                print("[turn done — your turn, or say 'continue']")
            idle = True
        else:
            print(f"[loop] reached max_steps={cfg.max_steps}")
    finally:
        quit_event.set()
        if input_task is not None:
            input_task.cancel()
        stack.close()  # exits patch_stdout, restoring the plain terminal
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
