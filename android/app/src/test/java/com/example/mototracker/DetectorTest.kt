package com.example.mototracker

import kotlin.math.max
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * The detector, replayed over every real ride in the archive.
 *
 * This is what stands in for crash samples, which do not exist and never will:
 *
 *  1. **No false alarms.** Every window of every archived ride — 3.1 hours of real
 *     riding across two riders and two bikes, including Dana's 11.9 g readings —
 *     goes through the detector, and it must stay silent throughout.
 *  2. **It catches the thing.** Synthetic crashes are spliced into those same real
 *     rides: a high-side, a low-side that slides, a head-on stop, and a hit from
 *     behind at a light. Each must be caught.
 *  3. **It does not catch what looks like one.** A pothole at speed, a hard stop at
 *     a light, and a bike knocked over while parked must all stay silent.
 *
 * Run with: cd android && ./gradlew test
 */
class DetectorTest {

    private fun ride(name: String): List<Detector.Window> {
        val text = javaClass.classLoader!!.getResourceAsStream(name)!!
            .bufferedReader().readLines()
        return text.drop(1).filter { it.isNotBlank() }.map { line ->
            val c = line.split(",")
            Detector.Window(
                atMs = c[0].toLong(), speedKmh = c[1].toDouble(), peakG = c[2].toDouble(),
                meanG = c[3].toDouble(), peakRot = c[4].toDouble(), accelN = c[5].toInt())
        }
    }

    /** Derived from the archive by analysis/baseline.py. */
    private val dana = Detector.Baseline("dana", gP999 = 8.75, gMax = 8.79,
        rotP999 = 5.54, rotMax = 6.04, decelMax = 6.7, movingSamples = 1389)
    private val jack = Detector.Baseline("jack", gP999 = 5.88, gMax = 5.88,
        rotP999 = 3.81, rotMax = 3.81, decelMax = 6.4, movingSamples = 862)

    private fun baselineFor(name: String) = if (name.contains("dana")) dana else jack

    private val rides = listOf(
        "ride-29-dana.csv", "ride-30-jack.csv",
        "ride-33-dana.csv", "ride-34-dana.csv")

    private fun replay(windows: List<Detector.Window>, baseline: Detector.Baseline):
        List<Detector.Verdict> {
        val engine = Detector.Engine(baseline)
        return windows.mapNotNull { engine.offer(it) }
    }

    // ---------------------------------------------------------------- 1. silence

    @Test
    fun `no real ride raises a candidate`() {
        var windows = 0
        for (name in rides) {
            val ride = ride(name)
            windows += ride.size
            val verdicts = replay(ride, baselineFor(name))
            assertTrue(verdicts.isEmpty(),
                "$name raised ${verdicts.size}: ${verdicts.firstOrNull()?.reason}")
        }
        assertTrue(windows > 6000, "expected the whole archive, got $windows windows")
        println("silent across $windows windows of real riding (${windows * 5 / 3600.0} h)")
    }

    // ---------------------------------------------------------------- 2. catches

    /**
     * Splices a crash onto the end of a real ride, at a point where the rider was
     * actually riding, so everything before the impact is real data.
     */
    private fun spliceCrash(
        name: String, impactG: Double, impactRot: Double, stopWithinS: Int = 5,
        stillS: Int = 40,
    ): List<Detector.Window> {
        val ride = ride(name)
        val cut = ride.indexOfLast { it.speedKmh >= 45 }.takeIf { it > 10 } ?: (ride.size - 1)
        val before = ride.take(cut + 1)
        val last = before.last()
        val out = before.toMutableList()
        var t = last.atMs
        // The impact itself.
        t += 5000
        out += Detector.Window(t, speedKmh = max(0.0, last.speedKmh - 10), peakG = impactG,
            meanG = 1.4, peakRot = impactRot, accelN = 250)
        // Coming to rest.
        var speed = max(0.0, last.speedKmh - 10)
        while (speed > 3 && stopWithinS > 0) {
            t += 5000
            speed = max(0.0, speed - last.speedKmh / (stopWithinS / 5.0 + 1))
            out += Detector.Window(t, speed, peakG = 1.2, meanG = 1.0, peakRot = 0.4, accelN = 250)
        }
        // Lying there.
        repeat(stillS / 5) {
            t += 5000
            out += Detector.Window(t, 0.0, peakG = 1.01, meanG = 1.0, peakRot = 0.05, accelN = 250)
        }
        return out
    }

