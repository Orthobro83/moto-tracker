package com.example.mototracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Observer mode: watching the other rider's ride, and **still watching with the app in
 * the background** (Jack, 2026-09-16) — an Observer's phone lives in a pocket, and an
 * alarm that needs the screen open is no alarm.
 *
 * What it deliberately is not: something that runs all the time waiting for a ride to
 * begin. Nothing watches for a ride to start. This begins when the app is opened and
 * finds the other rider already out, and it retires itself the moment their trip ends
 * or this phone starts riding — from then on the ride service's IncidentWatch polls
 * for both riders, which is how a riding phone still watches (Hybrid, 2026-09-24).
 *
 * The consequence, stated plainly: a ride nobody opens an app for is recorded by the
 * relay and watched by nobody. That is the shape Jack chose.
 */
class ObserverService : Service() {

    private val io = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Watching a ride", NotificationManager.IMPORTANCE_LOW)
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val note = Notification.Builder(this, CHANNEL)
            .setContentTitle("Moto Tracker")
            .setContentText(watching?.let { "Watching $it" } ?: "Watching the ride")
            .setSmallIcon(R.drawable.ic_stat_watch)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, note)
        }
        running = true
        RideLog.write(this, "WATCH", "observing ${watching ?: "the other rider"}")
        NetWatch.start(this)

        io.launch {
            while (isActive) {
                if (DeviceKey.current != null) {
                    runCatching { IncidentWatch.poll(this@ObserverService) }
                        .onFailure { RideLog.write(this@ObserverService, "WATCH", "failed: ${it.message}") }

                    // Retire when there is nothing left to watch. The app is usually
                    // closed by now, so this cannot be left to the screen to decide.
                    val other = Relay.snapshot?.other(this@ObserverService)
                    val done = Relay.snapshot != null && other?.tripId == null && other?.incident == null
                    val riding = Trip.state(this@ObserverService) != Trip.State.IDLE
                    if (done || riding) {
                        RideLog.write(this@ObserverService, "WATCH",
                            if (riding) "this phone is riding now — the ride service takes over"
                            else "their trip ended — done watching")
                        watching = null
                        stopSelf()
                        return@launch
                    }
                }
                delay(POLL_MS)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        // Retiring because this phone started riding hands the alarm to the ride
        // service's watcher, which carries on watching the other rider (Jack,
        // 2026-09-24). Stopping it here would cut out a sounding alarm until then.
        if (Trip.state(this) == Trip.State.IDLE) PhoneAlarm.set(this, false, "stopped watching")
        RideLog.write(this, "WATCH", "stopped")
        NetWatch.stop(this)
        io.cancel()
        running = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        /** Whose ride is being watched, for the notification. */
        @Volatile var watching: String? = null

        private const val CHANNEL = "observing"
        private const val NOTIF_ID = 3

        /** Often enough to alarm promptly, seldom enough to leave the battery alone. */
        private const val POLL_MS = 5_000L

        @Volatile var running = false
            private set

        fun start(ctx: Context) = ctx.startForegroundService(Intent(ctx, ObserverService::class.java))
        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, ObserverService::class.java))
    }
}
