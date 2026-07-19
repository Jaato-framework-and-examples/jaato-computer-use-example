package com.jaato.a11ybridge.state

import java.util.concurrent.atomic.AtomicLong

/**
 * Process-wide monotonic "world version" (device design §4).
 *
 * Bumped by the settle detector on every qualifying content/state change, so that
 * `observe`, `settled`, and bundled screenshots can all stamp the same value and the
 * daemon can tell whether the world moved between two frames. Never reused, never reset.
 */
object SnapshotClock {
    private val value = AtomicLong(0)

    val current: Long get() = value.get()

    /** Advance the version; returns the new value. */
    fun bump(): Long = value.incrementAndGet()
}
