# jaato-computer-use — On-Device Design (Windows / C#)

**Implements:** `01-PROTOCOL.md`. Counterpart to `02-DEVICE_DESIGN.md` (Android). This document
does not restate envelopes or verb schemas; it describes how a Windows device fulfils that contract,
and — importantly — **where Windows forces the contract to bend**.

**Role:** identical to the Android half. A resident app whose only job is to be a *dumb, configurable
mechanism*: enumerate UI, execute controller-named actions, debounce events into `settled`, capture
pixels, and pipe it all over one outbound WebSocket. **No grounding policy, no heuristics, no model.**

**Status:** design only. Nothing here is implemented. Several claims are marked ⚠ **UNVALIDATED** —
see §12; they must be measured on real hardware before code is committed to them.

---

## 1. The one idea that changes everything

Android has *one* foreground app and *one* tree. Windows has **many overlapping top-level windows
across many processes**, and the full UIA desktop tree is enormous.

Nearly every divergence below follows from that single fact. Concretely:

- `observe` cannot mean "walk the screen" — it must name **one window**.
- `packageScope` becomes a **process** allowlist, and "what is on screen" is a *set*, not a singleton.
- The `windows` verb (added late to the Android build) is **not an extra here — it is the entry point**.

---

## 2. Module layout

Deliberately isomorphic to the Android layout, so the two halves stay comparable:

```
JaatoBridge/
 ├─ Program.cs                    // tray app entry, single-instance guard, session wiring
 ├─ Transport/
 │   ├─ WsClient.cs               // ClientWebSocket, reconnect, keepalive
 │   ├─ Envelope.cs               // System.Text.Json models for §3 messages
 │   └─ BinaryFrame.cs            // 4-byte BE len + JSON header + blob (§4)
 ├─ Observe/
 │   ├─ UiaSession.cs             // CUIAutomation8 lifetime, cache-request templates
 │   ├─ TreeWalker.cs             // IUIAutomationElement -> NodeSnap[] (bulk, cached)
 │   ├─ Pruner.cs                 // fixed pruning transform (§8) — shared contract w/ Android
 │   └─ WindowLister.cs           // top-level window enumeration, foreground + shell resolution
 ├─ Act/
 │   ├─ Resolver.cs               // Selector -> IUIAutomationElement, mechanical
 │   └─ Actuator.cs               // control patterns; SendInput only when explicitly asked
 ├─ Settle/
 │   └─ SettleDetector.cs         // same debounce state machine, fed by UIA events
 ├─ Shot/
 │   ├─ ScreenCapturer.cs         // Windows.Graphics.Capture (per-window / per-monitor)
 │   └─ Redactor.cs               // mask IsPassword bounds pre-compression
 ├─ State/
 │   ├─ SessionConfig.cs          // atomically swappable active config
 │   └─ SnapshotClock.cs          // process-wide world version
 └─ CommandRouter.cs              // req -> handler dispatch, res/event emission
```

Stack: **.NET 8, C#, raw `IUIAutomation` COM interop** (not a wrapper). Rationale in §4.2 — direct
control of `IUIAutomationCacheRequest` is the difference between a usable and an unusable device.

---

## 3. Hosting, lifecycle, privilege

### 3.1 It is a tray app, not a service

Android's `AccessibilityService` is a system-bound, user-granted component. Windows has **no
equivalent permission model** — a UIA client is just a process. Two hard constraints follow:

1. **Session 0 isolation.** A classic Windows Service runs in session 0 and **cannot see the user's
   desktop at all**. The bridge therefore runs **in the interactive user session** — a tray app,
   launched at logon (Startup shortcut or a scheduled task with *run only when user is logged on*).
2. **No permission prompt means no built-in consent moment.** The Android build got user consent for
   free via the accessibility toggle. Here, consent is implicit in running the binary, so the
   transparency burden shifts entirely onto us: a **persistent tray icon** whenever the bridge is
   live, plus the same explicit **CONNECT / DISCONNECT** kill switch, both mirroring the Android
   notification. This is not decoration — it is the only standing signal the user has.

Note what *disappears*: there is no Doze, no App Standby, no OEM battery management. The entire
Android foreground-service saga has no counterpart. Windows Modern Standby can suspend on sleep, but
it does not throttle a running foreground process's socket.

