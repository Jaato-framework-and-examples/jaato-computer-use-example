"""The ``screen.*`` host-tool surface (03-DAEMON_DESIGN.md §8).

These are the *only* tools the agent sees. Each is client-provided (registered
via ``IPCClient.register_client_tools``); its async handler runs in this process
and drives the device through the :class:`Controller`. The agent speaks only in
set-of-marks ``ref``s and intents — never selectors, snapshot versions, or
settle params (those are the daemon's job).

Handlers are ``async`` — the SDK awaits a coroutine returned by a host-tool
handler on the same loop that runs the device WS session, so they call the
controller directly with no cross-thread bridging.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List

from . import annotate
from .controller import Controller
from .device_session import BridgeServer, DeviceSession

log = logging.getLogger("a11y.tools")

#: Platform operating guides bundled with this package — the platform-specific
#: half of the operator's grounding, returned by ``connect_device`` as data when a
#: device connects (rather than baked into the agent persona). These are an asset
#: of the selection tool, so they live with it, not in the framework's ``.jaato/``.
_GUIDES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guides")


def load_guide(platform: str) -> str:
    """The operating guide for ``platform`` (``guides/<platform>.md`` in this
    package). A missing guide is a real error (a device declared a platform we can't
    operate) — surfaced loudly, no silent fallback."""
    path = os.path.join(_GUIDES_DIR, f"{platform}.md")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no operating guide for platform {platform!r}: expected {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _logged(name: str, handler):
    """Wrap a handler so every model tool-call and its result are logged — the
    ground truth for 'the agent said it would X but nothing happened'."""
    async def _w(args: dict) -> dict:
        log.info("tool-call %s %s", name, args)
        res = await handler(args)
        log.info("tool-result %s -> %r", name, res.get("result"))
        return res
    return _w


def devices_text(devices: List[DeviceSession]) -> str:
    """Render the advertised-device registry as a compact list the model shows the
    operator to choose from: one line per device — its id (what ``connect_device``
    takes) and declared platform. Backs ``list_devices`` in device selection."""
    if not devices:
        return ("No devices are advertised right now. A device advertises by "
                "dialing in to the bridge (its Daemon URL points here); ask the "
                "operator to open/connect their device, then list again.")
    lines = ["Advertised devices (pick one by its id):"]
    for d in devices:
        lines.append(f"  id={d.device_id!r}  platform={d.platform or '<undeclared>'}")
    return "\n".join(lines)


def build_selection_tools(bridge: BridgeServer, connect_cb) -> List[Dict[str, Any]]:
    """Device-selection tools (``list_devices`` / ``connect_device``) for the single
    operator agent (03 §2, device selection).

    ``connect_device`` validates the id, delegates the binding to
    ``connect_cb(device_id)`` — an async host callback that binds the device to a
    :class:`Controller` and registers its platform-gated ``screen.*`` tools,
    returning that live controller — then loads the platform operating guide (a
    bundled asset of THIS tool) and returns guide + first set-of-marks screen as the
    tool result. So the SAME agent reads how the chosen device works and sees its
    screen, then drives — no separate operator session, no task hand-off (the agent
    already holds the task). ``connect_cb`` is injected by the host (the CLI or the
    telegram bot) so this module stays free of Controller/client wiring; the guide,
    being the tool's own asset, is owned here."""

    async def list_devices(args: dict) -> dict:
        return {"result": devices_text(bridge.list_devices())}

    async def connect_device(args: dict) -> dict:
        device_id = str(args["device_id"])
        if bridge.get_device(device_id) is None:
            return {"result": f"No advertised device with id {device_id!r}. "
                              "Call list_devices to see what is currently connected."}
        controller = await connect_cb(device_id)
        try:
            guide = load_guide(controller.platform)
        except FileNotFoundError as exc:
            return {"result": f"Connected to {device_id}, but there is no operating "
                              f"guide for its platform ({controller.platform!r}): {exc}. "
                              "I can't drive this device — tell the operator."}
        # Guide + first screen as the tool result: the agent reads how this device
        # works and sees it, then drives.
        return screen_result(controller, guide)

    specs = [
        {
            "name": "list_devices",
            "description": "List the devices currently advertised to the bridge (each "
                           "line: its id and platform). Call this first when the operator "
                           "wants to act on a device, then show them the options so they "
                           "can choose which one to use.",
            "parameters": {"type": "object", "properties": {}},
            "handler": list_devices,
        },
        {
            "name": "connect_device",
            "description": "Connect to the device the operator chose, by its id (from "
                           "list_devices). Call ONLY once the operator has picked. The "
                           "result tells you how that device works and shows its current "
                           "screen — read and follow it, then carry out the task. Its "
                           "screen_* tools become available right after you connect.",
            "parameters": {"type": "object",
                           "properties": {
                               "device_id": {"type": "string",
                                             "description": "the chosen device's id"}},
                           "required": ["device_id"]},
            "handler": connect_device,
        },
    ]
    for spec in specs:
        spec["handler"] = _logged(spec["name"], spec["handler"])
    return specs


