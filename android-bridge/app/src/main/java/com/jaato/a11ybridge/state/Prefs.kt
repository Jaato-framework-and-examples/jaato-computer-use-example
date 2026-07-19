package com.jaato.a11ybridge.state

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * Operator-provided connection settings. The daemon endpoint and token come from here —
 * never hardcoded (a wrong default would silently point a keylogger-grade stream somewhere).
 *
 * NOTE (v1): stored in plain SharedPreferences. The token is sensitive; hardening this to
 * EncryptedSharedPreferences / a device-bound keystore key is a tracked follow-up.
 */
object Prefs {
    private const val NAME = "jaato_a11y_bridge"
    private const val KEY_URL = "daemon_url"
    private const val KEY_TOKEN = "token"
    private const val KEY_DEVICE_ID = "device_id"
    private const val KEY_CONNECT_DESIRED = "connect_desired"

    private fun sp(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE)

    fun daemonUrl(ctx: Context): String = sp(ctx).getString(KEY_URL, "").orEmpty()

    fun token(ctx: Context): String = sp(ctx).getString(KEY_TOKEN, "").orEmpty()

    fun save(ctx: Context, url: String, token: String) {
        sp(ctx).edit().putString(KEY_URL, url).putString(KEY_TOKEN, token).apply()
    }

    /**
     * Whether the operator wants the bridge connected. Defaults to true (auto-connect when the
     * service is enabled + configured); a manual DISCONNECT persists as a kill switch until CONNECT.
     */
    fun connectDesired(ctx: Context): Boolean = sp(ctx).getBoolean(KEY_CONNECT_DESIRED, true)

    fun setConnectDesired(ctx: Context, desired: Boolean) {
        sp(ctx).edit().putBoolean(KEY_CONNECT_DESIRED, desired).apply()
    }

    /** Stable device identity, generated once and persisted (device-bound, not hardcoded). */
    fun deviceId(ctx: Context): String {
        val prefs = sp(ctx)
        prefs.getString(KEY_DEVICE_ID, null)?.let { return it }
        val id = UUID.randomUUID().toString()
        prefs.edit().putString(KEY_DEVICE_ID, id).apply()
        return id
    }
}
