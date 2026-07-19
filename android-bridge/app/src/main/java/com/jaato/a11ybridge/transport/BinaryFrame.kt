@file:OptIn(ExperimentalSerializationApi::class)

package com.jaato.a11ybridge.transport

import kotlinx.serialization.EncodeDefault
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import okio.Buffer
import okio.ByteString

/**
 * Self-describing binary frame (PROTOCOL §4):
 *
 *   [ 4-byte BE uint32 headerLen ][ headerLen bytes UTF-8 JSON header ][ payload bytes ]
 *
 * No base64, no ordering assumptions — correlation is explicit in the header.
 */
object BinaryFrame {

    fun frame(headerJson: String, payload: ByteArray): ByteString {
        val header = headerJson.encodeToByteArray()
        val buf = Buffer()
        buf.writeInt(header.size)   // Okio writeInt is big-endian
        buf.write(header)
        buf.write(payload)
        return buf.readByteString()
    }
}

/**
 * Header schema for binary frames (§4). `snapshotVersion` is present only when the blob
 * is tied to a tree version (bundled captures / settle); `reason` ∈ on_demand | bundled.
 */
@Serializable
data class ShotHeader(
    @EncodeDefault(EncodeDefault.Mode.ALWAYS) val type: String = "screenshot",
    val correlationId: String,
    val snapshotVersion: Long? = null,
    val format: String,
    val width: Int,
    val height: Int,
    val reason: String,
) {
    fun toJson(): String = Wire.json.encodeToString(this)

    companion object {
        const val REASON_ON_DEMAND = "on_demand"
        const val REASON_BUNDLED = "bundled"
    }
}
