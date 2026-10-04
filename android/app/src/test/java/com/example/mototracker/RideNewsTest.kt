package com.example.mototracker

import com.example.mototracker.RideNews.Change
import com.example.mototracker.RideNews.Seen
import com.example.mototracker.RideNews.change
import org.junit.Assert.assertEquals
import org.junit.Test

class RideNewsTest {

    private val home = Seen(null, false)
    private fun off(trip: Int) = Seen(trip, false)
    private fun on(trip: Int) = Seen(trip, true)

    @Test fun `Start trip is announced`() = assertEquals(Change.OPENED, change(home, off(41), false))

    @Test fun `Start ride is announced`() = assertEquals(Change.STARTED, change(off(41), on(41), false))

    @Test fun `off the bike is announced`() = assertEquals(Change.PAUSED, change(on(41), off(41), true))

    @Test fun `riding again is announced`() = assertEquals(Change.RESUMED, change(off(41), on(41), true))

    @Test fun `End trip is announced`() = assertEquals(Change.ENDED, change(on(41), home, true))

    @Test fun `ending from off the bike is announced`() = assertEquals(Change.ENDED, change(off(41), home, true))

    @Test fun `trip and ride opened between two reads is a start`() =
        assertEquals(Change.STARTED, change(home, on(41), false))

    @Test fun `nothing changed stays quiet`() {
        assertEquals(null, change(on(41), on(41), true))
        assertEquals(null, change(off(41), off(41), false))
        assertEquals(null, change(home, home, false))
    }

    @Test fun `a new trip straight after the last is announced`() =
        assertEquals(Change.OPENED, change(on(41), off(42), true))

    @Test fun `first answer ever, mid-trip, says where things stand`() {
        assertEquals(Change.STARTED, change(null, on(41), true))
        assertEquals(Change.OPENED, change(null, off(41), false))
    }

    @Test fun `first answer ever, nobody out, stays quiet`() = assertEquals(null, change(null, home, false))
}
