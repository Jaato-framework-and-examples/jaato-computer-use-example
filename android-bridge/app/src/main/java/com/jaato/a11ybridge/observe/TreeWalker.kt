package com.jaato.a11ybridge.observe

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.jaato.a11ybridge.transport.Screen
import com.jaato.a11ybridge.transport.Snapshot

/** Raw (unpruned) walk result: the foreground package plus every visited node. */
data class RawTree(val pkg: String?, val nodes: List<RawNode>)

/**
 * Walks the live accessibility tree into plain [RawNode] POJOs (device design §4).
 *
 * The default walk copies each node's fields and recycles the live handle immediately
 * (pre-33), so no live [AccessibilityNodeInfo] escapes — the recycle discipline of §2.3
 * lives here. A `retain` walk instead keeps the live handles keyed by raw id, so the
 * resolver can map a `{ref}` or a `bounds` hit back to a live node to act on; the caller
 * then owns recycling everything it does not keep.
 *
 * Package scoping (§13 blast-radius limiter) is applied at the root: only windows whose
 * root belongs to an in-scope package are walked at all. Empty scope ⇒ nothing walked.
 */
class TreeWalker(private val service: AccessibilityService) {

    fun snapshot(
        version: Long,
        scope: List<String>,
        activity: String?,
        screen: Screen,
        screenshotRef: String?,
    ): Snapshot {
        val tree = walkRaw(scope)
        return Snapshot(
            snapshotVersion = version,
            pkg = tree.pkg,
            activity = activity,
            screen = screen,
            screenshotRef = screenshotRef,
            nodes = Pruner.prune(tree.nodes),
        )
    }

    /**
     * Pre-order raw walk of all in-scope windows.
     *
     * @param retain when non-null, live nodes are stored here by raw id and NOT recycled;
     *   the caller must recycle them. When null, nodes are recycled as they are copied.
     */
    fun walkRaw(
        scope: List<String>,
        retain: MutableMap<Int, AccessibilityNodeInfo>? = null,
    ): RawTree {
        val roots = roots(scope)
        val out = ArrayList<RawNode>()
        var pkg: String? = null
        for (root in roots) {
            if (pkg == null) pkg = root.packageName?.toString()
            walk(root, parentId = null, out = out, retain = retain)
            if (retain == null) Nodes.recycle(root)
        }
        return RawTree(pkg, out)
    }

    /** Live in-scope window roots. Caller owns recycling. */
    fun roots(scope: List<String>): List<AccessibilityNodeInfo> {
        val fromWindows = runCatching { service.windows }.getOrNull()
            ?.mapNotNull { it.root }
            ?: emptyList()
        val candidates = fromWindows.ifEmpty { listOfNotNull(service.rootInActiveWindow) }
        // Fail closed: with no scope, serialize / act on nothing.
        return candidates.filter { it.packageName?.toString() in scope }
    }

    private fun walk(
        node: AccessibilityNodeInfo,
        parentId: Int?,
        out: ArrayList<RawNode>,
        retain: MutableMap<Int, AccessibilityNodeInfo>?,
    ) {
        val id = out.size
        val r = Rect()
        node.getBoundsInScreen(r)
        // The action list ships with the already-fetched node, so this costs no extra IPC.
        val actions = node.actionList
        fun advertises(action: AccessibilityNodeInfo.AccessibilityAction): Boolean =
            actions.any { it.id == action.id }
        out.add(
            RawNode(
                id = id,
                parentId = parentId,
                cls = node.className?.toString() ?: "",
                viewId = node.viewIdResourceName,
                text = node.text?.toString(),
                desc = node.contentDescription?.toString(),
                bounds = intArrayOf(r.left, r.top, r.right, r.bottom),
                clickable = node.isClickable,
                longClickable = node.isLongClickable,
                scrollable = node.isScrollable,
                editable = node.isEditable,
                checkable = node.isCheckable,
                checked = node.isChecked,
                enabled = node.isEnabled,
                focusable = node.isFocusable,
                focused = node.isFocused,
                visible = node.isVisibleToUser,
                password = node.isPassword,
                selected = node.isSelected,
                scrollableDown = advertises(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN),
                scrollableUp = advertises(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP),
                scrollableLeft = advertises(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT),
                scrollableRight = advertises(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT),
            )
        )
        if (retain != null) retain[id] = node

        val count = node.childCount
        for (i in 0 until count) {
            val child = node.getChild(i) ?: continue
            walk(child, parentId = id, out = out, retain = retain)
            if (retain == null) Nodes.recycle(child)
        }
    }
}
