# jaato-a11y-bridge — Wire Protocol Specification

**Status:** draft v1
**Scope:** the contract between the on-device AccessibilityService proxy ("device") and the jaato daemon-side controller ("daemon").

This document is the single source of truth for the wire. The Kotlin device design (`02-DEVICE_DESIGN.md`) and the Python daemon design (`03-DAEMON_DESIGN.md`) both implement *this* — they do not redefine envelopes, verbs, or field names.

---

## 1. Design invariant

> **The device is a dumb, configurable mechanism. The daemon is the mind.**

Everything that requires a live `AccessibilityNodeInfo` handle or an Android API call happens on the device. Every *decision* — which node to target, how long to wait for the UI to settle, what to screenshot, what to redact — is made by the daemon and pushed to the device as data. The device holds no cross-message state that encodes policy, and makes no heuristic choices. If a knob could conceivably be tuned, it lives in daemon-owned config, not in device code.

Consequences that fall out of this invariant and are baked into the protocol below:

- Actions are **selector-based, not handle-based.** The daemon never says "tap handle 42 you're holding for me"; it names a target and the device mechanically resolves it against the *current* tree.
- The settle detector runs on-device (it must consume a high-frequency local event stream) but is **fully parameterized by daemon-pushed `SettleConfig`**, mutable mid-session.
- Redaction, when it must happen before pixels leave the device, is an explicit daemon-set policy — the one place the device composites, because "do not emit these pixels" is a security boundary, not a decision.

---

## 2. Transport

- **Direction:** the device dials **out** to the daemon. The daemon never initiates. Rationale: the tablet is behind CGNAT / mobile NAT, roams networks, and sleeps.
- **URL:** `wss://<daemon-host>:<port>/a11y` — TLS mandatory, expected to run over the operator's existing VPN so the socket never touches the open internet.
- **One socket, two frame kinds:**
  - **Text frames** carry JSON control messages (requests, responses, events).
  - **Binary frames** carry length-prefixed opaque payloads (screenshots today; any future binary blob).
- **Keepalive:** WebSocket ping/pong at a fixed interval (default 15 s). A missed pong within `2 × interval` marks the socket half-open and triggers reconnect. This is required — Doze will silently wedge idle sockets and TCP alone will not notice.
- **Reconnect:** exponential backoff with jitter (e.g. 1 s → 30 s cap). On every (re)connect the device re-sends `hello` and the daemon re-pushes session `configure`; no state is assumed to survive a reconnect.

---

## 3. Message envelope (text frames)

All text frames are UTF-8 JSON objects with a `kind` discriminator.

### 3.1 Request (daemon → device)

```json
{ "kind": "req", "id": "r-8f3a", "verb": "observe", "args": { } }
```

- `id` — correlation id, unique per in-flight request. Echoed in the matching response.
- `verb` — one of §5.
- `args` — verb-specific, may be omitted when empty.

### 3.2 Response (device → daemon)

```json
{ "kind": "res", "id": "r-8f3a", "ok": true,  "data": { } }
{ "kind": "res", "id": "r-8f3a", "ok": false, "error": { "code": "STALE", "message": "…", "retryAfterMs": 0 } }
```

- Exactly one response per request. `ok:true` carries `data`; `ok:false` carries `error` (§7).
- `retryAfterMs` is present only where a retry is meaningful (e.g. `RATE_LIMITED`).

### 3.3 Event (device → daemon, unsolicited)

```json
{ "kind": "event", "event": "settled", "data": { } }
```

Events are fire-and-forget; the daemon does not ack them. See §6.

### 3.4 Common fields

Every frame carries `"pv": 1` (protocol version) at least on `hello`/`configure`; implementations MAY include it on all frames. A daemon and device with mismatched major `pv` MUST refuse to proceed (device emits `error` event, daemon closes).

---

## 4. Binary framing

Binary WebSocket frames are self-describing:

```
┌────────────┬───────────────────────────┬───────────────────────┐
│  4 bytes   │        headerLen bytes    │      payload bytes     │
│ headerLen  │      UTF-8 JSON header     │      raw blob          │
│ (BE uint32)│                           │                       │
└────────────┴───────────────────────────┴───────────────────────┘
```

Header schema (screenshot example):

```json
{
  "type": "screenshot",
  "correlationId": "r-8f3a",
  "snapshotVersion": 1287,
  "format": "webp",
  "width": 1080,
  "height": 2340,
  "reason": "bundled"
}
```