def screen_result(controller: Controller, ack: str) -> dict:
    """Bundle the action's ack with the FRESH set-of-marks screenshot + tree as a
    multimodal tool result. This lets a computer-use model see the effect of each
    action and act again within the same turn — instead of acting blind on a
    text-only ack. ``image_data`` is a base64 STRING, not raw bytes: a client-tool
    result crosses the IPC as JSON, so bytes aren't serializable; the OpenAI-family
    converter accepts an already-base64 string directly (03/SDK ``_multimodal``)."""
    obs = controller.pending_observation
    if obs is None:
        return {"result": ack}
    result: Dict[str, Any] = {"result": f"{ack}\n\n{annotate.tree_text(obs)}"}
    if obs.image is not None:
        result.update({
            "_multimodal": True,
            "_multimodal_type": "image",
            "image_data": base64.b64encode(annotate.set_of_marks(obs)).decode("ascii"),
            "mime_type": "image/png",
            "display_name": f"screen_v{obs.snapshot.version}.png",
        })
    return result


def windows_text(data: dict) -> str:
    """Render the ``windows`` verb result (Windows top-level windows) as a compact
    list the model reads: one line per window — hwnd id, title, program, and a
    FOREGROUND marker. Backs ``screen_windows`` and the first-turn desktop preamble
    (03/04 §9). The device owns the shape; a window's package identity is its AUMID
    (UWP) or executable path (Win32), shown here as the program's basename."""
    wins = data.get("windows") or []
    if not wins:
        return "no top-level windows reported by the device"
    lines = ["Top-level windows on the desktop (the FOREGROUND one is marked):"]
    for w in wins:
        ident = w.get("aumid") or w.get("exePath") or ""
        prog = ident.rsplit("\\", 1)[-1] if ident else "?"
        title = (w.get("title") or "").replace("\n", " ")
        fg = "  [FOREGROUND]" if w.get("foreground") else ""
        lines.append(f"  [{w.get('id')}] {title!r} — {prog}{fg}")
    return "\n".join(lines)


