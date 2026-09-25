package com.example.mototracker

/*
 * WARNING: UNTESTED SAFETY LOGIC. DO NOT RELY ON THIS.
 *
 * This crash detection has never been tested against a real crash, because no
 * crash data exists. It has only been tuned to stay quiet during normal riding.
 * It may miss a real crash entirely, fire when nothing happened, or fail
 * silently because of a dead battery, lost signal, OS power management or a bug.
 * It is not a safety device, emergency service or substitute for one. Nobody
 * should rely on it, ever, for anyone's safety. See the README.
 */

import kotlin.math.max

/**
 * Crash detection. Plain Kotlin — no Android, no clock, no I/O — so the same code
 * that runs on the bike can be replayed over the whole archive in a unit test.
 *
 * ## Why it is built this way
 *
 * There are no crash samples, and there never will be: nobody is going to crash on
 * purpose to provide one. So nothing here is learned from examples of a crash. What
 * the archive gives is the opposite, and it is enough — **how violent normal riding
 * gets for each rider**:
 *
 *     Dana  peak 3.65 g median, 8.75 g at p99.9, 8.79 g worst    rot 5.54 at p99.9
 *     Jack  peak 2.19 g median, 5.88 g at p99.9, 5.88 g worst    rot 3.81 at p99.9
 *
 * Her ordinary riding reaches forces that would be a fair guess at "crash" for him.
 * A single fixed threshold would therefore either miss his crash or fire all day on
 * hers, which is why every threshold below is **relative to that rider's own
 * baseline**, and why no single signal is ever enough on its own.
 *
 * ## The test
 *
 * A candidate needs **an impact** and **a consequence**, within a few seconds:
 *
 *  - IMPACT — a window whose peak acceleration stands far outside this rider's
 *    normal riding, or whose rotation does — **while they were actually riding**.
 *    Below riding speed a much higher bar applies and rotation is ignored, because
 *    the archive's wildest ordinary readings come from driveways and filtering, not
 *    from riding (Jack, 2026-09-16).
 *  - CONSEQUENCE — the speed collapses from riding speed to a stop, and stays
 *    there. A bike that is still moving normally has not crashed, whatever the
 *    accelerometer felt going over a pothole.
 *
 * Both must hold. The rider is then asked, and has [Config.CONFIRM_WINDOW_S]
 * seconds to say it was nothing before anyone is alarmed — which is the real
 * safety net under all of this, and why the thresholds are set to catch rather
 * than to be clever.
 */
object Detector {

    /** One 5-second window, the same shape the phone already sends. */
    data class Window(
        val atMs: Long,
        val speedKmh: Double,
        val peakG: Double,
        val meanG: Double,
        val peakRot: Double,
        val accelN: Int = 0,
    )

    /**
     * What normal riding looks like for one rider, from their own archive.
     * `analysis/baseline.py` produces these, and the app re-derives them as the
     * archive grows, so a new bike or a new phone mount moves the numbers with it.
     */
    data class Baseline(
        val rider: String,
        val gP999: Double,
        val gMax: Double,
        val rotP999: Double,
        val rotMax: Double,
        /** Hardest ordinary braking seen, km/h lost per second. */
        val decelMax: Double,
        val movingSamples: Int = 0,
    ) {
        /**
         * An impact must clear the rider's worst ordinary jolt by half again, and
         * a floor no rider's normal riding has ever approached. Dana's worst
         * ordinary window is 8.79 g, so hers lands at 13.2 g; Jack's 5.88 g would
         * give 8.8 g, which the floor lifts to 11 g.
         */
        val impactG: Double get() = max(max(gMax, gP999) * 1.5, IMPACT_G_FLOOR)

        /** Same rule for rotation: a highside spins the bike far past a pothole. */
        val impactRot: Double get() = max(max(rotMax, rotP999) * 1.5, IMPACT_ROT_FLOOR)

        /** A collapse in speed that no braking of theirs has ever produced. */
        val collapseKmhPerS: Double get() = max(decelMax * 1.6, COLLAPSE_FLOOR)

        /**
         * Below walking pace the bar is much higher (Jack, 2026-09-16). Dana's
         * worst reading in the whole archive — 8.79 g — is her going over the bump
         * at the end of their driveway, and bumps at a red light or a kerb look the
         * same. At those speeds there is no ride to interrupt, so an ordinary
         * deviation means nothing and only a violent one does: being hit from
         * behind while stopped. That is what this threshold is for, and it sits
         * far above anything a kerb or a speed bump has ever produced.
         */
        val rearEndG: Double get() = max(impactG * REAR_END_MARGIN, REAR_END_G_FLOOR)

        companion object {
            /**
             * Until a rider has ridden enough for their own numbers, they get the
             * harder of the two known riders' — it is better to ask a rider who is
             * fine than to stay quiet for one who is not, but a brand-new baseline
             * must not be so tight that every ride sets it off.
             */
            val UNKNOWN = Baseline("unknown", gP999 = 8.75, gMax = 8.79,
                rotP999 = 5.54, rotMax = 6.04, decelMax = 6.7)
        }
    }

