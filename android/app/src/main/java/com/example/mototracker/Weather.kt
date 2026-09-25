package com.example.mototracker

import android.content.Context
import kotlin.math.roundToInt
import org.json.JSONObject

/**
 * Weather for the Observer's inset: one icon, the wind under it (Jack, 2026-09-16).
 *
 * Open-Meteo, which needs no account and no key. The position is snapped onto a
 * grid — about five kilometres — before it leaves the phone, so a weather service
 * never learns exactly where either of them is.
 */
object Weather {

    data class Now(
        val icon: String,          // one glyph: what it is doing outside
        val label: String,
        val tempC: Double?,
        val windKmh: Double,
        val windFrom: String,      // compass point the wind is coming from
        val atMs: Long,
    ) {
        /** "14 km/h NE" — the line under the icon. */
        val wind: String get() = "${windKmh.roundToInt()} km/h $windFrom"
    }

    @Volatile private var cached: Now? = null
    @Volatile private var cachedFor: Pair<Double, Double>? = null
    @Volatile private var lastTryMs = 0L

    private const val FRESH_MS = 10 * 60 * 1000L

    /**
     * Older than this and it is not worth showing. A reading kept from an hour ago
     * is how the Observer's screen came to say "Thunderstorm" over a sunny ride
     * (2026-09-20): a fetch that failed simply left the last one on screen, with
     * nothing to say how old it was.
     */
    private const val STALE_MS = 45 * 60 * 1000L

    /**
     * How far the rider must travel before it is worth asking again — about five
     * kilometres. It used to be one, which at riding speed asked Open-Meteo for a
     * fresh answer every couple of minutes, all day, from one address.
     */
    private const val GRID = 0.05

    fun current(): Now? = cached?.takeIf { System.currentTimeMillis() - it.atMs < STALE_MS }

    /** Blocking; call off the main thread. Returns the cached answer if it is fresh. */
    fun fetch(ctx: Context, lat: Double, lon: Double): Now? {
        val key = Pair(snap(lat), snap(lon))
        val started = System.currentTimeMillis()
        cached?.let {
            if (cachedFor == key && started - it.atMs < FRESH_MS) return it
        }
        // Whatever the rider has ridden past, one look every ten minutes is enough.
        if (started - lastTryMs < FRESH_MS) return current()
        lastTryMs = started
        val url = "https://api.open-meteo.com/v1/forecast?latitude=${key.first}" +
            "&longitude=${key.second}&current=temperature_2m,weather_code,precipitation," +
            "cloud_cover,wind_speed_10m,wind_direction_10m,is_day&wind_speed_unit=kmh"
        val body = runCatching {
            java.net.URL(url).openConnection().apply {
                connectTimeout = 8000
                readTimeout = 8000
            }.getInputStream().bufferedReader().readText()
        }.getOrElse {
            RideLog.write(ctx, "WX", "failed: ${it.javaClass.simpleName} — keeping the last reading")
            return current()
        }
        val cur = runCatching { JSONObject(body).getJSONObject("current") }.getOrNull()
        if (cur == null) {
            RideLog.write(ctx, "WX", "no current conditions in the answer")
            return current()
        }
        val code = cur.optInt("weather_code", -1)
        val day = cur.optInt("is_day", 1) == 1
        val precip = cur.optDouble("precipitation", 0.0).let { if (it.isNaN()) 0.0 else it }
        val cloud = cur.optInt("cloud_cover", -1)
        val (icon, label) = describe(code, day, precip, cloud)
        val now = Now(
            icon = icon, label = label,
            tempC = cur.optDouble("temperature_2m").takeIf { !it.isNaN() },
            windKmh = cur.optDouble("wind_speed_10m", 0.0),
            windFrom = compass(cur.optDouble("wind_direction_10m", 0.0)),
            atMs = System.currentTimeMillis())
        cached = now
        cachedFor = key
        // Written down, so what the screen said at a given moment can be checked
        // against what the service was saying (Jack, 2026-09-20).
        RideLog.write(ctx, "WX", "code $code -> $label · ${now.tempC ?: "?"}C ${now.wind}" +
            " · rain ${precip}mm cloud ${cloud}% at ${key.first},${key.second}")
        return now
    }

    /** Below this, the model has a trace and nothing a rider would call rain. */
    private const val WET_MM = 0.2

    /**
     * What to show. **Rain is only claimed when there is rain to claim** (Jack,
     * 2026-09-20). Twice now the code has said drizzle or thunderstorm over a dry
     * ride with 0.0–0.1 mm behind it; the code is the model's opinion, while
     * precipitation and cloud cover are quantities it actually carries. Fog is the
     * exception: it is about seeing, not wetness, so the code keeps it.
     */
    internal fun describe(code: Int, day: Boolean, precipMm: Double, cloud: Int): Pair<String, String> {
        if (code == 45 || code == 48) return "≋" to "Fog"
        if (precipMm >= WET_MM) return when (code) {
            in 51..57 -> "🌧" to "Drizzle"
            in 61..67 -> "🌧" to "Rain"
            in 71..77 -> "❄" to "Snow"
            in 80..82 -> "🌧" to "Showers"
            85, 86 -> "❄" to "Snow showers"
            95, 96, 99 -> "⛈" to "Thunderstorm"
            else -> "🌧" to "Rain"
        }
        return when {
            cloud < 0 -> if (day) "☀" to "—" else "☾" to "—"      // nothing to go on
            cloud < 20 -> if (day) "☀" to "Sunny" else "☾" to "Clear"
            cloud < 60 -> if (day) "⛅" to "Partly cloudy" else "☾" to "Partly cloudy"
            cloud < 88 -> if (day) "⛅" to "Cloudy" else "☾" to "Cloudy"
            else -> "☁" to "Overcast"
        }
    }

    /** Rounded onto the grid, and tidied: 13.5, not 13.500000000000002. */
    private fun snap(v: Double): Double = Math.round(Math.round(v / GRID) * GRID * 100) / 100.0

    private fun compass(degrees: Double): String {
        val points = listOf("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        val i = (((degrees % 360) + 360) % 360 / 45.0).roundToInt() % 8
        return points[i]
    }
}
