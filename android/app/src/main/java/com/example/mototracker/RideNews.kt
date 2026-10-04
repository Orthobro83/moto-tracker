package com.example.mototracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent

/**
 * Every change in the other rider's trip, said out loud with the phone locked: the
 * trip opening, the ride starting, off the bike, riding again, the trip ending
 * (Jack, 2026-09-27; every state within a trip, 2026-09-29).
 *
 * Checked on every read of the relay, whoever made it: the listener service, the
 * ride service's watcher, the open screen. What was last seen is remembered across
 * restarts, so each change is announced once, and a phone switched on mid-trip says
 * where things stand on its first answer instead of staying quiet about it.
 *
 * A change between two reads can be missed — off the bike and back on inside one
 * poll is said as nothing. That is 5 seconds while the other rider is out.
 *
 * This is news, not an alarm, so the phone decides how it sounds (Jack, 2026-09-28):
 * the notification sound normally, a buzz on vibrate, nothing on silent or in Do Not
 * Disturb. The crash alarm is a different sound on a different stream and ignores all
 * of that (PhoneAlarm).
 */
object RideNews {

    private const val PREFS = "ride_news"
    private const val KEY_TRIP = "their_trip"
    private const val KEY_RIDING = "their_riding"
    private const val KEY_RODE = "their_rode"
    private const val KEY_KNOWN = "known"
    /** New id: 1.0.9's channel forced vibration on, and channel settings stick. */
    private const val CHANNEL = "rides"
    private const val OLD_CHANNEL = "ride_news"
    private const val NOTIF_ID = 9

    enum class Change { OPENED, STARTED, PAUSED, RESUMED, ENDED }

    /** The other rider as far as the news cares: which trip, and on the bike or not. */
    data class Seen(val trip: Int?, val riding: Boolean)

    /**
     * Pure, so the rule tests on the JVM. [before] is null on the first answer ever;
     * [rode] is whether this trip has had a ride in it yet, which is what separates
     * starting from resuming.
     */
    fun change(before: Seen?, now: Seen, rode: Boolean): Change? = when {
        now.trip == null -> if (before?.trip != null) Change.ENDED else null
        before == null || before.trip != now.trip ->   // first sight, or a new trip
            if (now.riding) Change.STARTED else Change.OPENED
        before.riding == now.riding -> null
        now.riding -> if (rode) Change.RESUMED else Change.STARTED
        else -> Change.PAUSED
    }

    @Synchronized
    fun check(ctx: Context, snap: Relay.Snapshot) {
        val other = snap.other(ctx) ?: return
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val before = if (!prefs.getBoolean(KEY_KNOWN, false)) null else Seen(
            if (prefs.contains(KEY_TRIP)) prefs.getInt(KEY_TRIP, 0) else null,
            prefs.getBoolean(KEY_RIDING, false))
        val now = Seen(other.tripId, other.tripId != null && other.state == "riding")
        // A new trip starts with no ride in it; any ride seen in this one counts.
        val rode = (before?.trip == now.trip && prefs.getBoolean(KEY_RODE, false)) ||
            (before == null && now.riding)
        val what = change(before, now, rode)
        if (before != now) {
            prefs.edit().putBoolean(KEY_KNOWN, true)
                .apply { if (now.trip == null) remove(KEY_TRIP) else putInt(KEY_TRIP, now.trip) }
                .putBoolean(KEY_RIDING, now.riding)
                .putBoolean(KEY_RODE, rode || now.riding)
                .commit()
        }
        what ?: return
        val name = other.name
        val (title, text) = when (what) {
            Change.OPENED -> "$name opened a trip" to "Off the bike for now"
            Change.STARTED -> "$name started riding" to "Tap to watch"
            Change.PAUSED -> "$name is off the bike" to "The trip is still open"
            Change.RESUMED -> "$name is riding again" to "Tap to watch"
            Change.ENDED -> "$name ended their trip" to "Their trip is closed"
        }
        RideLog.write(ctx, "NEWS", "$title (trip ${now.trip ?: before?.trip})")
        announce(ctx, title, text)
    }

    private fun announce(ctx: Context, title: String, text: String) {
        val mgr = ctx.getSystemService(NotificationManager::class.java)
        mgr.deleteNotificationChannel(OLD_CHANNEL)
        // HIGH with the default sound and nothing forced: the ringer mode and DND
        // decide between sound, buzz and silence.
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL, "Trip updates", NotificationManager.IMPORTANCE_HIGH)
                .apply { lockscreenVisibility = Notification.VISIBILITY_PUBLIC }
        )
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        val n = Notification.Builder(ctx, CHANNEL)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_watch)
            .setContentIntent(open)
            .setAutoCancel(true)
            .setCategory(Notification.CATEGORY_STATUS)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .build()
        // One slot: each change replaces the last instead of stacking under it, and
        // still sounds, because a re-post alerts again.
        mgr.notify(NOTIF_ID, n)
    }
}
