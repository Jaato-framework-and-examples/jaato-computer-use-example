package com.jaato.a11ybridge.observe

import com.jaato.a11ybridge.transport.NodeSnap

/**
 * A node captured verbatim from the live tree, before pruning. Plain data — the live
 * [android.view.accessibility.AccessibilityNodeInfo] is already recycled by the time a
 * RawNode exists (device design §2.3 recycle discipline).
 *
 * `id` is the node's pre-order index in the raw list; `parentId` is the raw parent's id.
 */
data class RawNode(
    val id: Int,
    val parentId: Int?,
    val cls: String,
    val viewId: String?,
    val text: String?,
    val desc: String?,
    val bounds: IntArray,      // [left, top, right, bottom]
    val clickable: Boolean,
    val longClickable: Boolean,
    val scrollable: Boolean,
    val editable: Boolean,
    val checkable: Boolean,
    val checked: Boolean,
    val enabled: Boolean,
    val focusable: Boolean,
    val focused: Boolean,
    val visible: Boolean,
    val password: Boolean,
    val selected: Boolean,
)

/**
 * The fixed, mechanical pruning transform (PROTOCOL §8). NOT policy.
 *
 * Contract: emit a node iff `visible ∧ (actionable ∨ text-bearing ∨ content-described)`.
 * Reparenting each kept node to its nearest kept ancestor is what collapses single-child
 * structural chains and drops pure layout containers — both fall out for free, no special
 * casing. Nothing screen-*semantic* lives here.
 */
object Pruner {

    /** Emission order of the compact flag set (§8). Absent flags are false. */
    private fun flagsOf(n: RawNode): List<String> = buildList {
        if (n.clickable) add("clickable")
        if (n.longClickable) add("longClickable")
        if (n.scrollable) add("scrollable")
        if (n.editable) add("editable")
        if (n.checkable) add("checkable")
        if (n.checked) add("checked")
        if (n.enabled) add("enabled")
        if (n.focusable) add("focusable")
        if (n.focused) add("focused")
        if (n.visible) add("visible")
        if (n.password) add("password")
        if (n.selected) add("selected")
    }

    private fun actionable(n: RawNode): Boolean =
        n.clickable || n.longClickable || n.scrollable || n.editable || n.checkable

    private fun keep(n: RawNode): Boolean =
        n.visible && (actionable(n) || !n.text.isNullOrBlank() || !n.desc.isNullOrBlank())

    /**
     * Raw ids of kept nodes, in ref order (pre-order). `ref` == index into this list.
     * Shared with the resolver so `{ref}` selectors reproduce exactly the ordering the
     * last `observe` published — one keep rule, no divergence.
     */
    fun keptRawIds(raw: List<RawNode>): List<Int> {
        val out = ArrayList<Int>()
        for (n in raw) if (keep(n)) out.add(n.id)
        return out
    }

    fun prune(raw: List<RawNode>): List<NodeSnap> {
        // Assign refs to kept nodes in pre-order. `ref` is unique within this version only.
        val refOf = HashMap<Int, Int>(raw.size)
        var nextRef = 0
        for (rawId in keptRawIds(raw)) refOf[rawId] = nextRef++

        val byId = HashMap<Int, RawNode>(raw.size)
        for (n in raw) byId[n.id] = n

        fun keptAncestorRef(n: RawNode): Int? {
            var pid = n.parentId
            while (pid != null) {
                val ancestor = byId[pid] ?: return null
                refOf[ancestor.id]?.let { return it }
                pid = ancestor.parentId
            }
            return null
        }

        val out = ArrayList<NodeSnap>(refOf.size)
        for (n in raw) {
            val ref = refOf[n.id] ?: continue
            out.add(
                NodeSnap(
                    ref = ref,
                    cls = n.cls,
                    viewId = n.viewId,
                    text = n.text,
                    desc = n.desc,
                    bounds = n.bounds.toList(),
                    flags = flagsOf(n),
                    parent = keptAncestorRef(n),
                )
            )
        }
        return out
    }
}
