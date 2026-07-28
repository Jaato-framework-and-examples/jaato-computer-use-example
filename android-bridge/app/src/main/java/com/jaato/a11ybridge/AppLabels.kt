package com.jaato.a11ybridge

import android.content.Context
import android.content.pm.PackageManager

/**
 * Friendly app-label resolution for the consent surfaces. If a package can't be resolved
 * (not installed / not visible) it falls back to the raw package id — the truthful identifier
 * we do have, never a fabricated name.
 */
internal object AppLabels {
    fun of(ctx: Context, pkg: String): String = try {
        val pm = ctx.packageManager
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
    } catch (e: PackageManager.NameNotFoundException) {
        pkg
    }

    /** Sorted, comma-joined labels for a set of packages. */
    fun list(ctx: Context, pkgs: Set<String>): String =
        pkgs.map { of(ctx, it) }.sorted().joinToString(", ")
}
