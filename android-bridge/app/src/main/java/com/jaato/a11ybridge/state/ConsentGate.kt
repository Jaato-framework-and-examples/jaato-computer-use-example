package com.jaato.a11ybridge.state

/**
 * The human-in-the-loop authorization gate (device design §13).
 *
 * The bridge is a no-policy mechanism for *task* logic, but *authorization* is the one policy
 * that belongs on the device: the operator decides which packages the single configured daemon
 * may drive. Consent is expressed ENTIRELY through the effective scope — an un-consented package
 * is simply not in scope, so every downstream reader stays fail-closed with no new wire codes.
 *
 * Design B ("no lingering consent"): consent maps to *currently requested* access.
 *  - widen  → a package enters the requested scope that isn't consented → held pending (PROMPT).
 *  - narrow → the daemon drops packages → their consent is RETRACTED (re-adding re-prompts).
 *
 * Lifetime is in-memory, tied to the service: the grant survives transport reconnects (a network
 * blip re-`configure`d with the same scope does not re-prompt — see [abandonPending]) but not a
 * service teardown/reboot (a fresh session re-asks). No disk persistence, so no dormant consent
 * across process death.
 *
 * Pure logic, no Android deps — unit-tested directly. The router owns the effect (pushing the
 * effective scope into [SessionConfig]); the service owns the UI (the consent notification).
 */
class ConsentGate {
    private val lock = Any()

    // Guarded by [lock].
    private var consented: Set<String> = emptySet()   // approved; always ⊆ the last requested scope
    private var pending: Set<String> = emptySet()     // awaiting the operator's decision
    private var generation: Int = 0                    // bumped per raised prompt; approve/deny must match

    /** Outcome of a scope request: what may be active now, and what (if anything) needs a prompt. */
    data class Outcome(
        val effective: Set<String>,
        val pending: Set<String>,
        val generation: Int,
    ) {
        val needsConsent: Boolean get() = pending.isNotEmpty()
    }

    /**
     * The daemon requests session scope [requested]. Retracts consent for any package it drops
     * (Design B) and returns the widening (if any) that needs the operator's approval. The
     * effective scope is the already-consented subset — the widening stays out until approved.
     */
    fun request(requested: Set<String>): Outcome = synchronized(lock) {
        consented = consented intersect requested   // retract-on-narrow
        pending = requested - consented             // the widening (⊆ requested, disjoint from consented)
        if (pending.isNotEmpty()) generation++
        Outcome(effective = consented, pending = pending, generation = generation)
    }

    /** Operator approved widening [gen]. Returns the new effective scope, or null if stale/empty. */
    fun approve(gen: Int): Set<String>? = synchronized(lock) {
        if (gen != generation || pending.isEmpty()) return null
        consented = consented + pending
        pending = emptySet()
        consented
    }

    /** Operator denied widening [gen]; the pending packages stay out. False if stale/empty. */
    fun deny(gen: Int): Boolean = synchronized(lock) {
        if (gen != generation || pending.isEmpty()) return false
        pending = emptySet()
        true
    }

    /** Drop any in-flight prompt (transport blip / session reset); keep the standing grant. */
    fun abandonPending() = synchronized(lock) { pending = emptySet() }

    /** Void the entire grant (daemon-settings change / explicit revoke). */
    fun revoke() = synchronized(lock) {
        consented = emptySet()
        pending = emptySet()
    }

    /** The currently-consented (effective) scope. */
    fun effective(): Set<String> = synchronized(lock) { consented }
}
