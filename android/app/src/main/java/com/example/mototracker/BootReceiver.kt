package com.example.mototracker

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Puts the listener back after a reboot or an app update, so "Dana started a ride"
 * does not depend on anyone remembering to open the app first (Jack, 2026-09-27).
 * TrackerApp has already loaded the device key by the time this runs.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        RideLog.write(ctx, "WATCH", "woken by ${intent.action?.substringAfterLast('.')}")
        ObserverService.start(ctx)
    }
}
