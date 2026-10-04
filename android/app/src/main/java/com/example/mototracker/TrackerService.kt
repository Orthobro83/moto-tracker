package com.example.mototracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.os.BatteryManager
import android.os.IBinder
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.util.Locale

/**
 * Foreground service, location type, FusedLocationProvider ticks: every 5 s while
 * riding, every 30 s off the bike and standing still (Trip.tickMs).
 *
 * Countermeasures this relies on, all granted by hand per phone and none of them
 * available to the app itself (design.md "OS interference"):
 *   - battery optimization exemption
 *   - removal from One UI "Sleeping apps" / "Deep sleeping apps"
 */
class TrackerService : Service() {

    private val io = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val sensors by lazy { Sensors(this) }
    /** Crash detection, judged against this rider's own baseline (Detector). */
    private val engine = Detector.Engine()
    private val client by lazy { LocationServices.getFusedLocationProviderClient(this) }
    private var ticks = 0

    /** The interval the location request is currently running at. */
    private var tickMs = 0L

    /**
     * Off the bike the phone reports every 30 s, riding every 5 s. A button press
     * changes the trip state from outside the service, so the rate follows the state
     * itself rather than waiting for the next (slow) fix to notice.
     */
    private val stateWatch = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
        if (key == Trip.KEY_STATE) applyCadence()
    }

    @Synchronized
    private fun applyCadence() {
        val want = Trip.tickMs(this)
        if (want == tickMs) return
        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, want)
            .setMinUpdateIntervalMillis(want)
            .setWaitForAccurateLocation(false)
            .build()
        runCatching { client.requestLocationUpdates(request, callback, mainLooper) }
            .onSuccess {
                RideLog.write(this, "SVC", "location every ${want / 1000} s (was ${tickMs / 1000} s)")
                tickMs = want
            }
            .onFailure { RideLog.write(this, "SVC", "requestLocationUpdates failed: ${it.message}") }
    }

    private val callback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            val loc = result.lastLocation ?: return
            ticks++
            // age = how long the fix sat between being taken and reaching us.
            // Near zero means real-time. Hundreds of seconds means the process was
            // frozen and the OS queued the callback — which is exactly what the
            // 2026-09-10 test ride showed, and the number that decides the gate.
            val ageS = (System.currentTimeMillis() - loc.time) / 1000.0
            val riding = Trip.sensorsWanted(this@TrackerService)
            // Off the bike, location keeps reporting and the sensors do not: the
            // trip continues, but nothing is being detected (Jack, 2026-09-16).
            val s = if (riding) sensors.drain() else null.also { sensors.drain() }
            val line = if (s != null) String.format(
                Locale.US,
                "lat=%.6f lon=%.6f acc=%.1f speed=%.1f batt=%d age=%.1f tick=%d " +
                    "g=%.2f gmin=%.2f gmean=%.2f grms=%.2f rot=%.2f rotmean=%.2f n=%d/%d",
                loc.latitude, loc.longitude, loc.accuracy, loc.speed * 3.6f,
                battery(), ageS, ticks,
                s.peakG, s.minG, s.meanG, s.rmsG, s.peakRot, s.meanRot, s.n, s.gyroN
            ) else String.format(
                Locale.US,
                "lat=%.6f lon=%.6f acc=%.1f speed=%.1f batt=%d age=%.1f tick=%d  off-bike",
                loc.latitude, loc.longitude, loc.accuracy, loc.speed * 3.6f,
                battery(), ageS, ticks
            )
            // Local first — this is the ground truth for the gate. The network
            // half can fail all it likes without costing us the endurance record.
            RideLog.write(this@TrackerService, "TICK", line)

            // Detection runs on the phone, on the window just measured, before the
            // upload — so a crash is judged even if this very packet never lands.
            val speedKmh = (loc.speed * 3.6f).toDouble()
            val verdict = if (s == null) null else engine.offer(
                Detector.Window(
                    atMs = System.currentTimeMillis(),
                    speedKmh = speedKmh,
                    peakG = s.peakG, meanG = s.meanG, peakRot = s.peakRot, accelN = s.n))

            // A rider who pulls away without pressing anything is riding.
            if (Trip.noticeMovement(this@TrackerService, speedKmh)) {
                io.launch {
                    Trip.resumeRide(this@TrackerService, assumed = true)
                    engine.reset()
                }
            }

            io.launch {
                val ok = Uploader.postPosition(
                    this@TrackerService, loc.latitude, loc.longitude,
                    loc.accuracy, loc.speed * 3.6f, battery(), s
                )
                // Transitions only; the failure reason itself is already logged as NET.
                Link.record(this@TrackerService, ok, "tick")
                if (verdict != null) raise(verdict)
                // Anything concluded while there was no signal goes now.
                if (ok) runCatching { Relay.deliverHeld(this@TrackerService) }
                // While riding, this is what notices the rider's own incident and
                // puts the pop-up in front of them: the watcher only runs when this
                // phone is observing somebody else (Jack, 2026-09-16).
                if (ok) runCatching { IncidentWatch.poll(this@TrackerService) }
            }
            // Movement seen off the bike speeds the rate up; stopping again slows it.
            applyCadence()
        }
    }

    /**
     * A candidate, raised the moment the phone concludes one. The relay owns it from
     * here: it holds the confirm window, escalates if nobody retracts, and — if this
     * phone goes silent instead — treats that silence as the answer.
     */
    private fun raise(v: Detector.Verdict) {
        RideLog.write(this, "CRASH", "candidate: ${v.reason}")
        val id = Relay.raiseCandidate(this, v)
        if (id == null) {
            // The relay could not be told. The phone keeps its own record, and the
            // relay's silence watchdog is what covers this case from the other end.
            RideLog.write(this, "CRASH", "the relay did not accept the candidate — " +
                "if this phone now goes quiet, the relay raises it from that instead")
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIF_ID, notification())
        RideLog.write(this, "SVC", "onCreate, starting location updates")
        // Without this the log cannot say whether a freeze means "One UI throttles
        // even an exempt foreground service" or "the exemption was never granted".
        // Those demand completely different responses, so record it every time.
        RideLog.write(this, "SVC", "battery exemption: ${if (Power.exempt(this)) "GRANTED" else "DENIED"}")
        // Without this the server never leaves Sleep and the dashboard shows
        // nothing, however many positions arrive.
        sensors.start(this)
        // The thresholds come from the Mini's whole archive, through the relay, so
        // the phone and the relay judge this ride by exactly the same numbers.
        io.launch {
            engine.baseline = Relay.fetchBaseline(this@TrackerService)
            RideLog.write(this@TrackerService, "BASELINE",
                "impact ${"%.1f".format(engine.baseline.impactG)} g / " +
                    "${"%.1f".format(engine.baseline.impactRot)} rad/s (${engine.baseline.rider})")
        }
        // Logs every network change during a ride, so a dead link sits next to the
        // switch-over that caused it.
        NetWatch.start(this)

        getSharedPreferences(Trip.PREFS, MODE_PRIVATE).registerOnSharedPreferenceChangeListener(stateWatch)
        applyCadence()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        RideLog.write(this, "SVC", "onStartCommand action=${intent?.action}")
        if (intent?.action == ACTION_TEST_OVERLAY) {
            // Deliberately fired from the service, not an activity: in the real
            // design the stop prompt is raised while the app sits behind Waze.
            RideLog.write(this, "OVERLAY", "scheduled in ${Config.TAKEOVER_DELAY_MS / 1000}s")
            android.os.Handler(mainLooper).postDelayed(
                { OverlayPrompt.show(this) }, Config.TAKEOVER_DELAY_MS
            )
        }
        return START_STICKY
    }

    override fun onDestroy() {
        // If Samsung kills the service this is the last thing written. Its absence
        // from the log is just as informative as its presence.
        RideLog.write(this, "SVC", "onDestroy after $ticks ticks")
        engine.reset()
        getSharedPreferences(Trip.PREFS, MODE_PRIVATE).unregisterOnSharedPreferenceChangeListener(stateWatch)
        runCatching { client.removeLocationUpdates(callback) }
        runCatching { sensors.stop() }
        runCatching { NetWatch.stop(this) }
        // The service stopping is Sleep, and End trip has already told the server.
        // Nothing to announce here: announcing off-bike on destroy would be wrong,
        // because off-bike deliberately keeps the service running.
        io.cancel()
        // The trip is over: back to listening for the other rider.
        ObserverService.start(this)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun battery(): Int {
        val status = registerReceiver(null as BroadcastReceiver?, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = status?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = status?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        return if (level >= 0 && scale > 0) level * 100 / scale else -1
    }

    private fun createChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Trip in progress", NotificationManager.IMPORTANCE_LOW)
        )
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("Moto Tracker")
            .setContentText("Sharing your position")
            .setSmallIcon(R.drawable.ic_stat_ride)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val CHANNEL = "spike"
        private const val NOTIF_ID = 1
        const val ACTION_TEST_OVERLAY = "com.example.mototracker.TEST_OVERLAY"

        fun testOverlay(ctx: Context) = ctx.startForegroundService(
            Intent(ctx, TrackerService::class.java).setAction(ACTION_TEST_OVERLAY)
        )

        fun start(ctx: Context) = ctx.startForegroundService(Intent(ctx, TrackerService::class.java))
        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, TrackerService::class.java))
    }
}
