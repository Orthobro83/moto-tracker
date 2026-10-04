package com.example.mototracker

import java.time.LocalDateTime
import java.time.ZoneOffset
import kotlin.math.abs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Sunrise and sunset against the US Naval Observatory's published times
 * (aa.usno.navy.mil/api/rstt/oneday, fetched 2026-09-29), which are given to the
 * minute — hence a minute and a half of slack either way.
 */
class SunTest {

    private val lat = 13.6929                  // San Salvador, where the maps open
    private val lon = -89.2182

    private fun at(y: Int, mo: Int, d: Int, h: Int, mi: Int, utcOffsetH: Int = -6): Long =
        LocalDateTime.of(y, mo, d, h, mi).toInstant(ZoneOffset.ofHours(utcOffsetH)).toEpochMilli()

    private fun epochDay(y: Int, mo: Int, d: Int): Long = java.time.LocalDate.of(y, mo, d).toEpochDay()

    private fun near(what: String, usno: Long, ours: Long) = assertTrue(
        "$what is ${(ours - usno) / 1000} s from the Naval Observatory's", abs(ours - usno) <= 90_000)

    private fun agrees(y: Int, mo: Int, d: Int, rise: Pair<Int, Int>, set: Pair<Int, Int>) {
        val day = Sun.day(epochDay(y, mo, d), lat, lon)
        near("sunrise $y-$mo-$d", at(y, mo, d, rise.first, rise.second), day.sunriseMs)
        near("sunset $y-$mo-$d", at(y, mo, d, set.first, set.second), day.sunsetMs)
    }

    @Test fun `today, the day of the false alarm`() = agrees(2026, 9, 29, 5 to 46, 17 to 48)

    @Test fun `the June solstice`() = agrees(2026, 6, 21, 5 to 31, 18 to 27)

    @Test fun `the December solstice`() = agrees(2026, 12, 21, 6 to 15, 17 to 35)

    @Test fun `the latest sunrise of the year, in February`() = agrees(2027, 2, 5, 6 to 23, 17 to 59)

    @Test fun `solar noon falls where the observatory puts it`() =
        near("noon 2026-09-29", at(2026, 9, 29, 11, 47), Sun.day(epochDay(2026, 9, 29), lat, lon).noonMs)

    @Test fun `light from sunrise, dark from sunset`() {
        assertFalse("dark two minutes before sunrise", Sun.isUp(at(2026, 9, 29, 5, 44), lat, lon))
        assertTrue("light two minutes after sunrise", Sun.isUp(at(2026, 9, 29, 5, 48), lat, lon))
        assertTrue("light two minutes before sunset", Sun.isUp(at(2026, 9, 29, 17, 46), lat, lon))
        assertFalse("dark two minutes after sunset", Sun.isUp(at(2026, 9, 29, 17, 50), lat, lon))
        assertFalse("dark at midnight", Sun.isUp(at(2026, 9, 29, 0, 0), lat, lon))
        assertTrue("light at noon", Sun.isUp(at(2026, 9, 29, 12, 0), lat, lon))
    }

    @Test fun `the day moves with the seasons, not the clock`() {
        // 05:50: well after the June sunrise (05:31), well before February's (06:23).
        assertTrue(Sun.isUp(at(2026, 6, 21, 5, 50), lat, lon))
        assertFalse(Sun.isUp(at(2027, 2, 5, 5, 50), lat, lon))
        // 18:00: before the June sunset (18:27), after December's (17:35).
        assertTrue(Sun.isUp(at(2026, 6, 21, 18, 0), lat, lon))
        assertFalse(Sun.isUp(at(2026, 12, 21, 18, 0), lat, lon))
    }

    @Test fun `a sunset on the next UTC date still ends its own day`() {
        // Los Angeles, 2026-06-21 in UTC: the observatory lists the previous evening's
        // sunset at 03:07 and the sunrise at 12:42.
        val la = 34.0522 to -118.2437
        near("LA sunrise", at(2026, 6, 21, 12, 42, 0), Sun.day(epochDay(2026, 6, 21), la.first, la.second).sunriseMs)
        near("LA sunset", at(2026, 6, 21, 3, 7, 0), Sun.day(epochDay(2026, 6, 20), la.first, la.second).sunsetMs)
        assertTrue("still light at 02:00 UTC", Sun.isUp(at(2026, 6, 21, 2, 0, 0), la.first, la.second))
        assertFalse("dark at 04:00 UTC", Sun.isUp(at(2026, 6, 21, 4, 0, 0), la.first, la.second))
        assertTrue("light again at 13:00 UTC", Sun.isUp(at(2026, 6, 21, 13, 0, 0), la.first, la.second))
    }

    @Test fun `no sunrise or no sunset is handled, not a crash`() {
        val svalbard = 78.22 to 15.65
        assertFalse("polar night", Sun.isUp(at(2026, 12, 21, 12, 0, 0), svalbard.first, svalbard.second))
        assertTrue("midnight sun", Sun.isUp(at(2026, 6, 21, 0, 0, 0), svalbard.first, svalbard.second))
    }

    @Test fun `the log line reads as local sunrise and sunset`() {
        val was = java.util.TimeZone.getDefault()
        try {
            java.util.TimeZone.setDefault(java.util.TimeZone.getTimeZone("America/El_Salvador"))
            assertEquals("05:46–17:48", Sun.describe(at(2026, 9, 29, 9, 0), lat, lon))
        } finally {
            java.util.TimeZone.setDefault(was)
        }
    }
}
