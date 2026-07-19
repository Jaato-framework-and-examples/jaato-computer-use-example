package com.jaato.a11ybridge

import com.jaato.a11ybridge.transport.ActArgs
import com.jaato.a11ybridge.transport.ErrorCode
import com.jaato.a11ybridge.transport.Req
import com.jaato.a11ybridge.transport.Selector
import com.jaato.a11ybridge.transport.SettleConfig
import com.jaato.a11ybridge.transport.Wire
import com.jaato.a11ybridge.transport.encode
import com.jaato.a11ybridge.transport.errRes
import com.jaato.a11ybridge.transport.okRes
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Envelope + payload wire conformance (PROTOCOL §3, §5, §7, §9, §10). */
class WireTest {

    @Test
    fun `decode an act request and its selector`() {
        val frame = """
            {"kind":"req","id":"r-3","verb":"act","args":{
              "target":{"viewId":"com.foo.app:id/submit"},
              "action":"CLICK",
              "settleOverride":{"quietWindowMs":1000}
            }}
        """.trimIndent()
        val req = Wire.json.decodeFromString(Req.serializer(), frame)
        assertEquals("r-3", req.id)
        assertEquals("act", req.verb)

        val args = Wire.json.decodeFromJsonElement(ActArgs.serializer(), req.args)
        assertEquals("CLICK", args.action)
        assertEquals("com.foo.app:id/submit", args.target.viewId)
        assertEquals(1000L, args.settleOverride?.quietWindowMs)
    }

    @Test
    fun `ok response always carries kind and id, no error`() {
        val json = okRes("r-3", buildJsonObject { put("resolved", true) }).encode()
        assertTrue(json.contains("\"kind\":\"res\""))
        assertTrue(json.contains("\"id\":\"r-3\""))
        assertTrue(json.contains("\"ok\":true"))
        assertFalse(json.contains("\"error\""))
    }

    @Test
    fun `error response carries code and retryAfterMs when present`() {
        val json = errRes("r-4", ErrorCode.RATE_LIMITED, "cooldown", retryAfterMs = 640).encode()
        assertTrue(json.contains("\"ok\":false"))
        assertTrue(json.contains("\"code\":\"RATE_LIMITED\""))
        assertTrue(json.contains("\"retryAfterMs\":640"))
    }

    @Test
    fun `error response omits retryAfterMs when null`() {
        val json = errRes("r-4", ErrorCode.NOT_FOUND, "gone").encode()
        assertFalse(json.contains("retryAfterMs"))
    }

    @Test
    fun `ref selector round-trips`() {
        val sel = Selector(ref = 42, snapshotVersion = 1291)
        val json = Wire.json.encodeToString(Selector.serializer(), sel)
        val back = Wire.json.decodeFromString(Selector.serializer(), json)
        assertEquals(42, back.ref)
        assertEquals(1291L, back.snapshotVersion)
        assertNull(back.viewId) // omitted, not null-encoded
    }

    @Test
    fun `SettleConfig safe default matches device design §8`() {
        val d = SettleConfig.SAFE_DEFAULT
        assertEquals(600L, d.quietWindowMs)
        assertEquals(6000L, d.hardTimeoutMs)
        assertEquals(listOf("WINDOW_CONTENT_CHANGED", "WINDOW_STATE_CHANGED"), d.eventMask)
        assertTrue(d.packageScope.isEmpty())
        assertEquals(SettleConfig.MODE_QUIET, d.mode)
    }

    @Test
    fun `windows report serializes with foreground and launcher packages`() {
        val report = com.jaato.a11ybridge.transport.WindowsReport(
            foregroundPkg = "com.android.settings",
            foregroundActivity = "com.android.settings/.Home",
            launcherPkg = "com.sec.android.app.launcher",
            windows = listOf(
                com.jaato.a11ybridge.transport.WindowInfo(
                    pkg = "com.android.settings", title = "Settings",
                    type = "application", focused = true, layer = 0,
                ),
                com.jaato.a11ybridge.transport.WindowInfo(
                    pkg = "com.samsung.ime", title = null,
                    type = "input_method", focused = false, layer = 1,
                ),
            ),
        )
        val json = Wire.json.encodeToString(
            com.jaato.a11ybridge.transport.WindowsReport.serializer(), report,
        )
        assertTrue(json.contains("\"foregroundPkg\":\"com.android.settings\""))
        assertTrue(json.contains("\"launcherPkg\":\"com.sec.android.app.launcher\""))
        assertTrue(json.contains("\"type\":\"input_method\""))
        assertTrue(json.contains("\"focused\":true"))
        // null title omitted (explicitNulls=false)
        assertFalse(json.contains("\"title\":null"))
    }

    @Test
    fun `unknown daemon fields are ignored (forward compatible)`() {
        val frame = """{"kind":"req","id":"r-9","verb":"ping","args":{},"futureField":true}"""
        val req = Wire.json.decodeFromString(Req.serializer(), frame)
        assertEquals("ping", req.verb)
    }
}
