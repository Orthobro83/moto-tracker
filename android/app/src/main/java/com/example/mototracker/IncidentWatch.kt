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

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent

/**
 * Watches the relay for incidents and decides what this phone should be doing about
 * them (spike-15). Runs inside the network-test service, so it keeps watching with
 * the screen off and the phone in a pocket — which is the only state an Observer's
 * phone is ever actually in.
 *
 * The rules, from design.md 2026-09-15, widened 2026-09-24 for both riding at once:
 *  - My own candidate: nothing — the telemetry answers it.
 *  - My own escalated incident: vibrate and pop up until I close my half.
 *  - The other rider's incident: sound the Observer alarm, with a pop-up, until
 *    somebody silences it — whether or not this phone is riding too. Silence is
 *    shared, so the relay is what stops this phone, not this phone.
 */
object IncidentWatch {

    private const val CHANNEL = "incident"
    private const val NOTIF_ID = 7

    /** What the screen should show, set on every poll. */
    @Volatile var headline: String = ""
        private set

    /** What the last poll acted on, so nothing is announced twice. */
    @Volatile private var announced: String? = null

    /** Fetches, then decides. Used by the ride service, which has no snapshot. */
    fun poll(ctx: Context) {
        val snap = Relay.refresh(ctx) ?: return
        decide(ctx, snap)
    }

    /** The little of an incident that deciding needs, so the rules test on the JVM. */
    data class Seen(val id: Int, val state: String, val silenced: Boolean, val riderClosed: Boolean) {
        constructor(i: Relay.Incident) : this(i.id, i.state, i.silenced, i.riderClosed)
    }

    enum class Sound { OFF, LOUD, VIBRATE }

    /** What this phone does about the incidents it can see: its sound, and the one thing to announce. */
    data class Plan(val sound: Sound, val announce: String?)

    /**
     * Both riders can be on a trip at once (Jack, 2026-09-24), so this phone may have
     * its own incident and the other rider's open together. Neither hides the other.
     *
     *  - The sound: the other rider's unsilenced alarm wins, riding or not — this
     *    phone has to wake somebody, on a bike over Spotify and Waze alike. My own
     *    incident only vibrates: I know I crashed (Jack, 2026-09-16). My own
     *    candidate is never said aloud; the telemetry answers it.
     *  - The announcement: the most urgent thing, once. Their silenced incident ranks
     *    below my own open one, so silencing theirs lets mine be seen.
     */
    fun plan(mine: Seen?, theirs: Seen?): Plan {
        val mineOpen = mine != null && mine.state == "sos" && !mine.riderClosed
        val theirsOpen = theirs != null && theirs.state == "sos"
        val theirsLoud = theirsOpen && !theirs!!.silenced
        val sound = when {
            theirsLoud -> Sound.LOUD
            mineOpen -> Sound.VIBRATE
            else -> Sound.OFF
        }
        val announce = when {
            theirsLoud -> "theirs:${theirs!!.id}"
            mineOpen -> "mine:${mine!!.id}"
            theirsOpen -> "theirs:${theirs!!.id}:silenced"
            theirs != null && theirs.state == "pending" -> "pending:${theirs.id}"
            else -> null
        }
        return Plan(sound, announce)
    }

    /** Decides on a snapshot the caller already has — the open screen has one. */
    fun decide(ctx: Context, snap: Relay.Snapshot) {
        val other = snap.other(ctx)
        val mine = snap.me(ctx)?.incident
        val theirs = other?.incident
        val name = other?.name ?: "The other rider"
        val plan = plan(mine?.let(::Seen), theirs?.let(::Seen))

        when (plan.sound) {
            Sound.LOUD -> PhoneAlarm.set(ctx, true, "incident #${theirs!!.id} for ${other.id}")
            Sound.VIBRATE -> PhoneAlarm.set(ctx, true, "my own incident #${mine!!.id}", silent = true)
            Sound.OFF -> PhoneAlarm.set(ctx, false, if (plan.announce == null) "nothing open" else "silenced or candidate")
        }

        val key = plan.announce
        headline = when {
            key == null -> ""
            key.startsWith("mine:") -> "Incident open — tap to say you are OK"
            key.startsWith("pending:") -> "$name: possible crash, checking…"
            theirs!!.riderClosed -> "$name says they are OK — close it to agree"
            else -> "$name may have crashed"
        }
        if (key == null) {
            if (announced != null) {
                announced = null
                OverlayPrompt.hideIncident(ctx)
                clear(ctx)
            }
            return
        }
        once(key) {
            when {
                key.startsWith("mine:") -> {
                    notify(ctx, "Rider incident open",
                        "Moto Tracker detected a potential incident.", urgent = true)
                    OverlayPrompt.incident(ctx, key, "Rider incident open",
                        "Moto Tracker detected a potential incident.")
                }
                key.startsWith("pending:") -> notify(ctx, headline, "No alarm yet", urgent = false)
                key.endsWith(":silenced") -> {
                    // Silenced somewhere: the pop-up goes, the incident stays open.
                    OverlayPrompt.hideIncident(ctx)
                    notify(ctx, headline, "Alarm silenced — the incident is still open", urgent = true)
                }
                else -> {
                    notify(ctx, headline, "Open Moto Tracker to silence and answer", urgent = true)
                    OverlayPrompt.incident(ctx, key, "$name may have crashed",
                        "Moto Tracker raised an alarm for $name.")
                }
            }
        }
    }

    /**
     * Says a thing once. The watcher runs every few seconds, and re-posting the same
     * notification each time is what turned one alert into a stream of them
     * (Jack, 2026-09-16: one is sufficient — the pop-up is what counts).
     */
    private fun once(key: String, body: () -> Unit) {
        if (announced == key) return
        announced = key
        body()
    }

    private fun notify(ctx: Context, title: String, text: String, urgent: Boolean) {
        val mgr = ctx.getSystemService(NotificationManager::class.java)
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Incidents",
                if (urgent) NotificationManager.IMPORTANCE_HIGH else NotificationManager.IMPORTANCE_DEFAULT)
        )
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        val n = Notification.Builder(ctx, CHANNEL)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentIntent(open)
            .setOngoing(urgent)
            .setCategory(Notification.CATEGORY_ALARM)
            .apply { if (urgent) setFullScreenIntent(open, true) }
            .build()
        mgr.notify(NOTIF_ID, n)
    }

    private fun clear(ctx: Context) {
        ctx.getSystemService(NotificationManager::class.java).cancel(NOTIF_ID)
    }
}
