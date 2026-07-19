package com.jaato.a11ybridge

import com.jaato.a11ybridge.act.Actuator
import com.jaato.a11ybridge.act.Resolver
import com.jaato.a11ybridge.observe.Nodes
import com.jaato.a11ybridge.observe.TreeWalker
import com.jaato.a11ybridge.observe.WindowLister
import com.jaato.a11ybridge.settle.SettleDetector
import com.jaato.a11ybridge.shot.ScreenshotCapturer
import com.jaato.a11ybridge.state.SessionStore
import com.jaato.a11ybridge.state.SessionConfig
import com.jaato.a11ybridge.state.SnapshotClock
import com.jaato.a11ybridge.transport.ActArgs
import com.jaato.a11ybridge.transport.CancelArgs
import com.jaato.a11ybridge.transport.ConfigureArgs
import com.jaato.a11ybridge.transport.DeviceError
import com.jaato.a11ybridge.transport.ErrorCode
import com.jaato.a11ybridge.transport.ObserveArgs
import com.jaato.a11ybridge.transport.Req
import com.jaato.a11ybridge.transport.Res
import com.jaato.a11ybridge.transport.Screen
import com.jaato.a11ybridge.transport.ShotHeader
import com.jaato.a11ybridge.transport.ShotParams
import com.jaato.a11ybridge.transport.Snapshot
import com.jaato.a11ybridge.transport.WaitForSettleArgs
import com.jaato.a11ybridge.transport.Wire
import com.jaato.a11ybridge.transport.WindowsReport
import com.jaato.a11ybridge.transport.WsClient
import com.jaato.a11ybridge.transport.deviceEvent
import com.jaato.a11ybridge.transport.encode
import com.jaato.a11ybridge.transport.errRes
import com.jaato.a11ybridge.transport.okRes
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Turns inbound `req` frames into handler calls and emits `res`/`event` frames (device
 * design §8). All requests are drained by a SINGLE consumer coroutine, so handlers never
 * overlap — a natural ordering guarantee, and the reason handlers must never *block* on
 * settle (`waitForSettle` arms and returns; its response is sent later by the settle callback).
 *
 * The router holds no policy. Every decision is the daemon's, arriving as `configure`,
 * `act.target`, or a `SettleConfig`.
 */
