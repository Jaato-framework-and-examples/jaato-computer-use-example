"""jaato-a11y-bridge — daemon-side controller ("the mind").

The device (`02-DEVICE_DESIGN.md`, Kotlin AccessibilityService) is a dumb,
configurable mechanism that dials out over a WebSocket. This package is the
counterpart daemon described by `03-DAEMON_DESIGN.md`, re-cast to ride jaato's
SDK: the WS listener + request/response mux live in the client process, and the
agent drives the device through client-provided ``screen.*`` host tools while
all grounding / settle / recovery / annotation policy stays here.

Module map (mirrors 03 §1):

- ``wire``           protocol version, error taxonomy, ``DeviceError`` (01 §3/§7)
- ``framing``        binary frame parse (01 §4)
- ``protocol``       Snapshot/Node/Selector/SettleConfig/Action (01 §8-§11)
- ``device_session`` WS listener + ``DeviceSession`` mux (03 §2)
- ``grounding``      ref -> Selector policy + recovery (03 §4)
- ``annotate``       set-of-marks compositing (03 §5)
- ``settle_policy``  SettleConfig authoring (03 §6)
- ``audit``          append-only action log (03 §9)
- ``host_tools``     the ``screen.*`` tool surface bound to a session (03 §8)
"""