    @Test
    fun `a high-side is caught`() {
        for (name in rides) {
            val verdicts = replay(spliceCrash(name, impactG = 18.0, impactRot = 14.0),
                baselineFor(name))
            assertEquals(1, verdicts.size, "$name: ${verdicts.map { it.reason }}")
            assertTrue(verdicts[0].crash)
            println("high-side on $name -> ${verdicts[0].reason}")
        }
    }

    @Test
    fun `a low-side that slides is caught`() {
        // Less of a jolt, but the bike spins and the rider stops.
        for (name in rides) {
            val verdicts = replay(spliceCrash(name, impactG = 9.0, impactRot = 12.0, stopWithinS = 10),
                baselineFor(name))
            assertEquals(1, verdicts.size, "$name: ${verdicts.map { it.reason }}")
        }
    }

    @Test
    fun `a head-on stop is caught`() {
        for (name in rides) {
            val verdicts = replay(spliceCrash(name, impactG = 25.0, impactRot = 3.0, stopWithinS = 0),
                baselineFor(name))
            assertEquals(1, verdicts.size, "$name: ${verdicts.map { it.reason }}")
            assertTrue(verdicts[0].speedBefore >= 45)
        }
    }

    /**
     * Below walking pace (Jack, 2026-09-16). Everything here happens at a standstill,
     * where a bump at a light and a car in the back look identical to an
     * accelerometer except in how hard they hit.
     */
    private fun whileStopped(rideName: String, g: Double, rot: Double = 1.0,
                             stillS: Int = 40): List<Detector.Window> {
        val ride = ride(rideName)
        // Come to a stop first, the way a rider arrives at a light.
        val at = ride.last().atMs
        val stopped = (1..6).map {
            Detector.Window(at + it * 5000L, 0.0, peakG = 1.05, meanG = 1.0, peakRot = 0.1)
        }
        val hit = Detector.Window(at + 35000, 0.0, peakG = g, meanG = 1.5, peakRot = rot)
        val after = (1..stillS / 5).map {
            Detector.Window(at + 35000 + it * 5000L, 0.0, peakG = 1.02, meanG = 1.0, peakRot = 0.1)
        }
        return ride + stopped + hit + after
    }

    @Test
    fun `the driveway bump is not a crash`() {
        // This is the real one: Dana's worst reading in the whole archive, 8.79 g,
        // is her going over the bump at the end of their own driveway. At walking
        // pace it has to clear a much higher bar than that, and it does not.
        assertTrue(replay(whileStopped("ride-33-dana.csv", g = 8.79), dana).isEmpty())
        assertTrue(replay(whileStopped("ride-33-dana.csv", g = 12.0), dana).isEmpty(),
            "a hard kerb at walking pace is still not a crash")
        assertTrue(replay(whileStopped("ride-30-jack.csv", g = 15.0), jack).isEmpty(),
            "nor is a speed bump taken badly")
    }

    @Test
    fun `being rear-ended while stopped is caught`() {
        for ((name, b) in listOf("ride-30-jack.csv" to jack, "ride-33-dana.csv" to dana)) {
            val verdicts = replay(whileStopped(name, g = 30.0, rot = 6.0), b)
            assertEquals(1, verdicts.size, "$name: ${verdicts.map { it.reason }}")
            assertEquals("rear-end", verdicts[0].kind)
            println("rear-end on $name -> ${verdicts[0].reason}")
        }
    }

    @Test
    fun `the walking-pace bar sits far above any kerb`() {
        for (b in listOf(dana, jack)) {
            assertTrue(b.rearEndG >= 20.0, "${b.rider}: ${b.rearEndG}")
            assertTrue(b.rearEndG >= b.gMax * 2.2,
                "${b.rider}: ${b.rearEndG} is too close to their worst ordinary ${b.gMax}")
        }
        println("walking-pace threshold — dana ${dana.rearEndG} g, jack ${jack.rearEndG} g")
    }

    // ---------------------------------------------------------------- 3. near misses

