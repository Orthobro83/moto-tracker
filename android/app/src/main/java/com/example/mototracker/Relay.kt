package com.example.mototracker

import android.content.Context
import org.json.JSONObject

/**
 * The read side of the relay, and the incident actions (spike-15).
 *
 * Until now the spike only pushed. From relay-3 a rider key may also read, because
 * a phone is the OTHER rider's Observer whenever it is not the one riding
 * (design.md 2026-09-15). Which mode this phone is in is never a setting: it is
 * whatever the relay says about who has a trip open.
 */
object Relay {

    enum class Mode { HOME, RIDING, OBSERVER, HYBRID }

    class Incident(o: JSONObject) {
        val id = o.optInt("id")
        val kind: String = o.optString("kind")
        /** pending | sos | closed | retracted */
        val state: String = o.optString("state")
        /** pending (amber, still confirming) | red | yellow */
        val display: String = o.optString("display")
        val raisedAt: String = o.optString("raised_at")
        val escalateAt: String? = o.optString("escalate_at").takeIf { it.isNotEmpty() && it != "null" }
        val silenced = o.optString("silenced_at").let { it.isNotEmpty() && it != "null" }
        val riderClosed = o.optString("rider_closed_at").let { it.isNotEmpty() && it != "null" }
        val observerClosed = o.optString("observer_closed_at").let { it.isNotEmpty() && it != "null" }
        val g = o.optDouble("g", Double.NaN)
        val rot = o.optDouble("rot", Double.NaN)
        val speedBefore = o.optDouble("speed_before", Double.NaN)
        val speedAfter = o.optDouble("speed_after", Double.NaN)
        val lat: Double?
        val lon: Double?

        init {
            val p = o.optJSONObject("last_position")
            lat = p?.optDouble("lat")
            lon = p?.optDouble("lon")
        }

        /** Seconds left to retract, from the relay's own clock. */
        fun secondsToEscalate(asOfMs: Long): Long? {
            val at = Time.parse(escalateAt) ?: return null
            return ((at - asOfMs) / 1000).coerceAtLeast(0)
        }
    }

    class Rider(val id: String, o: JSONObject) {
        val name: String = o.optString("name", id)
        /** riding | offbike | sleep */
        val state: String = o.optString("state", "sleep")
        val tripId: Int? = if (o.isNull("trip_id")) null else o.optInt("trip_id")
        val speed = o.optDouble("speed", Double.NaN)
        val battery = if (o.isNull("battery")) null else o.optInt("battery")
        val lastSeenS = o.optDouble("last_seen_s", Double.NaN)
        val lat = if (o.isNull("lat")) null else o.optDouble("lat")
        val lon = if (o.isNull("lon")) null else o.optDouble("lon")
        val incident: Incident? = o.optJSONObject("incident")?.let { Incident(it) }
        /** The last closed trip, as the relay remembers it. Null once it ages out. */
        val previousTrip: JSONObject? = o.optJSONObject("previous_trip")
    }

    class Snapshot(val riders: Map<String, Rider>, val asOfMs: Long, val fetchedAt: Long) {
        fun me(ctx: Context): Rider? = riders[Identity.get(ctx)]
        fun other(ctx: Context): Rider? = riders.entries.firstOrNull { it.key != Identity.get(ctx) }?.value

        /**
         * The relay decides the mode. Opening the app asks it; a trip ending puts the
         * phone back on its home screen without anyone choosing anything.
         */
        fun mode(ctx: Context): Mode = when {
            me(ctx)?.tripId != null && other(ctx)?.tripId != null -> Mode.HYBRID
            me(ctx)?.tripId != null -> Mode.RIDING
            other(ctx)?.tripId != null -> Mode.OBSERVER
            else -> Mode.HOME
        }
    }

    /** The last answer, for whatever needs it without waiting for a fetch. */
    @Volatile var snapshot: Snapshot? = null
        private set

    fun refresh(ctx: Context): Snapshot? {
        val o = Uploader.getJson(ctx, "/state") ?: return null
        val riders = HashMap<String, Rider>()
        var asOf = 0L
        for (key in o.keys()) {
            val r = o.optJSONObject(key) ?: continue
            riders[key] = Rider(key, r)
            Time.parse(r.optString("as_of"))?.let { if (it > asOf) asOf = it }
        }
        // Counters run on the relay's clock, never this phone's.
        val snap = Snapshot(riders, if (asOf > 0) asOf else System.currentTimeMillis(),
                            System.currentTimeMillis())
        snapshot = snap
        return snap
    }

    /** Relay time now, carried forward in real time since the last answer. */
    fun relayNow(): Long {
        val s = snapshot ?: return System.currentTimeMillis()
        return s.asOfMs + (System.currentTimeMillis() - s.fetchedAt)
    }

    // ---------------------------------------------------------------- the rider's own side