    // Floors, so a quiet baseline can never make the detector hysterical. No normal
    // riding in the archive comes within 20% of any of them.
    const val IMPACT_G_FLOOR = 11.0          // g, resultant over a 5 s window
    const val IMPACT_ROT_FLOOR = 8.0         // rad/s
    const val COLLAPSE_FLOOR = 11.0          // km/h lost per second

    /** Below this the bike is not riding. Above it, a crash has something to stop. */
    const val RIDING_KMH = 25.0
    const val STOPPED_KMH = 8.0

    /** Walking pace. Below it, only a rear-end-sized impact counts at all. */
    const val SLOW_KMH = 5.0
    const val REAR_END_MARGIN = 1.8
    const val REAR_END_G_FLOOR = 20.0

    /** How long it must stay down, counted from when it actually stopped. */
    const val STILL_S = 15

    /**
     * How long the bike may take to come to rest and still count as the impact's
     * doing — **eight seconds, plus one for every 10 km/h it was carrying**.
     *
     * A flat number cannot work at both ends. Twenty seconds was the original, and
     * the dwell had to fit inside it too, so the bike had to be stopped within about
     * eight: a slide from 120 km/h at 0.3 g takes eleven, and missed. Simply raising
     * the number lets in the opposite error — a rider who hits an expansion joint,
     * rides on for half a minute and parks normally. Scaling with speed separates
     * them: a slide sheds speed at 10 km/h per second or more, a rider carrying on
     * does not.
     *
     *     120 km/h -> 12 s      60 km/h -> 8 s      stopped -> 8 s
     */
    fun stopDeadlineS(speedAtImpact: Double): Double =
        minOf(max(8.0, speedAtImpact / 10.0), STOP_WITHIN_CAP_S)

    /** Nothing waits longer than this, whatever the speed. */
    const val STOP_WITHIN_CAP_S = 45.0

    /** Riding again after the jolt means it was not a crash. */
    const val RESUMED_KMH = 8.0

    /** Above this the bike is moving under power, for the recent-motion test. */
    const val MOVING_KMH = 15.0

    /** How recently the bike must have been moving for a low-speed blow to count. */
    const val RECENT_MOTION_S = 180

    /** Hysteresis around the band edge, so 25 km/h is not a cliff. */
    const val RIDING_ENTER_KMH = 28.0
    const val RIDING_LEAVE_KMH = 22.0

    /** A window with fewer samples than this was throttled by the OS; ignore its peak. */
    const val MIN_ACCEL_SAMPLES = 100

    /** A phone lying on the ground: about a g, and not turning. */
    const val REST_SPEED_CEILING = 20.0
    const val REST_ROT = 1.5
    const val REST_G = 2.5

    /** How much of the dwell must be at rest, allowing for GPS chatter. */
    const val REST_FRACTION = 0.7

    /** What the detector concluded, and everything needed to explain it afterwards. */
    data class Verdict(
        val crash: Boolean,
        val reason: String,
        /** impact (something interrupted a ride) | rear-end (hit while stopped) */
        val kind: String = "impact",
        val impactG: Double = 0.0,
        val impactRot: Double = 0.0,
        val speedBefore: Double = 0.0,
        val speedAfter: Double = 0.0,
        val decelKmhPerS: Double = 0.0,
        val atMs: Long = 0,
    )

    /**
     * Feed windows in order. Holds only what it needs: the last half-minute.
     *
     * Not thread-safe by design — one rider, one sensor thread, one instance.
     */
    class Engine(var baseline: Baseline = Baseline.UNKNOWN) {
        private val recent = ArrayDeque<Window>()
        private var lastRidingAtMs = 0L        // last time the bike was genuinely moving
        private var inRidingBand = false       // hysteresis, so 25 km/h is not a cliff

        // The armed impact and everything being judged against it.
        private var impact: Window? = null
        private var impactAtMs = 0L
        private var speedBeforeImpact = 0.0
        private var impactWasSlow = false
        private var impactAtWalkingPace = false
        private var minSinceImpact = Double.MAX_VALUE
        private var firstStopAtMs = 0L

        /** Cleared when a trip ends, or after a candidate is raised. */
        fun reset() {
            recent.clear()
            disarm()
            lastRidingAtMs = 0
            inRidingBand = false
        }

        private fun disarm() {
            impact = null
            impactAtMs = 0
            speedBeforeImpact = 0.0
            minSinceImpact = Double.MAX_VALUE
            firstStopAtMs = 0
        }

        val armedImpact: Window? get() = impact

        /**
         * How fast the bike was going just before this window. The SECOND highest of
         * the last four, not the highest: at low speed a single GPS sample can read
         * several km/h high, and one of those must not decide which threshold applies.
         */
        private fun speedBefore(): Double {
            val last = recent.takeLast(4).map { it.speedKmh }.sortedDescending()
            return when {
                last.isEmpty() -> 0.0
                last.size == 1 -> last[0]
                else -> last[1]
            }
        }

        /** A window whose sensor stream was throttled cannot be trusted for a peak. */
        private fun trustworthy(w: Window) = w.accelN == 0 || w.accelN >= MIN_ACCEL_SAMPLES