    @Test
    fun `a pothole at speed is not a crash`() {
        val ride = ride("ride-33-dana.csv")
        val cut = ride.indexOfLast { it.speedKmh >= 50 }
        val hit = ride[cut]
        val out = ride.take(cut) + listOf(
            Detector.Window(hit.atMs, hit.speedKmh, peakG = 15.0, meanG = 1.5, peakRot = 4.0)) +
            ride.drop(cut + 1).take(12)      // and she rides on
        assertTrue(replay(out, dana).isEmpty(), "a jolt with no consequence must stay silent")
    }

    @Test
    fun `a hard stop at a light is not a crash`() {
        val ride = ride("ride-30-jack.csv")
        val cut = ride.indexOfLast { it.speedKmh >= 60 }
        val at = ride[cut]
        val out = ride.take(cut + 1) + listOf(
            Detector.Window(at.atMs + 5000, 20.0, peakG = 3.0, meanG = 1.2, peakRot = 0.8),
            Detector.Window(at.atMs + 10000, 0.0, peakG = 1.5, meanG = 1.0, peakRot = 0.3),
            Detector.Window(at.atMs + 15000, 0.0, peakG = 1.1, meanG = 1.0, peakRot = 0.1),
            Detector.Window(at.atMs + 20000, 0.0, peakG = 1.1, meanG = 1.0, peakRot = 0.1),
            Detector.Window(at.atMs + 25000, 0.0, peakG = 1.1, meanG = 1.0, peakRot = 0.1),
            Detector.Window(at.atMs + 30000, 0.0, peakG = 1.1, meanG = 1.0, peakRot = 0.1))
        assertTrue(replay(out, jack).isEmpty(), "stopping hard is not crashing")
    }

    @Test
    fun `a rider stopping after a jolt still has to have been jolted hard`() {
        // Dana's worst real window, 8.79 g, followed by a genuine stop. Her own
        // normal riding reaches that, so it must not be enough on its own.
        val ride = ride("ride-33-dana.csv")
        val cut = ride.indexOfLast { it.speedKmh >= 50 }
        val at = ride[cut]
        val out = ride.take(cut + 1) + listOf(
            Detector.Window(at.atMs + 5000, 30.0, peakG = 8.79, meanG = 1.9, peakRot = 6.04),
            Detector.Window(at.atMs + 10000, 0.0, peakG = 1.2, meanG = 1.0, peakRot = 0.2)) +
            (3..12).map {
                Detector.Window(at.atMs + it * 5000L, 0.0, peakG = 1.05, meanG = 1.0, peakRot = 0.1)
            }
        assertTrue(replay(out, dana).isEmpty(),
            "her own worst ordinary jolt must never be a crash on its own")
    }

    // ------------------------------------------------- the 2026-09-16 hardening

    /** Windows every five seconds, from a list of (speed, g, rot). */
    private fun windows(vararg spec: Triple<Double, Double, Double>, t0: Long = 1_700_000_000_000):
        List<Detector.Window> = spec.mapIndexed { i, (speed, g, rot) ->
        Detector.Window(t0 + i * 5000L, speed, peakG = g, meanG = 1.0, peakRot = rot, accelN = 250)
    }

    @Test
    fun `a long slide from highway speed is caught`() {
        // 0.3 g on asphalt is about 10.6 km/h per second, so 120 km/h takes eleven
        // seconds to scrub off. The old twenty-second horizon had to contain the
        // dwell as well, so the bike had to be stopped within eight — this case was
        // a miss, in exactly the situation the system exists for.
        val v = windows(
            Triple(118.0, 1.2, 0.3), Triple(120.0, 1.3, 0.4), Triple(119.0, 1.2, 0.3),
            Triple(120.0, 21.0, 9.0),                                   // the impact
            Triple(67.0, 3.0, 2.0), Triple(14.0, 2.0, 1.0),             // sliding
            Triple(0.0, 1.05, 0.1), Triple(0.0, 1.02, 0.05), Triple(0.0, 1.01, 0.05),
            Triple(0.0, 1.01, 0.05), Triple(0.0, 1.01, 0.05))
        val verdicts = replay(v, jack)
        assertEquals(1, verdicts.size, verdicts.map { it.reason }.toString())
        println("long slide -> ${verdicts[0].reason}")
    }

