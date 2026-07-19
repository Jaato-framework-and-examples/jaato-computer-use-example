# jaato-a11y-bridge — On-Device Service Design (Kotlin / Android)

**Implements:** `01-PROTOCOL.md`. This document does not restate envelopes or verb schemas; it describes how the device fulfills that contract.

**Role:** a resident Android app whose only job is to be a *dumb, configurable mechanism* — capture the node tree and pixels, execute daemon-named actions, debounce the event stream into `settled`, and pipe all of it over one outbound WebSocket. It contains **no grounding policy, no screen heuristics, no LLM anything.** Every judgment arrives as data (`configure`, `act.target`, `SettleConfig`).

---

## 1. Module layout

```
app/
 ├─ BridgeAccessibilityService.kt   // AccessibilityService entry point + event pump
 ├─ transport/
 │   ├─ WsClient.kt                  // outbound OkHttp WebSocket, reconnect, keepalive
 │   ├─ Envelope.kt                  // kotlinx.serialization models for §3 messages
 │   └─ BinaryFrame.kt               // 4-byte-len header + blob (§4)
 ├─ observe/
 │   ├─ TreeWalker.kt                // AccessibilityNodeInfo → List<NodeSnap> (POJO)
 │   └─ Pruner.kt                    // fixed pruning transform (§8)
 ├─ act/
 │   ├─ Resolver.kt                  // Selector → live node (§10), mechanical
 │   └─ Actuator.kt                  // performAction / dispatchGesture / global (§11)
 ├─ settle/
 │   └─ SettleDetector.kt            // configurable debounce state machine (§9)
 ├─ shot/
 │   ├─ ScreenshotCapturer.kt        // takeScreenshot + encode + downsample (§12)
 │   └─ Redactor.kt                  // mask password/extra bounds pre-compression (§13)
 ├─ state/
 │   └─ SessionConfig.kt             // atomically swappable active config
 └─ CommandRouter.kt                 // req → handler dispatch, res/event emission
```

Stack: Kotlin, coroutines, `kotlinx.serialization` (JSON + manual binary framing), OkHttp WebSocket. Min-SDK 30 (gates `takeScreenshot`); `hello.capabilities` degrades gracefully below where unavoidable.

---

## 2. The AccessibilityService

### 2.1 Manifest / config

`res/xml/a11y_config.xml` — the flags that decide success:

```xml
<accessibility-service
    android:accessibilityEventTypes="typeWindowStateChanged|typeWindowContentChanged|typeViewScrolled|typeViewTextChanged|typeViewFocused"
    android:accessibilityFeedbackType="feedbackGeneric"
    android:canRetrieveWindowContent="true"
    android:canPerformGestures="true"
    android:accessibilityFlags="flagReportViewIds|flagRetrieveInteractiveWindows|flagIncludeNotImportantViews"
    android:notificationTimeout="100" />
```

- `canRetrieveWindowContent` — no tree without it.
- `canPerformGestures` — required for `dispatchGesture`.
- `flagReportViewIds` — populates `viewIdResourceName`, the single most valuable selector.
- `flagRetrieveInteractiveWindows` — `getWindows()` across IME / dialogs / overlays.
- `flagIncludeNotImportantViews` — some apps mark actionable nodes not-important; include and let the daemon prune semantically.

We deliberately subscribe to a **superset** of event types here and let `SettleConfig.eventMask` decide at runtime which ones *count* — the manifest is the mechanism envelope, the daemon owns the policy.

### 2.2 Runtime tuning