### 3.2 Elevation / UIPI — fail loudly, never silently

Default is **non-elevated**. A medium-integrity process **cannot** automate a higher-integrity
(elevated) window: UIA reads are refused, and `SendInput` to it is **silently discarded** by UIPI.
Silent discard is the dangerous part — the controller would see an action "succeed" and nothing happen.

So the device **detects and reports** rather than letting it fail quietly:

- On resolve/act against a window, compare the target process's integrity level to our own
  (`GetTokenInformation` / `TokenIntegrityLevel`).
- If the target is higher integrity → fail with **`PERMISSION`** (existing §7 code — "no (needs
  user)"), message naming elevation as the cause.

No new error code, no auto-elevation, no silent no-op. An elevated mode may be offered later as an
explicit opt-in; `uiAccess=true` (signed binary in Program Files) is the correct long-term answer and
is deliberately out of scope for a reference implementation.

---

## 4. Observe path

### 4.1 Window-targeted, not screen-wide

`observe` names **one window**. Omitting the target means *the foreground window*.

```
windows                      -> enumerate top-level windows (metadata only, non-scope-gated)
observe { window: <id> }     -> pruned tree of THAT window
observe { }                  -> pruned tree of the foreground window
```

This is the Android `windows`→scope→act pattern promoted to the primary navigation loop. It keeps
every walk bounded, which is what makes the performance problem tractable at all.

`window.id` is the **HWND** (as a stable integer for the window's lifetime), reported by `windows`.

### 4.2 Performance is the whole game

⚠ **UNVALIDATED — must be measured (§12).** Every UIA property read is a cross-process COM call.
A naive per-property walk of a large window is widely reported to take **seconds**. The mitigation is
non-optional:

- Build one `IUIAutomationCacheRequest` listing **every** property and pattern we need.
- Walk with **`FindAllBuildCache(TreeScope_Subtree, condition, cacheRequest)`** — one bulk call that
  returns the whole subtree with properties pre-fetched.
- Read only `Cached*` accessors afterwards. Touching a `Current*` accessor silently reintroduces a
  cross-process round trip per node and destroys the gain.

A second, free win Android does not have: **UIA already ships a pruned view.** Its *Content view*
filters out structural chrome. We use `ContentViewCondition` as the walk condition and apply our own
§8 prune on top, rather than pruning a raw tree ourselves.

### 4.3 Node mapping

| Protocol (§8) | Windows / UIA |
|---|---|
| `viewId` | `AutomationId` |
| `text` | `Name` (plus `ValuePattern.Value` for editable content) |
| `desc` | `HelpText` / `FullDescription` |
| `cls` | `ControlType` (+ `ClassName`, `FrameworkId`) |
| `bounds` | `BoundingRectangle` |
| `visible` | `!IsOffscreen` |
| `enabled` | `IsEnabled` |
| `focusable` / `focused` | `IsKeyboardFocusable` / `HasKeyboardFocus` |
| `password` | `IsPassword` — **maps exactly**, so redaction ports unchanged |
| `clickable` / `editable` / `scrollable` / `checkable` | derived from **supported patterns**: Invoke, Value, Scroll, Toggle |

**`ref` gets a better backing than on Android.** UIA provides `RuntimeId`, a transient identity stable
for the element's lifetime. `ref` remains a small integer scoped to one `snapshotVersion` (unchanged
on the wire), but the device keeps a `ref → RuntimeId` table for that version, so re-resolution is by
identity rather than by positional index. Same contract, sturdier mechanism.

The **pruning contract is unchanged and shared with Android** (§8): emit a node iff visible ∧
(actionable ∨ text-bearing ∨ described); collapse single-child chains; drop layout-only containers.

---

## 5. Act path

### 5.1 Resolver

Identical resolution order and identical failure semantics to Android (§10): `{ref,snapshotVersion}`
→ `viewId` → `text`/`desc` → `bounds`. Zero matches `NOT_FOUND`, multiple without a disambiguator
`AMBIGUOUS`, stale version `STALE`. Resolution runs against the **current** tree of the target window.
The device never guesses which match was meant.

