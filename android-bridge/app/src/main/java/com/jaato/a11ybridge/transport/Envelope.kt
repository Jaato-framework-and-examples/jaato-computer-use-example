@file:OptIn(ExperimentalSerializationApi::class)

package com.jaato.a11ybridge.transport

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * Wire envelopes and the shared JSON codec (PROTOCOL §3).
 *
 * The device never redefines the wire; these types are a 1:1 transcription of the
 * protocol document. `args`/`data` are kept as [JsonObject]/[JsonElement] because
 * their shape is verb-specific and decoded on demand by the router.
 */
object Wire {
    /** Protocol major version (§3.4). A mismatch is fatal (fail closed). */
    const val PV = 1

    val json: Json = Json {
        ignoreUnknownKeys = true   // forward-compatible with additive daemon fields
        encodeDefaults = false     // absent == default, keeps frames compact
        explicitNulls = false      // omit null fields rather than emit "x": null
        isLenient = false
    }
}

/** Request: daemon → device (§3.1). */
@Serializable
data class Req(
    val kind: String = "req",
    val id: String,
    val verb: String,
    val args: JsonObject = JsonObject(emptyMap()),
    val pv: Int? = null,
)

/** Response: device → daemon (§3.2). Exactly one per request. */
@Serializable
data class Res(
    @EncodeDefault(EncodeDefault.Mode.ALWAYS) val kind: String = "res",
    val id: String,
    val ok: Boolean,
    val data: JsonElement? = null,
    val error: WireError? = null,
)

/** Event: device → daemon, unsolicited (§3.3). */
@Serializable
data class Event(
    @EncodeDefault(EncodeDefault.Mode.ALWAYS) val kind: String = "event",
    val event: String,
    val data: JsonElement,
)

/** Error payload carried by a failed [Res] or an `error` event (§7). */
@Serializable
data class WireError(
    val code: String,
    val message: String,
    val retryAfterMs: Long? = null,
)

/** The canonical error codes of §7. Values match the wire strings exactly. */
object ErrorCode {
    const val NOT_FOUND = "NOT_FOUND"
    const val AMBIGUOUS = "AMBIGUOUS"
    const val STALE = "STALE"
    const val NOT_ACTIONABLE = "NOT_ACTIONABLE"
    const val RATE_LIMITED = "RATE_LIMITED"
    const val SECURE_WINDOW = "SECURE_WINDOW"
    const val CANCELED = "CANCELED"
    const val TIMEOUT = "TIMEOUT"
    const val PROTOCOL_VERSION = "PROTOCOL_VERSION"
    const val PERMISSION = "PERMISSION"
    const val INTERNAL = "INTERNAL"
}

/**
 * Typed device-side failure. Thrown by handlers and mapped to a failed [Res] by the
 * router (§8 of the device design). Carries the §7 code verbatim.
 */
class DeviceError(
    val code: String,
    override val message: String,
    val retryAfterMs: Long? = null,
) : Exception(message)
