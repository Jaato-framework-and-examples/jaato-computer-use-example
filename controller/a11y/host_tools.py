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
from typing import Any, Dict, List

from . import annotate
from .controller import Controller

log = logging.getLogger("a11y.tools")


def _screen_result(controller: Controller, ack: str) -> dict:
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


def build_tools(controller: Controller) -> List[Dict[str, Any]]:
    """Return the ``register_client_tools`` specs bound to ``controller``."""

    async def screen_tap(args: dict) -> dict:
        ack = await controller.act_ref(int(args["ref"]), _click())
        return _screen_result(controller, ack)

    async def screen_type(args: dict) -> dict:
        from .protocol import Action
        ack = await controller.act_ref(int(args["ref"]), Action.set_text(str(args["text"])))
        return _screen_result(controller, ack)

    async def screen_scroll(args: dict) -> dict:
        from .protocol import Action
        ack = await controller.act_ref(int(args["ref"]), Action.scroll_dir(str(args["direction"])))
        return _screen_result(controller, ack)

    async def screen_back(args: dict) -> dict:
        return _screen_result(controller, await controller.global_action("BACK"))

    async def screen_home(args: dict) -> dict:
        return _screen_result(controller, await controller.global_action("HOME"))

    async def screen_recents(args: dict) -> dict:
        return _screen_result(controller, await controller.global_action("RECENTS"))

    async def screen_gesture(args: dict) -> dict:
        path = [[int(p[0]), int(p[1])] for p in args["path"]]
        ack = await controller.gesture(path, int(args.get("duration_ms", 300)))
        return _screen_result(controller, ack)

    async def screen_wait(args: dict) -> dict:
        return _screen_result(controller, await controller.wait())

    async def screen_done(args: dict) -> dict:
        return {"result": controller.mark_done(str(args.get("summary", "")))}

    def _logged(name: str, handler):
        """Wrap a handler so every model tool-call and its result are logged —
        the ground truth for 'the agent said it would X but nothing happened'."""
        async def _w(args: dict) -> dict:
            log.info("tool-call %s %s", name, args)
            res = await handler(args)
            log.info("tool-result %s -> %r", name, res.get("result"))
            return res
        return _w

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
        {"name": "screen_back", "description": "Press the system Back button.",
         "parameters": {"type": "object", "properties": {}}, "handler": screen_back},
        {"name": "screen_home", "description": "Go to the Home screen.",
         "parameters": {"type": "object", "properties": {}}, "handler": screen_home},
        {"name": "screen_recents", "description": "Open the Recents (app switcher).",
         "parameters": {"type": "object", "properties": {}}, "handler": screen_recents},
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
    for spec in specs:
        spec["handler"] = _logged(spec["name"], spec["handler"])
    return specs


def _click():
    from .protocol import Action
    return Action.click()