def build_tools(controller: Controller) -> List[Dict[str, Any]]:
    """Return the ``register_client_tools`` specs bound to ``controller``."""

    async def screen_tap(args: dict) -> dict:
        ack = await controller.act_ref(int(args["ref"]), _click())
        return screen_result(controller, ack)

    async def screen_type(args: dict) -> dict:
        from .protocol import Action
        ack = await controller.act_ref(int(args["ref"]), Action.set_text(str(args["text"])))
        return screen_result(controller, ack)

    async def screen_scroll(args: dict) -> dict:
        from .protocol import Action
        ack = await controller.act_ref(int(args["ref"]), Action.scroll_dir(str(args["direction"])))
        return screen_result(controller, ack)

    async def screen_submit(args: dict) -> dict:
        from .protocol import Action
        ack = await controller.act_ref(int(args["ref"]), Action.ime_enter())
        return screen_result(controller, ack)

    async def screen_back(args: dict) -> dict:
        return screen_result(controller, await controller.global_action("BACK"))

    async def screen_home(args: dict) -> dict:
        return screen_result(controller, await controller.global_action("HOME"))

    async def screen_recents(args: dict) -> dict:
        return screen_result(controller, await controller.global_action("RECENTS"))

    async def screen_gesture(args: dict) -> dict:
        path = [[int(p[0]), int(p[1])] for p in args["path"]]
        ack = await controller.gesture(path, int(args.get("duration_ms", 300)))
        return screen_result(controller, ack)

    async def screen_observe(args: dict) -> dict:
        # Pure look: re-observe (and follow the foreground) and return the fresh
        # set-of-marks screen WITHOUT acting. The agent's way to (re)ground when
        # resuming or unsure — the observation flows through tool results now, not
        # an injected per-turn user message.
        await controller.first_observation()
        return screen_result(controller, "current screen")

    async def screen_wait(args: dict) -> dict:
        return screen_result(controller, await controller.wait())

    async def screen_windows(args: dict) -> dict:
        # Metadata query (non-scope-gated); doesn't change the screen, so it
        # returns just the window list, not a fresh set-of-marks bundle.
        return {"result": windows_text(await controller.list_windows())}

    async def screen_start_menu(args: dict) -> dict:
        # GLOBAL START_MENU (Windows key) opens Start with search focused from any
        # window — the reliable "reach the shell" step. Returns the fresh screen
        # (Start is now foreground) so the model can type + pick a result.
        ack = await controller.global_action("START_MENU")
        return screen_result(controller, ack)

    async def screen_type_text(args: dict) -> dict:
        # Focus-directed: types into whatever holds keyboard focus, no ref needed
        # (e.g. the Start search box after screen_start_menu).
        ack = await controller.type_text(str(args["text"]))
        return screen_result(controller, ack)

    async def screen_enter(args: dict) -> dict:
        ack = await controller.press_key("ENTER")
        return screen_result(controller, ack)

    async def screen_close_window(args: dict) -> dict:
        # CLOSE_WINDOW (Alt+F4) closes the FOREGROUND window.
        ack = await controller.global_action("CLOSE_WINDOW")
        return screen_result(controller, ack)

    async def screen_switch_window(args: dict) -> dict:
        # SWITCH_WINDOW (Alt+Tab) switches to the previous window; call again to
        # keep cycling until the target is foreground.
        ack = await controller.global_action("SWITCH_WINDOW")
        return screen_result(controller, ack)

    async def screen_done(args: dict) -> dict:
        return {"result": controller.mark_done(str(args.get("summary", "")))}

    specs = [
        {
            "name": "screen_tap",
            "description": "Tap (click) the actionable element with the given set-of-marks ref. "
                           "Use the numbered mark on the screenshot.",
            "parameters": {"type": "object",
                           "properties": {"ref": {"type": "integer", "description": "the mark number"}},
                           "required": ["ref"]},
            "handler": screen_tap,
        },
        {
            "name": "screen_type",
            "description": "Type text into the editable field with the given ref (replaces its contents).",
            "parameters": {"type": "object",
                           "properties": {"ref": {"type": "integer"},
                                          "text": {"type": "string"}},
                           "required": ["ref", "text"]},
            "handler": screen_type,
        },
        {
            "name": "screen_scroll",
            "description": "Scroll the scrollable container at ref one screenful in a direction: "
                           "'down'/'up'/'left'/'right'. This targets the element (survives layout "
                           "shifts and picks the right axis), so prefer it over a raw swipe. If the "
                           "container can't scroll that way you get a NOT_ACTIONABLE telling you "
                           "whether to target a different ref or that you've reached the end.",
            "parameters": {"type": "object",
                           "properties": {"ref": {"type": "integer"},
                                          "direction": {"type": "string",
                                                        "enum": ["down", "up", "left", "right"]}},
                           "required": ["ref", "direction"]},
            "handler": screen_scroll,
        },
        {
            "name": "screen_submit",
            "description": "Submit an editable field by firing its keyboard action (Search/Go/Send/"
                           "Done) — use this to RUN a search or send after screen_type. Focus the "
                           "field first (tap it), then type, then submit the same ref. Fields that "
                           "support this show `editable:submit` in the tree; a NOT_ACTIONABLE means "
                           "the field doesn't advertise submit or isn't focused.",
            "parameters": {"type": "object",
                           "properties": {"ref": {"type": "integer"}},
                           "required": ["ref"]},
            "handler": screen_submit,
        },
        {
            "name": "screen_gesture",
            "description": "Escape hatch: dispatch a raw gesture by screen-pixel coordinates. "
                           "path is a list of [x,y] points ([[x,y]] = tap, [[x1,y1],[x2,y2]] = swipe).",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "array",
                                                   "items": {"type": "array", "items": {"type": "integer"}}},
                                          "duration_ms": {"type": "integer"}},
                           "required": ["path"]},
            "handler": screen_gesture,
        },
        {"name": "screen_observe",
         "description": "Look at the device's CURRENT screen without acting — returns the "
                        "fresh screenshot + tree. Use it to (re)ground when you resume or "
                        "are unsure what's on screen now, before choosing an action.",
         "parameters": {"type": "object", "properties": {}}, "handler": screen_observe},
        {"name": "screen_wait",
         "description": "Wait for the screen to stop changing (e.g. after a slow load), then refresh. "
                        "Does not act.",
         "parameters": {"type": "object", "properties": {}}, "handler": screen_wait},
        {"name": "screen_done",
         "description": "Signal that the goal is complete. Provide a short summary of what was accomplished.",
         "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                        "required": ["summary"]},
         "handler": screen_done},
    ]
    # Platform-gated navigation. The system-nav globals differ per platform, so a
    # tool that maps to a global one platform lacks would only ever return
    # NOT_ACTIONABLE (observed: screen_home -> "unknown global 'HOME'" on Windows).
    # Offer each device only the globals it actually implements.
    if controller.platform == "windows":
        # Windows is a multi-window desktop: without this the model sees only the
        # scoped foreground window and mis-reads the machine from its content (an
        # SSH terminal -> "I'm on Linux").
        specs.append({
            "name": "screen_windows",
            "description": "List every top-level window on the Windows desktop "
                           "(id, title, program; the foreground one is marked). The "
                           "foreground window is only ONE of many, and its content is "
                           "not the machine — use this to see everything that's open.",
            "parameters": {"type": "object", "properties": {}},
            "handler": screen_windows,
        })
        specs.append({
            "name": "screen_start_menu",
            "description": "Open the Windows Start menu (search box auto-focused) "
                           "from ANY window — the reliable way to launch an app, no "
                           "need to find or tap the taskbar. After it opens, "
                           "screen_type_text the app name, then screen_enter (or tap "
                           "the top result).",
            "parameters": {"type": "object", "properties": {}},
            "handler": screen_start_menu,
        })
        specs.append({
            "name": "screen_type_text",
            "description": "Type text into the currently FOCUSED element (Windows). "
                           "Unlike screen_type it needs no ref — it types wherever "
                           "keyboard focus is (e.g. the Start search box right after "
                           "screen_start_menu). NOT_ACTIONABLE if nothing is focused.",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string"}},
                           "required": ["text"]},
            "handler": screen_type_text,
        })
        specs.append({
            "name": "screen_enter",
            "description": "Press Enter on the focused element (Windows) — e.g. to "
                           "launch the highlighted Start search result, or submit a "
                           "focused field. Needs no ref.",
            "parameters": {"type": "object", "properties": {}},
            "handler": screen_enter,
        })
        specs.append({
            "name": "screen_close_window",
            "description": "Close the FOREGROUND window (Windows, Alt+F4). Use this "
                           "to close an app — first make the target its foreground "
                           "window (it's foreground right after you open it). This is "
                           "how you CLOSE an app; do NOT open Start and type its name.",
            "parameters": {"type": "object", "properties": {}},
            "handler": screen_close_window,
        })
        specs.append({
            "name": "screen_switch_window",
            "description": "Switch to the previous window (Windows, Alt+Tab) to bring "
                           "another open window to the foreground. Call again to keep "
                           "cycling; check the screen header each time until your "
                           "target window is foreground (screen_windows lists them).",
            "parameters": {"type": "object", "properties": {}},
            "handler": screen_switch_window,
        })
    elif controller.platform == "android":
        # BACK/HOME/RECENTS are Android globals; Windows has no equivalents, so
        # these are Android-only (a Windows device would reject all three).
        specs.extend([
            {"name": "screen_back", "description": "Press the system Back button.",
             "parameters": {"type": "object", "properties": {}}, "handler": screen_back},
            {"name": "screen_home", "description": "Go to the Home screen.",
             "parameters": {"type": "object", "properties": {}}, "handler": screen_home},
            {"name": "screen_recents", "description": "Open the Recents (app switcher).",
             "parameters": {"type": "object", "properties": {}}, "handler": screen_recents},
        ])
    for spec in specs:
        spec["handler"] = _logged(spec["name"], spec["handler"])
    return specs


def _click():
    from .protocol import Action
    return Action.click()