### 5.2 Actuator — patterns first, synthetic input only on request

| Action | Windows |
|---|---|
| `CLICK` | `InvokePattern.Invoke()` (fallback `LegacyIAccessiblePattern.DoDefaultAction`) |
| `LONG_CLICK` | no pattern equivalent → `SendInput` press-hold-release |
| `SET_TEXT` | `ValuePattern.SetValue()` |
| `SCROLL_DOWN/UP/LEFT/RIGHT` | `ScrollPattern.Scroll(vertical, horizontal)` with explicit amounts |
| `SCROLL_FORWARD/BACKWARD` | `ScrollPattern` large-increment (kept for parity; ambiguous by nature) |
| `FOCUS` | `IUIAutomationElement.SetFocus()` |
| `GESTURE` | `SendInput` mouse path |
| `GLOBAL` | see §9 — the Android set does not exist here |

**A real behavioural divergence to state plainly:** `SendInput` **moves the physical cursor and
steals real input**. Android's `dispatchGesture` did not. So on Windows the gesture fallback is
user-visible and racy against a human at the keyboard. This makes the existing "prefer semantic
actions" rule stronger here than on Android — pattern-based actions touch no cursor at all.

Consistent with the Android half: a pattern that is unsupported or returns failure surfaces
**`NOT_ACTIONABLE`**, and the device **does not** silently fall back to `SendInput`. That is the
controller's decision, exactly as before. Where the platform lets us distinguish *unsupported* from
*supported-but-refused* (e.g. a scroll already at its extent), we report which — same discipline
adopted for directional scroll on Android.

---

## 6. Settle detector

The state machine is **unchanged** — same debounce, same `SettleConfig`, same `quiet` / `timeout`
outcomes, still the only stateful component and still holding zero policy. Only the event source changes.

| `eventMask` value | UIA source |
|---|---|
| `WINDOW_CONTENT_CHANGED` | `AddStructureChangedEventHandler` |
| `WINDOW_STATE_CHANGED` | `UIA_Window_WindowOpenedEventId` / `WindowClosedEventId` |
| `VIEW_SCROLLED` | property-changed on `ScrollPattern` scroll percent |
| `VIEW_TEXT_CHANGED` | property-changed on `ValuePattern.Value` |
| `VIEW_FOCUSED` | `AddFocusChangedEventHandler` |

Two Windows-specific hazards:

1. **Subscription cost.** Broad UIA event subscriptions are expensive and can measurably slow the
   *target* app. Subscriptions are therefore **scoped to in-scope windows**, never desktop-wide, and
   torn down when scope changes.
2. **Threading.** UIA event callbacks arrive on RPC/MTA threads, not a UI thread. The client runs MTA
   and marshals callbacks onto the single background pump — the same "snapshot fast, hand off"
   discipline as the Android main thread rule, for a different underlying reason.

---

## 7. Screenshot path

**Windows.Graphics.Capture (WGC)**, via `IGraphicsCaptureItemInterop.CreateForWindow(hwnd)` or
`CreateForMonitor`, into a `Direct3D11CaptureFramePool`.

- **Per-window capture is the default**, matching the window-targeted `observe`. Per-monitor is
  available for whole-screen grabs.
- **Rate limiting largely disappears.** There is no ~1 fps cap as on Android. `RATE_LIMITED` stays in
  the taxonomy but should be rare; the device still never sleeps-and-retries.
- **Redaction is unchanged**: composite opaque rects over `IsPassword` element bounds on the
  full-resolution frame **before** encoding, then crop, then downsample. `IsPassword` maps exactly,
  so this ports as-is.
- **`WDA_EXCLUDEFROMCAPTURE`** is the `FLAG_SECURE` analogue — windows that opt out are excluded by
  the OS, self-censoring for free.
- If WGC is unavailable, `hello.capabilities.takeScreenshot` reports **false** and the controller runs
  tree-only. No silent degradation to a lesser API.

⚠ **UNVALIDATED:** WGC historically draws a coloured border around captured windows;
`GraphicsCaptureSession.IsBorderRequired = false` exists on newer builds but availability varies.
Needs checking (§12) — a permanent border on every captured window is a real UX problem.

---

## 8. Session state & router

