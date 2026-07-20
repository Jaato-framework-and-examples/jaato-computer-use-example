"""WS listener + per-device session mux (03-DAEMON_DESIGN.md §2).

The device dials **out** to us (01 §2: it is behind NAT and sleeps), so the
controller is the WS *server*. :class:`BridgeServer` accepts the dial-in,
authenticates the bearer token (01 §13.1), reads ``hello``, checks ``pv``, and
hands a live :class:`DeviceSession` to the controller.

:class:`DeviceSession` is the request/response multiplexer: every outgoing
``req`` parks a Future resolved by the inbound pump on its matching ``res``;
unsolicited events (``settled``, ``window_changed`` …) route to queues/callbacks;
binary frames are demuxed and reunited with their tree by ``snapshotVersion``
(§2.1). Everything runs on the one asyncio loop that also drives the jaato
IPC client, so host-tool handlers can ``await session.observe()`` directly.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

import websockets

from .framing import BinaryFrame, parse_binary_frame
from .protocol import Action, Selector, SettleConfig, Snapshot, configure_args
from .wire import PV, DeviceError, ErrorCode

log = logging.getLogger("a11y.session")


@dataclass
class Observation:
    """A tree + the frame it describes, sharing one ``snapshotVersion`` (§2.1).

    ``image`` is the raw encoded screenshot bytes (png), or ``None`` when the
    observe was tree-only. Produced by :meth:`DeviceSession.observe`.
    """

    snapshot: Snapshot
    image: Optional[bytes] = None

    @property
    def version(self) -> int:
        return self.snapshot.version


@dataclass
class Settled:
    """A decoded ``settled`` event (01 §6.2)."""

    reason: str  # "quiet" | "timeout"
    version: int
    pkg: str
    has_bundled_screenshot: bool = False


class DeviceSession:
    """Request/response mux + event pump over one device WebSocket.

    Lifecycle: constructed by :class:`BridgeServer` after a validated ``hello``;
    lives until the socket closes (``bye`` / drop), at which point :meth:`pump`
    returns and :meth:`_fail_pending` rejects every in-flight request. There is
    at most one action outstanding at a time (the canonical loop is strictly
    ``act -> await_settled -> observe``), so ``settled`` events map 1:1 to acts
    and are delivered through a single queue.
    """

    #: Per-verb response timeouts (seconds). A screenshot-bearing observe waits
    #: longer because the device captures + downsamples pixels.
    _VERB_TIMEOUT = {"observe": 15.0, "screenshot": 15.0, "configure": 10.0,
                     "act": 10.0, "ping": 5.0, "waitForSettle": 30.0, "cancel": 5.0}

    def __init__(self, ws, hello: dict,
                 on_window_changed: Optional[Callable[[dict], None]] = None) -> None:
        self._ws = ws
        self.hello = hello
        self.device_id: str = hello.get("deviceId", "unknown")
        self.capabilities: dict = hello.get("capabilities", {})
        self.screen: dict = hello.get("screen", {})
        #: Platform the device declares in hello (e.g. "android" / "windows").
        #: The window-model dialect the ``windows`` verb speaks — and therefore how
        #: foreground-pick reads it (controller §9) — differs per platform. The
        #: daemon dispatches on this declared value rather than sniffing which
        #: fields a response happens to carry: the device is the source of truth
        #: for what it is, so it says so instead of us guessing.
        self.platform: str = hello.get("platform", "")
        self._on_window_changed = on_window_changed

        self._id = 0
        self._pending: Dict[str, asyncio.Future] = {}
        self._settled: "asyncio.Queue[Settled]" = asyncio.Queue()
        self._blobs: Dict[int, BinaryFrame] = {}
        self._blob_waiters: Dict[int, asyncio.Future] = {}
        self._alive = True
        #: Set from a ``bye`` event's reason. ``"user_disconnect"`` means the
        #: operator hit DISCONNECT on the device (intentional) — the controller
        #: must NOT auto-recover from that, vs a network drop where it should.
        self.bye_reason: Optional[str] = None
        self.current_snapshot: Optional[Snapshot] = None

    @property
    def alive(self) -> bool:
        """False once the socket has closed or a ``bye`` arrived."""
        return self._alive

    # -- outbound ------------------------------------------------------------
    def _next_id(self) -> str:
        self._id += 1
        return f"r-{self._id}"

    async def _send_text(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    async def request(self, verb: str, args: Optional[dict] = None) -> dict:
        """Send a ``req`` and await its ``res`` (01 §3). Raises
        :class:`DeviceError` on ``ok:false`` and on socket death."""
        if not self._alive:
            raise DeviceError(ErrorCode.INTERNAL, "device offline")
        rid = self._next_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        frame = {"kind": "req", "id": rid, "verb": verb, "pv": PV, "args": args or {}}
        await self._send_text(frame)
        try:
            res = await asyncio.wait_for(fut, timeout=self._VERB_TIMEOUT.get(verb, 10.0))
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise DeviceError(ErrorCode.TIMEOUT, f"{verb} timed out")
        if not res.get("ok"):
            raise DeviceError.from_wire(res.get("error", {}))
        return res.get("data", {})

    # -- high-level verbs ----------------------------------------------------
    async def configure(self, settle: SettleConfig, package_scope: List[str],
                        screenshot_defaults: dict, redaction: dict) -> None:
        await self.request("configure", configure_args(
            settle, package_scope, screenshot_defaults, redaction))

    async def observe(self, screenshot: bool = True,
                     screenshot_params: Optional[dict] = None) -> Observation:
        """Snapshot the tree (and, by default, the frame). The image arrives as a
        separate binary frame; it is reunited with the tree by ``snapshotVersion``
        (§2.1)."""
        args: dict = {"includeScreenshot": screenshot}
        if screenshot and screenshot_params:
            args["screenshot"] = screenshot_params
        data = await self.request("observe", args)
        snap = Snapshot.parse(data)
        self.current_snapshot = snap
        image = None
        if screenshot:
            frame = await self._await_blob(snap.version, timeout=5.0)
            image = frame.payload
        return Observation(snapshot=snap, image=image)

    async def act(self, selector: Selector, action: Action,
                 settle_override: Optional[SettleConfig] = None) -> dict:
        """Resolve ``selector`` and perform ``action`` (§5.3). Does not block on
        settle — the caller awaits :meth:`await_settled` next."""
        args = {"target": selector.to_wire()}
        action.merge_into(args)
        if settle_override is not None:
            args["settleOverride"] = settle_override.to_wire()
        return await self.request("act", args)

    async def await_settled(self, timeout: float) -> Settled:
        """Park on the next ``settled`` event (§6.2). The outer ``timeout`` guards
        a lost event; on expiry raises :class:`DeviceError` TIMEOUT so the
        controller re-observes rather than hanging."""
        try:
            return await asyncio.wait_for(self._settled.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise DeviceError(ErrorCode.TIMEOUT, "settled event not received")

    async def wait_for_settle(self, settle: Optional[SettleConfig] = None,
                             timeout: float = 30.0) -> Settled:
        """``waitForSettle`` verb (§5.5): run the settle detector without acting.
        Used e.g. after an externally-fired deep link."""
        args = {"settle": settle.to_wire()} if settle else {}
        data = await self.request("waitForSettle", args)
        return Settled(reason=data.get("reason", "quiet"),
                       version=int(data.get("snapshotVersion", 0)), pkg=data.get("pkg", ""))

    async def ping(self) -> dict:
        return await self.request("ping")

    # -- binary reunion ------------------------------------------------------
    async def _await_blob(self, snapshot_version: int, timeout: float) -> BinaryFrame:
        """Return the binary frame for ``snapshot_version`` — already buffered by
        the pump, or awaited if it hasn't landed yet (it may arrive just before
        or after the ``res``, §2.1)."""
        if snapshot_version in self._blobs:
            return self._blobs.pop(snapshot_version)
        fut = asyncio.get_event_loop().create_future()
        self._blob_waiters[snapshot_version] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._blob_waiters.pop(snapshot_version, None)
            raise DeviceError(ErrorCode.TIMEOUT,
                              f"screenshot blob v{snapshot_version} not received")

    # -- inbound pump --------------------------------------------------------
    async def pump(self) -> None:
        """Read the socket until it closes, routing frames. Returns when the
        device disconnects; :meth:`_fail_pending` then rejects in-flight requests."""
        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    self._handle_binary(bytes(msg))
                else:
                    self._handle_text(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._alive = False
            self._fail_pending()

    def _handle_text(self, msg: str) -> None:
        obj = json.loads(msg)
        kind = obj.get("kind")
        if kind == "res":
            fut = self._pending.pop(obj.get("id"), None)
            if fut and not fut.done():
                fut.set_result(obj)
        elif kind == "event":
            self._handle_event(obj.get("event"), obj.get("data", {}))
        else:
            log.warning("unknown frame kind=%r", kind)

    def _handle_event(self, event: str, data: dict) -> None:
        if event == "settled":
            self._settled.put_nowait(Settled(
                reason=data.get("reason", "quiet"),
                version=int(data.get("snapshotVersion", 0)),
                pkg=data.get("pkg", ""),
                has_bundled_screenshot=bool(data.get("hasBundledScreenshot")),
            ))
        elif event == "window_changed":
            if self._on_window_changed:
                self._on_window_changed(data)
        elif event == "screenshot_error":
            log.warning("screenshot_error %s", data)
        elif event == "error":
            log.error("device error event %s", data)
        elif event == "bye":
            self.bye_reason = data.get("reason")
            log.info("device bye: %s", self.bye_reason)
            self._alive = False
        else:
            log.info("event %s %s", event, data)

    def _handle_binary(self, data: bytes) -> None:
        frame = parse_binary_frame(data)
        version = frame.snapshot_version
        if version is None:
            log.warning("binary frame without snapshotVersion: %s", frame.header)
            return
        waiter = self._blob_waiters.pop(version, None)
        if waiter and not waiter.done():
            waiter.set_result(frame)
        else:
            self._blobs[version] = frame

    def _fail_pending(self) -> None:
        err = DeviceError(ErrorCode.INTERNAL, "device offline")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        for fut in self._blob_waiters.values():
            if not fut.done():
                fut.set_exception(err)
        self._blob_waiters.clear()


class BridgeServer:
    """The device-facing WS listener (03 §2). One device at a time (the tablet).

    Authenticates the bearer token on the upgrade (01 §13.1) unless explicitly
    run in unsafe no-auth mode, validates ``pv`` on ``hello``, and exposes the
    resulting :class:`DeviceSession` via :meth:`wait_for_device`.
    """

    def __init__(self, host: str, port: int, path: str,
                 token: Optional[str], unsafe_no_auth: bool = False,
                 on_window_changed: Optional[Callable[[dict], None]] = None) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._token = token
        self._unsafe_no_auth = unsafe_no_auth
        self._on_window_changed = on_window_changed
        self.session: Optional[DeviceSession] = None
        self._ready: asyncio.Event = asyncio.Event()
        self._server = None
        # First-wins single-device slot. Claimed synchronously in _on_connect
        # (before any await) so two near-simultaneous dial-ins can't both pass;
        # released in that method's finally on disconnect / failed handshake.
        self._connected = False

    async def start(self) -> None:
        # Keepalive tuned for a mobile client that may be briefly network-
        # throttled while backgrounded: the device already pings every 15s
        # (OkHttp), so the default 20s ping / 20s pong-timeout tears down a
        # throttled-but-alive socket. Ping less often and allow a generous pong
        # window; still finite so a genuinely dead peer is detected and the
        # first-wins slot frees for the reconnect (03 §2).
        self._server = await websockets.serve(
            self._on_connect, self._host, self._port, max_size=16 * 1024 * 1024,
            ping_interval=30, ping_timeout=60)
        log.info("a11y bridge listening on ws://%s:%d%s", self._host, self._port, self._path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def wait_for_device(self, timeout: Optional[float] = None) -> DeviceSession:
        """Block until a device has connected, authenticated, and sent ``hello``."""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        assert self.session is not None
        return self.session

    def _authenticate(self, ws) -> bool:
        if self._unsafe_no_auth:
            return True
        header = ws.request.headers.get("Authorization", "")
        query_token = None
        # Browsers can't set headers on new WebSocket(); allow ?token= (01 §13 note).
        if "?" in ws.request.path:
            from urllib.parse import parse_qs, urlparse
            query_token = parse_qs(urlparse(ws.request.path).query).get("token", [None])[0]
        presented = header[7:] if header.startswith("Bearer ") else query_token
        if not presented or self._token is None:
            return False
        return hmac.compare_digest(presented, self._token)

    async def _on_connect(self, ws) -> None:
        path = ws.request.path.split("?")[0]
        if path != self._path:
            log.warning("rejecting connection on path %r (want %r)", path, self._path)
            await ws.close(code=1008, reason="bad path")
            return
        if not self._authenticate(ws):
            log.warning("rejecting unauthenticated device connection")
            await ws.close(code=1008, reason="unauthorized")
            return
        # First-wins: one device at a time. Claim the slot synchronously (no await
        # between the check and the set) so a second, concurrent dial-in is turned
        # away rather than silently taking over the session we already serve.
        if self._connected:
            log.warning("rejecting a second device — one is already connected (first-wins)")
            await ws.close(code=1013, reason="bridge busy: a device is already connected")
            return
        self._connected = True
        try:
            try:
                hello = await self._read_hello(ws)
            except (websockets.ConnectionClosed, asyncio.TimeoutError, ValueError) as exc:
                log.warning("no valid hello: %s", exc)
                await ws.close(code=1002, reason="expected hello")
                return
            if int(hello.get("pv", 0)) != PV:
                log.error("pv mismatch: device pv=%s want %s", hello.get("pv"), PV)
                await ws.send(json.dumps({"kind": "event", "event": "error",
                                          "data": {"code": ErrorCode.PROTOCOL_VERSION,
                                                   "message": f"pv {hello.get('pv')} unsupported"}}))
                await ws.close(code=1002, reason="pv mismatch")
                return
            session = DeviceSession(ws, hello, on_window_changed=self._on_window_changed)
            self.session = session
            self._ready.set()
            log.info("device connected: %s platform=%s sdk=%s caps=%s",
                     session.device_id, session.platform or "<undeclared>",
                     hello.get("androidSdk"), session.capabilities)
            await session.pump()
        finally:
            # Socket closed / handshake failed — release the slot so a fresh
            # dial-in (or a legitimate reconnect of the same device) can take over.
            self._connected = False
            self.session = None
            self._ready.clear()

    async def _read_hello(self, ws) -> dict:
        """Read frames until the ``hello`` event arrives (01 §6.1)."""
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
            if isinstance(msg, (bytes, bytearray)):
                continue
            obj = json.loads(msg)
            if obj.get("kind") == "event" and obj.get("event") == "hello":
                return obj.get("data", {})
