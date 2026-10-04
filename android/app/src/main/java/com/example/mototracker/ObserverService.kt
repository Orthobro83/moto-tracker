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
 * The listener: always on while this phone is paired and not riding (Jack, 2026-09-27),
 * so the other rider starting a ride is heard with the phone locked in a pocket, and
 * an Observer's alarm never needs the screen open.
 *
 * This reverses 2026-09-16, when nothing was to sit in the background waiting for a
 * ride to begin. Now something does, gently: every [IDLE_MS] while nobody is out,
 * every [POLL_MS] once the other rider is, so an incident still alarms promptly. Each
 * read goes through Relay.refresh, which is where RideNews says a trip started or
 * ended.
 *
 * It steps aside while this phone rides — the ride service's IncidentWatch polls for
 * both riders then (Hybrid, 2026-09-24) — and the ride service hands back when the
 * trip ends. It comes back after a reboot or an app update by itself (BootReceiver).
 */
class ObserverService : Service() {

    private val io = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        channel(this)
        val note = note()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, note)
        }
        running = true
        RideLog.write(this, "WATCH", "listening — battery exemption " +
            if (Power.exempt(this)) "GRANTED" else "DENIED")
        NetWatch.start(this)

        io.launch {
            while (isActive) {
                // This phone riding: the ride service watches both riders from here on.
                if (Trip.state(this@ObserverService) != Trip.State.IDLE) {
                    RideLog.write(this@ObserverService, "WATCH",
                        "this phone is riding now — the ride service takes over")
                    stopSelf()
                    return@launch
                }
                if (DeviceKey.current == null) {
                    // Unpaired: nothing to listen to, and nothing will change until the
                    // app is opened to pair — which starts this again.
                    RideLog.write(this@ObserverService, "WATCH", "not paired — stopping")
                    stopSelf()
                    return@launch
                }
                runCatching { IncidentWatch.poll(this@ObserverService) }
                    .onFailure { RideLog.write(this@ObserverService, "WATCH", "failed: ${it.message}") }

                val other = Relay.snapshot?.other(this@ObserverService)
                watching = other?.takeIf { it.tripId != null }?.name

                // Quick while there is something to alarm about, slow while nobody is out.
                val busy = other?.tripId != null || other?.incident != null ||
                    Relay.snapshot?.me(this@ObserverService)?.incident != null
                delay(if (busy) POLL_MS else IDLE_MS)
            }
        }
    }

    /**
     * The notice Android insists a background service posts. Nobody wants it in the
     * list (Jack, 2026-09-28): it is posted at the lowest priority, never updated, and
     * the home screen offers to switch its channel off. The service runs on regardless.
     */
    private fun note(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("Moto Tracker")
            .setContentText("Listening for rides")
            .setSmallIcon(R.drawable.ic_stat_watch)
            .setContentIntent(open)
            .build()
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
        /** Whose ride is being watched. */
        @Volatile var watching: String? = null

        /** New id: a channel's importance cannot be lowered once it exists. */
        const val CHANNEL = "listening"
        private const val OLD_CHANNEL = "observing"
        private const val NOTIF_ID = 3

        /** While the other rider is out: often enough to alarm promptly. */
        private const val POLL_MS = 5_000L

        /**
         * While nobody is out: how late "Dana started a ride" can be. OkHttp keeps
         * the connection to the relay open between reads, so each one is small.
         */
        private const val IDLE_MS = 30_000L

        @Volatile var running = false
            private set

        fun channel(ctx: Context) {
            val mgr = ctx.getSystemService(NotificationManager::class.java)
            mgr.deleteNotificationChannel(OLD_CHANNEL)
            // MIN: no status-bar icon, no sound, folded away at the bottom of the list.
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL, "Listening for rides", NotificationManager.IMPORTANCE_MIN)
                    .apply { description = "Safe to switch off — listening carries on without it" }
            )
        }

        /** Whether the listening notice still shows. False once its channel is switched off. */
        fun noticeShown(ctx: Context): Boolean {
            val ch = ctx.getSystemService(NotificationManager::class.java).getNotificationChannel(CHANNEL)
            return ch != null && ch.importance != NotificationManager.IMPORTANCE_NONE
        }

        /** Safe to call from anywhere, as often as wanted: only a paired, idle phone listens. */
        fun start(ctx: Context) {
            if (running || DeviceKey.current == null || Trip.state(ctx) != Trip.State.IDLE) return
            runCatching { ctx.startForegroundService(Intent(ctx, ObserverService::class.java)) }
                .onFailure { RideLog.write(ctx, "WATCH", "could not start listening: ${it.message}") }
        }
        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, ObserverService::class.java))
    }
}