Unchanged in shape: an immutable `SessionConfig` in an atomic reference, swapped whole by `configure`;
a single-consumer command queue so handlers never overlap; `waitForSettle` arms and returns rather
than blocking. Fail-closed defaults are identical — **empty scope observes and acts on nothing**,
password masking on, conservative settle.

**Scope identity** is the one substantive change. `packageScope` entries are matched against the
target window's process:

- primary: **full executable path**, case-insensitive (precise, hard to spoof)
- convenience: bare **process name** (`notepad.exe`)

⚠ **UNVALIDATED:** UWP/Store apps run under shared host processes, so exe path is not always
discriminating; those may need `AUMID` / package family name. To be confirmed against a real Store app.

---

## 9. Protocol deltas

Everything else on the wire is untouched. These are the only changes, listed so the controller can
mirror them exactly:

| Change | Detail |
|---|---|
| `observe` gains `window` | optional; omitted = foreground window |
| `Snapshot` gains `window` | `{id, title, processId, exePath}` — required on a multi-window desktop |
| `pkg` semantics | now "process identity" (exe path), not an Android package name |
| `viewId` semantics | now `AutomationId` |
| `global` action set | Android's `BACK`/`HOME`/`RECENTS` do not exist. Windows set: `MINIMIZE_ALL`, `SHOW_DESKTOP`, `SWITCH_WINDOW`, `CLOSE_WINDOW`, `LOCK_SCREEN` |
| `eventMask` values | same names, remapped to UIA sources (§6) |
| elevation blocked | reported as existing `PERMISSION` — no new code |
| `hello.capabilities` | adds `canCaptureWindow`, `isElevated`, `uiAccess` |

No new verbs. No new error codes. The controller's planning loop is unchanged.

---

## 10. Lifecycle & failure

| Condition | Behaviour |
|---|---|
| WS drops | backoff reconnect; on reopen emit `hello`, await `configure`; no state survives |
| Machine sleep / resume | socket dies, reconnect on wake (same as Android lid-close) |
| Session lock / unlock | UI still enumerable while locked is **not** assumed — ⚠ validate (§12) |
| Target window closes mid-op | `NOT_FOUND` / `STALE`, never a crash |
| Target is elevated | `PERMISSION`, named explicitly |
| UIA call hangs | every UIA call is time-boxed; expiry → `TIMEOUT` rather than a wedged pump |

**Non-negotiable, unchanged:** the device never degrades to a *less safe* mode — never disables
redaction, never widens scope, never captures an excluded window, never substitutes synthetic input
for a refused semantic action.

---

## 11. What is deliberately NOT here

Same list as Android §10, and for the same reason: selector choice, stale/retry strategy, set-of-mark
drawing, screenshot cadence, "wait longer on this kind of window" heuristics, any model call, any
persistence of `ref`s across versions, and — new here — **which window to look at**. Window choice is
navigation, and navigation is the controller's job.

---

## 12. Open questions — measure before building

This design is written from API knowledge, not observation. The Windows host was unreachable at
authoring time. The following must be settled empirically **before** committing to the design, because
each one can invalidate part of it:

1. **UIA walk latency** (§4.2) — time `FindAllBuildCache` over a realistic window (Explorer, a browser,
   Settings) at several subtree sizes. *If cached bulk walks are still seconds-slow, the
   window-targeted model is not enough and we need incremental/lazy subtree fetching.* This is the
   single highest-risk assumption.
2. **Cache completeness** — confirm every property/pattern we need is actually cacheable, and that no
   `Current*` access sneaks into the hot path.
3. **WGC border** (§7) — does `IsBorderRequired = false` work on the target build?
4. **UWP/Store scope identity** (§8) — is exe path discriminating, or is AUMID required?
5. **UIPI behaviour** (§3.2) — confirm reads *and* input are both refused against an elevated window,
   and that integrity comparison detects it reliably.
6. **Locked session** (§10) — what, if anything, is enumerable while the workstation is locked.
7. **Event subscription cost** (§6) — measure the slowdown imposed on a target app by our handlers.

Recommended first step is a **throwaway spike** covering (1) and (3) only. Those two carry most of the
risk; the rest of the design is a faithful re-expression of a contract already proven on Android.
