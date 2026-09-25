package com.example.mototracker

import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * The road's pace follows the rider's speed (Jack, 2026-09-24): still at 0 km/h, full
 * at 100 km/h and above, in 5% steps of 5 km/h each.
 *
 * Run with: cd android && ./gradlew test
 */
class SceneTest {

    private fun rate(kmh: Double) = roadRate(kmh)

    @Test fun stoppedIsStill() {
        assertEquals(0f, rate(0.0))
    }

    @Test fun gpsJitterWhileStoppedStaysStill() {
        for (kmh in listOf(0.4, 1.0, 2.8, 4.99)) assertEquals(0f, rate(kmh), "at $kmh km/h")
    }

    @Test fun everyFiveKmhIsAnotherFivePercent() {
        for (step in 1..20) {
            assertEquals(step * 0.05f, rate(step * 5.0), 1e-6f, "at ${step * 5} km/h")
        }
    }

    @Test fun inBetweenRoundsDown() {
        assertEquals(0.05f, rate(9.9), 1e-6f)
        assertEquals(0.50f, rate(52.0), 1e-6f)
        assertEquals(0.95f, rate(99.9), 1e-6f)
    }

    @Test fun fullSpeedAtAHundredAndNoFaster() {
        for (kmh in listOf(100.0, 101.0, 140.0, 300.0)) assertEquals(1f, rate(kmh), 1e-6f, "at $kmh km/h")
    }

    @Test fun noSpeedYetIsStill() {
        assertEquals(0f, rate(Double.NaN))
        assertEquals(0f, rate(-1.0))
    }
}
