package com.example.mototracker

import android.content.Context

/**
 * This phone's device key for the relay (spike-14, design.md 2026-09-15).
 *
 * Obtained once by typing a single-use pairing code, then kept in the app's
 * private storage and sent on every request. It is never shown on screen and
 * never written to the log. The manifest's allowBackup="false" keeps it out of
 * phone backups, so a new or reset phone pairs afresh rather than inheriting it.
 *
 * Only uninstalling the app, clearing its storage, or Unpair removes it.
 * Installing a newer spike over this one keeps it.
 */
object DeviceKey {
    private const val PREFS = "relay"

    /** Read by the HTTP client on every request, so kept in memory once loaded. */
    @Volatile var current: String? = null
        private set

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun load(ctx: Context) {
        current = prefs(ctx).getString("key", null)
    }

    fun save(ctx: Context, key: String, role: String, rider: String?, name: String) {
        // commit(), not apply(): a key lost to a crash would silently unpair the phone.
        prefs(ctx).edit()
            .putString("key", key)
            .putString("role", role)
            .putString("rider", rider)
            .putString("name", name)
            .commit()
        current = key
    }

    fun clear(ctx: Context) {
        prefs(ctx).edit().clear().commit()
        current = null
    }

    fun describe(ctx: Context): String {
        if (current == null) return "not paired"
        val p = prefs(ctx)
        val rider = p.getString("rider", null)
        return "${p.getString("name", "?")} — ${p.getString("role", "?")}" + (rider?.let { ", $it" } ?: "")
    }
}
