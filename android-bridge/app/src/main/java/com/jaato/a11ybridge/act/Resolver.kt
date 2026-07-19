package com.jaato.a11ybridge.act

import android.view.accessibility.AccessibilityNodeInfo
import com.jaato.a11ybridge.observe.Nodes
import com.jaato.a11ybridge.observe.Pruner
import com.jaato.a11ybridge.observe.TreeWalker
import com.jaato.a11ybridge.state.SnapshotClock
import com.jaato.a11ybridge.transport.DeviceError
import com.jaato.a11ybridge.transport.ErrorCode
import com.jaato.a11ybridge.transport.Selector

/** The live node a selector resolved to, plus how it was matched (for the `act` ack §5.3). */
class ResolveResult(
    val node: AccessibilityNodeInfo,
    val matchedBy: String,
    val matchedRef: Int?,
)

/**
 * Resolves a [Selector] to a live node against the CURRENT tree (PROTOCOL §10).
 * Mechanical, no guessing: zero matches ⇒ NOT_FOUND, >1 with no disambiguator ⇒ AMBIGUOUS,
 * a `{ref}` from a stale version ⇒ STALE. The device never picks "the one it meant".
 *
 * Handle ownership: the returned [ResolveResult.node] is live and owned by the caller
 * (the actuator recycles it). Every other node touched during resolution is recycled here.
 */
class Resolver(private val walker: TreeWalker) {

    fun resolve(sel: Selector, scope: List<String>): ResolveResult = when {
        sel.ref != null -> resolveByRef(sel.ref, sel.snapshotVersion, scope)
        sel.viewId != null -> resolveByQuery(sel, scope, primaryBy = "viewId") { root ->
            root.findAccessibilityNodeInfosByViewId(sel.viewId)
        }
        sel.text != null || sel.desc != null -> {
            val key = sel.text ?: sel.desc!!
            val by = if (sel.text != null) "text" else "desc"
            resolveByQuery(sel, scope, primaryBy = by) { root ->
                root.findAccessibilityNodeInfosByText(key)
            }
        }
        sel.bounds != null -> resolveByBounds(sel.bounds, scope)
        else -> throw DeviceError(ErrorCode.INTERNAL, "empty selector")
    }

    // §10.1 — tight, immediate-action binding. Valid only against the current version.
    private fun resolveByRef(ref: Int, version: Long?, scope: List<String>): ResolveResult {
        if (version == null || version != SnapshotClock.current) {
            throw DeviceError(ErrorCode.STALE, "ref $ref references version $version, current=${SnapshotClock.current}")
        }
        val retain = HashMap<Int, AccessibilityNodeInfo>()
        val raw = walker.walkRaw(scope, retain).nodes
        val rawId = Pruner.keptRawIds(raw).getOrNull(ref)
        if (rawId == null) {
            Nodes.recycleAll(retain.values)
            throw DeviceError(ErrorCode.NOT_FOUND, "ref $ref not present on current tree")
        }
        val node = retain.remove(rawId)!!
        Nodes.recycleAll(retain.values)
        return ResolveResult(node, matchedBy = "ref", matchedRef = ref)
    }

    // §10.4 — last resort: deepest visible node whose bounds contain the target centre.
    private fun resolveByBounds(bounds: List<Int>, scope: List<String>): ResolveResult {
        val cx = (bounds[0] + bounds[2]) / 2
        val cy = (bounds[1] + bounds[3]) / 2
        val retain = HashMap<Int, AccessibilityNodeInfo>()
        val raw = walker.walkRaw(scope, retain).nodes
        var bestId = -1
        var bestArea = Long.MAX_VALUE
        for (n in raw) {
            if (!n.visible) continue
            val b = n.bounds
            if (cx in b[0]..b[2] && cy in b[1]..b[3]) {
                val area = (b[2] - b[0]).toLong() * (b[3] - b[1]).toLong()
                if (area < bestArea) { bestArea = area; bestId = n.id }
            }
        }
        if (bestId < 0) {
            Nodes.recycleAll(retain.values)
            // No node under the point. The daemon may re-issue as a GESTURE at the centre.
            throw DeviceError(ErrorCode.NOT_FOUND, "no node contains centre ($cx,$cy)")
        }
        val node = retain.remove(bestId)!!
        Nodes.recycleAll(retain.values)
        return ResolveResult(node, matchedBy = "bounds", matchedRef = null)
    }

    // §10.2/§10.3 — viewId or text/desc query, with composite filters + index disambiguation.
    private fun resolveByQuery(
        sel: Selector,
        scope: List<String>,
        primaryBy: String,
        query: (AccessibilityNodeInfo) -> List<AccessibilityNodeInfo>,
    ): ResolveResult {
        val roots = walker.roots(scope)
        try {
            val candidates = roots.flatMap(query)
            val filtered = candidates.filter { it.isVisibleToUser && matches(it, sel) }
            val chosen = when {
                filtered.isEmpty() ->
                    throw DeviceError(ErrorCode.NOT_FOUND, "selector matched 0 nodes")
                filtered.size == 1 -> filtered[0]
                sel.index != null -> filtered.getOrNull(sel.index)
                    ?: throw DeviceError(ErrorCode.NOT_FOUND, "index ${sel.index} out of ${filtered.size} matches")
                else -> throw DeviceError(ErrorCode.AMBIGUOUS, "selector matched ${filtered.size} nodes")
            }
            // Recycle every candidate except the chosen one.
            Nodes.recycleAll(candidates.filter { it !== chosen })
            return ResolveResult(chosen, matchedBy = primaryBy, matchedRef = null)
        } finally {
            Nodes.recycleAll(roots)
        }
    }

    /** Composite match: every present textual field must hold (§10 "all conditions"). */
    private fun matches(node: AccessibilityNodeInfo, sel: Selector): Boolean {
        sel.viewId?.let { if (node.viewIdResourceName != it) return false }
        sel.text?.let {
            if (!containsCi(node.text, it) && !containsCi(node.contentDescription, it)) return false
        }
        sel.desc?.let { if (!containsCi(node.contentDescription, it)) return false }
        return true
    }

    private fun containsCi(cs: CharSequence?, needle: String): Boolean =
        cs?.toString()?.contains(needle, ignoreCase = true) == true
}