    @Test
    fun `a low-side goes down on rotation alone, with ordinary G-forces`() {
        // Jack, 2026-09-16: the bike goes over and slides. Asphalt is not a hard
        // impact, so the accelerometer may read nothing remarkable — 4.5 g is
        // squarely inside his ordinary riding, which tops out at 5.88. The gyro is
        // what is out of bounds, and rotation is an OR in the riding band precisely
        // so that this case is caught: rotation, then deceleration, then stationary.
        val v = windows(
            Triple(74.0, 1.3, 0.4), Triple(76.0, 1.2, 0.3), Triple(75.0, 1.3, 0.4),
            Triple(72.0, 4.5, 13.0),                        // down: low g, high rotation
            Triple(38.0, 3.0, 4.0), Triple(9.0, 2.0, 1.2),  // sliding to a stop
            Triple(0.0, 1.05, 0.1), Triple(0.0, 1.02, 0.05), Triple(0.0, 1.01, 0.05),
            Triple(0.0, 1.01, 0.05))
        val verdicts = replay(v, jack)
        assertEquals(1, verdicts.size, verdicts.map { it.reason }.toString())
        assertTrue(verdicts[0].impactG < jack.impactG,
            "the accelerometer never crossed its threshold: ${verdicts[0].impactG}")
        println("low-side on rotation alone -> ${verdicts[0].reason}")
    }

    @Test
    fun `a bike knocked over while parked is not a crash`() {
        // The trip is open — off-bike keeps it open by design — but nothing has moved
        // for ten minutes. A 30 g blow here is the bike going over on its stand, not
        // a rider being hit (Jack, 2026-09-16).
        val parked = (0..130).map {
            Detector.Window(1_700_000_000_000 + it * 5000L, 0.0,
                peakG = 1.02, meanG = 1.0, peakRot = 0.05, accelN = 250)
        }
        val knocked = Detector.Window(parked.last().atMs + 5000, 0.0,
            peakG = 30.0, meanG = 2.0, peakRot = 7.0, accelN = 250)
        val after = (1..8).map {
            Detector.Window(knocked.atMs + it * 5000L, 0.0,
                peakG = 1.02, meanG = 1.0, peakRot = 0.05, accelN = 250)
        }
        // Rode in at the start, then sat for well over ten minutes.
        val arriving = windows(Triple(40.0, 1.5, 0.5), Triple(20.0, 1.4, 0.4), Triple(4.0, 1.2, 0.3))
        assertTrue(replay(arriving + parked + knocked + after, jack).isEmpty())
    }

    @Test
    fun `but a rear-end at a light still fires, because the bike was moving a moment ago`() {
        val v = windows(
            Triple(45.0, 1.4, 0.5), Triple(20.0, 1.5, 0.6), Triple(3.0, 1.2, 0.3),
            Triple(0.0, 1.05, 0.1), Triple(0.0, 1.05, 0.1),             // waiting
            Triple(0.0, 28.0, 6.0),                                     // hit from behind
            Triple(0.0, 1.05, 0.1), Triple(0.0, 1.02, 0.05), Triple(0.0, 1.01, 0.05),
            Triple(0.0, 1.01, 0.05), Triple(0.0, 1.01, 0.05))
        val verdicts = replay(v, jack)
        assertEquals(1, verdicts.size, verdicts.map { it.reason }.toString())
        assertEquals("rear-end", verdicts[0].kind)
    }

    @Test
    fun `GPS chatter during the dwell does not lose the crash`() {
        // The phone is in the grass and the fix wanders. Every one of these windows
        // is still, on the IMU, which is what settles it.
        val v = windows(
            Triple(70.0, 1.3, 0.4), Triple(72.0, 1.2, 0.3),
            Triple(70.0, 19.0, 11.0),                                   // the impact
            Triple(8.0, 2.0, 0.8),
            Triple(0.0, 1.05, 0.1), Triple(11.0, 1.08, 0.2), Triple(0.0, 1.03, 0.1),
            Triple(9.0, 1.06, 0.15), Triple(0.0, 1.02, 0.05), Triple(0.0, 1.01, 0.05))
        val verdicts = replay(v, jack)
        assertEquals(1, verdicts.size, verdicts.map { it.reason }.toString())
    }