- `correlationId` ties the blob to the request that asked for it (standalone `screenshot`) **or** to the `observe`/`settled` it was bundled with.
- `snapshotVersion` (§8) is the stronger tie: when present it asserts "this image and the tree under this version describe the same visual frame."
- `reason` ∈ `on_demand | bundled`.
- No base64. No "next binary frame belongs to the last text message" ordering assumption — correlation is explicit and self-contained.

---

## 5. Verbs (daemon → device)

| verb | purpose |
|------|---------|
| `configure` | set/replace session policy: `SettleConfig`, screenshot defaults, redaction policy, package allowlist |
| `observe` | snapshot the current node tree; optionally bundle a screenshot |
| `act` | resolve a target selector and perform an action; optional per-call settle override |
| `screenshot` | capture a standalone screenshot |
| `waitForSettle` | wait for the UI to settle without acting; cancelable |
| `cancel` | cancel an in-flight `waitForSettle` |
| `ping` | liveness / RTT probe at the app layer (distinct from WS ping) |

### 5.1 `configure`

```json
{ "kind":"req", "id":"r-1", "verb":"configure", "args": {
  "settle": { /* SettleConfig, §9 */ },
  "screenshotDefaults": { "format":"webp", "quality":80, "maxDimension":1280, "crop":null },
  "redaction": { "maskPasswordNodes": true, "extraMaskSelectors": [] },
  "packageScope": ["com.foo.app"]
}}
```

- Any subset may be provided; unspecified sections keep their prior value.
- The device swaps the active config **atomically** (§ device design). Takes effect for all subsequent operations.
- `data` on success: `{ "applied": true }`.

### 5.2 `observe`

```json
{ "kind":"req", "id":"r-2", "verb":"observe", "args": {
  "includeScreenshot": true,
  "screenshot": { "format":"webp", "quality":80, "maxDimension":1280 }
}}
```

- `includeScreenshot` (default false). When true the device captures tree + frame as close to atomically as the APIs allow, stamps both with one `snapshotVersion`, returns the tree in `data`, and sends the image as a separate **binary frame** whose header carries the same `snapshotVersion` and `reason:"bundled"`.
- `screenshot` overrides `screenshotDefaults` for this call only.
- `data` success = a **Snapshot** (§8).

### 5.3 `act`

```json
{ "kind":"req", "id":"r-3", "verb":"act", "args": {
  "target": { /* Selector, §10 */ },
  "action": "CLICK",
  "text": null,
  "gesture": null,
  "global": null,
  "settleOverride": { "quietWindowMs": 1000 }
}}
```

- Exactly one of the action shapes applies per §11.
- `settleOverride` (optional) applies a `SettleConfig` **for the settle that follows this action only**, then reverts to session config. This is how the daemon says "this tap opens a slow WebView, wait 1 s *this time*" without a reconfigure round-trip.
- The device resolves `target` against the current root, performs the action, and returns `data`:

```json
{ "resolved": true, "matchedRef": 42, "matchedBy": "viewId", "settleAwaited": false }
```

- If resolution fails: `ok:false` with `NOT_FOUND` or `AMBIGUOUS` (§7).
- **Settle coupling:** `act` does *not* itself block on settle. The daemon's loop is `act → (await) settled → observe`. If `settleOverride` is present it seeds the next settle window; the device emits the `settled` event when quiet/timeout is reached.

### 5.4 `screenshot`

```json
{ "kind":"req", "id":"r-4", "verb":"screenshot", "args": {
  "format":"webp", "quality":80, "maxDimension":1280, "crop":[0,0,1080,1200],
  "allowCached": false
}}
```

- Standalone capture, no tree. Image returns as a binary frame with `reason:"on_demand"` and `correlationId = "r-4"`.
- `allowCached:true` permits the device to return the most recent frame if the rate-limit window (§12) has not elapsed, instead of erroring. Default false — coalescing is a daemon choice, never a silent device behavior.
- On rate-limit with `allowCached:false`: `ok:false`, `RATE_LIMITED`, `retryAfterMs`.

### 5.5 `waitForSettle`

```json
{ "kind":"req", "id":"r-5", "verb":"waitForSettle", "args": { "settle": { /* optional override */ } }}
```

