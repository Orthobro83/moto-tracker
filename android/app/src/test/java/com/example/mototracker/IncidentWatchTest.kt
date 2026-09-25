package com.example.mototracker

import com.example.mototracker.IncidentWatch.Seen
import com.example.mototracker.IncidentWatch.Sound
import com.example.mototracker.IncidentWatch.plan
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * What a phone does about the incidents it can see, now that both riders can be on
 * a trip at once (Jack, 2026-09-24). Neither rider's incident may hide the other's.
 *
 * Run with: cd android && ./gradlew test
 */
class IncidentWatchTest {

    private fun sos(id: Int, silenced: Boolean = false, riderClosed: Boolean = false) =
        Seen(id, "sos", silenced, riderClosed)
    private fun pending(id: Int) = Seen(id, "pending", false, false)

    @Test fun nothingOpenIsQuiet() {
        assertEquals(IncidentWatch.Plan(Sound.OFF, null), plan(null, null))
    }

    @Test fun myOwnCandidateIsNeverSaidAloud() {
        assertEquals(IncidentWatch.Plan(Sound.OFF, null), plan(pending(1), null))
    }

    @Test fun myOwnIncidentVibratesAndPopsUp() {
        assertEquals(IncidentWatch.Plan(Sound.VIBRATE, "mine:1"), plan(sos(1), null))
    }

    @Test fun myOwnIncidentGoesQuietOnceIHaveAnswered() {
        assertEquals(IncidentWatch.Plan(Sound.OFF, null), plan(sos(1, riderClosed = true), null))
    }

    @Test fun theirIncidentSoundsLoud() {
        assertEquals(IncidentWatch.Plan(Sound.LOUD, "theirs:2"), plan(null, sos(2)))
    }

    @Test fun theirIncidentStillSoundsWhileMyOwnCandidateIsPending() {
        // Before 2026-09-24 my own candidate returned early and swallowed theirs.
        assertEquals(IncidentWatch.Plan(Sound.LOUD, "theirs:2"), plan(pending(1), sos(2)))
    }

    @Test fun theirAlarmWinsOverMyOwnIncident() {
        // Before 2026-09-24 my own incident returned early and theirs never sounded.
        assertEquals(IncidentWatch.Plan(Sound.LOUD, "theirs:2"), plan(sos(1), sos(2)))
    }

    @Test fun silencingTheirsLetsMineBeSeen() {
        assertEquals(IncidentWatch.Plan(Sound.VIBRATE, "mine:1"), plan(sos(1), sos(2, silenced = true)))
    }

    @Test fun theirSilencedIncidentStaysAnnouncedWithoutSound() {
        assertEquals(IncidentWatch.Plan(Sound.OFF, "theirs:2:silenced"), plan(null, sos(2, silenced = true)))
    }

    @Test fun theirCandidateIsAQuietNotice() {
        assertEquals(IncidentWatch.Plan(Sound.OFF, "pending:2"), plan(null, pending(2)))
    }

    @Test fun myIncidentOutranksTheirCandidate() {
        assertEquals(IncidentWatch.Plan(Sound.VIBRATE, "mine:1"), plan(sos(1), pending(2)))
    }
}
