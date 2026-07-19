package com.jaato.a11ybridge.transport

import kotlinx.serialization.Serializable

/**
 * Protocol domain models (PROTOCOL §8–§11). Pure data; no Android types, no behaviour.
 * These are the payloads that ride inside [Req.args] / [Res.data].
 */

// ---------------------------------------------------------------------------
// §9 SettleConfig
// ---------------------------------------------------------------------------

@Serializable
data class SettleConfig(
    val quietWindowMs: Long = 500,
    val hardTimeoutMs: Long = 5000,
    val eventMask: List<String> = listOf("WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED"),
    val packageScope: List<String> = emptyList(),
    val mode: String = MODE_QUIET,
    val minEventCount: Int = 1,
    val bundleScreenshotOnSettle: Boolean = false,
) {
    companion object {
        const val MODE_QUIET = "quiet"
        const val MODE_MIN_EVENTS_THEN_QUIET = "minEventsThenQuiet"

        /**
         * Conservative fail-closed default used until the daemon issues `configure`
         * (device design §8). Content+state mask, generous quiet/timeout.
         */
        val SAFE_DEFAULT = SettleConfig(
            quietWindowMs = 600,
            hardTimeoutMs = 6000,
            eventMask = listOf("WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED"),
            packageScope = emptyList(),
            mode = MODE_QUIET,
            minEventCount = 1,
            bundleScreenshotOnSettle = false,
        )
    }
}

// ---------------------------------------------------------------------------
// §10 Selector
// ---------------------------------------------------------------------------

@Serializable
data class Selector(
    val viewId: String? = null,
    val text: String? = null,
    val desc: String? = null,
    val ref: Int? = null,
    val snapshotVersion: Long? = null,
    val bounds: List<Int>? = null,
    val index: Int? = null,
)

// ---------------------------------------------------------------------------
// §11 Action / Gesture / Global
// ---------------------------------------------------------------------------

/** A dispatch-gesture path (§11). `type` ∈ tap | swipe; path is a list of [x,y]. */
@Serializable
data class Gesture(
    val type: String,
    val path: List<List<Int>>,
    val durationMs: Long = 60,
)

// ---------------------------------------------------------------------------
// §12 Screenshot params
// ---------------------------------------------------------------------------

@Serializable
data class ShotParams(
    val format: String = "webp",
    val quality: Int = 80,
    val maxDimension: Int = 1280,
    val crop: List<Int>? = null,
    val allowCached: Boolean = false,
)

// ---------------------------------------------------------------------------
// §13 Redaction policy
// ---------------------------------------------------------------------------

@Serializable
data class RedactionPolicy(
    val maskPasswordNodes: Boolean = true,
    val extraMaskSelectors: List<Selector> = emptyList(),
) {
    companion object {
        /** Fail-closed default: password masking on. */
        val SAFE_DEFAULT = RedactionPolicy(maskPasswordNodes = true, extraMaskSelectors = emptyList())
    }
}

// ---------------------------------------------------------------------------
// §8 Snapshot / Node
// ---------------------------------------------------------------------------

@Serializable
data class Screen(
    val width: Int,
    val height: Int,
    val density: Double? = null,
)

@Serializable
data class NodeSnap(
    val ref: Int,
    val cls: String,
    val viewId: String? = null,
    val text: String? = null,
    val desc: String? = null,
    val bounds: List<Int>,
    val flags: List<String>,
    val parent: Int? = null,
)

@Serializable
data class Snapshot(
    val snapshotVersion: Long,
    val pkg: String? = null,
    val activity: String? = null,
    val screen: Screen,
    val screenshotRef: String? = null,
    val nodes: List<NodeSnap>,
)

// ---------------------------------------------------------------------------
// `windows` verb — non-scope-gated window metadata (protocol extension)
// ---------------------------------------------------------------------------

/**
 * One on-screen window's metadata. NAMES ONLY — no tree, no pixels — so it is reported
 * regardless of packageScope (the containment boundary is about content, not existence).
 * `focused` maps to `AccessibilityWindowInfo.isActive` (the foreground window).
 */
@Serializable
data class WindowInfo(
    val pkg: String? = null,
    val title: String? = null,
    val type: String,
    val focused: Boolean,
    val layer: Int,
)

/**
 * Result of the `windows` verb: what is on screen right now, plus the resolved foreground
 * and launcher packages. Solves the connect-time chicken-and-egg — the daemon learns the
 * foreground package to scope to, and the launcher package to navigate for app-hopping,
 * without a scoped observe or a hardcoded launcher name.
 */
@Serializable
data class WindowsReport(
    val foregroundPkg: String? = null,
    val foregroundActivity: String? = null,
    val launcherPkg: String? = null,
    val windows: List<WindowInfo>,
)

// ---------------------------------------------------------------------------
// Verb-specific arg payloads (§5)
// ---------------------------------------------------------------------------

@Serializable
data class ConfigureArgs(
    val settle: SettleConfig? = null,
    val screenshotDefaults: ShotParams? = null,
    val redaction: RedactionPolicy? = null,
    val packageScope: List<String>? = null,
)

@Serializable
data class ObserveArgs(
    val includeScreenshot: Boolean = false,
    val screenshot: ShotParams? = null,
)

@Serializable
data class ActArgs(
    val target: Selector,
    val action: String,
    val text: String? = null,
    val gesture: Gesture? = null,
    val global: String? = null,
    val settleOverride: SettleConfig? = null,
)

@Serializable
data class WaitForSettleArgs(
    val settle: SettleConfig? = null,
)

@Serializable
data class CancelArgs(
    val target: String,
)
