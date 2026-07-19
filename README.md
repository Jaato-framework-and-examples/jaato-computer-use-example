# jaato-computer-use-example

A working reference implementation of **computer use on Android**: an LLM controller that observes a
real device's UI, decides what to do, and acts on it — over a small, explicit wire protocol.

It is split into two halves with a deliberately hard boundary:

> **The device is a dumb, configurable mechanism. The daemon is the mind.**

Everything that needs a live `AccessibilityNodeInfo` or an Android API call happens on the device.
Every *decision* — which node to target, how long to wait for the UI to settle, what to screenshot,
what to redact — is made by the controller and pushed to the device as data. The device holds no
policy and makes no heuristic choices.

That boundary is the point of this repo. It is what makes the controller portable (it never knows
it is talking to Android) and the device auditable (it can only do what it was told, and it never
guesses).

## Layout

```
docs/
  01-PROTOCOL.md        the wire contract — single source of truth for both halves
  02-DEVICE_DESIGN.md   how the Android device half implements that contract
android-bridge/         Android AccessibilityService (Kotlin) — the device half
tools/
  e2e_daemon.py         minimal Python harness: enough "mind" to exercise the wire by hand
controller/             the real LLM controller (landing separately)
```

## Status

| Component | State |
|---|---|
| Wire protocol (`docs/01-PROTOCOL.md`) | stable; extended in-repo with a `windows` verb |
| Android bridge (`android-bridge/`) | complete, unit-tested, **verified end-to-end on real hardware** (Android 16 / SDK 36) |
| e2e harness (`tools/e2e_daemon.py`) | working; drives the full loop by hand |
| LLM controller (`controller/`) | developed alongside; lands separately |

The full canonical loop is exercised on a physical device: `configure → observe → act → settled`,
plus screenshot capture with on-device redaction, foreground tracking, and reconnect recovery.

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

Build and install the device half:

```bash
cd android-bridge
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Open the app, set the daemon URL (`ws://host:port/a11y`) and a token, enable the service under
**Accessibility**, then run the harness:

```bash
python3 tools/e2e_daemon.py --port 8765
```

Type `help` for commands (`scope <pkg>`, `observe`, `click <viewId>`, `recents`, …). Screenshots
land in `./captures/`.

Requires JDK 17 + Android SDK (min-SDK 30, which gates `takeScreenshot`).

## Security posture

**Read this before running it.** This socket carries a live, screen-grade stream of everything the
user sees and types, and grants the connected controller full device authority. The protocol
document is blunt about it: treat it as a keylogger built on purpose.

The design takes that seriously rather than assuming good behaviour:

- **Fail closed.** On connect, and on every reconnect, the device resets to an empty
  `packageScope` — it observes and acts on *nothing* until the controller explicitly declares what
  it may touch. No daemon state survives a reconnect.
- **Package allowlist.** `packageScope` bounds both observation and action. Out-of-scope apps are
  neither serialized nor actionable — if a banking app comes to the foreground, its content never
  leaves the device.
- **Redaction at source.** Password fields are composited over **before compression**, so those
  pixels never leave the device. `FLAG_SECURE` windows are already excluded by the OS.
- **Metadata vs content.** Window *names* (which app is on screen) flow freely so the controller
  can navigate; window *content* (tree, pixels) is strictly scope-gated.
- **No hidden fallbacks.** The device never silently retries, re-resolves a stale reference, or
  degrades to a synthetic gesture when a semantic action fails. It reports and the controller
  decides. There is no hardcoded endpoint or token anywhere in the source.
- **Visible while running.** An ongoing notification is shown whenever the bridge is live, and the
  app has an explicit CONNECT/DISCONNECT kill switch that persists across restarts.

Transport is `wss://` with a device-bound token in the upgrade, and is expected to run VPN-only.
The debug build additionally permits cleartext `ws://` for local testing; release builds do not.

## License

Apache-2.0. See [LICENSE](LICENSE).
