# windows-bridge — Windows device bridge (.NET)

The on-desktop implementation of the a11y wire protocol for **Windows 11**, the
counterpart to [`android-bridge/`](../android-bridge). It runs on the target
desktop, dials the controller's WebSocket listener, and exposes the desktop to
the daemon as a *dumb mechanism* — it observes/acts/screenshots/settles exactly
as the daemon configures, and owns none of the grounding/settle/scope **policy**
(that is the controller's). Implements [`docs/04-DEVICE_DESIGN-WINDOWS.md`](../docs/04-DEVICE_DESIGN-WINDOWS.md);
speaks the same wire as [`docs/01-PROTOCOL.md`](../docs/01-PROTOCOL.md).

Built with raw `IUIAutomation` + Windows Graphics Capture (WGC) + `SendInput`
(.NET 8). `bin/`, `obj/`, and `dist/` are gitignored.

## Structure (`JaatoBridge/`)

| Subsystem   | Role |
|-------------|------|
| `Transport` | WebSocket transport + envelope/frames (§3) |
| `Observe`   | UIA tree walk + pruning + `WindowLister` (top-level windows, foreground, scope gate) |
| `Act`       | `Actuator` / `SyntheticInput` / `Resolver` / `Selector` / `ActService` — CLICK/SET_TEXT/GESTURE, focus-directed `TYPE_TEXT`/`PRESS_KEY`, and the GLOBAL set (`START_MENU`/`SWITCH_WINDOW`/`CLOSE_WINDOW`/…) |
| `Shot`      | `ScreenCapturer` — per-window + full-monitor WGC capture |
| `Settle`    | `SettleDetector` — desktop-wide UIA event debounce the daemon arms per act |
| `State`     | `SessionConfig` (scope, settle, `ObserveShellSurfaces`, redaction) |
| `Platform`  | Win32/UIA interop |
| `Tray`      | tray UI / lifecycle |

## Fixes behind the clean one-per-turn launch

These are the validated device-side pieces of the "open Notepad" launch working
on any model (including the small open Qwen3-VL-30B), all daemon-driven:

- **`Observe/WindowLister.InScope(scope, observeShellSurfaces)`** — exempts OS
  shell/launcher surfaces (SearchHost / StartMenuExperienceHost / …) from the
  fail-closed scope gate **only when the daemon sets `observeShellSurfaces`** in
  `configure`. The daemon owns *whether* (the flag); the bridge owns *which*
  windows are the shell + applies it. This is what lets the model **see** Start
  when it opens (was: `observe 'Buscar' OUT OF SCOPE → empty tree`).
- **`Settle/SettleDetector`** — honors the daemon's per-act settle override, so a
  GLOBAL settles on the desktop-wide `VIEW_FOCUSED` change (~200ms) instead of
  burning the hard timeout watching the old window's subtree.
- **`Shot/ScreenCapturer.CaptureMonitor`** — full-monitor WGC fallback when a
  per-window capture fails or the foreground is `None`.
