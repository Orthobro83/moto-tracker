package com.example.mototracker

import android.content.Context
import android.os.PowerManager

/**
 * Whether this app is exempt from Doze / battery optimisation.
 *
 * The 2026-09-10 test ride showed the process frozen for 76% of the ride while
 * GPS kept capturing at a perfect 5-second cadence underneath — the classic
 * signature of an app that is *not* exempt. But the log had no way to say whether
 * the exemption had actually been granted, which left the result ambiguous.
 * This closes that.
 *
 * Note: One UI's "Sleeping apps" lists are a separate Samsung layer and are not
 * readable from here. GRANTED below does not prove the phone is fully configured.
 */
object Power {
    fun exempt(ctx: Context): Boolean =
        ctx.getSystemService(PowerManager::class.java)
            .isIgnoringBatteryOptimizations(ctx.packageName)
}
