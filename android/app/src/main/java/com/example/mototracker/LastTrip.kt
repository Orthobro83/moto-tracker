package com.example.mototracker

import android.content.Context
import org.json.JSONObject

/**
 * The previous trip, as the home screen shows it.
 *
 * The relay keeps a closed trip for six hours and then forgets it — the Mini holds
 * the history, not the VPS. So the phone keeps the last summary it saw, and the home
 * screen still has something to show the next morning.
 */
object LastTrip {

    data class Summary(
        val day: String, val started: String, val ended: String, val duration: String,
        val maxSpeed: String, val avgSpeed: String, val peakG: String, val peakRot: String,
    )

    private const val PREFS = "trip"
    private const val KEY = "last_trip"

    /** Stores a fresh summary if there is one, and returns the best available. */
    fun remember(ctx: Context, fresh: JSONObject?): Summary? {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (fresh != null && fresh.optString("started_at").isNotEmpty()) {
            prefs.edit().putString(KEY, fresh.toString()).apply()
            return format(fresh)
        }
        val kept = prefs.getString(KEY, null) ?: return null
        return runCatching { format(JSONObject(kept)) }.getOrNull()
    }

    private fun format(o: JSONObject): Summary {
        val started = Time.parse(o.optString("started_at"))
        val ended = Time.parse(o.optString("ended_at"))
        fun clock(ms: Long?) = ms?.let {
            java.text.SimpleDateFormat("HH:mm", java.util.Locale.US).format(java.util.Date(it))
        } ?: "—"
        val seconds = o.optDouble("duration_s", Double.NaN)
        return Summary(
            day = started?.let {
                java.text.SimpleDateFormat("EEE d MMM", java.util.Locale.US)
                    .format(java.util.Date(it))
            } ?: "—",
            started = clock(started),
            ended = clock(ended),
            duration = if (seconds.isNaN()) "—" else String.format(
                java.util.Locale.US, "%d:%02d:%02d",
                (seconds / 3600).toInt(), ((seconds % 3600) / 60).toInt(), (seconds % 60).toInt()),
            maxSpeed = number(o, "max_speed", 0, " km/h"),
            avgSpeed = number(o, "avg_speed", 0, " km/h"),
            peakG = number(o, "max_g", 2, " g"),
            peakRot = number(o, "max_rot", 2, " rad/s"),
        )
    }

    private fun number(o: JSONObject, key: String, digits: Int, unit: String): String {
        val v = o.optDouble(key, Double.NaN)
        return if (v.isNaN()) "—" else String.format(java.util.Locale.US, "%.${digits}f$unit", v)
    }
}
