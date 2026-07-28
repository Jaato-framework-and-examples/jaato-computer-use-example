package com.jaato.a11ybridge

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The invasive, center-screen consent modal (device design §13, Option A): a draw-over-other-apps
 * overlay the service raises when the daemon requests a scope widening. Requires
 * `SYSTEM_ALERT_WINDOW` (the caller checks `Settings.canDrawOverlays`); when that grant is absent
 * the service uses the heads-up [ConsentNotifier] instead.
 *
 * Security. This window belongs to our own package, which is never in the daemon's `packageScope`,
 * so the daemon can neither serialize nor node-click its buttons. The one remaining vector — a
 * blind coordinate `GESTURE` tapping "Allow" — is closed in [CommandRouter.act], which suppresses
 * `GESTURE` actuation while a prompt is live. The buttons additionally filter obscured touches
 * (tapjacking by a window drawn on top).
 *
 * All `WindowManager` mutations are marshalled to the main thread; the caller runs on a background
 * coroutine.
 */
class ConsentOverlay(private val ctx: Context) {

    private val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val main = Handler(Looper.getMainLooper())
    private var view: View? = null

    fun show(
        pending: Set<String>,
        generation: Int,
        onAllow: (Int) -> Unit,
        onDeny: (Int) -> Unit,
    ) {
        val labels = AppLabels.list(ctx, pending)
        main.post {
            removeCurrent() // a superseding request replaces the previous card
            view = buildView(labels, generation, onAllow, onDeny).also {
                runCatching { wm.addView(it, layoutParams()) }
            }
        }
    }

    fun hide() {
        main.post { removeCurrent() }
    }

    private fun removeCurrent() {
        view?.let { runCatching { wm.removeView(it) } }
        view = null
    }

    private fun layoutParams() = WindowManager.LayoutParams(
        WindowManager.LayoutParams.MATCH_PARENT,
        WindowManager.LayoutParams.MATCH_PARENT,
        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
        // NOT_FOCUSABLE: don't steal key/IME focus from the app behind. The full-screen scrim
        // still captures every touch (it covers the display), so the app behind is non-interactive.
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
        PixelFormat.TRANSLUCENT,
    ).apply { gravity = Gravity.CENTER }

    private fun buildView(
        labels: String,
        generation: Int,
        onAllow: (Int) -> Unit,
        onDeny: (Int) -> Unit,
    ): View {
        val themed = ContextThemeWrapper(ctx, android.R.style.Theme_Material_Light)
        val d = ctx.resources.displayMetrics.density
        fun dp(v: Int) = (v * d).toInt()

        // Full-screen dimmed scrim; clickable so touches on the background are swallowed, not
        // passed through to the app the modal is covering.
        val scrim = FrameLayout(themed).apply {
            setBackgroundColor(0x99000000.toInt())
            isClickable = true
        }

        val card = LinearLayout(themed).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(24), dp(24), dp(16))
            background = GradientDrawable().apply {
                cornerRadius = dp(16).toFloat()
                setColor(Color.WHITE)
            }
            elevation = dp(12).toFloat()
        }
        card.addView(TextView(themed).apply {
            text = "Controller access request"
            textSize = 18f
            setTextColor(0xFF1A1A1A.toInt())
            setPadding(0, 0, 0, dp(12))
        })
        card.addView(TextView(themed).apply {
            text = "The controller wants to drive:"
            setTextColor(0xFF444444.toInt())
        })
        card.addView(TextView(themed).apply {
            text = labels
            textSize = 16f
            setTextColor(0xFF000000.toInt())
            setPadding(0, dp(4), 0, dp(20))
        })

        val row = LinearLayout(themed).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
        }
        row.addView(Button(themed).apply {
            text = "Deny"
            filterTouchesWhenObscured = true
            setOnClickListener { onDeny(generation); hide() }
        })
        row.addView(Button(themed).apply {
            text = "Allow"
            filterTouchesWhenObscured = true
            setOnClickListener { onAllow(generation); hide() }
        })
        card.addView(row)

        scrim.addView(
            card,
            FrameLayout.LayoutParams(dp(320), FrameLayout.LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.CENTER
            },
        )
        return scrim
    }
}
