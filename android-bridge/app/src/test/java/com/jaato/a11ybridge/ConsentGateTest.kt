package com.jaato.a11ybridge

import com.jaato.a11ybridge.state.ConsentGate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Design B ("no lingering consent") authorization semantics (device design §13):
 * gate widenings, retract on narrow, survive transport blips, void on revoke.
 */
class ConsentGateTest {

    @Test
    fun `widening from empty is held pending until approved`() {
        val g = ConsentGate()
        val o = g.request(setOf("com.a"))
        assertTrue(o.needsConsent)
        assertEquals(setOf("com.a"), o.pending)
        assertEquals(emptySet<String>(), o.effective) // fail-closed until the operator approves
        assertEquals(setOf("com.a"), g.approve(o.generation))
        assertEquals(setOf("com.a"), g.effective())
    }

    @Test
    fun `narrowing needs no consent and stays live within the grant`() {
        val g = ConsentGate()
        g.approve(g.request(setOf("com.a", "com.b")).generation)
        val o = g.request(setOf("com.a")) // pure narrow
        assertFalse(o.needsConsent)
        assertEquals(setOf("com.a"), o.effective)
    }

    @Test
    fun `narrow retracts consent so re-widening re-prompts`() {
        val g = ConsentGate()
        g.approve(g.request(setOf("com.a", "com.b")).generation)
        g.request(setOf("com.a"))                    // daemon drops com.b
        val o = g.request(setOf("com.a", "com.b"))   // and re-adds it
        assertTrue(o.needsConsent)
        assertEquals(setOf("com.b"), o.pending)
        assertEquals(setOf("com.a"), o.effective)    // com.a stays live, com.b waits
    }

    @Test
    fun `a superseding request invalidates a stale approval`() {
        val g = ConsentGate()
        val first = g.request(setOf("com.a"))
        val second = g.request(setOf("com.a", "com.b")) // supersedes; generation bumps
        assertNull(g.approve(first.generation))          // stale → no-op
        assertEquals(setOf("com.a", "com.b"), g.approve(second.generation))
    }

    @Test
    fun `deny excludes the widening but keeps prior consent`() {
        val g = ConsentGate()
        g.approve(g.request(setOf("com.a")).generation)
        val o = g.request(setOf("com.a", "com.b"))
        assertTrue(g.deny(o.generation))
        assertEquals(setOf("com.a"), g.effective())      // com.b excluded, com.a untouched
        val re = g.request(setOf("com.a", "com.b"))      // re-configure prompts again for com.b
        assertTrue(re.needsConsent)
        assertEquals(setOf("com.b"), re.pending)
    }

    @Test
    fun `a transport blip keeps the grant so a same-scope reconnect is silent`() {
        val g = ConsentGate()
        g.approve(g.request(setOf("com.a")).generation)
        g.abandonPending()                     // socket flapped
        val o = g.request(setOf("com.a"))      // daemon re-pushes the same scope on reconnect
        assertFalse(o.needsConsent)            // no nag
        assertEquals(setOf("com.a"), o.effective)
    }

    @Test
    fun `revoke voids the grant and forces re-consent`() {
        val g = ConsentGate()
        g.approve(g.request(setOf("com.a")).generation)
        g.revoke()
        assertEquals(emptySet<String>(), g.effective())
        assertTrue(g.request(setOf("com.a")).needsConsent)
    }
}
