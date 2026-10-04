package com.example.mototracker

/**
 * Phase 1 endurance spike. Throwaway by design — see checklist.md Phase 1.
 *
 * The one question this app exists to answer: does a foreground service keep
 * ticking every 5 seconds through a multi-hour ride on One UI, or does Samsung
 * throttle it? Everything here serves that and nothing else.
 */
object Config {
    /**
     * The relay on the VPS, over HTTPS (spike-14, design.md 2026-09-15). The phone
     * needs nothing but internet access to reach it — no Meshnet, and NordVPN on or
     * off makes no difference. Trust is our own CA only; see
     * res/xml/network_security_config.xml. Every request carries this phone's
     * device key (DeviceKey), obtained once with a pairing code.
     */
    const val SERVER = "https://203.0.113.10"

    const val TICK_MS = 5_000L

    /**
     * Off the bike and standing still, a fix every 30 s is plenty and costs far less
     * battery (Jack, 2026-10-04). Riding, or any sign of movement, is back to
     * [TICK_MS] at once.
     */
    const val OFF_BIKE_TICK_MS = 30_000L

    /**
     * Network test probe interval. Finer than a tick, so each switch-over can be
     * timed to within a few seconds. A failed probe adds the 4 s connect timeout.
     */
    const val PROBE_MS = 3_000L

    /** Where the log lands on the device, under getExternalFilesDir(null). */
    const val LOG_FILE = "spike.log"

    /**
     * How long a simulated crash waits before the relay escalates it (spike-15).
     * Long enough to get a glove off, short enough that a real crash is not sitting
     * unreported: design.md's candidate protocol uses the same window.
     */
    const val CONFIRM_WINDOW_S = 30

    /**
     * Nothing heard from the other rider for this long, and the map says "Signal lost
     * for m:ss" rather than how long they have been stopped: they are not known to be
     * stopped, only not heard (Jack, 2026-09-24). The relay decides this since relay-15
     * (Relay.Signal); this is the old rule, kept only for an older relay. It clears the
     * 30-70 s a wifi-to-mobile handover costs.
     */
    const val SIGNAL_LOST_S = 60

    /** Delay before the full-screen-intent test fires, giving time to switch to Waze. */
    const val TAKEOVER_DELAY_MS = 20_000L
}