    /**
     * The map key, fetched from the relay with this phone's device key and kept in
     * the app's own private storage (Jack, 2026-09-16). It is deliberately NOT in the
     * APK — a file anyone holding the phone could read it out of — and the tiles it
     * unlocks come straight from TomTom to this phone, so no tile byte crosses the
     * VPS. Revoking this phone's device key takes its maps with it.
     */
    fun mapKey(ctx: Context): String? {
        val prefs = ctx.getSharedPreferences("relay", Context.MODE_PRIVATE)
        val cached = prefs.getString("map_key", null)
        val age = System.currentTimeMillis() - prefs.getLong("map_key_at", 0)
        if (cached != null && age < 7 * 24 * 3600_000L) {
            RideLog.write(ctx, "MAP", "key from this phone's storage, ${age / 3600_000} h old")
            return cached
        }
        val fresh = Uploader.getJson(ctx, "/map-key")?.optString("key")?.takeIf { it.isNotEmpty() }
        if (fresh == null) {
            // Keep using what we have: no signal must not mean no map.
            RideLog.write(ctx, "MAP", if (cached == null) "no map key — the relay gave none"
                                      else "the relay gave no key; using the stored one")
            return cached
        }
        prefs.edit().putString("map_key", fresh)
            .putLong("map_key_at", System.currentTimeMillis()).commit()
        RideLog.write(ctx, "MAP", "key fetched from the relay")
        return fresh
    }

    /**
     * This rider's thresholds, computed by the Mini from the whole archive. Asked
     * for at the start of every trip, so a baseline that has moved since the last
     * ride is picked up without an app update. If the relay cannot be reached, the
     * detector falls back to the harder of the two known riders' numbers rather
     * than to nothing.
     */
    fun fetchBaseline(ctx: Context): Detector.Baseline {
        val rider = Identity.get(ctx)
        val o = Uploader.getJson(ctx, "/baseline?rider=$rider")
        if (o == null || !o.has("impact_g")) {
            RideLog.write(ctx, "BASELINE", "no baseline from the relay — using the default")
            return Detector.Baseline.UNKNOWN.copy(rider = rider)
        }
        // The relay stores the thresholds themselves, already margined by the Mini.
        val g = o.optDouble("impact_g", Detector.IMPACT_G_FLOOR)
        val rot = o.optDouble("impact_rot", Detector.IMPACT_ROT_FLOOR)
        return Detector.Baseline(
            rider = rider,
            gP999 = g / 1.5, gMax = g / 1.5,
            rotP999 = rot / 1.5, rotMax = rot / 1.5,
            decelMax = o.optDouble("decel_kmh_s", 6.7),
            movingSamples = ((o.optDouble("moving_h", 0.0) * 3600) / 5).toInt())
    }

    /**
     * The phone's own conclusion, sent as a candidate. The rider gets the confirm
     * window to say it was nothing; nobody else is told until it expires.
     */
    fun raiseCandidate(ctx: Context, v: Detector.Verdict): Int? {
        val evidence = JSONObject()
            .put("peak_g", v.impactG).put("peak_rot", v.impactRot)
            .put("speed_before", v.speedBefore).put("speed_after", v.speedAfter)
            .put("decel_kmh_s", v.decelKmhPerS).put("reason", v.reason)
            .put("kind", v.kind).put("detector", "on-device")
        val body = JSONObject()
            .put("rider", Identity.get(ctx))
            .put("confirm_window_s", Config.CONFIRM_WINDOW_S)
            .put("at", isoAt(v.atMs))
            .put("evidence", evidence)
        val answer = Uploader.postJson(ctx, "/incident/candidate", body)
        if (answer == null) {
            // No signal. The conclusion is not thrown away: it is held here and sent
            // the moment the relay can be reached (Jack, 2026-09-16). A dead spot
            // must not be the reason a crash goes unreported.
            hold(ctx, body)
            return null
        }
        val id = answer.optInt("incident_id")
        RideLog.write(ctx, "CRASH", "candidate #$id raised with the relay: ${v.reason}")
        return id
    }

    private const val HELD = "held_candidate"

    private fun hold(ctx: Context, body: JSONObject) {
        ctx.getSharedPreferences("relay", Context.MODE_PRIVATE)
            .edit().putString(HELD, body.toString()).commit()
        RideLog.write(ctx, "CRASH", "no signal — holding the candidate until there is")
    }

    /**
     * Called on every tick that reaches the relay. Delivers anything the phone
     * concluded while it had no signal, with the time it actually happened.
     */
    fun deliverHeld(ctx: Context) {
        val prefs = ctx.getSharedPreferences("relay", Context.MODE_PRIVATE)
        val held = prefs.getString(HELD, null) ?: return
        val body = runCatching { JSONObject(held) }.getOrNull()
        if (body == null) {
            prefs.edit().remove(HELD).commit()
            return
        }
        // Older than an hour and the relay will not believe the timestamp anyway.
        val at = Time.parse(body.optString("at"))
        if (at != null && System.currentTimeMillis() - at > 3_600_000) {
            prefs.edit().remove(HELD).commit()
            RideLog.write(ctx, "CRASH", "held candidate was too old to deliver; dropped")
            return
        }
        val answer = Uploader.postJson(ctx, "/incident/candidate", body) ?: return
        prefs.edit().remove(HELD).commit()
        RideLog.write(ctx, "CRASH",
            "held candidate delivered as #${answer.optInt("incident_id")}")
    }

