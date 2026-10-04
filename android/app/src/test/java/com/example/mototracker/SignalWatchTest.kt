package com.example.mototracker

import com.example.mototracker.SignalWatch.Act
import com.example.mototracker.SignalWatch.act
import org.junit.Assert.assertEquals
import org.junit.Test

/** When the other rider's signal loss chimes, and when it stops being news (Jack, 2026-09-29). */
class SignalWatchTest {

    private val a = "2026-09-29T22:24:15.155+00:00"      // one signal loss
    private val b = "2026-09-29T22:40:02.310+00:00"      // a later one

    @Test fun `heard, nothing said`() = assertEquals(null, act(null, true, null, false))

    @Test fun `unheard under two minutes, nothing said yet`() = assertEquals(null, act(null, true, a, false))

    @Test fun `two minutes unheard alerts`() = assertEquals(Act.ALERT, act(null, true, a, true))

    @Test fun `and only once however many answers follow`() = assertEquals(null, act(a, true, a, true))

    @Test fun `an incident taking over the signal loss says nothing more`() =
        assertEquals(null, act(a, true, a, false))

    @Test fun `the incident closed, still the same signal loss, no second chime`() =
        assertEquals(null, act(a, true, a, true))

    @Test fun `heard again says so`() = assertEquals(Act.BACK, act(a, true, null, false))

    @Test fun `heard again before the alert is not news`() = assertEquals(null, act(null, true, null, false))

    @Test fun `a new signal loss missed its return between two reads, and alerts again`() =
        assertEquals(Act.ALERT, act(a, true, b, true))

    @Test fun `their trip ending withdraws the alert`() = assertEquals(Act.CLEAR, act(a, false, null, false))

    @Test fun `no trip and nothing said stays quiet`() = assertEquals(null, act(null, false, null, false))
}
