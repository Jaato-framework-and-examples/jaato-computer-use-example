package com.jaato.a11ybridge.transport

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.JsonElement

/** Small builders + encoders for outbound text frames. Keeps the router/service terse. */

fun okRes(id: String, data: JsonElement? = null): Res =
    Res(id = id, ok = true, data = data)

fun errRes(id: String, code: String, message: String, retryAfterMs: Long? = null): Res =
    Res(id = id, ok = false, error = WireError(code, message, retryAfterMs))

fun deviceEvent(name: String, data: JsonElement): Event =
    Event(event = name, data = data)

fun Res.encode(): String = Wire.json.encodeToString(this)

fun Event.encode(): String = Wire.json.encodeToString(this)