    private fun isoAt(ms: Long): String =
        java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", java.util.Locale.US)
            .format(java.util.Date(ms))

    /**
     * DEV ONLY: raises a real crash candidate through the relay, exactly
     * as the detector will when it exists. Everything downstream is then real — the
     * confirm window, the escalation, the Mini's alarm, the Observer phone — which is
     * the whole point: a rehearsal that proves the chain, not a mock of it.
     */
    fun simulateCrash(ctx: Context, windowS: Int = Config.CONFIRM_WINDOW_S): String {
        val evidence = JSONObject()
            .put("peak_g", 9.4).put("peak_rot", 6.1)
            .put("simulated", true)
        val body = JSONObject()
            .put("rider", Identity.get(ctx))
            .put("confirm_window_s", windowS)
            .put("evidence", evidence)
        val answer = Uploader.postJson(ctx, "/incident/candidate", body)
            ?: return "✗ the relay did not accept it"
        val id = answer.optInt("incident_id")
        RideLog.write(ctx, "SIMCRASH", "simulated candidate #$id raised, ${windowS}s to retract")
        return "candidate #$id raised — ${windowS}s to press I'm OK"
    }

    /** Within the confirm window: nothing happened, drop it before anyone is told. */
    fun retract(ctx: Context, id: Int): Boolean {
        val ok = Uploader.postJson(ctx, "/incident/retract",
            JSONObject().put("rider", Identity.get(ctx)).put("incident_id", id)) != null
        RideLog.write(ctx, "INCIDENT", "retract #$id ${if (ok) "accepted" else "REFUSED"}")
        return ok
    }

    /**
     * The rider's half of closing, after it has escalated. It does not close the
     * incident by itself: an Observer has to close it too, which is what makes the
     * two of them speak to each other (design.md 2026-09-15).
     */
    fun resolve(ctx: Context, id: Int, resolution: String): Boolean {
        val ok = Uploader.postJson(ctx, "/incident/resolve",
            JSONObject().put("incident_id", id).put("rider", Identity.get(ctx))
                .put("resolution", resolution)) != null
        RideLog.write(ctx, "INCIDENT", "rider close #$id as $resolution ${if (ok) "accepted" else "REFUSED"}")
        return ok
    }

    /**
     * I need help, pressed on an incident that is already open. Overrides every other
     * rule on the relay: no confirm window, no automatic retraction (Jack, 2026-09-16).
     */
    fun escalate(ctx: Context, id: Int): Boolean {
        val ok = Uploader.postJson(ctx, "/incident/escalate",
            JSONObject().put("rider", Identity.get(ctx)).put("incident_id", id)) != null
        RideLog.write(ctx, "INCIDENT", "I NEED HELP on #$id ${if (ok) "accepted" else "REFUSED"}")
        return ok
    }

    /** The rider asking for help outright. Raises an incident that never waits. */
    fun needHelp(ctx: Context): Boolean {
        val ok = Uploader.postJson(ctx, "/stop-answer",
            JSONObject().put("rider", Identity.get(ctx)).put("answer", "help")) != null
        RideLog.write(ctx, "INCIDENT", "I NEED HELP pressed ${if (ok) "— relay told" else "— REFUSED"}")
        return ok
    }

    // ---------------------------------------------------------------- the Observer's side

    /** Quiets every Observer at once, and closes nothing. */
    fun silence(ctx: Context, id: Int): Boolean {
        val ok = Uploader.postJson(ctx, "/incident/silence", JSONObject().put("incident_id", id)) != null
        RideLog.write(ctx, "INCIDENT", "silence #$id ${if (ok) "accepted" else "REFUSED"}")
        return ok
    }

    /** The Observer's half of closing. The rider still has to close it too. */
    fun observerClose(ctx: Context, id: Int): Boolean {
        val ok = Uploader.postJson(ctx, "/incident/observer-close",
            JSONObject().put("incident_id", id)) != null
        RideLog.write(ctx, "INCIDENT", "observer close #$id ${if (ok) "accepted" else "REFUSED"}")
        return ok
    }
}

/** ISO-8601 from the relay, in milliseconds. */
object Time {
    private val formats = listOf("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", "yyyy-MM-dd'T'HH:mm:ssXXX")

    fun parse(iso: String?): Long? {
        if (iso.isNullOrEmpty() || iso == "null") return null
        for (f in formats) {
            runCatching {
                return java.text.SimpleDateFormat(f, java.util.Locale.US).parse(iso)?.time
            }
        }
        return null
    }
}
