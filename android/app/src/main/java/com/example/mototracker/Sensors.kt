package com.example.mototracker

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlin.math.sqrt

/**
 * Accelerometer and gyroscope sampling, summarised per tick.
 *
 * Purpose right now is **baselining**, not detection: we need to know what normal
 * riding looks like before any threshold can be chosen. design.md is explicit that
 * the discriminator is the whole shape of an event, and shape is meaningless
 * without a baseline to compare against.
 *
 * Units chosen to be read by a human at a glance:
 *   g      — resultant acceleration / 9.81. At rest this reads ~1.0, not 0.
 *   rot    — gyroscope magnitude in rad/s. At rest ~0.
 *
 * `n` is the sample count in the window and matters as much as the values: if the
 * expected ~250 samples per 5 s window arrives as 20, the OS is throttling the
 * sensor and any detection built on it is unreliable. That is invisible in the g
 * values themselves.
 *
 * Phase 6 needs a 30-second rolling buffer to ship as crash evidence. This keeps
 * only the window since the last tick, which is what baselining needs and is far
 * cheaper. The ring buffer comes with the real detector.
 */
class Sensors(ctx: Context) : SensorEventListener {

    private val mgr = ctx.getSystemService(SensorManager::class.java)
    private val accel: Sensor? = mgr.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val gyro: Sensor? = mgr.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
    // Braking cannot be seen in the figures above: a hard stop is about 0.5 g along
    // the bike, and once gravity is included and the phone is shaken by the road it
    // disappears under peaks of 3-4 g (Jack's brake tests, 2026-09-16, were invisible
    // in the speed series too). These two sensors measure it directly: gravity says
    // which way is down, linear acceleration has gravity already removed, and what is
    // left in the horizontal plane is the bike speeding up, slowing down or leaning.
    private val linear: Sensor? = mgr.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION)
    private val gravity: Sensor? = mgr.getDefaultSensor(Sensor.TYPE_GRAVITY)

    private val lock = Any()
    private var aN = 0
    private var aSum = 0.0
    private var aPeak = 0.0
    private var aMin = Double.MAX_VALUE
    private var aSq = 0.0
    private var gN = 0
    private var gSum = 0.0
    private var gPeak = 0.0
    private var down = floatArrayOf(0f, 0f, 9.81f)   // last known "down", from gravity
    private var hPeak = 0.0                          // hardest horizontal push, in g
    private var hSum = 0.0
    private var hN = 0

    fun start(ctx: Context) {
        // SENSOR_DELAY_GAME is ~50 Hz, the low end of design.md's 50-100 Hz.
        // Enough for baselining and far kinder to the battery than FASTEST.
        val ok = mutableListOf<String>()
        accel?.let { mgr.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME); ok += "accel" }
        gyro?.let { mgr.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME); ok += "gyro" }
        linear?.let { mgr.registerListener(this, it, SensorManager.SENSOR_DELAY_GAME); ok += "linear" }
        gravity?.let { mgr.registerListener(this, it, SensorManager.SENSOR_DELAY_UI); ok += "gravity" }
        RideLog.write(ctx, "SENS", if (ok.isEmpty()) "NO SENSORS AVAILABLE" else "sampling ${ok.joinToString("+")}")
    }

    fun stop() = mgr.unregisterListener(this)

    override fun onSensorChanged(e: SensorEvent) {
        val m = sqrt(
            (e.values[0] * e.values[0] + e.values[1] * e.values[1] +
                e.values[2] * e.values[2]).toDouble()
        )
        synchronized(lock) {
            when (e.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> {
                    val g = m / 9.81
                    aN++; aSum += g; aSq += g * g
                    if (g > aPeak) aPeak = g
                    if (g < aMin) aMin = g
                }
                Sensor.TYPE_GYROSCOPE -> {
                    gN++; gSum += m
                    if (m > gPeak) gPeak = m
                }
                Sensor.TYPE_GRAVITY -> {
                    down = e.values.copyOf()
                }
                Sensor.TYPE_LINEAR_ACCELERATION -> {
                    // What is left after taking out the part pointing along gravity:
                    // the horizontal push, which is braking, accelerating or leaning.
                    val gm = sqrt((down[0] * down[0] + down[1] * down[1] +
                        down[2] * down[2]).toDouble())
                    val h = if (gm < 1e-3) m else {
                        val dot = (e.values[0] * down[0] + e.values[1] * down[1] +
                            e.values[2] * down[2]) / gm
                        val vertical = dot
                        sqrt((m * m - vertical * vertical).coerceAtLeast(0.0))
                    }
                    val g = h / 9.81
                    hN++; hSum += g
                    if (g > hPeak) hPeak = g
                }
            }
        }
    }

    override fun onAccuracyChanged(s: Sensor?, accuracy: Int) {}

    /** Summary of the window since the previous call, then resets. */
    fun drain(): Summary = synchronized(lock) {
        val s = Summary(
            peakHorizG = hPeak,
            meanHorizG = if (hN > 0) hSum / hN else 0.0,
            horizN = hN,
            n = aN,
            peakG = aPeak,
            minG = if (aMin == Double.MAX_VALUE) 0.0 else aMin,
            meanG = if (aN > 0) aSum / aN else 0.0,
            rmsG = if (aN > 0) sqrt(aSq / aN) else 0.0,
            gyroN = gN,
            peakRot = gPeak,
            meanRot = if (gN > 0) gSum / gN else 0.0,
        )
        aN = 0; aSum = 0.0; aPeak = 0.0; aMin = Double.MAX_VALUE; aSq = 0.0
        gN = 0; gSum = 0.0; gPeak = 0.0
        hN = 0; hSum = 0.0; hPeak = 0.0
        s
    }

    data class Summary(
        val n: Int, val peakG: Double, val minG: Double, val meanG: Double,
        val rmsG: Double, val gyroN: Int, val peakRot: Double, val meanRot: Double,
        /** Hardest horizontal push in the window, in g, with gravity taken out. */
        val peakHorizG: Double = 0.0,
        val meanHorizG: Double = 0.0,
        val horizN: Int = 0,
    )
}