- Runs the settle detector without acting (e.g. after an externally-fired deep link). Resolves when quiet or timeout. `data`: `{ "reason":"quiet", "snapshotVersion": 1290 }`.
- May be aborted by `cancel` (below); if canceled the response is `ok:false`, `CANCELED`.

### 5.6 `cancel`

```json
{ "kind":"req", "id":"r-6", "verb":"cancel", "args": { "target": "r-5" }}
```

- Cancels the in-flight request named by `args.target` (currently only `waitForSettle` is cancelable). `data`: `{ "canceled": true }`.

### 5.7 `ping`

`data`: `{ "t": <device epoch ms> }`. App-layer liveness independent of WS ping.

---

## 6. Events (device → daemon)

| event | when |
|-------|------|
| `hello` | first frame after (re)connect — device identity + capabilities |
| `settled` | settle detector reached quiet or hard timeout |
| `window_changed` | foreground window/activity transition (independent of settle) |
| `screenshot_error` | asynchronous capture failure not tied to a specific request |
| `error` | protocol/fatal device-side condition |
| `bye` | device is intentionally disconnecting (service stopped, permission revoked) |

### 6.1 `hello`

```json
{ "kind":"event", "event":"hello", "data": {
  "pv": 1,
  "platform": "android",
  "deviceId": "tab-samsung-01",
  "androidSdk": 34,
  "capabilities": {
    "takeScreenshot": true,
    "canPerformGestures": true,
    "reportViewIds": true,
    "retrieveInteractiveWindows": true
  },
  "screen": { "width":1080, "height":2340, "density":2.75 }
}}
```

