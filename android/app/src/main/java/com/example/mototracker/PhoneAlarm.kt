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

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/**
 * The phone's alarm (spike-15).
 *
 * It plays on the ALARM stream, so it is heard through a silenced ringer and over
 * Waze — a crash alert that a Do Not Disturb setting can swallow is no alert. It
 * follows the relay: one Observer silencing it quiets every Observer at once, and
 * this phone stops on the next poll without being told directly.
 *
 * It also takes the audio focus exclusively while it sounds (Jack, 2026-09-24), so
 * Spotify, YouTube and anything else playing pause instead of mixing under it — a
 * rider can be riding with music on when the other rider crashes. They resume when
 * the alarm stops.
 */
object PhoneAlarm {
    private var player: MediaPlayer? = null
    private var vibrator: Vibrator? = null
    private var focus: AudioFocusRequest? = null

    @Volatile var sounding = false
        private set

    /** True while only the vibration is running — the rider's own incident. */
    @Volatile var silentMode = false
        private set

    @Synchronized
    fun set(ctx: Context, on: Boolean, why: String = "", silent: Boolean = false) {
        if (on == sounding && silent == silentMode) return
        val wasOn = sounding
        sounding = on
        silentMode = silent
        if (on && wasOn) {            // switching between silent and loud
            stop(ctx, "mode change")
            start(ctx, why)
        } else if (on) start(ctx, why) else stop(ctx, why)
    }

    private fun start(ctx: Context, why: String) {
        RideLog.write(ctx, "ALARM", (if (silentMode) "vibrating — " else "sounding — ") + why)
        // The rider's own phone never makes noise. They know they crashed; an alarm
        // in their ear adds chaos and nothing else (Jack, 2026-09-16).
        if (!silentMode) runCatching {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            val attrs = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            val audio = ctx.getSystemService(AudioManager::class.java)
            // Refused during a phone call; the alarm plays regardless.
            focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
                .setAudioAttributes(attrs)
                .setOnAudioFocusChangeListener { }
                .build()
            val granted = audio.requestAudioFocus(focus!!) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
            RideLog.write(ctx, "ALARM", if (granted) "audio focus taken — other audio paused"
                                        else "audio focus refused — sounding over it anyway")
            player = MediaPlayer().apply {
                setAudioAttributes(attrs)
                setDataSource(ctx, uri)
                isLooping = true
                prepare()
                start()
            }
            // An alarm at 10% volume is not an alarm.
            val max = audio.getStreamMaxVolume(AudioManager.STREAM_ALARM)
            if (audio.getStreamVolume(AudioManager.STREAM_ALARM) < max * 0.7) {
                audio.setStreamVolume(AudioManager.STREAM_ALARM, (max * 0.8).toInt(), 0)
            }
        }.onFailure { RideLog.write(ctx, "ALARM", "sound failed: ${it.javaClass.simpleName} ${it.message}") }

        runCatching {
            vibrator = if (Build.VERSION.SDK_INT >= 31) {
                ctx.getSystemService(VibratorManager::class.java).defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                ctx.getSystemService(Vibrator::class.java)
            }
            vibrator?.vibrate(
                VibrationEffect.createWaveform(longArrayOf(0, 600, 400), 0)
            )
        }
    }

    private fun stop(ctx: Context, why: String) {
        RideLog.write(ctx, "ALARM", "stopped — $why")
        runCatching { player?.stop() }
        runCatching { player?.release() }
        player = null
        focus?.let { f ->
            runCatching { ctx.getSystemService(AudioManager::class.java).abandonAudioFocusRequest(f) }
        }
        focus = null
        runCatching { vibrator?.cancel() }
        vibrator = null
    }
}
