package com.jaato.a11ybridge.state

import java.util.concurrent.atomic.AtomicReference

/**
 * Process-wide, lock-free connection status published by the socket layer and read by the UI.
 * Single-process design (device design §2), so a shared object is enough — no IPC, no LiveData.
 *
 * This reflects only the SOCKET state. Whether the accessibility service is enabled, and whether
 * a daemon URL/token exist, are inferred by the UI from the OS setting + prefs — the service
 * isn't even alive to report when it's disabled.
 */
object BridgeStatus {
    enum class Conn { DISCONNECTED, CONNECTING, CONNECTED }

    private val conn = AtomicReference(Conn.DISCONNECTED)

    fun set(state: Conn) = conn.set(state)

    fun get(): Conn = conn.get()
}