class CommandRouter(
    private val scope: CoroutineScope,
    private val session: SessionStore,
    private val settle: SettleDetector,
    private val walker: TreeWalker,
    private val resolver: Resolver,
    private val actuator: Actuator,
    private val capturer: ScreenshotCapturer,
    private val windowLister: WindowLister,
    private val ws: WsClient,
    private val pkgProvider: () -> String?,
    private val activityProvider: () -> String?,
    private val screenProvider: () -> Screen,
    private val onConfigApplied: (SessionConfig) -> Unit,
) {
    private val inbox = Channel<String>(Channel.UNLIMITED)

    init {
        scope.launch { for (frame in inbox) handleFrame(frame) }
    }

    /** Enqueue an inbound text frame for ordered handling (called from the socket thread). */
    fun submit(frame: String) {
        inbox.trySend(frame)
    }

    /** Reset to fail-closed session state (on (re)connect, before the daemon configures). */
    fun resetSession() {
        session.reset()
        settle.disarm()
        settle.applySession(session.get().settle)
    }

    // -----------------------------------------------------------------------
    // Frame dispatch
    // -----------------------------------------------------------------------

    private suspend fun handleFrame(frame: String) {
        val req = try {
            Wire.json.decodeFromString(Req.serializer(), frame)
        } catch (e: Exception) {
            // Cannot correlate a malformed frame to an id — surface as a device error event.
            ws.sendText(
                deviceEvent(
                    "error",
                    buildJsonObject {
                        put("code", ErrorCode.INTERNAL)
                        put("message", "malformed frame: ${e.message}")
                    },
                ).encode(),
            )
            return
        }

        if (req.pv != null && req.pv != Wire.PV) {
            ws.sendText(errRes(req.id, ErrorCode.PROTOCOL_VERSION, "pv ${req.pv} unsupported").encode())
            return
        }

        val res: Res? = try {
            when (req.verb) {
                "configure" -> configure(req)
                "observe" -> observe(req)
                "windows" -> windows(req)
                "act" -> act(req)
                "screenshot" -> screenshot(req)
                "waitForSettle" -> { waitForSettle(req); null } // response is deferred to settle
                "cancel" -> cancel(req)
                "ping" -> okRes(req.id, buildJsonObject { put("t", System.currentTimeMillis()) })
                else -> errRes(req.id, ErrorCode.INTERNAL, "unknown verb ${req.verb}")
            }
        } catch (e: DeviceError) {
            errRes(req.id, e.code, e.message, e.retryAfterMs)
        } catch (e: Throwable) {
            errRes(req.id, ErrorCode.INTERNAL, e.message ?: "handler failure")
        }
        res?.let { ws.sendText(it.encode()) }
    }

    // -----------------------------------------------------------------------
    // Verb handlers (§5)
    // -----------------------------------------------------------------------

    private fun configure(req: Req): Res {
        val args = Wire.json.decodeFromJsonElement(ConfigureArgs.serializer(), req.args)
        val cfg = session.apply(args)
        settle.applySession(cfg.settle)
        onConfigApplied(cfg)
        return okRes(req.id, buildJsonObject { put("applied", true) })
    }

    private fun observe(req: Req): Res {
        val args = Wire.json.decodeFromJsonElement(ObserveArgs.serializer(), req.args)
        val cfg = session.get()
        val version = SnapshotClock.current
        val screenshotRef = if (args.includeScreenshot) req.id else null
        val snapshot = walker.snapshot(
            version = version,
            scope = cfg.packageScope,
            activity = activityProvider(),
            screen = screenProvider(),
            screenshotRef = screenshotRef,
        )
        if (args.includeScreenshot) {
            val params = args.screenshot ?: cfg.screenshotDefaults
            captureAsync(params, version, ShotHeader.REASON_BUNDLED, req.id)
        }
        return okRes(req.id, Wire.json.encodeToJsonElement(Snapshot.serializer(), snapshot))
    }

    /** `windows`: non-scope-gated window metadata + foreground/launcher resolution. */
    private fun windows(req: Req): Res {
        val report = windowLister.report(activityProvider())
        return okRes(req.id, Wire.json.encodeToJsonElement(WindowsReport.serializer(), report))
    }

    private fun act(req: Req): Res {
        val args = Wire.json.decodeFromJsonElement(ActArgs.serializer(), req.args)
        val pkgScope = session.get().packageScope
        val needsNode = args.action != "GESTURE" && args.action != "GLOBAL"
        val resolved = if (needsNode) resolver.resolve(args.target, pkgScope) else null

        // Arm settle BEFORE performing (device §5.3) so the debounce catches the action's
        // first event. settleOverride seeds this one cycle, then reverts to session config.
        settle.arm(args.settleOverride, requestId = null, onComplete = actSettle)
        try {
            actuator.perform(resolved?.node, args)
        } catch (e: Throwable) {
            settle.disarm() // no settled will fire for a failed action
            resolved?.let { Nodes.recycle(it.node) }
            throw e
        }
        resolved?.let { Nodes.recycle(it.node) }

        val data = buildJsonObject {
            put("resolved", true)
            resolved?.matchedRef?.let { put("matchedRef", it) }
            put("matchedBy", resolved?.matchedBy ?: args.action.lowercase())
            put("settleAwaited", false)
        }
        return okRes(req.id, data)
    }

    private suspend fun screenshot(req: Req): Res {
        val params = Wire.json.decodeFromJsonElement(ShotParams.serializer(), req.args)
        val result = capturer.capture(params, version = null, ShotHeader.REASON_ON_DEMAND, correlationId = req.id)
        return result.fold(
            onSuccess = { okRes(req.id) },
            onFailure = { e ->
                if (e is DeviceError) errRes(req.id, e.code, e.message, e.retryAfterMs)
                else errRes(req.id, ErrorCode.INTERNAL, e.message ?: "screenshot failed")
            },
        )
    }

    private fun waitForSettle(req: Req) {
        val args = Wire.json.decodeFromJsonElement(WaitForSettleArgs.serializer(), req.args)
        settle.arm(args.settle, requestId = req.id) { reason, version, bundle ->
            if (bundle) captureBundledOnSettle(version)
            val data = buildJsonObject {
                put("reason", reason)
                put("snapshotVersion", version)
            }
            ws.sendText(okRes(req.id, data).encode())
        }
    }

    private fun cancel(req: Req): Res {
        val args = Wire.json.decodeFromJsonElement(CancelArgs.serializer(), req.args)
        val canceled = settle.cancel(args.target)
        if (canceled) {
            ws.sendText(errRes(args.target, ErrorCode.CANCELED, "canceled by ${req.id}").encode())
        }
        return okRes(req.id, buildJsonObject { put("canceled", canceled) })
    }

    // -----------------------------------------------------------------------
    // Settle completion → `settled` event (§6.2)
    // -----------------------------------------------------------------------

    private val actSettle = SettleDetector.OnComplete { reason, version, bundle ->
        if (bundle) captureBundledOnSettle(version)
        val data = buildJsonObject {
            put("reason", reason)
            put("snapshotVersion", version)
            pkgProvider()?.let { put("pkg", it) }
            put("hasBundledScreenshot", bundle)
        }
        ws.sendText(deviceEvent("settled", data).encode())
    }

    private fun captureBundledOnSettle(version: Long) {
        captureAsync(session.get().screenshotDefaults, version, ShotHeader.REASON_BUNDLED, "v$version")
    }

    private fun captureAsync(params: ShotParams, version: Long?, reason: String, correlationId: String) {
        scope.launch {
            capturer.capture(params, version, reason, correlationId).onFailure { e ->
                if (e is DeviceError) {
                    ws.sendText(
                        deviceEvent(
                            "screenshot_error",
                            buildJsonObject {
                                put("code", e.code)
                                e.retryAfterMs?.let { put("retryAfterMs", it) }
                            },
                        ).encode(),
                    )
                }
            }
        }
    }
}
