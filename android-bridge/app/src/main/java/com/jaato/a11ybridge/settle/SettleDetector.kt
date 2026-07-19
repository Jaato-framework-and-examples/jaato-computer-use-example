package com.jaato.a11ybridge.settle

import android.view.accessibility.AccessibilityEvent
import com.jaato.a11ybridge.state.SnapshotClock
import com.jaato.a11ybridge.transport.SettleConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Maps `AccessibilityEvent` types to the wire strings of SettleConfig.eventMask (§9). */
object EventTypes {
    fun typeName(eventType: Int): String? = when (eventType) {
        AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED -> "WINDOW_CONTENT_CHANGED"
        AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> "WINDOW_STATE_CHANGED"
        AccessibilityEvent.TYPE_VIEW_SCROLLED -> "VIEW_SCROLLED"
        AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED -> "VIEW_TEXT_CHANGED"
        AccessibilityEvent.TYPE_VIEW_FOCUSED -> "VIEW_FOCUSED"
        else -> null
    }
}

/**
 * Deterministic debounce state machine (device design §6), the ONLY device component that
 * spans events — and it still holds zero policy. Every threshold and mask is daemon-pushed
 * via [SettleConfig]; the detector only counts, times, and fires.
 *
 * One settle cycle is active at a time. [arm] (re)starts it with a config and a completion
 * callback; the callback fires exactly once, with reason `quiet` or `timeout`. `waitForSettle`
 * arms with a `requestId` so [cancel] can abort that specific cycle.
 *
 * The completion callback must not block or re-enter the detector — it only enqueues an
 * outbound frame (and may kick off an async bundled capture).
 */
class SettleDetector(private val scope: CoroutineScope) {

    /**
     * Settle completion (§6.2). [bundleScreenshot] is the effective config's
     * `bundleScreenshotOnSettle` at fire time — so an override that turned bundling on/off
     * is honoured without the caller re-reading config.
     */
    fun interface OnComplete {
        operator fun invoke(reason: String, version: Long, bundleScreenshot: Boolean)
    }

    private val lock = Any()

    @Volatile
    private var sessionCfg: SettleConfig = SettleConfig.SAFE_DEFAULT

    // Guarded by [lock].
    private var cfg: SettleConfig = SettleConfig.SAFE_DEFAULT
    private var quietJob: Job? = null
    private var hardJob: Job? = null
    private var qualifyingCount = 0
    private var armed = false
    private var onComplete: OnComplete? = null
    private var armRequestId: String? = null

    /** Session default from `configure` (§5.1). Applies to future arms, not the active one. */
    fun applySession(c: SettleConfig) {
        sessionCfg = c
    }

    /**
     * Begin a settle cycle. [override] applies for this cycle only, then the next arm reverts
     * to the session default. [requestId] tags the cycle for `cancel` (waitForSettle only).
     */
    fun arm(override: SettleConfig?, requestId: String?, onComplete: OnComplete) {
        synchronized(lock) {
            cfg = override ?: sessionCfg
            this.onComplete = onComplete
            armRequestId = requestId
            armed = true
            qualifyingCount = 0
            restartHardTimeoutLocked()
            // No quiet timer yet: quiet is measured from the first qualifying event. If none
            // ever arrives, the hard timeout guarantees liveness (§9 mandatory upper bound).
        }
    }

    /** Feed one accessibility event. Filtered by the active config's scope + mask. */
    fun onEvent(event: AccessibilityEvent) {
        val pkg = event.packageName?.toString()
        val type = EventTypes.typeName(event.eventType)
        synchronized(lock) {
            if (!armed) return
            if (pkg == null || pkg !in cfg.packageScope) return
            if (type == null || type !in cfg.eventMask) return
            qualifyingCount++
            // NOTE: the world-version bump is NOT here — the clock must advance on every
            // in-scope change regardless of whether a settle cycle is armed, otherwise a
            // `{ref,snapshotVersion}` selector can look fresh after the tree moved. The
            // service's event pump owns SnapshotClock.bump() (see BridgeAccessibilityService).
            restartQuietTimerLocked()
        }
    }

    /** Abort the active cycle unconditionally, firing nothing (failed action / session reset). */
    fun disarm() {
        synchronized(lock) { disarmLocked() }
    }

    /** Abort the active cycle if it was armed by [requestId]. Returns true if it was. */
    fun cancel(requestId: String): Boolean {
        synchronized(lock) {
            if (armed && armRequestId == requestId) {
                disarmLocked()
                return true
            }
            return false
        }
    }

    private fun restartQuietTimerLocked() {
        quietJob?.cancel()
        quietJob = scope.launch {
            delay(cfg.quietWindowMs)
            fireIfReady("quiet")
        }
    }

    private fun restartHardTimeoutLocked() {
        hardJob?.cancel()
        hardJob = scope.launch {
            delay(cfg.hardTimeoutMs)
            fireIfReady("timeout")
        }
    }

    private fun fireIfReady(reason: String) {
        var cb: OnComplete? = null
        var version = 0L
        var bundle = false
        synchronized(lock) {
            if (!armed) return
            if (reason == "quiet" &&
                cfg.mode == SettleConfig.MODE_MIN_EVENTS_THEN_QUIET &&
                qualifyingCount < cfg.minEventCount
            ) {
                // Not enough qualifying events yet; keep waiting (hard timeout still guards).
                return
            }
            cb = onComplete
            version = SnapshotClock.current
            bundle = cfg.bundleScreenshotOnSettle
            disarmLocked()
        }
        cb?.invoke(reason, version, bundle)
    }

    private fun disarmLocked() {
        armed = false
        onComplete = null
        armRequestId = null
        quietJob?.cancel(); quietJob = null
        hardJob?.cancel(); hardJob = null
        qualifyingCount = 0
    }
}
