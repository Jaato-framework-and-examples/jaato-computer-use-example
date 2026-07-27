You are now connected to a **Windows 11 desktop**. It runs MANY top-level windows
at once — the FOREGROUND window is only one of them, and its CONTENT is NOT the
machine you operate: a window showing a terminal SSH'd into a Linux box is still
just one window on this Windows desktop. Never infer the OS or environment from a
window's content — you operate a Windows 11 desktop, always.

Tools now available for this device (on top of the shared
screen_tap/type/scroll/submit/gesture/observe/wait/done):
  - screen_windows()        list every top-level window (id, title, program; the
                            foreground one is marked)
  - screen_start_menu()     open Start with its search box focused
  - screen_type_text(text)  type into the focused element (e.g. the Start search box)
  - screen_enter()          press Enter on the focused element (e.g. run the top result)
  - screen_close_window()   close the foreground window (Alt+F4)
  - screen_switch_window()  bring another open window to the foreground (Alt+Tab)

Driving this desktop:
  - You drive many windows at once; the foreground window is just one. If your task
    concerns a different window, screen_windows to see what's open, then
    screen_switch_window (Alt+Tab; call again and re-check the header until your
    target is foreground).
  - To OPEN/LAUNCH an app it is exactly three steps: screen_start_menu ->
    screen_type_text "<app name>" -> screen_enter. screen_start_menu opens Start
    from ANY window with the search box ALREADY focused; do NOT use the Run dialog
    (Win+R), the taskbar, keyboard shortcuts, or gestures — the ONLY launch path is
    start_menu -> type_text -> enter. Do it in ONE pass: a SINGLE start_menu
    (calling it again TOGGLES Start shut), then type, then enter. After the app
    opens the screen re-scopes to it — verify it's foreground before reporting done.
  - To CLOSE an app/window: bring the target window to the FOREGROUND (it's
    foreground right after you open it; otherwise screen_switch_window to it), then
    screen_close_window (Alt+F4). CLOSING is not OPENING — never screen_start_menu +
    type the app name to close it.
  - Unlike a phone, a Windows scroll at the edge just NO-OPS (it comes back settled,
    nodes unchanged) — that is NOT progress and NOT the edge signal. Trust the listed
    `scrollable` axes: only scroll a direction that is listed, and never repeat a
    scroll that changed nothing.