    @Test
    fun `one noisy GPS sample does not move a crawl into the riding band`() {
        // Paddling out of a parking space with a single 26 km/h fix. If that chose
        // the band, a 12 g kerb strike would be judged by the riding threshold.
        val v = windows(
            Triple(8.0, 1.3, 0.8), Triple(10.0, 1.4, 0.9), Triple(26.0, 1.3, 0.7),
            Triple(9.0, 12.0, 5.0),                                     // the kerb
            Triple(0.0, 1.1, 0.2), Triple(0.0, 1.05, 0.1), Triple(0.0, 1.02, 0.05),
            Triple(0.0, 1.01, 0.05), Triple(0.0, 1.01, 0.05))
        assertTrue(replay(v, jack).isEmpty())
    }

    @Test
    fun `a rider who was at speed a moment ago is still judged as riding`() {
        // Hysteresis the other way: slowing through the band edge keeps the riding
        // thresholds, because they were unmistakably riding.
        val v = windows(
            Triple(60.0, 1.3, 0.4), Triple(48.0, 1.3, 0.4), Triple(30.0, 1.2, 0.3),
            Triple(24.0, 13.0, 6.0),                                    // hit at 24 km/h
            Triple(0.0, 1.1, 0.2), Triple(0.0, 1.05, 0.1), Triple(0.0, 1.02, 0.05),
            Triple(0.0, 1.01, 0.05), Triple(0.0, 1.01, 0.05))
        assertEquals(1, replay(v, jack).size)
    }

    @Test
    fun `a throttled sensor window cannot raise anything`() {
        // One UI can cut the sensor stream to a trickle. A peak from twenty samples
        // is not a measurement, and must not be treated as one.
        val t0 = 1_700_000_000_000
        val v = listOf(
            Detector.Window(t0, 70.0, 1.3, 1.0, 0.4, accelN = 250),
            Detector.Window(t0 + 5000, 70.0, 1.2, 1.0, 0.3, accelN = 250),
            Detector.Window(t0 + 10000, 70.0, 30.0, 2.0, 12.0, accelN = 20),   // throttled
            Detector.Window(t0 + 15000, 0.0, 1.05, 1.0, 0.1, accelN = 18),
            Detector.Window(t0 + 20000, 0.0, 1.02, 1.0, 0.05, accelN = 19),
            Detector.Window(t0 + 25000, 0.0, 1.02, 1.0, 0.05, accelN = 20),
            Detector.Window(t0 + 30000, 0.0, 1.02, 1.0, 0.05, accelN = 21))
        assertTrue(replay(v, jack).isEmpty())
    }

    @Test
    fun `a jolt followed by riding on is discarded even if they stop later`() {
        // The urban shape: a vicious expansion joint, then a normal arrival at a
        // parking space half a minute later.
        val v = windows(
            Triple(40.0, 1.4, 0.5), Triple(42.0, 13.0, 5.0),            // the joint
            Triple(38.0, 1.5, 0.6), Triple(41.0, 1.4, 0.5),             // rides on
            Triple(30.0, 1.4, 0.5), Triple(12.0, 1.3, 0.4),
            Triple(0.0, 1.1, 0.2), Triple(0.0, 1.05, 0.1), Triple(0.0, 1.02, 0.05),
            Triple(0.0, 1.01, 0.05), Triple(0.0, 1.01, 0.05))
        assertTrue(replay(v, jack).isEmpty())
    }

    // ---------------------------------------------------------------- thresholds

    @Test
    fun `each rider's thresholds sit clear of their own worst riding`() {
        for (b in listOf(dana, jack)) {
            assertTrue(b.impactG >= b.gMax * 1.4,
                "${b.rider}: impact ${b.impactG} too close to normal ${b.gMax}")
            assertTrue(b.impactG >= Detector.IMPACT_G_FLOOR)
        }
        // And the two riders really do need different numbers.
        assertTrue(dana.impactG > jack.impactG)
        println("thresholds — dana ${dana.impactG} g / ${dana.impactRot} rad/s, " +
            "jack ${jack.impactG} g / ${jack.impactRot} rad/s")
    }
}
