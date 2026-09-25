package com.example.mototracker

import android.content.Context
import android.os.SystemClock
import java.util.Locale

/**
 * Is the server reachable, and for how long has it not been?
 *
 * Logs only the transitions, so a switch-over reads as two lines:
 *   LINK  DOWN (probe: SocketTimeoutException …) networks WIFI✓ VPN✓
 *   LINK  UP after 41.3 s down, 9 failed — networks CELL✓ VPN✓
 * The gap between them is the cost of that switch-over; a DOWN with no UP until
 * a RECONNECT or a NordVPN restart is a stuck tunnel.
 */
object Link {
    private var up: Boolean? = null
    private var downSince = 0L
    private var failed = 0

    @Synchronized
    fun record(ctx: Context, ok: Boolean, source: String, detail: String? = null) {
        val now = SystemClock.elapsedRealtime()
        when {
            ok && up == false -> {
                RideLog.write(ctx, "LINK", String.format(
                    Locale.US, "UP after %.1f s down, %d failed (%s) — networks %s",
                    (now - downSince) / 1000.0, failed, source, NetWatch.snapshot(ctx)
                ))
                up = true; failed = 0
            }
            ok && up == null -> {
                RideLog.write(ctx, "LINK", "UP ($source) — networks ${NetWatch.snapshot(ctx)}")
                up = true
            }
            !ok && up != false -> {
                RideLog.write(ctx, "LINK", "DOWN ($source: ${detail ?: "failed"}) — networks ${NetWatch.snapshot(ctx)}")
                up = false; downSince = now; failed = 1
            }
            !ok -> failed++
        }
    }

    /** Seconds the link has been down, or null if it is up or not yet known. */
    @Synchronized
    fun downSeconds(): Double? =
        if (up == false) (SystemClock.elapsedRealtime() - downSince) / 1000.0 else null

    @Synchronized
    fun describe(): String = when (up) {
        null -> "unknown"
        true -> "up"
        false -> String.format(Locale.US, "down %.0f s, %d failed", downSeconds() ?: 0.0, failed)
    }
}
