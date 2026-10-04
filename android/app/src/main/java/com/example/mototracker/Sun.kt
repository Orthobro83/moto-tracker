package com.example.mototracker

import kotlin.math.acos
import kotlin.math.asin
import kotlin.math.cos
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.tan

/**
 * Sunrise and sunset from latitude, longitude and date (Jack, 2026-09-29). The phone's
 * map is light by day and dark by night, and the day is the real one where the rider
 * is: in San Salvador the sun rises at 05:31 in June and 06:23 in February, and sets
 * at 17:35 in December and 18:27 in June. A fixed clock time would be up to fifty
 * minutes wrong.
 *
 * NOAA's solar equations (after Meeus), good to about a minute. Sunrise and sunset
 * are the official ones: the sun's upper edge on the horizon, allowing for refraction
 * (a zenith of 90.833°). Plain Kotlin — no Android, no network — so SunTest holds it
 * to the US Naval Observatory's published times.
 */
object Sun {

    private const val MINUTE_MS = 60_000L
    private const val HOUR_MS = 60 * MINUTE_MS
    private const val DAY_MS = 24 * HOUR_MS
    private const val ZENITH = 90.833

    /**
     * One solar day: the sun is up from [sunriseMs] until [sunsetMs] (UTC instants).
     * Where it never sets that day they are noon ± 12 h; where it never rises, both
     * are noon — so "is it up" is always the same test.
     */
    data class Day(val noonMs: Long, val sunriseMs: Long, val sunsetMs: Long) {
        operator fun contains(atMs: Long) = atMs in sunriseMs until sunsetMs
    }

    /** The solar day whose noon falls on UTC date [epochDay]. */
    fun day(epochDay: Long, lat: Double, lon: Double): Day {
        val start = epochDay * DAY_MS
        // Noon in two steps: the equation of time barely moves within a day.
        var noon = start + minutes(720 - 4 * lon - position(start + 12 * HOUR_MS).eqTimeMin)
        noon = start + minutes(720 - 4 * lon - position(noon).eqTimeMin)
        val cosH = cosHourAngle(lat, position(noon).declDeg)
        if (cosH < -1) return Day(noon, noon - 12 * HOUR_MS, noon + 12 * HOUR_MS)   // midnight sun
        if (cosH > 1) return Day(noon, noon, noon)                                   // polar night
        val halfDay = Math.toDegrees(acos(cosH))
        // Each event again with the sun's position at that moment, not at noon.
        fun event(sign: Int): Long {
            val p = position(noon + sign * minutes(4 * halfDay))
            val c = cosHourAngle(lat, p.declDeg).coerceIn(-1.0, 1.0)
            return start + minutes(720 - 4 * lon - p.eqTimeMin + sign * 4 * Math.toDegrees(acos(c)))
        }
        return Day(noon, event(-1), event(+1))
    }

    /**
     * Is the sun up at [atMs], at this place? The days either side are asked too, so
     * a sunset that falls on the next UTC date — any evening west of the Americas'
     * Pacific coast — still counts for the day it ends.
     */
    fun isUp(atMs: Long, lat: Double, lon: Double): Boolean {
        val today = Math.floorDiv(atMs, DAY_MS)
        return (today - 1..today + 1).any { atMs in day(it, lat, lon) }
    }

    /**
     * Today's sunrise and sunset in this phone's local time, for the log: "05:46–17:48",
     * rounded to the nearest minute as almanacs give them.
     */
    fun describe(atMs: Long, lat: Double, lon: Double): String {
        val today = Math.floorDiv(atMs, DAY_MS)
        val d = (today - 1..today + 1).map { day(it, lat, lon) }
            .minBy { kotlin.math.abs(it.noonMs - atMs) }
        val f = java.text.SimpleDateFormat("HH:mm", java.util.Locale.US)
        val hhmm = { ms: Long -> f.format(java.util.Date(ms + MINUTE_MS / 2)) }
        return "${hhmm(d.sunriseMs)}–${hhmm(d.sunsetMs)}"
    }

    private fun minutes(m: Double): Long = (m * MINUTE_MS).toLong()

    private fun cosHourAngle(lat: Double, declDeg: Double): Double {
        val phi = Math.toRadians(lat)
        val delta = Math.toRadians(declDeg)
        return cos(Math.toRadians(ZENITH)) / (cos(phi) * cos(delta)) - tan(phi) * tan(delta)
    }

    private class Position(val declDeg: Double, val eqTimeMin: Double)

    /** The sun's declination and the equation of time at an instant (NOAA). */
    private fun position(ms: Long): Position {
        val t = (ms / 86_400_000.0 + 2440587.5 - 2451545.0) / 36525.0     // Julian centuries from J2000
        val l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)).mod(360.0)
        val m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
        val e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
        val mr = Math.toRadians(m)
        val centre = sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
            sin(2 * mr) * (0.019993 - 0.000101 * t) + sin(3 * mr) * 0.000289
        val omega = Math.toRadians(125.04 - 1934.136 * t)
        val apparent = Math.toRadians(l0 + centre - 0.00569 - 0.00478 * sin(omega))
        val obliquity = Math.toRadians(
            23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0 +
                0.00256 * cos(omega))
        val decl = Math.toDegrees(asin(sin(obliquity) * sin(apparent)))
        val y = tan(obliquity / 2).pow(2)
        val l0r = Math.toRadians(l0)
        val eqTime = 4 * Math.toDegrees(
            y * sin(2 * l0r) - 2 * e * sin(mr) + 4 * e * y * sin(mr) * cos(2 * l0r) -
                0.5 * y * y * sin(4 * l0r) - 1.25 * e * e * sin(2 * mr))
        return Position(decl, eqTime)
    }
}
