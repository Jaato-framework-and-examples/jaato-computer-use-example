package com.jaato.a11ybridge.state

import com.jaato.a11ybridge.transport.ConfigureArgs
import com.jaato.a11ybridge.transport.RedactionPolicy
import com.jaato.a11ybridge.transport.SettleConfig
import com.jaato.a11ybridge.transport.ShotParams
import java.util.concurrent.atomic.AtomicReference

/**
 * The whole active session policy as one immutable value (device design §8).
 *
 * `configure` swaps this reference atomically; everything downstream reads the current
 * reference. No scattered mutable flags — a single source of truth for policy.
 *
 * Fail-closed defaults (§8 / §13): empty packageScope = act on / serialize nothing;
 * password masking on; conservative settle.
 */
data class SessionConfig(
    val settle: SettleConfig = SettleConfig.SAFE_DEFAULT,
    val screenshotDefaults: ShotParams = ShotParams(),
    val redaction: RedactionPolicy = RedactionPolicy.SAFE_DEFAULT,
    val packageScope: List<String> = emptyList(),
) {
    /** Apply a `configure` payload; unspecified sections keep their prior value (§5.1). */
    fun merge(args: ConfigureArgs): SessionConfig = copy(
        settle = args.settle ?: settle,
        screenshotDefaults = args.screenshotDefaults ?: screenshotDefaults,
        redaction = args.redaction ?: redaction,
        packageScope = args.packageScope ?: packageScope,
    )

    companion object {
        val SAFE_DEFAULT = SessionConfig()
    }
}

/** Atomically swappable holder for the active [SessionConfig]. */
class SessionStore {
    private val ref = AtomicReference(SessionConfig.SAFE_DEFAULT)

    fun get(): SessionConfig = ref.get()

    /** Merge a `configure` payload into the current config and publish the result. */
    fun apply(args: ConfigureArgs): SessionConfig {
        while (true) {
            val cur = ref.get()
            val next = cur.merge(args)
            if (ref.compareAndSet(cur, next)) return next
        }
    }

    /** Reset to fail-closed defaults (used on (re)connect before the daemon configures). */
    fun reset() { ref.set(SessionConfig.SAFE_DEFAULT) }
}
