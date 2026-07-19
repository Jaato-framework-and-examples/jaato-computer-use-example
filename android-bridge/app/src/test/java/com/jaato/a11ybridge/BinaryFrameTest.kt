package com.jaato.a11ybridge

import com.jaato.a11ybridge.transport.BinaryFrame
import com.jaato.a11ybridge.transport.ShotHeader
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

/** The 4-byte-BE-length binary framing is a hard interop contract (PROTOCOL §4). */
class BinaryFrameTest {

    @Test
    fun `frame layout is headerLen(BE) then header then payload`() {
        val header = """{"type":"screenshot"}"""
        val payload = byteArrayOf(1, 2, 3, 4, 5)
        val bytes = BinaryFrame.frame(header, payload).toByteArray()

        val headerBytes = header.encodeToByteArray()
        // First 4 bytes: big-endian header length.
        val len = ((bytes[0].toInt() and 0xFF) shl 24) or
            ((bytes[1].toInt() and 0xFF) shl 16) or
            ((bytes[2].toInt() and 0xFF) shl 8) or
            (bytes[3].toInt() and 0xFF)
        assertEquals(headerBytes.size, len)

        val gotHeader = bytes.copyOfRange(4, 4 + len)
        assertArrayEquals(headerBytes, gotHeader)

        val gotPayload = bytes.copyOfRange(4 + len, bytes.size)
        assertArrayEquals(payload, gotPayload)
    }

    @Test
    fun `shot header serializes with the wire field names`() {
        val json = ShotHeader(
            correlationId = "r-8f3a",
            snapshotVersion = 1287,
            format = "webp",
            width = 1080,
            height = 2340,
            reason = ShotHeader.REASON_BUNDLED,
        ).toJson()
        assertEquals(true, json.contains("\"correlationId\":\"r-8f3a\""))
        assertEquals(true, json.contains("\"snapshotVersion\":1287"))
        assertEquals(true, json.contains("\"reason\":\"bundled\""))
        assertEquals(true, json.contains("\"type\":\"screenshot\""))
    }
}
