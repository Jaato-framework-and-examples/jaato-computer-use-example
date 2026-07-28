package com.jaato.a11ybridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon

/**
 * The human-in-the-loop consent prompt (device design §13): a high-importance heads-up
 * notification asking the operator to Allow/Deny the single configured daemon driving a
 * newly-requested set of apps. Friendly app labels are shown; the Allow/Deny actions route to
 * [ConsentReceiver], which hands the decision back to the running service.
 *
 * The prompt is on its own IMPORTANCE_HIGH channel (distinct from the LOW ongoing-status channel)
 * so it surfaces as a heads-up banner. It is ongoing (not swipe-dismissable) so a widening can't
 * be ignored into silent denial — the operator must Allow or Deny.
 */
object ConsentNotifier {
    private const val CHANNEL_ID = "bridge_consent"
    private const val NOTIF_ID = 1002

    fun prompt(ctx: Context, pending: Set<String>, generation: Int) {
        ensureChannel(ctx)
        val labels = AppLabels.list(ctx, pending)
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = Notification.Builder(ctx, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_bridge)
            .setContentTitle("Allow the controller to drive these apps?")
            .setContentText(labels)
            .setStyle(Notification.BigTextStyle().bigText(labels))
            .setCategory(Notification.CATEGORY_CALL)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(action(ctx, ConsentReceiver.ACTION_ALLOW, generation, "Allow"))
            .addAction(action(ctx, ConsentReceiver.ACTION_DENY, generation, "Deny"))
            .build()
        ctx.getSystemService(NotificationManager::class.java)?.notify(NOTIF_ID, notif)
    }

    fun dismiss(ctx: Context) {
        ctx.getSystemService(NotificationManager::class.java)?.cancel(NOTIF_ID)
    }

    private fun ensureChannel(ctx: Context) {
        val ch = NotificationChannel(
            CHANNEL_ID,
            "Controller access requests",
            NotificationManager.IMPORTANCE_HIGH,
        ).apply { description = "Asks permission before the daemon can drive newly-requested apps." }
        ctx.getSystemService(NotificationManager::class.java)?.createNotificationChannel(ch)
    }

    private fun action(
        ctx: Context,
        action: String,
        generation: Int,
        title: String,
    ): Notification.Action {
        val intent = Intent(ctx, ConsentReceiver::class.java).apply {
            this.action = action
            putExtra(ConsentReceiver.EXTRA_GENERATION, generation)
        }
        // Distinct request codes per (generation, action) so a superseding prompt's PendingIntents
        // never collide with a stale one; the receiver still validates the generation.
        val requestCode = generation * 2 + if (action == ConsentReceiver.ACTION_ALLOW) 0 else 1
        val pi = PendingIntent.getBroadcast(
            ctx, requestCode, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val icon = Icon.createWithResource(ctx, R.drawable.ic_stat_bridge)
        return Notification.Action.Builder(icon, title, pi).build()
    }
}
