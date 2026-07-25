# jaato-computer-use-example

A working reference implementation of **computer use**: an LLM controller that observes a real
device's UI, decides what to do, and acts on it — over a small, explicit wire protocol. The same
controller drives an **Android** phone/tablet and a **Windows 11** desktop without knowing which it
is talking to.

It is split into two halves with a deliberately hard boundary:

> **The device is a dumb, configurable mechanism. The daemon is the mind.**

Everything that needs a live accessibility node or a platform API call happens on the device. Every
*decision* — which node to target, how long to wait for the UI to settle, what to screenshot, what
to redact — is made by the controller and pushed to the device as data. The device holds no policy
and makes no heuristic choices.

That boundary is the point of this repo. It is what makes the controller portable — it speaks one
wire to any device half — and each device auditable: it can only do what it was told, and it never
guesses.

## Layout

```
docs/
  01-PROTOCOL.md               the wire contract — single source of truth for every half
  02-DEVICE_DESIGN.md          how the Android device half implements that contract
  04-DEVICE_DESIGN-WINDOWS.md  how the Windows device half implements the same contract
android-bridge/                Android AccessibilityService (Kotlin) — a device half
windows-bridge/                Windows desktop bridge (.NET / UIA) — a device half
controller/                    the LLM controller (Python) — the mind, shared by both
tools/
  e2e_daemon.py                minimal harness: enough "mind" to exercise the wire by hand
```

Both device halves implement the identical wire in `docs/01-PROTOCOL.md`; the controller dispatches
per-platform behaviour (persona, tool set, foreground rules) off the `platform` the device declares
when it connects. Nothing platform-specific lives in the wire itself.

## Status

| Component | State |
|---|---|
| Wire protocol (`docs/01-PROTOCOL.md`) | stable; carries the `windows` verb + directional scrolls, one contract for both halves |
| Android bridge (`android-bridge/`) | complete, unit-tested, **verified end-to-end on real hardware** (Android 16 / SDK 36) |
| Windows bridge (`windows-bridge/`) | complete (.NET / raw UIA + WGC + SendInput), **verified end-to-end on real hardware** (Windows 11) |
| LLM controller (`controller/`) | complete, unit-tested; drives either device half via vision + set-of-marks |
| e2e harness (`tools/e2e_daemon.py`) | working; drives the full loop by hand, no model |

On both platforms the full canonical loop runs against a physical device — `configure → observe →
act → settled`, plus screenshot capture with on-device redaction, foreground tracking, and
reconnect recovery. An LLM launches and navigates real apps end-to-end (e.g. opening an app from the
Android drawer, or from the Windows Start menu).

## The protocol in one screen

One outbound WebSocket carries JSON control frames and length-prefixed binary blobs.

**Verbs** (controller → device): `configure`, `observe`, `windows`, `act`, `screenshot`,
`waitForSettle`, `cancel`, `ping`
**Events** (device → controller): `hello`, `settled`, `window_changed`, `screenshot_error`,
`error`, `bye`

The loop:

```
configure(settle, screenshotDefaults, redaction, packageScope)
loop:
    observe(includeScreenshot=true)   → pruned node tree + screenshot (shared snapshotVersion)
    [controller plans against tree + set-of-marks image]
    act(target, action)               → resolved ack
    await settled                     → the UI has stopped moving
```

Targets are named by **selector**, never by a handle the controller holds — the device resolves
them mechanically against the *current* tree. Zero matches is `NOT_FOUND`, multiple is `AMBIGUOUS`,
a stale reference is `STALE`. The device never picks the one it thinks you meant.

See `docs/01-PROTOCOL.md` for the full contract.

## Quickstart

### 1. Build and install a device half

**Android** — requires JDK 17 + Android SDK (min-SDK 30, which gates `takeScreenshot`):

```bash
cd android-bridge
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Open the app, set the daemon URL (`ws://host:8765/a11y`) and a token, then enable the service under
**Accessibility**.

**Windows 11** — requires the .NET 8 SDK:

```bash
cd windows-bridge/JaatoBridge
dotnet run
```

Set the same daemon URL and token in the tray UI, then **CONNECT**.

### 2. Drive it

**By hand, no model** — the quickest smoke test of the wire:

```bash
python3 tools/e2e_daemon.py --port 8765
```

Type `help` for commands (`scope <pkg>`, `observe`, `click <viewId>`, `recents`, …). Screenshots
land in `./captures/`.

**With an LLM** — the full computer-use loop:

```bash
cd controller
pip install -r requirements.txt        # plus the in-repo jaato_sdk (installed editable)
python run_controller.py "open the settings app"
```

The controller stands up the device-facing listener (`ws://<host>:8765/a11y`), connects to the
jaato daemon for the model, and runs the observe→act→settle loop — auto-selecting the persona and
tool set from the platform the device declared. You can type a new instruction or correction at any
time, including mid-turn. Provider/model/key live in `controller/.jaato/profiles/a11y-controller.yaml`;
listener, scope, and screenshot policy in `controller/.jaato/a11y-bridge.yaml`. See
[`controller/README.md`](controller/README.md) for the full setup.

## Security posture

**Read this before running it.** This socket carries a live, screen-grade stream of everything the
user sees and types, and grants the connected controller full device authority. The protocol
document is blunt about it: treat it as a keylogger built on purpose.

The design takes that seriously rather than assuming good behaviour:

- **Fail closed.** On connect, and on every reconnect, the device resets to an empty `packageScope`
  — it observes and acts on *nothing* until the controller explicitly declares what it may touch. No
  daemon state survives a reconnect.
- **Scope allowlist.** `packageScope` bounds both observation and action. Out-of-scope apps and
  windows are neither serialized nor actionable — if a banking app comes to the foreground, its
  content never leaves the device.
- **Redaction at source.** Password fields are composited over **before compression**, so those
  pixels never leave the device. OS-marked secure windows are excluded before capture.
- **Metadata vs content.** Window *names* (which app is on screen) flow freely so the controller can
  navigate; window *content* (tree, pixels) is strictly scope-gated.
- **No hidden fallbacks.** The device never silently retries, re-resolves a stale reference, or
  degrades to a synthetic gesture when a semantic action fails. It reports and the controller
  decides. There is no hardcoded endpoint or token anywhere in the source.
- **Visible while running.** The bridge is visibly live whenever it is connected (an ongoing
  notification on Android, a tray indicator on Windows) with an explicit CONNECT/DISCONNECT kill
  switch that persists across restarts.

Transport is `wss://` with a device-bound token in the upgrade, and is expected to run VPN-only.
Debug/local builds additionally permit cleartext `ws://` for testing; release builds do not.

## License

Apache-2.0. See [LICENSE](LICENSE).
