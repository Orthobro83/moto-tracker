package com.example.mototracker

import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * What the Observer's inset says it is doing outside.
 *
 * Twice the screen claimed rain over a dry ride — "Thunderstorm" on 2026-09-19 and
 * "Light drizzle" on 2026-09-20 — because the weather code said so while the model
 * carried 0.0–0.1 mm of rain behind it. The label follows the quantities now.
 */
class WeatherTest {

    private fun label(code: Int, precip: Double, cloud: Int, day: Boolean = true) =
        Weather.describe(code, day, precip, cloud).second

    @Test
    fun aTraceOfRainIsNotRain() {
        // The two real ones: drizzle code over a dry, cloudy sky, and a thunderstorm
        // code over a dry one.
        assertEquals("Overcast", label(51, 0.1, 98))
        assertEquals("Partly cloudy", label(95, 0.0, 40))
    }

    @Test
    fun realRainIsStillRain() {
        assertEquals("Drizzle", label(51, 0.4, 98))
        assertEquals("Rain", label(63, 2.0, 100))
        assertEquals("Showers", label(80, 1.2, 90))
        assertEquals("Thunderstorm", label(95, 3.5, 100))
    }

    @Test
    fun aClearSkyReadsAsOne() {
        assertEquals("Sunny", label(0, 0.0, 5))
        assertEquals("Clear", label(0, 0.0, 5, day = false))
        assertEquals("Partly cloudy", label(2, 0.0, 45))
        assertEquals("Cloudy", label(3, 0.0, 70))
    }

    @Test
    fun fogIsAboutSeeing() {
        assertEquals("Fog", label(45, 0.0, 100))
        assertEquals("Fog", label(48, 0.3, 100))
    }

    @Test
    fun withNoCloudFigureItSaysNothingRatherThanGuessing() {
        assertEquals("—", label(3, 0.0, -1))
    }
}
