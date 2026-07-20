package com.jaato.a11ybridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import com.jaato.a11ybridge.act.Actuator
import com.jaato.a11ybridge.act.Resolver
import com.jaato.a11ybridge.observe.TreeWalker
import com.jaato.a11ybridge.observe.WindowLister
import com.jaato.a11ybridge.settle.SettleDetector
import com.jaato.a11ybridge.shot.ScreenshotCapturer
import com.jaato.a11ybridge.state.BridgeStatus
import com.jaato.a11ybridge.state.Prefs
import com.jaato.a11ybridge.state.SessionConfig
import com.jaato.a11ybridge.state.SessionStore
import com.jaato.a11ybridge.state.SnapshotClock
import com.jaato.a11ybridge.transport.DaemonConfig
import com.jaato.a11ybridge.transport.Screen
import com.jaato.a11ybridge.transport.Wire
import com.jaato.a11ybridge.transport.WsClient
import com.jaato.a11ybridge.transport.deviceEvent
import com.jaato.a11ybridge.transport.encode
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * AccessibilityService entry point and event pump (device design §2).
 *
 * Per the chosen single-service architecture, this one service hosts everything: the event
 * pump, the transport, and the command router. It contains NO grounding policy, NO screen
 * heuristics — every judgment arrives as data from the daemon.
 *
 * Threading (§2.3): accessibility callbacks land on the main thread. We read the event's
 * fields synchronously (the framework recycles the event after the callback) and hand off
 * all serialization / socket work to a background coroutine scope.
 */
class BridgeAccessibilityService : AccessibilityService(), WsClient.Listener {

    private lateinit var scope: CoroutineScope
    private lateinit var session: SessionStore
    private lateinit var settle: SettleDetector
    private lateinit var walker: TreeWalker
    private lateinit var resolver: Resolver
    private lateinit var actuator: Actuator
    private lateinit var capturer: ScreenshotCapturer
    private lateinit var windowLister: WindowLister
    private lateinit var ws: WsClient
    private lateinit var router: CommandRouter

    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile private var currentPkg: String? = null
    @Volatile private var currentActivity: String? = null

    /** Last foreground application package emitted via window_changed (dedupe for option b). */
    private val lastForegroundAppPkg = java.util.concurrent.atomic.AtomicReference<String?>(null)

    override fun onServiceConnected() {
        super.onServiceConnected()
        if (::router.isInitialized) return // already wired (reconnect / re-enable)

        scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        session = SessionStore()
        settle = SettleDetector(scope)
        walker = TreeWalker(this)
        resolver = Resolver(walker)
        actuator = Actuator(this)
        ws = WsClient(scope, ::daemonConfig, this)
        capturer = ScreenshotCapturer(this, walker, resolver, session, ws::sendBinary)
        windowLister = WindowLister(this)
        router = CommandRouter(
            scope = scope,
            session = session,
            settle = settle,
            walker = walker,
            resolver = resolver,
            actuator = actuator,
            capturer = capturer,
            windowLister = windowLister,
            ws = ws,
            pkgProvider = { currentPkg },
            activityProvider = { currentActivity },
            screenProvider = ::screen,
            onConfigApplied = ::onConfigApplied,
        )

        // Software-only scoping: receive events/windows for ALL packages (packageNames=null)
        // and enforce packageScope in our own code (TreeWalker/Resolver/settle/clock). This is
        // what lets the non-scope-gated `windows` verb see out-of-scope windows. Fail-closed is
        // unchanged — an empty scope still serializes and acts on nothing (enforced in software).
        clearOsPackageFilter()
        // Pin the process at foreground importance so the socket survives app-switches.
        BridgeForegroundService.start(this)
        instance = this

        // Honour the operator's desired-connection flag (CONNECT/DISCONNECT buttons).
        if (Prefs.connectDesired(this)) {
            BridgeStatus.set(BridgeStatus.Conn.CONNECTING)
            ws.start()
        } else {
            BridgeStatus.set(BridgeStatus.Conn.DISCONNECTED)
        }
    }

    /**
     * Apply the persisted CONNECT/DISCONNECT desire. Called by [MainActivity] when the operator
     * taps a button; also honoured at service start. DISCONNECT is a persistent kill switch.
     */
    fun applyConnectionDesire() {
        if (!::ws.isInitialized) return
        if (Prefs.connectDesired(this)) {
            BridgeStatus.set(BridgeStatus.Conn.CONNECTING)
            ws.start()
        } else {
            ws.stop(byeFrame = byeEvent("user_disconnect"))
            BridgeStatus.set(BridgeStatus.Conn.DISCONNECTED)
        }
    }

    // -----------------------------------------------------------------------
    // Event pump (§2.3)
    // -----------------------------------------------------------------------

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        if (!::settle.isInitialized) return

        // World clock: any in-scope accessibility event means the serialized tree could now
        // differ, so advance the version unconditionally (independent of settle arming). This
        // is what makes `{ref,snapshotVersion}` staleness detection sound — over-bumping is
        // safe (refs go STALE eagerly); under-bumping would let a moved tree look fresh.
        val evPkg = event.packageName?.toString()
        if (evPkg != null && evPkg in session.get().packageScope) {
            SnapshotClock.bump()
        }

