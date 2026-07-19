package com.jaato.a11ybridge.observe

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.content.pm.PackageManager
import android.view.accessibility.AccessibilityWindowInfo
import com.jaato.a11ybridge.transport.WindowInfo
import com.jaato.a11ybridge.transport.WindowsReport

/**
 * Enumerates the currently-displayed windows and resolves the foreground + launcher
 * packages (the `windows` verb).
 *
 * Deliberately NOT scope-gated: it reports window metadata (package/title/type/layer),
 * never tree content or pixels. Existence-of-a-window is a much narrower disclosure than
 * its content, and the daemon needs it to bootstrap its first scope and to navigate the
 * launcher for app-hopping. Content (observe/act) stays strictly scope-gated elsewhere.
 *
 * `launcherPkg` comes from ONE bounded `PackageManager` HOME-intent resolve — "who is the
 * default home app" — not an installed-app enumeration.
 */
class WindowLister(private val service: AccessibilityService) {

    fun report(foregroundActivity: String?): WindowsReport {
        val windows = runCatching { service.windows }.getOrNull() ?: emptyList()
        val infos = ArrayList<WindowInfo>(windows.size)
        var foregroundPkg: String? = null

        for (w in windows) {
            val root = w.root
            val pkg = root?.packageName?.toString()
            val focused = w.isActive
            if (focused && w.type == AccessibilityWindowInfo.TYPE_APPLICATION && foregroundPkg == null) {
                foregroundPkg = pkg
            }
            infos.add(
                WindowInfo(
                    pkg = pkg,
                    title = w.title?.toString(),
                    type = windowType(w.type),
                    focused = focused,
                    layer = w.layer,
                )
            )
            Nodes.recycle(root)
        }
        if (foregroundPkg == null) foregroundPkg = infos.firstOrNull { it.focused }?.pkg

        return WindowsReport(
            foregroundPkg = foregroundPkg,
            foregroundActivity = foregroundActivity,
            launcherPkg = resolveLauncherPkg(),
            windows = infos,
        )
    }

    /**
     * The foreground application package: the active window of type APPLICATION. Cheap query
     * (no launcher resolve, no full report) for the `window_changed` foreground-change detector.
     * IME / system / overlay windows are ignored — that is what filters the noise for option (b).
     */
    fun foregroundAppPkg(): String? {
        val windows = runCatching { service.windows }.getOrNull() ?: return null
        var pkg: String? = null
        for (w in windows) {
            val root = w.root
            if (pkg == null && w.isActive && w.type == AccessibilityWindowInfo.TYPE_APPLICATION) {
                pkg = root?.packageName?.toString()
            }
            Nodes.recycle(root)
        }
        return pkg
    }

    /** The default home app — a single bounded resolve, NOT an installed-app listing. */
    private fun resolveLauncherPkg(): String? {
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME)
        return runCatching {
            @Suppress("DEPRECATION")
            service.packageManager.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY)
        }.getOrNull()?.activityInfo?.packageName
    }

    private fun windowType(type: Int): String = when (type) {
        AccessibilityWindowInfo.TYPE_APPLICATION -> "application"
        AccessibilityWindowInfo.TYPE_INPUT_METHOD -> "input_method"
        AccessibilityWindowInfo.TYPE_SYSTEM -> "system"
        AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY -> "accessibility_overlay"
        AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER -> "split_screen_divider"
        else -> "unknown"
    }
}
