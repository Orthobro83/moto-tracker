package com.example.mototracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.net.Uri

/**
 * The other rider unheard for two minutes, with nothing violent before it (Jack,
 * 2026-09-29): an alert, not a crash. A single chime and a standard notification,
 * once per signal loss, alongside the bar the map shows. When the signal comes back
 * the notification says so, silently, instead of vanishing and leaving the question.
 *
 * The relay decides when (Relay.Signal), and this runs only on a fresh answer from
 * it (Relay.refresh), so this phone losing the relay itself never alerts about the
 * other rider. A signal loss that becomes an incident chimes no more: the incident's
 * own alarm is what sounds (IncidentWatch).
 *
 * News, not an alarm, so the phone decides how it sounds, as for RideNews: the chime
 * normally, a buzz on vibrate, nothing on silent or in Do Not Disturb.
 */
object SignalWatch {

    private const val PREFS = "signal_watch"
    /** The signal loss already alerted for: its `since`, which names it. */
    private const val KEY_SINCE = "alerted_since"
    private const val CHANNEL = "signal"
    /** The signal coming back: its own channel, low, so it can never make a sound. */
    private const val CHANNEL_BACK = "signal_back"
    private const val NOTIF_ID = 11

    enum class Act { ALERT, BACK, CLEAR }

    /**
     * Pure, so the rule tests on the JVM. [alerted] is the signal loss already
     * alerted for, if any; [tripOpen], [since] and [alert] are the other rider now.
     */
    fun act(alerted: String?, tripOpen: Boolean, since: String?, alert: Boolean): Act? = when {
        !tripOpen -> if (alerted != null) Act.CLEAR else null     // their trip is over: moot
        since == null -> if (alerted != null) Act.BACK else null  // heard again
        alert && since != alerted -> Act.ALERT                     // two minutes, and not yet said
        else -> null        // under two minutes, already said, or an incident has taken over
    }

    @Synchronized
    fun check(ctx: Context, snap: Relay.Snapshot) {
        val other = snap.other(ctx) ?: return
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val alerted = prefs.getString(KEY_SINCE, null)
        val signal = other.signal
        when (act(alerted, other.tripId != null, signal?.since, signal?.alert == true)) {
            Act.ALERT -> {
                prefs.edit().putString(KEY_SINCE, signal!!.since).commit()
                val seen = when {
                    signal.since == other.tripStarted || other.speed.isNaN() -> "nothing heard since the trip opened"
                    other.state == "offbike" -> "last seen off the bike"
                    else -> "last seen at ${other.speed.toInt()} km/h"
                }
                RideLog.write(ctx, "SIGNAL", "${other.name}: signal lost for " +
                    "${signal.lostFor(Relay.relayNow())} — alert, $seen")
                post(ctx, "${other.name} — signal lost", "Awaiting acquisition of signal · $seen",
                     since = signal.sinceOnThisPhone(), silent = false)
            }
            Act.BACK -> {
                prefs.edit().remove(KEY_SINCE).commit()
                val from = Time.parse(alerted)?.let(::hhmm) ?: "—"
                val to = Time.parse(other.lastReceivedAt)?.let(::hhmm) ?: "now"
                RideLog.write(ctx, "SIGNAL", "${other.name}: signal back (unheard $from to $to)")
                post(ctx, "${other.name} — signal back", "Nothing was heard from $from to $to",
                     since = null, silent = true)
            }
            Act.CLEAR -> {
                prefs.edit().remove(KEY_SINCE).commit()
                RideLog.write(ctx, "SIGNAL", "${other.name}'s trip ended during a signal loss — alert withdrawn")
                ctx.getSystemService(NotificationManager::class.java).cancel(NOTIF_ID)
            }
            null -> Unit
        }
    }

    private fun hhmm(ms: Long): String =
        java.text.SimpleDateFormat("HH:mm", java.util.Locale.US).format(java.util.Date(ms))

    private fun post(ctx: Context, title: String, text: String, since: Long?, silent: Boolean) {
        val mgr = ctx.getSystemService(NotificationManager::class.java)
        // HIGH with its own chime and nothing forced: the ringer mode and DND decide
        // between the chime, a buzz and silence. A channel's sound is fixed once it
        // exists, so a different chime would need a new channel id.
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Signal lost", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "The other rider unheard for two minutes. One chime; not a crash alarm."
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
                setSound(Uri.parse("android.resource://${ctx.packageName}/${R.raw.signal_chime}"),
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build())
            }
        )
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL_BACK, "Signal back", NotificationManager.IMPORTANCE_LOW).apply {
                description = "The other rider heard again after a signal loss. Never makes a sound."
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            }
        )
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        val n = Notification.Builder(ctx, if (silent) CHANNEL_BACK else CHANNEL)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_watch)
            .setContentIntent(open)
            .setAutoCancel(true)
            .setCategory(Notification.CATEGORY_STATUS)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .apply {
                // The header counts up from the last packet, like the bar on the map.
                if (since != null) setWhen(since).setShowWhen(true).setUsesChronometer(true)
            }
            .build()
        // One slot: the signal coming back replaces the loss rather than stacking under it.
        mgr.notify(NOTIF_ID, n)
    }
}
