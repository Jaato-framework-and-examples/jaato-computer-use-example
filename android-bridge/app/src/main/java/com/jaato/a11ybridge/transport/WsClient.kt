package com.jaato.a11ybridge.transport

import java.util.concurrent.TimeUnit
import kotlin.random.Random
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

/** Daemon endpoint: the wss URL and the device-bound auth token (device design §3). */
data class DaemonConfig(val url: String, val token: String)

/**
 * Outbound WebSocket transport (PROTOCOL §2, device design §3).
 *
 * The device always dials OUT. One socket carries text (JSON control) and binary (blob)
 * frames; OkHttp's own send queue preserves order and is thread-safe, so no custom queue
 * is needed. Keepalive is OkHttp's ping/pong at a fixed interval — a missed pong makes
 * OkHttp force-close and surface `onFailure`, which drives reconnect (Doze-safe).
 *
 * No daemon state is cached across reconnects: on every open we hand control back to the
 * listener (which re-sends `hello`); until the daemon re-`configure`s we run on safe defaults.
 */
class WsClient(
    private val scope: CoroutineScope,
    private val configProvider: () -> DaemonConfig?,
    private val listener: Listener,
) {
    interface Listener {
        fun onConnected()
        fun onText(frame: String)
        fun onDisconnected(reason: String)
    }

    private val http: OkHttpClient = OkHttpClient.Builder()
        // NOTE: OkHttp's pingInterval doubles as the PONG DEADLINE — "if the server does not
        // respond to each ping with a pong within interval, this client will assume that
        // connectivity has been lost and close the web socket". It cannot express the protocol's
        // separate interval (15s) and tolerance (2 x interval = 30s), so we honour the TOLERANCE:
        // 30s here means we do not tear down a briefly-throttled-but-alive socket before the
        // protocol says we may. Cost is a 30s ping period instead of 15s — still far inside
        // typical NAT idle windows.
        .pingInterval(PING_INTERVAL_SECONDS, TimeUnit.SECONDS)
        .build()

    @Volatile private var webSocket: WebSocket? = null
    @Volatile private var running = false
    private var loopJob: Job? = null

    fun start() {
        if (running) return
        running = true
        loopJob = scope.launch { connectLoop() }
    }

    /** Optionally send a final `bye`, then close and stop reconnecting. */
    fun stop(byeFrame: String? = null) {
        running = false
        byeFrame?.let { webSocket?.send(it) }
        webSocket?.close(NORMAL_CLOSURE, "bye")
        webSocket = null
        loopJob?.cancel()
        loopJob = null
    }

    /** Returns false if the socket is not currently open (frame dropped). */
    fun sendText(frame: String): Boolean = webSocket?.send(frame) ?: false

    fun sendBinary(bytes: ByteString): Boolean = webSocket?.send(bytes) ?: false

    val isConnected: Boolean get() = webSocket != null

    private suspend fun connectLoop() {
        var backoffMs = MIN_BACKOFF_MS
        while (running) {
            val cfg = configProvider()
            if (cfg == null) {
                // Not configured yet (no daemon URL/token). Wait and re-check.
                delay(NO_CONFIG_POLL_MS)
                continue
            }

            val closed = CompletableDeferred<String>()
            val request = Request.Builder()
                .url(cfg.url)
                .header("Authorization", "Bearer ${cfg.token}")
                .build()

            http.newWebSocket(request, object : WebSocketListener() {
                override fun onOpen(ws: WebSocket, response: Response) {
                    webSocket = ws
                    listener.onConnected()
                }

                override fun onMessage(ws: WebSocket, text: String) {
                    listener.onText(text)
                }

                override fun onMessage(ws: WebSocket, bytes: ByteString) {
                    // Device does not accept inbound binary frames; ignore.
                }

                override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                    ws.close(NORMAL_CLOSURE, null)
                    if (!closed.isCompleted) closed.complete("closing($code): $reason")
                }

                override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                    if (!closed.isCompleted) closed.complete("failure: ${t.message}")
                }
            })

            // Reset backoff once we actually connect.
            val closeReason = closed.await()
            webSocket = null
            listener.onDisconnected(closeReason)
            if (!running) break

            val jitter = Random.nextLong(0, backoffMs / 2 + 1)
            delay(backoffMs + jitter)
            backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
        }
    }

    private companion object {
        /** = PROTOCOL §2 tolerance (2 x the 15s nominal interval). See pingInterval note above. */
        const val PING_INTERVAL_SECONDS = 30L
        const val NORMAL_CLOSURE = 1000
        const val MIN_BACKOFF_MS = 1_000L
        const val MAX_BACKOFF_MS = 30_000L
        const val NO_CONFIG_POLL_MS = 2_000L
    }
}
