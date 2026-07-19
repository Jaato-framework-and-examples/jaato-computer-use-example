# jaato-android-a11y-controller — the daemon-side "mind"

The Python controller half of **jaato-a11y-bridge**. The device
(`jaato-a11y-bridge`, a Kotlin `AccessibilityService`) is a dumb, configurable
mechanism that dials out over a WebSocket; this controller is the mind
(`03-DAEMON_DESIGN.md`): it grounds a vision LLM over a set-of-marks screenshot +
pruned node tree and drives the device through the `observe → plan → act →
settle` loop.

Built on the **jaato SDK** — the LLM rides the jaato daemon, and the device is
driven through client-provided (**host**) tools rather than a bespoke runtime.

## Architecture

```
 ┌───────────────── this process (run_controller.py) ─────────────────┐
 │                                                                    │
 │   IPCClient ──► jaato daemon ──► doubleword / Qwen3-VL              │
 │      ▲   │         (profile: a11y-controller, pass:// key)          │
 │      │   │                                                          │
 │  TURN_   │ register_client_tools([screen_tap, screen_type, ...])    │
 │  COMPLETED   send_message(tree + set-of-marks PNG)                  │
 │      │   ▼         (host-tool handler, async, same loop)            │
 │   Controller ──► DeviceSession (req/res mux, event pump, binary)    │
 │                        │                                            │
 └────────────────────────┼───────────────────────────────────────────┘
                          │  wss://…/a11y  (device dials IN)
                    ┌─────▼─────┐
                    │  Android  │  com.jaato.a11ybridge
                    │  device   │  (AccessibilityService)
                    └───────────┘
```

The loop is **computer-use**: each turn the controller pushes a set-of-marks image
+ tree, and the agent drives a full multi-action sequence — every `screen.*` tool
executes on the device (act → await settle → recover → re-observe) and returns the
*fresh* set-of-marks screenshot as its own tool result, so the model sees the effect
of each action and acts again within the same turn until the task is done. Images
reach the model both via `send_message(attachments=)` and as multimodal tool results
(base64-encoded to cross the IPC boundary as JSON).

All policy is daemon-side: selector grounding + recovery (`grounding.py`),
set-of-marks (`annotate.py`), settle authoring + adaptive tuning
(`settle_policy.py`), audit (`audit.py`). The device owns none of it.

## Layout

| path | role |
|------|------|
| `a11y/wire.py` | protocol version, error taxonomy, `DeviceError` (01 §3/§7) |
| `a11y/framing.py` | binary screenshot frame parsing (01 §4) |
| `a11y/protocol.py` | Snapshot/Node/Selector/SettleConfig/Action (01 §8–§11) |
| `a11y/device_session.py` | WS listener + `DeviceSession` req/res mux (03 §2) |
| `a11y/grounding.py` | ref → Selector + recovery (03 §4) |
| `a11y/annotate.py` | set-of-marks + tree text (03 §5) |
| `a11y/settle_policy.py` | SettleConfig authoring + tuner (03 §6) |
| `a11y/audit.py` | append-only action log (03 §9) |
| `a11y/controller.py` | the act→settle→reobserve cycle + loop state |
| `a11y/host_tools.py` | the `screen.*` tool surface (03 §8) |
| `a11y/config.py` | loads `.jaato/a11y-bridge.yaml` |
| `run_controller.py` | entrypoint: listener + jaato client + loop |

## Config

Two files, deliberately separate:

- **`.jaato/profiles/a11y-controller.yaml`** — the LLM (jaato profile):
  provider `doubleword`, model `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8`, key via
  `plugin_configs.doubleword.api_key: pass://jaato/doubleword/api-key`
  (resolved daemon-side; never a literal). No `.env`.
- **`.jaato/a11y-bridge.yaml`** — the device-facing listener: host/port/path,
  bearer auth, **`device.package_scope`** (REQUIRED non-empty — an empty scope
  keeps the device fail-closed), screenshot + redaction policy, loop bounds.

## Run

```bash
PY="$HOME/.local/share/jaato/venv/bin/python"   # your jaato daemon venv

# 1. set device.package_scope in .jaato/a11y-bridge.yaml (or pass --scope)
# 2. start the controller (autostarts its own daemon on a fresh socket):
$PY run_controller.py "Open Settings and turn Wi-Fi off" \
    --scope com.android.settings -v

# 3. in the device app: set Daemon URL = ws://<this-host>:8765/a11y, enable the
#    AccessibilityService. It dials in; the loop begins.
```

The audit trail lands in `.jaato/logs/a11y-audit.jsonl`.

## Verification status

- **Protocol/policy unit tests** — `pytest tests/` (28 tests: framing, snapshot,
  selector, settle, action, grounding, recovery, annotate, follow-foreground,
  viewport clip, reconnect recovery, single-device, STALE-retry).
- **Device-side end-to-end (mock device)** — `python tools/mock_device_smoke.py`:
  handshake + `pv`, configure, observe + binary-screenshot reunion, set-of-marks,
  act → settle → re-observe, SET_TEXT, audit trail.
- **jaato/LLM side** — `python tools/jaato_connect_smoke.py`: fresh-daemon
  autostart, `a11y-controller` profile + `pass://` key resolution, host-tool
  dispatch by the vision model.
- **Real hardware** — validated end-to-end: driven a physical Samsung tablet
  (Android 16 / sdk 36) through the computer-use loop over the live WS bridge —
  e.g. locating and opening an app nested inside a home-screen folder, each action
  feeding the fresh set-of-marks screenshot back to the model.

## v1 scope

- Vision grounding uses the bundled screenshot (`format: png`); webp is a later
  bandwidth optimisation once the loop is proven on hardware.
- Settle tuning is adaptive from `settled` telemetry; predicted-consequence
  overrides (`opens_webview`, …) are a follow-up.
- The agent's operating manual is sent as the first message; a `.jaato/agents/`
  persona is a later refinement.
