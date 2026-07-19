package com.jaato.a11ybridge

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.jaato.a11ybridge.state.BridgeStatus
import com.jaato.a11ybridge.state.Prefs

/**
 * Tiny operator console: set the daemon endpoint + token and jump to Accessibility settings.
 * The status line reflects LIVE state — OS accessibility setting + prefs + the socket status
 * published by the service (approach A: shared [BridgeStatus]) — refreshed on a 1s tick while
 * the screen is visible. No policy here.
 */
class MainActivity : Activity() {

    private lateinit var statusView: TextView
    private lateinit var urlField: EditText
    private lateinit var tokenField: EditText
    private lateinit var connectBtn: Button
    private lateinit var disconnectBtn: Button

    private val handler = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            updateStatus()
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        fun label(text: String) = TextView(this).apply {
            this.text = text
            setPadding(0, pad / 2, 0, pad / 8)
        }

        root.addView(TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 20f
            setPadding(0, 0, 0, pad / 2)
        })

        root.addView(TextView(this).apply {
            text = "Device ID: ${Prefs.deviceId(this@MainActivity)}"
            setPadding(0, 0, 0, pad / 2)
        })

        root.addView(label("Daemon URL (wss://host:port/a11y)"))
        urlField = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setText(Prefs.daemonUrl(this@MainActivity))
            hint = "wss://daemon.internal:8443/a11y"
        }
        root.addView(urlField)

        root.addView(label("Device-bound token"))
        tokenField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(Prefs.token(this@MainActivity))
        }
        root.addView(tokenField)

        statusView = TextView(this).apply { setPadding(0, pad, 0, 0) }

        root.addView(Button(this).apply {
            text = "Save"
            setOnClickListener {
                Prefs.save(
                    this@MainActivity,
                    urlField.text.toString().trim(),
                    tokenField.text.toString().trim(),
                )
                Toast.makeText(this@MainActivity, "Saved", Toast.LENGTH_SHORT).show()
                updateStatus()
            }
        })

        // CONNECT / DISCONNECT — set the persisted desire and signal the running service.
        connectBtn = Button(this).apply {
            text = "Connect"
            setOnClickListener {
                Prefs.setConnectDesired(this@MainActivity, true)
                BridgeAccessibilityService.instance?.applyConnectionDesire()
                updateStatus()
            }
        }
        disconnectBtn = Button(this).apply {
            text = "Disconnect"
            setOnClickListener {
                Prefs.setConnectDesired(this@MainActivity, false)
                BridgeAccessibilityService.instance?.applyConnectionDesire()
                updateStatus()
            }
        }
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(connectBtn, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(disconnectBtn, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        })

        root.addView(Button(this).apply {
            text = "Open Accessibility settings"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
        })

        root.addView(statusView)

        val scroll = ScrollView(this).apply {
            addView(
                root,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ),
            )
        }
        setContentView(scroll)
    }

    override fun onResume() {
        super.onResume()
        ensureNotificationPermission()
        handler.post(tick) // starts the 1s status refresh
    }

    /**
     * Android 13+ requires a RUNTIME grant for POST_NOTIFICATIONS. Without it the foreground
     * service still runs (and still protects the socket), but its notification is silently
     * suppressed — unacceptable for a tool with this much authority, since that notification is
     * the user's only standing indication that the bridge is live.
     */
    private fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_POST_NOTIFICATIONS)
        }
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(tick)
    }

    /** Live status: not-configured → manual-disconnect → not-enabled → connecting → connected. */
    private fun updateStatus() {
        val configured =
            Prefs.daemonUrl(this).isNotBlank() && Prefs.token(this).isNotBlank()
        val desired = Prefs.connectDesired(this)

        // Exactly one of the two buttons is actionable given the current desire.
        connectBtn.isEnabled = !desired
        disconnectBtn.isEnabled = desired

        statusView.text = when {
            !configured -> "Not configured — enter a daemon URL and token."
            !desired -> "⏸ Disconnected (manual) — press CONNECT to dial."
            !isServiceEnabled() -> "Saved. Enable the accessibility service to connect."
            BridgeStatus.get() == BridgeStatus.Conn.CONNECTED -> "● Connected — daemon session active."
            else -> "○ Enabled — connecting to daemon…"
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_POST_NOTIFICATIONS &&
            grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
        ) {
            // Re-post the foreground notification now that it can actually be drawn. Only if the
            // bridge is really running — a notification for an inactive bridge would mislead.
            if (BridgeAccessibilityService.instance != null) BridgeForegroundService.start(this)
        }
        updateStatus()
    }

    private fun isServiceEnabled(): Boolean {
        val enabled = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        val component = "$packageName/${BridgeAccessibilityService::class.java.name}"
        return enabled.split(':').any { it.equals(component, ignoreCase = true) }
    }

    private companion object {
        const val REQ_POST_NOTIFICATIONS = 1001
    }
}
