package com.jaato.a11ybridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Routes the Allow/Deny taps from the consent notification ([ConsentNotifier]) to the running
 * [BridgeAccessibilityService], which validates the generation and applies the decision through
 * the router. Registered non-exported: only this app's notification actions reach it.
 */
class ConsentReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val generation = intent.getIntExtra(EXTRA_GENERATION, -1)
        if (generation < 0) return
        val service = BridgeAccessibilityService.instance
        when (intent.action) {
            ACTION_ALLOW -> service?.onConsentApprove(generation)
            ACTION_DENY -> service?.onConsentDeny(generation)
        }
        // If the service is gone, the decision is moot — drop the stale prompt so it can't linger.
        if (service == null) ConsentNotifier.dismiss(context)
    }

    companion object {
        const val ACTION_ALLOW = "com.jaato.a11ybridge.CONSENT_ALLOW"
        const val ACTION_DENY = "com.jaato.a11ybridge.CONSENT_DENY"
        const val EXTRA_GENERATION = "generation"
    }
}
