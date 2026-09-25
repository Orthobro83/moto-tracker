package com.example.mototracker

import android.content.Context

/**
 * Which rider this phone is. The real app picks this once on first launch
 * (design.md: one APK, identity chosen and remembered). The spike just needs
 * the two phones not to report as the same person.
 */
object Identity {
    private const val PREFS = "spike"
    private const val KEY = "rider"

    fun get(ctx: Context): String =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, "jack") ?: "jack"

    /** Set from the pairing response: a rider key is bound to one rider. */
    fun set(ctx: Context, rider: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY, rider).commit()
        RideLog.write(ctx, "IDENT", "rider set to $rider by pairing")
    }

    fun toggle(ctx: Context): String {
        val next = if (get(ctx) == "jack") "dana" else "jack"
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY, next).apply()
        RideLog.write(ctx, "IDENT", "rider set to $next")
        return next
    }
}
