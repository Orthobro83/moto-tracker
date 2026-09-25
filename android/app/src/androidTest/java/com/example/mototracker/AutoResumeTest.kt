package com.example.mototracker

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Switching auto-resume off, for a trip on foot, on a bicycle or in a car (Jack,
 * 2026-09-20). The trip is still recorded; the bike machinery never wakes up.
 *
 * It needs real preferences, so it runs on a device: ./gradlew connectedDebugAndroidTest
 */
@RunWith(AndroidJUnit4::class)
class AutoResumeTest {

    private val ctx = InstrumentationRegistry.getInstrumentation().targetContext

    @Before
    fun offTheBike() {
        Trip.pauseRide(ctx)                 // the relay call fails here; the state is local
        Trip.setAutoResume(ctx, true)
    }

    @After
    fun tidyUp() {
        Trip.setAutoResume(ctx, true)
        Trip.endTrip(ctx)
    }

    @Test
    fun ridingSpeedNormallyStartsTheRide() {
        assertFalse("one tick is not enough", Trip.noticeMovement(ctx, 60.0))
        assertTrue("two ticks at riding speed is riding", Trip.noticeMovement(ctx, 60.0))
    }

    @Test
    fun switchedOffNothingStartsTheRide() {
        Trip.setAutoResume(ctx, false)
        repeat(10) { assertFalse("it started the ride anyway", Trip.noticeMovement(ctx, 90.0)) }
        assertFalse("and the sensors stay off", Trip.sensorsWanted(ctx))
    }

    @Test
    fun switchingItBackOnStartsWorkingAgain() {
        Trip.setAutoResume(ctx, false)
        repeat(3) { Trip.noticeMovement(ctx, 90.0) }
        Trip.setAutoResume(ctx, true)
        assertFalse("the count starts again from here", Trip.noticeMovement(ctx, 60.0))
        assertTrue("and then it resumes", Trip.noticeMovement(ctx, 60.0))
    }
}