- **`platform`** — `android | windows`. The device **declares** which platform it is, so the daemon routes to the correct window model (Android's single-tree `observe` vs the Windows multi-window set) rather than inferring it from which fields a response carries. The daemon MUST fail loudly on an absent/unknown value — no silent default.
- The daemon uses `capabilities` to refuse or degrade gracefully (e.g. no `takeScreenshot` on SDK < 30 → tree-only mode).

### 6.2 `settled`

```json
{ "kind":"event", "event":"settled", "data": {
  "reason": "quiet",
  "snapshotVersion": 1291,
  "pkg": "com.foo.app",
  "hasBundledScreenshot": false
}}
```

- `reason` ∈ `quiet | timeout`.
- `snapshotVersion` is the version the *next* `observe` will match if nothing changes. It lets the daemon detect whether the world moved between settle and observe.
- If the session/override requested a bundled frame on settle, `hasBundledScreenshot:true` and a binary frame with the same `snapshotVersion` follows.

### 6.3 `window_changed`

```json
{ "kind":"event", "event":"window_changed", "data": {
  "pkg":"com.foo.app", "activity":"com.foo.app/.CheckoutActivity", "snapshotVersion":1291 }}
```

Emitted on `TYPE_WINDOW_STATE_CHANGED` for in-scope packages. Advisory — lets the daemon react to navigation it didn't cause (deep links, external launches, ANR dialogs).

### 6.4 `screenshot_error`

```json
{ "kind":"event", "event":"screenshot_error", "data": { "code":"RATE_LIMITED", "retryAfterMs": 640 }}
```

### 6.5 `error` / `bye`

```json
{ "kind":"event", "event":"error", "data": { "code":"PROTOCOL_VERSION", "message":"pv 2 unsupported" }}
{ "kind":"event", "event":"bye",   "data": { "reason":"permission_revoked" }}
```

---

## 7. Error taxonomy

Returned in `res.error.code` or `event error.code`.

| code | meaning | retryable |
|------|---------|-----------|
| `NOT_FOUND` | selector resolved to zero nodes on current tree | after re-observe |
| `AMBIGUOUS` | selector matched >1 node and no disambiguator given | after refining selector |
| `STALE` | `{ref,snapshotVersion}` selector references a version no longer current | after re-observe |
| `NOT_ACTIONABLE` | node found but does not support the requested action | fall back to gesture |
| `RATE_LIMITED` | screenshot requested inside the capture cooldown | after `retryAfterMs` |
| `SECURE_WINDOW` | operation blocked by `FLAG_SECURE` (e.g. screenshot came back black by policy) | no |
| `CANCELED` | request aborted by `cancel` | no |
| `TIMEOUT` | operation exceeded its hard bound | maybe |
| `PROTOCOL_VERSION` | incompatible `pv` | no |
| `PERMISSION` | accessibility service not enabled / lost | no (needs user) |
| `INTERNAL` | unexpected device-side failure | maybe |

---

## 8. Snapshot & Node schema

A **Snapshot** is the pruned, serialized tree returned by `observe` (and referenced by `settled`).

```json
{
  "snapshotVersion": 1291,
  "pkg": "com.foo.app",
  "activity": "com.foo.app/.CheckoutActivity",
  "screen": { "width":1080, "height":2340 },
  "screenshotRef": "r-2",
  "nodes": [
    {
      "ref": 42,
      "cls": "android.widget.Button",
      "viewId": "com.foo.app:id/submit",
      "text": "Enviar",
      "desc": null,
      "bounds": [120, 1980, 960, 2100],
      "flags": ["clickable","enabled","visible","focusable"],
      "parent": 12
    }
  ]
}
```

- **`ref`** — ephemeral integer, unique **within this snapshotVersion only**. Used for set-of-marks and for `{ref,snapshotVersion}` selectors. Never reuse across versions; never persist.
- **`flags`** — compact string set drawn from: `clickable longClickable scrollable scrollableDown scrollableUp scrollableLeft scrollableRight editable checkable checked enabled focusable focused visible password selected`. Absent flags are false.
  - The four `scrollable<Dir>` tokens are **axis-explicit companions** to `scrollable`, emitted iff the node advertises the matching `ACTION_SCROLL_<DIR>` — a mechanical `getActionList()` read, no inference. They exist because a vertical feed and a horizontal pager are indistinguishable when both report only `scrollable`, forcing the daemon to guess which node to scroll (and, in practice, to page the tabs sideways instead of scrolling the feed). `SCROLL_FORWARD`/`BACKWARD` get no token: they are orientation-agnostic by nature, and `scrollable` already means "this scrolls at all".
  - Purely additive: a device that omits them is simply a device that does not report axes. No version gate.
  - **`imeEnter`** is the same idea for submission: emitted iff the node advertises `ACTION_IME_ENTER`, i.e. this field can submit itself. Without it the daemon must type and then hunt for a Go/Search button; with it, the submittable field is identifiable directly.
- **`bounds`** — `[left, top, right, bottom]` in screen pixels (`getBoundsInScreen`).
- **`parent`** — `ref` of the parent *in the pruned tree* (structural hint for disambiguation), or omitted for roots.
- **`screenshotRef`** — correlation id of the bundled binary frame, if `includeScreenshot` was set.

**Pruning contract (device-side, mechanical — not policy):** the device emits a node iff it is visible-to-user AND (actionable OR text-bearing OR content-described). Single-child structural chains are collapsed. Pure layout containers are dropped. This is a fixed transformation; anything screen-*semantic* (e.g. "wait longer on WebViews") is daemon policy and never appears here.

---

## 9. SettleConfig

```json
{
  "quietWindowMs": 500,
  "hardTimeoutMs": 5000,
  "eventMask": ["WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED"],
  "packageScope": ["com.foo.app"],
  "mode": "quiet",
  "minEventCount": 1,
  "bundleScreenshotOnSettle": false
}
```

- **`quietWindowMs`** — settle = this long with no *qualifying* event. Primary knob.
- **`hardTimeoutMs`** — emit `settled(reason:timeout)` regardless; guards perpetually-animating screens. Mandatory upper bound.
- **`eventMask`** — which accessibility event types reset the quiet timer. The highest-value knob: on a looping-animation screen the daemon masks out `WINDOW_CONTENT_CHANGED` and settles on state changes only. Allowed values mirror `AccessibilityEvent` types: `WINDOW_CONTENT_CHANGED`, `WINDOW_STATE_CHANGED`, `VIEW_SCROLLED`, `VIEW_TEXT_CHANGED`, `VIEW_FOCUSED`.
- **`packageScope`** — events from packages outside this set never reset the timer (ignores notifications/IME).
- **`mode`** — `quiet` (pure debounce) or `minEventsThenQuiet` (require ≥ `minEventCount` qualifying events, *then* a quiet window — useful when an action's effect is known to be delayed).
- **`bundleScreenshotOnSettle`** — when true, `settled` is accompanied by a bundled binary frame at the settle's `snapshotVersion`.

`SettleConfig` is set by `configure` (session default) and may be overridden per-call by `act.settleOverride` / `waitForSettle.settle`.

---

## 10. Selectors

A **Selector** names a target for `act`. Resolution is mechanical against the *current* root at action time.

```json
{ "viewId": "com.foo.app:id/submit" }
{ "text": "Enviar", "index": 0 }
{ "desc": "Submit order" }
{ "ref": 42, "snapshotVersion": 1291 }
{ "bounds": [120,1980,960,2100] }
```

Composite (all conditions must hold):

```json
{ "viewId": "com.foo.app:id/row", "text": "Order #1287" }
```

**Resolution order & rules (device, mechanical):**

1. `{ref,snapshotVersion}` — valid only if `snapshotVersion` == device's current version; else `STALE`. This is the tightest binding and the daemon's default when it acts immediately on a fresh observe.
2. `viewId` (+ optional `text`/`index` disambiguator) — via `findAccessibilityNodeInfosByViewId`.
3. `text` / `desc` — via `findAccessibilityNodeInfosByText` / content-description scan; `index` picks among matches.
4. `bounds` — last resort: hit-test the node whose bounds best contain/center-match; if none, gesture-tap the bounds center.

Zero matches → `NOT_FOUND`. >1 match with no `index`/composite disambiguator → `AMBIGUOUS`. The device never guesses which of several matches the daemon "meant."

---

## 11. Actions

Carried in `act.action` with the shape it requires:

Two kinds of action live here. **Node-directed** actions resolve `target` to a node first
(`CLICK`, `SET_TEXT`, `SCROLL_*`, `IME_ENTER`, `FOCUS`). **Focus/system-directed** actions take no
`target` — they act on whatever currently holds keyboard focus, or on the system
(`TYPE_TEXT`, `PRESS_KEY`, `GESTURE`, `GLOBAL`).

| action | extra field | node-directed | maps to (device) |
|--------|-------------|:---:|------------------|
| `CLICK` | — | ✓ | `ACTION_CLICK` |
| `LONG_CLICK` | — | ✓ | `ACTION_LONG_CLICK` |
| `SET_TEXT` | `text` | ✓ | `ACTION_SET_TEXT` + `ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE` |
| `SCROLL_FORWARD` | — | ✓ | `ACTION_SCROLL_FORWARD` |
| `SCROLL_BACKWARD` | — | ✓ | `ACTION_SCROLL_BACKWARD` |
| `SCROLL_{DOWN,UP,LEFT,RIGHT}` | — | ✓ | `ACTION_SCROLL_{DOWN,UP,LEFT,RIGHT}` (API 23+) |
| `IME_ENTER` | — | ✓ | `ACTION_IME_ENTER` (API 30+) |
| `FOCUS` | — | ✓ | `ACTION_FOCUS` |
| `TYPE_TEXT` | `text` | — | **windows:** `SendInput` unicode into the focused element |
| `PRESS_KEY` | `key` | — | **windows:** `SendInput` key event to the focused element |
| `GESTURE` | `gesture` | — | `dispatchGesture(StrokeDescription)` |
| `GLOBAL` | `global` | — | `performGlobalAction(...)` |

`gesture`:

```json
{ "type":"tap",   "path":[[540,1200]], "durationMs":60 }
{ "type":"swipe", "path":[[540,1800],[540,600]], "durationMs":300 }
```

`global` ∈ (android) `BACK | HOME | RECENTS | NOTIFICATIONS | QUICK_SETTINGS | LOCK_SCREEN`
∪ (windows) `START_MENU | SHOW_DESKTOP | SWITCH_WINDOW | MINIMIZE_ALL | CLOSE_WINDOW | LOCK_SCREEN`.
The available set is platform-specific (see `hello.platform`); a device rejects a `global` it does
not implement with `NOT_ACTIONABLE`.

### 11.1 Focus-directed keyboard input (`TYPE_TEXT` / `PRESS_KEY`) — Windows only

These are a **new category** the Android device deliberately does not have: an `AccessibilityService`
cannot inject key events (no `INJECT_EVENTS` permission), so on Android text goes in **node-directed**
via `SET_TEXT`/`IME_ENTER`. A Windows device *can* inject via `SendInput`, which enables genuinely
focus-directed input — act on whatever holds keyboard focus, with **no `target` / no ref**.

```json
{ "kind":"req", "id":"r-9", "verb":"act", "args": { "action":"TYPE_TEXT", "text":"Notepad" }}
{ "kind":"req", "id":"r-10","verb":"act", "args": { "action":"PRESS_KEY", "key":"ENTER" }}
```

- **`TYPE_TEXT`** — carries `text`; `SendInput` unicode into the focused element. `target` is omitted
  (ignored if present).
- **`PRESS_KEY`** — carries `key`; injects that key. `key` is an extensible enum, **`ENTER` only for
  now** — new keys can be added without reopening the wire.
- Both are **`platform:"windows"` only**. An Android device rejects them (`NOT_ACTIONABLE`/unknown).
- If **nothing holds keyboard focus**, the device returns `NOT_ACTIONABLE` — it does not guess a target.
- Why this matters for launching: Windows `START_MENU` auto-focuses the search box, so
  `START_MENU → TYPE_TEXT → PRESS_KEY{ENTER}` needs **no ref and no `ValuePattern`** — it is
  robust precisely because it is focus-directed, not node-directed.

**`IME_ENTER`** targets the editable node itself (same Selector shape as `SET_TEXT`) and fires *that field's own* editor action — Search / Go / Send / Done, per its `imeOptions`. It is the **only** way this device can submit a field: an AccessibilityService cannot inject key events (no `INJECT_EVENTS` permission) and `dispatchGesture` is touch-only, so `KEYCODE_ENTER` is not available to fall back on. Typical sequence is `CLICK`/`FOCUS` → `SET_TEXT` → `IME_ENTER` on the same ref; a field with no active input connection may refuse the action, which is reported, never worked around.

**Preference contract (daemon-side policy, stated here for both ends to agree):** prefer semantic actions (`CLICK`/`SET_TEXT`/`SCROLL_*`/`IME_ENTER`) because they target the node and survive minor layout shift; fall back to `GESTURE` only when the node is visible but `NOT_ACTIONABLE`. `SET_TEXT` is preferred over synthesizing per-character key events.

---

## 12. Screenshot semantics

- **Source:** `AccessibilityService.takeScreenshot()` (SDK 30+). No MediaProjection, no per-session consent, reuses the service context.
- **Rate limit:** ~1 capture/second. Faster requests fail with `RATE_LIMITED` + `retryAfterMs`, unless `allowCached:true` (returns last frame). The device MUST NOT silently sleep-and-retry.
- **Params (all daemon-set):** `format` (`webp` lossy recommended for VLM input — smaller than PNG, cleaner text/edges than JPEG), `quality`, `maxDimension` (downsample **on device** to model input resolution — the biggest bandwidth lever), `crop`.
- **`FLAG_SECURE` windows** return black from the OS — protected app screens self-censor for free; the device reports `SECURE_WINDOW` if the whole frame is secured and the daemon asked why.
- **Bundled captures** share the observe/settle `snapshotVersion` so tree and image describe one visual moment — the precondition for correct set-of-marks annotation (which the **daemon** performs; the device never draws marks).

---

## 13. Security

This socket carries a live, screen-grade stream of everything the user sees and types. Treat it as a keylogger built on purpose.

1. **`wss://` + mutual auth.** Client certificate or a strong device-bound pre-shared token in the WS upgrade; reject unauthenticated upgrades. Expected to run **VPN-only**.
2. **Redaction at source.** `maskPasswordNodes:true` (default) makes the device composite black over `isPassword` node bounds *before compression* so those pixels never leave the device; `extraMaskSelectors` extends this. `FLAG_SECURE` handles OS-level protected screens automatically.
3. **Package allowlist.** `packageScope` scopes both observation and action; out-of-scope packages are neither serialized nor actable — a blast-radius limiter.
4. **Audit.** The daemon logs every `act` (target + action + snapshotVersion) to a replayable trail. This protocol grants the LLM full device authority; the trail is non-optional.
5. **Fail closed.** On lost accessibility permission, expired auth, or `pv` mismatch, the device emits `bye`/`error` and stops; it never degrades to a less-safe mode.

---

## 14. Canonical loop

The daemon's core cycle, expressed in protocol terms:

```
configure(settle, screenshotDefaults, redaction, packageScope)
loop:
    observe(includeScreenshot=true)      → Snapshot + binary frame  (shared snapshotVersion)
    [daemon plans against tree + set-of-marks image]
    act(target, action, settleOverride?) → resolved ack
    await event settled                  → reason, snapshotVersion
    # if window_changed arrives unsolicited, re-observe before planning
```

Idempotency note: if `settled(timeout)` arrives, the daemon SHOULD re-`observe` and re-evaluate rather than assume the action's effect completed.
