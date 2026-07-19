package com.jaato.a11ybridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder

/**
 * Minimal foreground service. Its ONLY job is to hold the process at foreground importance so
 * Android — and OEM power management (Samsung especially) — does not throttle or suspend the
 * bridge's WebSocket while the app is backgrounded. It hosts no logic: the socket, tree walking,
 * actions and settle all stay in [BridgeAccessibilityService].
 *
 * Why this is needed even though an AccessibilityService is "protected": being bound by the
 * system protects the process from being KILLED, but it does not exempt it from background
 * NETWORK throttling. Without an active foreground service the socket flaps — abrupt drops with
 * no graceful `bye`, then a reconnect on the next CPU slice — whenever the app is backgrounded.
 *
 * Type is `specialUse`: `dataSync` is capped at ~6h/day on Android 14+, which would reintroduce
 * exactly the failure this fixes, and `connectedDevice` misdescribes a remote daemon socket.
 *
 * NOTE: a foreground service REDUCES drops; it cannot eliminate them (real Doze, screen-off,
 * network changes still kill sockets). The daemon must still adopt the newest session on reconnect.
 */
class BridgeForegroundService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(
            NOTIF_ID,
            buildNotification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
        )
        return START_STICKY
    }

    override fun onDestroy() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Bridge status", NotificationManager.IMPORTANCE_LOW)
                .apply { description = "Shows while the accessibility bridge is active." }
        )
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_bridge)
            .setContentTitle(getString(R.string.app_name))
            .setContentText("Bridge active — daemon has full device control")
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "bridge_status"
        private const val NOTIF_ID = 1001

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, BridgeForegroundService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, BridgeForegroundService::class.java))
        }
    }
}
