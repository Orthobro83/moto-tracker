package com.example.mototracker

import android.content.Context

/**
 * What the rider is doing, and the only place that decides it (Jack, 2026-09-16).
 *
 * Three states, two buttons:
 *
 *     IDLE      "Start trip"   -> opens the trip, off the bike. Location reports
 *                                from here; sensors and crash detection wait
 *                                (Jack, 2026-09-20).
 *     OFF_BIKE  "Start ride"   -> on the bike. "Resume ride" once this trip has
 *                                had a ride in it already.
 *     RIDING    "Pause ride"   -> off the bike. Location keeps reporting, sensors
 *                                and crash detection stop.
 *
 * "End trip" closes everything from either riding state and returns to IDLE.
 *
 * **Resumption is assumed.** A rider who pulls away without pressing anything is
 * riding, whatever the app was told: moving above [RESUME_KMH] for [RESUME_S]
 * seconds flips this back to RIDING by itself, and tells the relay so the monitor
 * and the other phone agree. Forgetting the button must never mean riding with
 * crash detection switched off. The one exception is [autoResume] switched off by
 * hand, for a trip that is not on the bike at all.
 */
object Trip {

    enum class State { IDLE, RIDING, OFF_BIKE }

    /**
     * Riding again, whatever the app was told. Two ways to decide it, because the
     * first attempt was too slow to be any use: on 2026-09-16 Jack rode off at 69 km/h
     * and it had still not resumed a minute later, so crash detection stayed off for
     * the whole of it.
     *
     * Nobody paddles a bike at 25 km/h, so two ticks at that speed settle it in about
     * ten seconds. The slower rule stays for anything gentler.
     */
    const val RESUME_KMH = 10.0
    const val RESUME_S = 20
    const val RESUME_OBVIOUS_KMH = 25.0
    const val RESUME_OBVIOUS_TICKS = 2

    private const val PREFS = "trip"
    private const val KEY_STATE = "state"
    private const val KEY_STARTED = "started_at"
    private const val KEY_OFF_SINCE = "off_bike_since"
    private const val KEY_RODE = "rode_in_this_trip"
    private const val KEY_AUTO = "auto_resume"

    @Volatile
    private var cached: State? = null

    private var movingSinceMs = 0L
    private var obviousTicks = 0

    fun state(ctx: Context): State {
        cached?.let { return it }
        val name = prefs(ctx).getString(KEY_STATE, State.IDLE.name) ?: State.IDLE.name
        val value = runCatching { State.valueOf(name) }.getOrDefault(State.IDLE)
        cached = value
        return value
    }

    /**
     * Whether moving starts the ride by itself. Jack (2026-09-20) wanted it off for
     * a trip on foot, on a bicycle or in a car: the trip is recorded, and none of
     * the crash machinery ever wakes up, because none of it means anything at
     * walking pace or in a car seat. It stays off until it is switched back on,
     * and every screen that can says so while it is.
     */
    fun autoResume(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_AUTO, true)

    fun setAutoResume(ctx: Context, on: Boolean) {
        prefs(ctx).edit().putBoolean(KEY_AUTO, on).commit()
        movingSinceMs = 0
        obviousTicks = 0
        RideLog.write(ctx, "TRIP", "auto-resume ${if (on) "on" else "OFF — riding will not start by itself"}")
    }

    /** Has this trip had any riding in it yet? Start ride, or resume it. */
    fun hasRidden(ctx: Context): Boolean = prefs(ctx).getBoolean(KEY_RODE, false)

    /** When the open trip began, in local wall-clock millis, or 0 when idle. */
    fun startedAt(ctx: Context): Long = prefs(ctx).getLong(KEY_STARTED, 0L)

    /**
     * When the rider got off the bike, or 0. Its own clock: showing the trip's start
     * here made "off the bike for" read as hours on a one-hour ride (Jack, 2026-09-16).
     */
    fun offBikeSince(ctx: Context): Long = prefs(ctx).getLong(KEY_OFF_SINCE, 0L)

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun set(ctx: Context, value: State) {
        cached = value
        val edit = prefs(ctx).edit().putString(KEY_STATE, value.name)
        if (value == State.IDLE) edit.remove(KEY_STARTED).remove(KEY_OFF_SINCE)
        else if (prefs(ctx).getLong(KEY_STARTED, 0L) == 0L)
            edit.putLong(KEY_STARTED, System.currentTimeMillis())
        if (value == State.OFF_BIKE) edit.putLong(KEY_OFF_SINCE, System.currentTimeMillis())
        else edit.remove(KEY_OFF_SINCE)
        if (value == State.RIDING) edit.putBoolean(KEY_RODE, true)
        else if (value == State.IDLE) edit.remove(KEY_RODE)
        edit.commit()
        movingSinceMs = 0
        RideLog.write(ctx, "TRIP", "state -> ${value.name.lowercase()}")
    }

    // ---------------------------------------------------------------- the two buttons

    /**
     * Start trip: the trip opens and the rider is **not** on the bike yet. Getting
     * ready, loading up and setting off are all part of a trip, and none of them
     * are riding (Jack, 2026-09-20). Location reports from this moment; sensors and
     * crash detection wait for Start ride — or for the bike to simply pull away.
     */
    fun startTrip(ctx: Context): Boolean {
        set(ctx, State.OFF_BIKE)
        TrackerService.start(ctx)
        return Uploader.tripStart(ctx, riding = false)
    }

    /** Pause ride: off the bike. Location still reports; sensors do not. */
    fun pauseRide(ctx: Context): Boolean {
        set(ctx, State.OFF_BIKE)
        return Uploader.offBike(ctx)
    }

    /** Start or resume the ride, pressed or assumed. */
    fun resumeRide(ctx: Context, assumed: Boolean = false): Boolean {
        if (assumed) {
            RideLog.write(ctx, "TRIP", "moving again without being told — assuming the ride resumed")
        }
        set(ctx, State.RIDING)
        return Uploader.rideStart(ctx)
    }

    /** End trip: close it, stop everything, and send the log without being asked. */
    fun endTrip(ctx: Context): Boolean {
        val ok = Uploader.tripEnd(ctx)
        set(ctx, State.IDLE)
        TrackerService.stop(ctx)
        Uploader.uploadRideLog(ctx)
        return ok
    }

    // ---------------------------------------------------------------- assumed resumption

    /**
     * Called on every position while off the bike. Returns true the moment it has
     * decided the rider is riding again, and leaves the caller to act on it.
     */
    fun noticeMovement(ctx: Context, speedKmh: Double): Boolean {
        if (!autoResume(ctx)) {
            movingSinceMs = 0
            obviousTicks = 0
            return false
        }
        if (state(ctx) != State.OFF_BIKE) {
            movingSinceMs = 0
            obviousTicks = 0
            return false
        }
        // Unmistakably riding: settled in about ten seconds.
        if (speedKmh >= RESUME_OBVIOUS_KMH) {
            obviousTicks++
            if (obviousTicks >= RESUME_OBVIOUS_TICKS) {
                obviousTicks = 0
                movingSinceMs = 0
                return true
            }
        } else {
            obviousTicks = 0
        }
        val now = System.currentTimeMillis()
        if (speedKmh < RESUME_KMH) {
            movingSinceMs = 0
            return false
        }
        if (movingSinceMs == 0L) {
            movingSinceMs = now
            return false
        }
        if (now - movingSinceMs < RESUME_S * 1000L) return false
        movingSinceMs = 0
        return true
    }

    /** Sensors and crash detection run while riding, and only then. */
    fun sensorsWanted(ctx: Context) = state(ctx) == State.RIDING
}
