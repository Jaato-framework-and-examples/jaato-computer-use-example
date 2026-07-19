# controller — the LLM "mind"

The controller half of the bridge: it holds the model, plans against the observed tree and
screenshot, and drives the device over the wire in `../docs/01-PROTOCOL.md`.

**Landing separately.** It is developed alongside the device half and will be added here once its
current work is complete.

Until then, `../tools/e2e_daemon.py` is a minimal stand-in — enough of a "mind" to exercise every
verb and event on the wire by hand, but with no model and no planning.

## What belongs here

Everything the device deliberately refuses to do (see `../docs/02-DEVICE_DESIGN.md` §10):

- selector choice and stale/retry strategy
- set-of-marks annotation of the screenshot
- screenshot cadence policy
- "wait longer on this kind of screen" heuristics
- scope policy — which packages the device may observe and act on
- the model call itself, and the audit trail of every action taken
