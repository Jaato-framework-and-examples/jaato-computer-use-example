# jaato-a11y-bridge — on-device service (Kotlin / Android)

The **device** half of jaato-a11y-bridge: a resident Android AccessibilityService that is a
*dumb, configurable mechanism*. It captures the node tree and pixels, executes daemon-named
actions, debounces the event stream into `settled`, and pipes everything over one outbound
WebSocket. It holds **no grounding policy, no screen heuristics, no LLM anything** — every
judgment arrives as data (`configure`, `act.target`, `SettleConfig`).

Implements [`../docs/01-PROTOCOL.md`](../docs/01-PROTOCOL.md) per [`../docs/02-DEVICE_DESIGN.md`](../docs/02-DEVICE_DESIGN.md).
The Python daemon (`03`) is out of scope here.

## Build

Requires JDK 17 + Android SDK (platforms 30/34/35, build-tools). `local.properties` points at
the SDK.

```bash
./gradlew :app:assembleDebug        # → app/build/outputs/apk/debug/app-debug.apk
./gradlew :app:testDebugUnitTest    # JVM unit tests (wire framing, pruning, envelopes)
```

- `applicationId` `com.jaato.a11ybridge`, `minSdk` 30 (gates `takeScreenshot`), `targetSdk` 34.
- Stack: Kotlin 2.0, coroutines, kotlinx.serialization, OkHttp.

## Install & run

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

1. Open **jaato a11y bridge**, set the **Daemon URL** (`wss://host:port/a11y`) and **token**, Save.
2. Tap **Open Accessibility settings** and enable the service.
3. The device dials out, sends `hello`, and waits for the daemon to `configure`. Until then it is
   **fail-closed**: empty `packageScope` (acts on / serialises nothing), password masking on.

## Architecture (single service)

One `AccessibilityService` hosts the event pump, transport, and router — an *enabled*
AccessibilityService is already system-bound, so no separate foreground service is needed for
survival; an ongoing notification provides user transparency. Module map mirrors §1 of the
device design: `transport/`, `observe/`, `act/`, `settle/`, `shot/`, `state/`, `CommandRouter`.

## Spec interpretations (points where 01/02 were ambiguous — confirm against the daemon)

These were resolved deterministically rather than guessed at runtime; flagged here so the daemon
side agrees:

1. **Screenshot crop ordering.** Protocol §5.4 gives `crop` in screen-pixel coordinates, but device
   §7 pseudocode downsamples *then* crops. Those are inconsistent. We apply **redact → crop (screen
   px) → downsample**, so `crop` always means absolute screen pixels (consistent with every other
   bounds field in the protocol). If the daemon expects crop in post-downsample coordinates, change
   `ScreenshotCapturer.processAndEncode`.
2. **`matchedRef` in the `act` ack.** Populated only for `{ref}` selectors (where the daemon already
   supplied the ref). For `viewId`/`text`/`bounds` it is **omitted** rather than fabricated — refs
   are ephemeral per-version and the device persists none. `matchedBy` is always present.
3. **`SECURE_WINDOW` detection.** `takeScreenshot` has no secure-window error; the OS simply returns
   black pixels for `FLAG_SECURE`. We do **not** run an all-black heuristic (it would false-positive
   on genuinely dark screens). The `SECURE_WINDOW` code exists in the taxonomy but is not
   proactively emitted; the daemon sees the black frame and decides. Revisit if a deterministic
   signal becomes available.
4. **`packageScope` empty ⇒ no event delivery.** On (re)connect and until `configure`, the service's
   `AccessibilityServiceInfo.packageNames` is set to the (empty) scope, so no events flow and nothing
   is observable/actable — the fail-closed blast-radius limiter of §13.

## Protocol extension: `windows` verb

Added after e2e, at the controller's request, to solve the connect-time chicken-and-egg
(how does the daemon learn the foreground package to scope to, before it has scoped to
anything?).

- **Request:** `{ "kind":"req", "id":"…", "verb":"windows", "args":{} }`
- **Response `data`:** a `WindowsReport` —
  ```json
  { "foregroundPkg":"com.android.settings",
    "foregroundActivity":"com.android.settings/.Home",
    "launcherPkg":"com.sec.android.app.launcher",
    "windows":[ {"pkg":"…","title":"…","type":"application","focused":true,"layer":0} ] }
  ```
- **Non-scope-gated by design:** reports window *metadata* (package/title/type/layer) only —
  never tree content or pixels — so it works before any scope is set. `launcherPkg` is one
  bounded `PackageManager` HOME-intent resolve, NOT an installed-app enumeration.

**Security model this establishes:** window *metadata* (names: `windows` verb, `window_changed`)
flows regardless of scope; window *content* (`observe` tree, `act`) stays strictly scope-gated.

**Scoping is now software-only:** `AccessibilityServiceInfo.packageNames` is left `null` (all
packages delivered) and `packageScope` is enforced entirely in code (TreeWalker/Resolver/settle/
clock). This is required for `windows` to see out-of-scope windows. Fail-closed is unchanged —
empty scope still serializes and acts on nothing. Trade-off: drops OS-level event filtering as a
redundant second layer (it never bounded a malicious daemon, which can widen scope via `configure`
anyway).

## Known follow-ups

- **Token at rest.** Stored in plain `SharedPreferences` (`state/Prefs.kt`). Harden to
  EncryptedSharedPreferences / a device-bound keystore key.
- **Mutual auth.** The upgrade sends a bearer token (§13.1); client-certificate mutual TLS and the
  VPN-only deployment are operator-side.
- No device/emulator was available in this environment, so runtime behaviour is verified by JVM unit
  tests over the pure protocol logic (pruning, binary framing, envelope round-trips) plus a full
  compile+package. On-device end-to-end against a live daemon remains to be exercised.