In `onServiceConnected()` we also set `AccessibilityServiceInfo` programmatically (so `packageNames` can follow the daemon's `packageScope` without a reinstall):

```kotlin
serviceInfo = serviceInfo.apply {
    packageNames = session.get().packageScope.toTypedArray()  // null = all, per policy
    flags = flags or FLAG_REQUEST_FILTER_KEY_EVENTS.inv().let { flags } // (keep existing)
}
```

`packageScope` changes from a `configure` re-apply this live.

### 2.3 Threading model — the ANR trap

Accessibility callbacks (`onAccessibilityEvent`, `onInterrupt`) land on the **main thread**. Two hard rules:

1. **Snapshot fast, hand off.** In the callback, walk the tree into plain `NodeSnap` POJOs *immediately* (nodes are live handles bound to their window; they cannot cross threads or outlive the window). Then post serialization + WS send to a coroutine on a background dispatcher. Never serialize-to-JSON or block on the socket on the main thread.
2. **Recycle discipline.** Pre-33: `recycle()` every `AccessibilityNodeInfo` you obtained. 33+: `recycle()` is a deprecated no-op — branch on `Build.VERSION.SDK_INT` and skip. `TreeWalker` owns this so no other component touches raw nodes.

A single-threaded background dispatcher (`newSingleThreadContext` or a confined `CoroutineScope`) serializes all outbound work, which also gives us a natural ordering guarantee for frames.

---

## 3. Transport (`WsClient`)

- OkHttp `WebSocket`, dials `wss://…/a11y` **outbound** with the device-bound auth token in an `Authorization` header on the upgrade.
- **Send queue:** a `Channel<Outgoing>` drained by the background scope; `Outgoing` is either `Text(String)` or `Binary(ByteString)`. Keeps main thread non-blocking and preserves order (tree text frame then its bundled binary frame).
- **Keepalive:** app-layer `ping` verb handled + OkHttp's `pingInterval(15, SECONDS)`. A watchdog coroutine tracks last-pong; on miss → force close → reconnect.
- **Reconnect:** exponential backoff w/ jitter (1s→30s). On open: emit `hello` (§6.1) with live `capabilities` + `screen`. Do **not** cache daemon config across reconnects — wait for the daemon to re-`configure`; until then operate on safe defaults (empty packageScope = act on nothing, `maskPasswordNodes=true`).
- **Foreground service:** `WsClient` lives inside a bound foreground service (persistent notification) so the OS doesn't reclaim it; this is separate from the AccessibilityService process lifecycle but co-resident.

### 3.1 Binary framing (`BinaryFrame`)

```kotlin
fun frame(header: ByteArray, payload: ByteArray): ByteString {
    val len = header.size
    val buf = Buffer()
    buf.writeInt(len)          // 4-byte BE (Okio writeInt is BE)
    buf.write(header)
    buf.write(payload)
    return buf.readByteString()
}
```

Header is the UTF-8 JSON of §4; payload is the encoded image bytes.

---

## 4. Observe path (`TreeWalker` + `Pruner`)

```kotlin
fun snapshot(version: Long): Snapshot {
    val roots = windows.mapNotNull { it.root }        // getWindows() → interactive windows
        .ifEmpty { listOfNotNull(rootInActiveWindow) }
    val raw = buildList { roots.forEach { walk(it, parentRef = null, out = this) } }
    // walk() copies each node into NodeSnap (cls, viewId, text, desc, bounds, flags) and recycles
    val pruned = Pruner.prune(raw)                    // §8 fixed transform
    return Snapshot(version, pkg, activity, screen, pruned)
}
```

- `walk` assigns each retained node a monotonic **`ref`** (unique within this `version`), records `parent` ref, snapshots the flag set (`isClickable`, `isEditable`, `isVisibleToUser`, `isPassword`, …) into strings, then recycles the live node (pre-33).
- **`Pruner`** applies the mechanical contract from protocol §8: keep visible ∧ (actionable ∨ text ∨ desc); collapse single-child chains; drop layout-only containers. No screen-type logic — that's the daemon's.
- Virtualized list items, inaccessible WebView content, and merged Compose semantics are simply absent; that's expected and why the daemon keeps screenshots as a fallback grounding channel.

The current `version` is a process-wide `AtomicLong` (`SnapshotClock`) incremented on every content/state change the settle detector observes, so `observe`, `settled`, and bundled screenshots can all stamp the same value.

---

## 5. Act path (`Resolver` + `Actuator`)

### 5.1 Resolver — mechanical, no guessing

Implements §10 resolution order against the **current** root (never a cached tree):

```kotlin
fun resolve(sel: Selector): Result<AccessibilityNodeInfo> = when {
    sel.ref != null ->
        if (sel.snapshotVersion == SnapshotClock.current) findByRef(sel.ref)   // else STALE
        else Result.failure(Stale)
    sel.viewId != null -> root.findAccessibilityNodeInfosByViewId(sel.viewId)
        .filterVisible().disambiguate(sel)            // AMBIGUOUS if >1 and no index/composite
    sel.text != null || sel.desc != null -> root.findByTextOrDesc(sel).disambiguate(sel)
    sel.bounds != null -> hitTest(sel.bounds)         // NOT_FOUND → caller may gesture the center
    else -> Result.failure(BadSelector)
}
```

`{ref,snapshotVersion}` is the tight, immediate-action path; anything older than the current version is rejected `STALE` rather than silently re-resolved — re-resolution policy belongs to the daemon.

### 5.2 Actuator

```kotlin
fun perform(node: AccessibilityNodeInfo?, a: Action): ActResult = when (a.kind) {
    CLICK        -> node!!.performAction(ACTION_CLICK)
    LONG_CLICK   -> node!!.performAction(ACTION_LONG_CLICK)
    SET_TEXT     -> node!!.performAction(ACTION_SET_TEXT, bundleOf(
                        ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE to a.text))
    SCROLL_FWD   -> node!!.performAction(ACTION_SCROLL_FORWARD)
    SCROLL_BACK  -> node!!.performAction(ACTION_SCROLL_BACKWARD)
    FOCUS        -> node!!.performAction(ACTION_FOCUS)
    GESTURE      -> dispatch(a.gesture)               // StrokeDescription(path, 0, durationMs)
    GLOBAL       -> performGlobalAction(a.global.toConst())
}
```

- A semantic action returning `false` (node not actionable) surfaces `NOT_ACTIONABLE`; the daemon decides whether to retry as `GESTURE`. The device does not auto-fall-back — that's policy.
- `dispatchGesture` builds a `GestureDescription` from the path; tap = single short stroke, swipe = timed multi-point stroke. Completion is reported via `GestureResultCallback`.
- `act` returns its ack **immediately** after performing; it does **not** await settle. Settle is a separate event stream (§6).

### 5.3 settleOverride wiring

If `act.args.settleOverride` is present, the router hands it to `SettleDetector.arm(overrideConfig)` *before* performing the action, so the debounce that the action triggers uses the override for exactly one settle cycle, then reverts to session config.

---

## 6. Settle detector (`SettleDetector`) — the one stateful primitive

A deterministic debounce state machine, **fully parameterized** by the active `SettleConfig`. It is the only device component that spans events, and it still holds *zero* policy — every threshold and mask is daemon-pushed.

```kotlin
class SettleDetector(private val scope: CoroutineScope, private val emit: (SettledEvent)->Unit) {
    @Volatile private var cfg: SettleConfig = SettleConfig.SAFE_DEFAULT
    private var quietJob: Job? = null
    private var hardJob: Job? = null
    private var qualifyingCount = 0
    private var armed = false

    fun applySession(c: SettleConfig) { cfg = c }          // from configure
    fun arm(override: SettleConfig? = null) {              // from act/waitForSettle
        cfg = override ?: sessionCfg
        armed = true; qualifyingCount = 0
        restartHardTimeout()
    }

    fun onEvent(e: AccessibilityEvent) {
        if (!armed) return
        if (e.packageName !in cfg.packageScope) return
        if (typeName(e.eventType) !in cfg.eventMask) return
        qualifyingCount++
        SnapshotClock.bump()                               // world changed → new version
        restartQuietTimer()
    }

    private fun restartQuietTimer() {
        quietJob?.cancel()
        quietJob = scope.launch {
            delay(cfg.quietWindowMs)
            if (cfg.mode == MIN_EVENTS_THEN_QUIET && qualifyingCount < cfg.minEventCount) return@launch
            fire("quiet")
        }
    }
    private fun restartHardTimeout() {
        hardJob?.cancel()
        hardJob = scope.launch { delay(cfg.hardTimeoutMs); fire("timeout") }
    }
    private fun fire(reason: String) {
        armed = false; quietJob?.cancel(); hardJob?.cancel()
        val v = SnapshotClock.current
        if (cfg.bundleScreenshotOnSettle) ScreenshotCapturer.captureBundled(v)
        emit(SettledEvent(reason, v, currentPkg))
    }
}
```

Key properties:

- **Atomic config swap:** `cfg` is `@Volatile`; `applySession`/`arm` publish a new immutable `SettleConfig`. The accessibility callback and the config write race only on a reference read, which is safe. (If stronger ordering is ever needed, post config swaps to the same single-thread dispatcher the callback pump uses.)
- **`eventMask` is where daemon control pays off:** on a looping-animation screen the daemon pushes `eventMask=[WINDOW_STATE_CHANGED]`, so endless `WINDOW_CONTENT_CHANGED` no longer resets quiet and the screen can settle.
- **`hardTimeoutMs`** guarantees liveness — a perpetually animating screen fires `timeout`, never hangs the daemon loop.
- **`waitForSettle`** calls `arm()` without acting; **`cancel`** disarms and resolves the pending request `CANCELED`.

---

## 7. Screenshot path (`ScreenshotCapturer` + `Redactor`)

```kotlin
suspend fun capture(req: ShotParams, version: Long?, reason: String): Result<Unit> {
    val res = takeScreenshotSuspend(Display.DEFAULT_DISPLAY)   // wraps takeScreenshot(exec, cb)
        ?: return Result.failure(RateLimited(retryAfterMs))    // ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT
    var bmp = Bitmap.wrapHardwareBuffer(res.hardwareBuffer, res.colorSpace)!!
    res.hardwareBuffer.close()
    bmp = Redactor.apply(bmp, session.get().redaction, lastSnapshot)   // §13, pre-compression
    bmp = downsample(bmp, req.maxDimension)                    // biggest bandwidth lever, on-device
    req.crop?.let { bmp = crop(bmp, it) }
    val bytes = encode(bmp, req.format, req.quality)           // webp lossy recommended
    val header = shotHeader(reason, req, version, bmp.width, bmp.height)
    ws.sendBinary(BinaryFrame.frame(header, bytes))
    return Result.success(Unit)
}
```

- **Rate limit is surfaced, never hidden.** On `null`/interval error → `RATE_LIMITED` + `retryAfterMs`. Only if the request set `allowCached:true` do we return the last encoded frame instead.
- **Redaction happens on the `Bitmap` before compression**, so masked pixels never leave the device. `Redactor` composites opaque rectangles over the `bounds` of `isPassword` nodes (from the tree captured at the same moment) plus any `extraMaskSelectors`. `FLAG_SECURE` windows are already black from the OS; if the whole frame is secured we can report `SECURE_WINDOW`.
- **Bundled captures** pass the observe/settle `version` so the binary header's `snapshotVersion` matches the tree — the atomicity guarantee set-of-marks depends on. "As atomic as the APIs allow" = capture screenshot and read tree back-to-back on the same dispatcher tick, before any new event bumps the clock.
- `takeScreenshot` unavailable (SDK<30) → capability advertised false in `hello`; daemon runs tree-only.

---

## 8. Command router & session state

```kotlin
suspend fun onText(frame: String) {
    val req = json.decodeFromString<Req>(frame)
    val res = try { when (req.verb) {
        "configure"     -> { session.swap(req.args); settle.applySession(session.get().settle); ok("applied" to true) }
        "observe"       -> observe(req.args)          // returns Snapshot; may emit bundled binary
        "act"           -> act(req.args)              // arm settle override, resolve, perform
        "screenshot"    -> { screenshot(req.args, req.id); ok() }
        "waitForSettle" -> waitForSettle(req.args, req.id)
        "cancel"        -> cancel(req.args.target)
        "ping"          -> ok("t" to now())
        else            -> err("INTERNAL", "unknown verb")
    }} catch (e: DeviceError) { err(e.code, e.message) }
    ws.sendText(res.withId(req.id))
}
```

- **`SessionConfig`** is an immutable data class held in an `AtomicReference`; `configure` swaps the whole thing (or merges provided sections). Everything downstream reads the current reference — no scattered mutable flags.
- **Safe defaults on cold connect:** empty `packageScope` (act on nothing), `maskPasswordNodes=true`, conservative `SettleConfig.SAFE_DEFAULT` (quiet=600, hardTimeout=6000, mask=[content,state]). Fail-closed until the daemon configures.

---

## 9. Lifecycle & failure

| condition | device behavior |
|-----------|-----------------|
| WS drops | backoff reconnect; on reopen emit `hello`, await `configure`; re-send current snapshot version context so daemon resyncs |
| Doze / half-open socket | pong watchdog forces close → reconnect |
| accessibility permission revoked | emit `bye{reason:permission_revoked}`, stop cleanly, foreground notification prompts user to re-enable |
| `pv` mismatch on `hello`/`configure` | emit `error{PROTOCOL_VERSION}`, close |
| screenshot interval error | `RATE_LIMITED` (per request) or `screenshot_error` event (async) |
| service process killed | foreground service + `START_STICKY` restart; AccessibilityService rebinds via OS |

**Non-negotiable:** the device never degrades to a *less safe* mode on failure (e.g. never disables redaction, never widens `packageScope`, never streams from secure windows) — it fails closed and waits for the daemon.

---

## 10. What is deliberately NOT here

To keep the invariant honest, the device does **not** contain: selector choice, stale/retry strategy, "wait longer on WebView" heuristics, set-of-mark drawing, screenshot cadence policy, any model call, any persistence of `ref`s across versions, or any decision that isn't a fixed mechanical transform. All of that is in `03-DAEMON_DESIGN.md`.