        /**
         * At rest: stopped by GPS, or slow with the phone lying still. After a crash
         * the GPS flickers through 5–15 km/h while the phone sits in the grass, and a
         * strictly consecutive count of "≤ 8 km/h" would throw the case away. The IMU
         * settles the question: a phone on the ground reads about 1 g and no rotation.
         */
        private fun atRest(w: Window) = w.speedKmh <= STOPPED_KMH ||
            (w.speedKmh <= REST_SPEED_CEILING && w.peakRot <= REST_ROT && w.peakG <= REST_G)

        fun offer(w: Window): Verdict? {
            recent.addLast(w)
            while (recent.size > 60) recent.removeFirst()          // five minutes
            if (w.speedKmh >= MOVING_KMH) lastRidingAtMs = w.atMs

            val before = speedBefore()
            // Hysteresis: the riding band is entered at 28 km/h and left only below
            // 22, so a bike hovering around 25 does not flip thresholds every tick.
            if (before >= RIDING_ENTER_KMH) inRidingBand = true
            else if (before < RIDING_LEAVE_KMH) inRidingBand = false

            // ---- impact
            if (impact == null && trustworthy(w)) {
                val armedNow = if (inRidingBand) {
                    // Riding: the rider's own baseline, exceeded on either axis.
                    w.peakG >= baseline.impactG || w.peakRot >= baseline.impactRot
                } else {
                    // Below riding speed nothing is being interrupted, and a bike is
                    // wrestled around at these speeds, so rotation says nothing and
                    // only a vehicle-sized blow counts. It counts ONLY if the bike was
                    // riding recently: stopped at a light, yes; parked outside a café
                    // with the trip still open, no — that is a bike being knocked
                    // over, not a rider being hit (Jack, 2026-09-16).
                    w.peakG >= baseline.rearEndG &&
                        lastRidingAtMs > 0 && w.atMs - lastRidingAtMs <= RECENT_MOTION_S * 1000L
                }
                if (armedNow) {
                    impact = w
                    impactAtMs = w.atMs
                    speedBeforeImpact = before
                    impactWasSlow = !inRidingBand
                    impactAtWalkingPace = before < SLOW_KMH
                    minSinceImpact = w.speedKmh
                    firstStopAtMs = if (w.speedKmh <= STOPPED_KMH) w.atMs else 0
                }
            }

            // ---- consequence
            val armed = impact ?: return null
            val sinceImpactMs = w.atMs - impactAtMs
            if (w.atMs > impactAtMs) {
                minSinceImpact = minOf(minSinceImpact, w.speedKmh)
                if (firstStopAtMs == 0L && atRest(w)) firstStopAtMs = w.atMs
            }

            // Still moving, and the ride resumed: whatever that was, it was not a
            // crash. A crashed bike does not get back up to speed.
            if (firstStopAtMs == 0L && w.speedKmh > minSinceImpact + RESUMED_KMH) {
                disarm()
                return null
            }
            // It never came to rest in the time a crash of that speed takes to stop.
            if (firstStopAtMs == 0L && sinceImpactMs > stopDeadlineS(speedBeforeImpact) * 1000L) {
                disarm()
                return null
            }
            if (firstStopAtMs == 0L) return null

            // ---- the dwell, anchored to when it actually stopped, not to the impact
            val dwell = recent.filter { it.atMs >= firstStopAtMs }
            val dwellS = ((w.atMs - firstStopAtMs) / 1000).toInt() + 5
            if (dwellS < STILL_S) return null
            val resting = dwell.count { atRest(it) }
            if (dwell.isNotEmpty() && resting.toDouble() / dwell.size < REST_FRACTION) {
                // It moved off again after pausing: a stop, not a wreck.
                disarm()
                return null
            }

            val secondsToStop = max(1.0, (firstStopAtMs - impactAtMs) / 1000.0 + 5.0)
            val verdict = Verdict(
                crash = true,
                kind = if (!impactWasSlow) "impact"
                       else if (impactAtWalkingPace) "rear-end" else "low-speed impact",
                reason = if (impactWasSlow)
                    "%s: %.1f g (low-speed threshold %.1f), still for %d s".format(
                        if (impactAtWalkingPace) "hit while stopped"
                        else "hit at %.0f km/h".format(speedBeforeImpact),
                        armed.peakG, baseline.rearEndG, dwellS)
                else buildString {
                    append(if (armed.peakG >= baseline.impactG)
                        "impact %.1f g (normal tops %.1f)".format(armed.peakG, baseline.gMax)
                    else "rotation %.1f rad/s (normal tops %.1f)".format(armed.peakRot, baseline.rotMax))
                    append(", then %.0f km/h to a stop in %.0f s, still for %d s"
                        .format(speedBeforeImpact, secondsToStop, dwellS))
                },
                impactG = armed.peakG, impactRot = armed.peakRot,
                speedBefore = speedBeforeImpact,
                speedAfter = w.speedKmh,
                decelKmhPerS = (speedBeforeImpact - minSinceImpact) / secondsToStop,
                atMs = armed.atMs,
            )
            disarm()                       // raised once; the relay owns it from here
            return verdict
        }
    }
}
