package com.jaato.a11ybridge.observe

import android.os.Build
import android.view.accessibility.AccessibilityNodeInfo

/** Single home for the recycle discipline (device design §2.3). */
object Nodes {
    fun recycle(node: AccessibilityNodeInfo?) {
        if (node == null) return
        // 33+: recycle() is a deprecated no-op; the platform reclaims automatically.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            @Suppress("DEPRECATION")
            node.recycle()
        }
    }

    fun recycleAll(nodes: Iterable<AccessibilityNodeInfo?>) = nodes.forEach { recycle(it) }
}
