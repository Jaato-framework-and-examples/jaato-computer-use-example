package com.jaato.a11ybridge

import com.jaato.a11ybridge.observe.Pruner
import com.jaato.a11ybridge.observe.RawNode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The pruning transform is correctness-critical: `ref` ordering and reparenting are what
 * set-of-marks annotation on the daemon depends on (PROTOCOL §8).
 */
class PrunerTest {

    private fun node(
        id: Int,
        parentId: Int?,
        cls: String = "android.view.View",
        viewId: String? = null,
        text: String? = null,
        desc: String? = null,
        visible: Boolean = true,
        clickable: Boolean = false,
    ) = RawNode(
        id = id, parentId = parentId, cls = cls, viewId = viewId, text = text, desc = desc,
        bounds = intArrayOf(0, 0, 10, 10),
        clickable = clickable, longClickable = false, scrollable = false, editable = false,
        checkable = false, checked = false, enabled = true, focusable = false, focused = false,
        visible = visible, password = false, selected = false,
    )

    @Test
    fun `layout container is dropped and its meaningful child is reparented (chain collapse)`() {
        // root(layout, id0) -> container(layout, id1) -> button(clickable, id2)
        val raw = listOf(
            node(0, null),                          // pure layout, dropped
            node(1, 0),                             // pure layout, dropped
            node(2, 1, text = "Go", clickable = true),
        )
        val pruned = Pruner.prune(raw)
        assertEquals(1, pruned.size)
        assertEquals(0, pruned[0].ref)              // first kept node → ref 0
        assertNull(pruned[0].parent)                // no kept ancestor → root in pruned tree
    }

    @Test
    fun `refs are assigned in pre-order over kept nodes only`() {
        val raw = listOf(
            node(0, null, text = "Header"),         // kept → ref 0
            node(1, 0),                             // layout, dropped
            node(2, 1, text = "Item A"),            // kept → ref 1, parent = ref 0
            node(3, 1, text = "Item B"),            // kept → ref 2, parent = ref 0
        )
        val pruned = Pruner.prune(raw)
        assertEquals(listOf(0, 1, 2), pruned.map { it.ref })
        assertEquals(listOf("Header", "Item A", "Item B"), pruned.map { it.text })
        assertEquals(listOf(null, 0, 0), pruned.map { it.parent })
    }

    @Test
    fun `invisible nodes are never kept even if actionable`() {
        val raw = listOf(node(0, null, text = "hidden", clickable = true, visible = false))
        assertEquals(0, Pruner.prune(raw).size)
    }

    @Test
    fun `content-described node is kept even with no text and no action`() {
        val raw = listOf(node(0, null, desc = "Close"))
        val pruned = Pruner.prune(raw)
        assertEquals(1, pruned.size)
        assertEquals("Close", pruned[0].desc)
    }

    @Test
    fun `flags are emitted in the documented order`() {
        val raw = listOf(
            RawNode(
                id = 0, parentId = null, cls = "x", viewId = null, text = "t", desc = null,
                bounds = intArrayOf(0, 0, 1, 1),
                clickable = true, longClickable = false, scrollable = false, editable = false,
                checkable = false, checked = false, enabled = true, focusable = true, focused = false,
                visible = true, password = false, selected = false,
            )
        )
        val flags = Pruner.prune(raw)[0].flags
        assertEquals(listOf("clickable", "enabled", "focusable", "visible"), flags)
    }

    @Test
    fun `vertical feed advertises only down-up, with the exact agreed strings`() {
        // This is the case that motivated the flags: a vertical feed and a horizontal pager
        // look identical in the tree (both just "scrollable") until the axes are exposed.
        val flags = Pruner.prune(listOf(scroller(down = true, up = true)))[0].flags
        assertEquals(listOf("scrollable", "scrollableDown", "scrollableUp", "enabled", "visible"), flags)
    }

    @Test
    fun `horizontal pager advertises only left-right`() {
        val flags = Pruner.prune(listOf(scroller(left = true, right = true)))[0].flags
        assertEquals(listOf("scrollable", "scrollableLeft", "scrollableRight", "enabled", "visible"), flags)
    }

    @Test
    fun `a scrollable advertising no direction emits no directional tokens`() {
        // Legacy node exposing only FORWARD/BACKWARD: still "scrollable", no axis claimed.
        val flags = Pruner.prune(listOf(scroller()))[0].flags
        assertEquals(listOf("scrollable", "enabled", "visible"), flags)
        assertFalse(flags.any { it.startsWith("scrollable") && it != "scrollable" })
    }

    private fun scroller(
        down: Boolean = false, up: Boolean = false,
        left: Boolean = false, right: Boolean = false,
    ) = RawNode(
        id = 0, parentId = null, cls = "android.widget.ScrollView", viewId = null,
        text = null, desc = "list", bounds = intArrayOf(0, 0, 100, 100),
        clickable = false, longClickable = false, scrollable = true, editable = false,
        checkable = false, checked = false, enabled = true, focusable = false, focused = false,
        visible = true, password = false, selected = false,
        scrollableDown = down, scrollableUp = up, scrollableLeft = left, scrollableRight = right,
    )
}