        settle.onEvent(event) // filtered by the active SettleConfig scope + mask

        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            // Grab event fields synchronously (the framework recycles the event after this
            // callback), then resolve the true foreground app off the main thread.
            val evCls = event.className?.toString()
            scope.launch { onWindowStateChanged(evPkg, evCls) }
        }
    }

    /**
     * `window_changed` option (b): fire only when the foreground APPLICATION package changes,
     * NON-scope-gated (metadata flows freely). Deduped via [lastForegroundAppPkg] so IME /
     * dialog / systemui state changes — which never change the foreground app — produce no
     * event. This is what the daemon follows to auto-re-scope on an app hop.
     */
    private fun onWindowStateChanged(evPkg: String?, evCls: String?) {
        val fgPkg = windowLister.foregroundAppPkg() ?: return

        // Track foreground identity for observe.activity + the `windows` verb. Only trust the
        // activity class when this event actually came from the foreground app (not an IME etc.).
        currentPkg = fgPkg
        if (evPkg == fgPkg && evCls != null) currentActivity = "$fgPkg/$evCls"

        // Atomic dedupe: emit exactly once per foreground-app transition.
        val prev = lastForegroundAppPkg.getAndSet(fgPkg)
        if (fgPkg != prev) {
            val version = SnapshotClock.current
            val activity = currentActivity
            ws.sendText(
                deviceEvent(
                    "window_changed",
                    buildJsonObject {
                        put("pkg", fgPkg)
                        activity?.let { put("activity", it) }
                        put("snapshotVersion", version)
                    },
                ).encode(),
            )
        }
    }

    override fun onInterrupt() {
        // Required override. Nothing to interrupt: the device holds no cross-message state.
    }

    // -----------------------------------------------------------------------
    // WsClient.Listener
    // -----------------------------------------------------------------------

    override fun onConnected() {
        // No daemon state survives a reconnect: reset to safe defaults, re-send hello, wait
        // for the daemon to re-configure (device design §3 / §9).
        router.resetSession()
        ws.sendText(helloEvent())
        BridgeStatus.set(BridgeStatus.Conn.CONNECTED)
    }

    override fun onText(frame: String) {
        router.submit(frame)
    }

    override fun onDisconnected(reason: String) {
        router.resetSession()
        // The socket auto-retries with backoff, so we are between attempts, not dead.
        BridgeStatus.set(BridgeStatus.Conn.CONNECTING)
    }

    // -----------------------------------------------------------------------
    // Config / capability wiring
    // -----------------------------------------------------------------------

    private fun daemonConfig(): DaemonConfig? {
        val url = Prefs.daemonUrl(this)
        val token = Prefs.token(this)
        if (url.isBlank() || token.isBlank()) return null
        return DaemonConfig(url, token)
    }

    /**
     * Scope is enforced entirely in software (TreeWalker/Resolver/settle/clock read the
     * current SessionConfig), so a `configure` needs no OS-level reapply. Kept as the wired
     * callback for symmetry / future use.
     */
    private fun onConfigApplied(cfg: SessionConfig) {
        // no-op: software-only scoping
    }

    /** Deliver events/windows for ALL packages; scope is enforced in software (see §onServiceConnected). */
    private fun clearOsPackageFilter() {
        mainHandler.post {
            val info = serviceInfo ?: return@post
            info.packageNames = null
            serviceInfo = info
        }
    }

    private fun screen(): Screen {
        val m = resources.displayMetrics
        return Screen(width = m.widthPixels, height = m.heightPixels, density = m.density.toDouble())
    }

    private fun helloEvent(): String {
        val info = serviceInfo
        val caps = info?.capabilities ?: 0
        val flags = info?.flags ?: 0
        val s = screen()
        val data = buildJsonObject {
            put("pv", Wire.PV)
            // The device DECLARES its platform so the controller routes to the right window
            // model (Android tree vs Windows window-set) instead of sniffing which fields a
            // response carries to GUESS it. One of: "android" | "windows".
            put("platform", "android")
            put("deviceId", Prefs.deviceId(this@BridgeAccessibilityService))
            put("androidSdk", Build.VERSION.SDK_INT)
            put(
                "capabilities",
                buildJsonObject {
                    put("takeScreenshot", Build.VERSION.SDK_INT >= Build.VERSION_CODES.R)
                    put("canPerformGestures", caps and AccessibilityServiceInfo.CAPABILITY_CAN_PERFORM_GESTURES != 0)
                    put("reportViewIds", flags and AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS != 0)
                    put("retrieveInteractiveWindows", flags and AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS != 0)
                },
            )
            put(
                "screen",
                buildJsonObject {
                    put("width", s.width)
                    put("height", s.height)
                    put("density", s.density ?: 0.0)
                },
            )
        }
        return deviceEvent("hello", data).encode()
    }

    // -----------------------------------------------------------------------
    // Lifecycle
    // -----------------------------------------------------------------------

    override fun onUnbind(intent: Intent?): Boolean {
        // Permission revoked / service stopped: fail closed with a bye, never a degraded mode.
        if (::ws.isInitialized) {
            ws.stop(byeFrame = byeEvent("service_stopped"))
        }
        teardown()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        teardown()
        super.onDestroy()
    }

    private fun teardown() {
        if (instance === this) instance = null
        if (::scope.isInitialized) {
            runCatching { scope.cancel() }
        }
        BridgeStatus.set(BridgeStatus.Conn.DISCONNECTED)
        runCatching { BridgeForegroundService.stop(this) }
    }

    private fun byeEvent(reason: String): String =
        deviceEvent("bye", buildJsonObject { put("reason", reason) }).encode()

    // -----------------------------------------------------------------------
    // Status notification (transparency + re-enable prompt)
    // -----------------------------------------------------------------------

    // The ongoing notification now belongs to BridgeForegroundService (one notification, and it
    // is the thing that actually grants foreground importance).

    companion object {
        /** Live service instance so [MainActivity] can signal CONNECT/DISCONNECT. Null when not running. */
        @Volatile
        var instance: BridgeAccessibilityService? = null
            private set
    }
}
