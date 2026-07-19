package com.jaato.a11ybridge.act

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction
import com.jaato.a11ybridge.transport.ActArgs
import com.jaato.a11ybridge.transport.DeviceError
import com.jaato.a11ybridge.transport.ErrorCode
import com.jaato.a11ybridge.transport.Gesture

/**
 * Executes a daemon-named action (PROTOCOL §11) against a resolved node (or, for
 * gestures/globals, against the service). Mechanical only:
 *
 * - A semantic action that returns `false` (node present but doesn't support it) surfaces
 *   NOT_ACTIONABLE. The device does NOT auto-fall-back to a gesture — that is daemon policy.
 * - `act` returns its ack immediately after performing; it never awaits settle (§5.2).
 */
class Actuator(private val service: AccessibilityService) {

    /** Perform the action; throws [DeviceError] on failure. */
    fun perform(node: AccessibilityNodeInfo?, args: ActArgs) {
        when (args.action) {
            "CLICK" -> semantic(node, AccessibilityNodeInfo.ACTION_CLICK)
            "LONG_CLICK" -> semantic(node, AccessibilityNodeInfo.ACTION_LONG_CLICK)
            "SET_TEXT" -> setText(node, args.text)
            // Orientation-agnostic (kept for nodes that advertise only these).
            "SCROLL_FORWARD" -> scroll(node, AccessibilityNodeInfo.ACTION_SCROLL_FORWARD, "SCROLL_FORWARD")
            "SCROLL_BACKWARD" -> scroll(node, AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD, "SCROLL_BACKWARD")
            // Axis-explicit (API 23+). Preferred: unambiguous on nodes that nest a horizontal
            // pager around a vertical list, where FORWARD/BACKWARD pages sideways instead.
            "SCROLL_DOWN" -> scroll(node, AccessibilityAction.ACTION_SCROLL_DOWN.id, "SCROLL_DOWN")
            "SCROLL_UP" -> scroll(node, AccessibilityAction.ACTION_SCROLL_UP.id, "SCROLL_UP")
            "SCROLL_LEFT" -> scroll(node, AccessibilityAction.ACTION_SCROLL_LEFT.id, "SCROLL_LEFT")
            "SCROLL_RIGHT" -> scroll(node, AccessibilityAction.ACTION_SCROLL_RIGHT.id, "SCROLL_RIGHT")
            "FOCUS" -> semantic(node, AccessibilityNodeInfo.ACTION_FOCUS)
            "GESTURE" -> gesture(args.gesture ?: err("GESTURE requires a gesture"))
            "GLOBAL" -> global(args.global ?: err("GLOBAL requires a global action name"))
            else -> throw DeviceError(ErrorCode.INTERNAL, "unknown action ${args.action}")
        }
    }

    /**
     * Scroll actions, with the one distinction the model actually needs. `performAction` returns
     * false BOTH when a node does not support the action AND when it supports it but is already
     * at the scroll extent — very different meanings ("try another ref" vs "you've hit the end").
     * `getActionList()` tells them apart deterministically, so we report which one it was.
     * Both surface as NOT_ACTIONABLE (§7); the device still never falls back on its own.
     */
    private fun scroll(node: AccessibilityNodeInfo?, actionId: Int, name: String) {
        val target = node ?: throw DeviceError(ErrorCode.NOT_FOUND, "no node to scroll")
        val advertised = target.actionList.any { it.id == actionId }
        if (!advertised) {
            throw DeviceError(ErrorCode.NOT_ACTIONABLE, "node does not advertise $name")
        }
        if (!target.performAction(actionId)) {
            throw DeviceError(
                ErrorCode.NOT_ACTIONABLE,
                "$name advertised but returned false (already at scroll extent)",
            )
        }
    }

    private fun semantic(node: AccessibilityNodeInfo?, action: Int) {
        val target = node ?: throw DeviceError(ErrorCode.NOT_FOUND, "no node to act on")
        if (!target.performAction(action)) {
            throw DeviceError(ErrorCode.NOT_ACTIONABLE, "node does not support action $action")
        }
    }

    private fun setText(node: AccessibilityNodeInfo?, text: String?) {
        val target = node ?: throw DeviceError(ErrorCode.NOT_FOUND, "no node to set text on")
        val value = text ?: err("SET_TEXT requires text")
        val bundle = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, value)
        }
        if (!target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)) {
            throw DeviceError(ErrorCode.NOT_ACTIONABLE, "node does not support SET_TEXT")
        }
    }

    private fun gesture(g: Gesture) {
        if (g.path.isEmpty()) err("gesture path is empty")
        val path = Path().apply {
            val first = g.path.first()
            moveTo(first[0].toFloat(), first[1].toFloat())
            for (i in 1 until g.path.size) {
                val p = g.path[i]
                lineTo(p[0].toFloat(), p[1].toFloat())
            }
        }
        val duration = g.durationMs.coerceAtLeast(1)
        val stroke = GestureDescription.StrokeDescription(path, 0, duration)
        val description = GestureDescription.Builder().addStroke(stroke).build()
        // Fire-and-forget: §5.2 says act does not await; completion rides the settle stream.
        if (!service.dispatchGesture(description, null, null)) {
            throw DeviceError(ErrorCode.NOT_ACTIONABLE, "gesture could not be dispatched")
        }
    }

    private fun global(name: String) {
        val action = when (name) {
            "BACK" -> AccessibilityService.GLOBAL_ACTION_BACK
            "HOME" -> AccessibilityService.GLOBAL_ACTION_HOME
            "RECENTS" -> AccessibilityService.GLOBAL_ACTION_RECENTS
            "NOTIFICATIONS" -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
            "QUICK_SETTINGS" -> AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS
            "LOCK_SCREEN" -> AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN
            else -> throw DeviceError(ErrorCode.INTERNAL, "unknown global action $name")
        }
        if (!service.performGlobalAction(action)) {
            throw DeviceError(ErrorCode.NOT_ACTIONABLE, "global action $name failed")
        }
    }

    private fun err(msg: String): Nothing = throw DeviceError(ErrorCode.INTERNAL, msg)
}
