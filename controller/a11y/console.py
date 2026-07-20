"""Full-screen operator console — the pinned-input layout the steering UX needs.

A deliberate simplification of jaato-tui's ``pt_display`` (same skeleton: a
scrollable output window fills the top; the input line and a state toolbar are
pinned at the bottom), with everything the controller doesn't need removed —
Rich rendering, multi-pane, popups, completion, selection. What's left is just:

- an output window that scrolls (new text tails to the bottom),
- a ``you>`` input line pinned above the bottom bar,
- a one-line toolbar that reflects state (idle vs. agent working).

Why a full-screen ``Application`` and not ``PromptSession`` + ``bottom_toolbar``:
the latter pins the toolbar to the screen bottom but leaves the prompt at the
cursor row, so with little output the input floats mid-screen far from the bar.
A full-screen layout puts output / input / toolbar in fixed regions, so the
input always sits right above the toolbar with output scrolling above it.

Output is written via :meth:`write`; each submitted line is echoed into the
output and pushed to :attr:`line_queue` for the caller to route (steer vs.
inject). ``/quit``/``/exit`` and Ctrl-C/Ctrl-D set the shared ``quit_event``.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl


class SteerConsole:
    """Full-screen console with a scrolling output area and a pinned input+toolbar.

    ``toolbar`` is a zero-arg callable returning prompt_toolkit formatted text
    (e.g. an ``HTML``); it is re-evaluated on every render so it tracks state.
    """

    def __init__(self, toolbar: Callable[[], object], quit_event: asyncio.Event) -> None:
        self.line_queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._quit = quit_event

        # Output: a read-only buffer. Appending text and moving the cursor to the
        # end makes the window scroll to the tail — a Window scrolls to keep its
        # control's cursor visible on render, even when it isn't the focus.
        self._out = Buffer(read_only=Condition(lambda: True), document=Document("", 0))
        out_win = Window(BufferControl(buffer=self._out, focusable=False),
                         wrap_lines=True)

        # Input: a single-line buffer with a fixed "you> " label beside it.
        self._in = Buffer(multiline=False, accept_handler=self._accept)
        input_row = VSplit([
            Window(FormattedTextControl(lambda: [("class:prompt", "you> ")]),
                   width=5, dont_extend_width=True),
            Window(BufferControl(buffer=self._in), height=1),
        ], height=1)

        toolbar_win = Window(FormattedTextControl(toolbar), height=1, style="reverse")

        root = HSplit([out_win, input_row, toolbar_win])
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("c-d")
        def _quit_kb(event) -> None:
            self._quit.set()
            event.app.exit()

        self._app = Application(
            layout=Layout(root, focused_element=self._in),
            key_bindings=kb, full_screen=True, mouse_support=True)

    def _accept(self, buff: Buffer) -> bool:
        """Enter handler: echo the line, then quit or queue it for routing.
        Returns False so the input line clears for the next entry."""
        line = buff.text.strip()
        if line:
            self.write(f"you> {line}")
            if line in ("/quit", "/exit"):
                self._quit.set()
                self._app.exit()
            else:
                self.line_queue.put_nowait(line)
        return False

    def write(self, text: str, end: str = "\n") -> None:
        """Append ``text`` to the output and scroll to it. ``end=""`` appends
        without a newline (for token-by-token agent streaming)."""
        new = self._out.text + text + end
        self._out.set_document(Document(new, len(new)), bypass_readonly=True)
        if self._app.is_running:
            self._app.invalidate()

    async def run(self) -> None:
        await self._app.run_async()

    def stop(self) -> None:
        if self._app.is_running:
            self._app.exit()
