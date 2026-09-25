package com.example.mototracker

import android.content.Context
import android.net.ConnectivityManager
import android.os.Build
import android.os.SystemClock
import okhttp3.ConnectionPool
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Ships ticks to the relay on the VPS, and the whole log on demand. From spike-14
 * every request carries this phone's device key and goes over HTTPS.
 *
 * Network failures are logged locally and otherwise ignored. The spike must keep
 * ticking through a dead mesh — a gap in the server log caused by the app giving
 * up would be indistinguishable from the throttling we are actually testing for.
 */
object Uploader {
    /** Replaced wholesale by [reconnect], so every sender reads it fresh. */
    @Volatile private var client = newClient()

    private fun newClient(): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .writeTimeout(6, TimeUnit.SECONDS)
        .readTimeout(6, TimeUnit.SECONDS)
        // Every request carries the device key once this phone has one.
        .addInterceptor { chain ->
            val key = DeviceKey.current
            chain.proceed(
                if (key == null) chain.request()
                else chain.request().newBuilder().header("Authorization", "Bearer $key").build()
            )
        }
        .build()

    /**
     * One map tile, to see whether TomTom will serve this phone. Its own client:
     * the shared one puts this phone's device key on every request, and that key is
     * ours, not TomTom's. The map key is never written to the log.
     */
    internal fun probeTile(key: String): String = runCatching {
        val bare = OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS).readTimeout(8, TimeUnit.SECONDS).build()
        val req = Request.Builder()
            .url("https://api.tomtom.com/map/1/tile/basic/night/14/3767/8199.png?key=$key")
            .get().build()
        bare.newCall(req).execute().use { "HTTP ${it.code}, ${it.body?.bytes()?.size ?: 0} bytes" }
    }.getOrElse { "${it.javaClass.simpleName}: ${it.message}" }

    /** GET returning JSON, for the read side (spike-15). Null on any failure. */
    internal fun getJson(ctx: Context, path: String): JSONObject? = runCatching {
        val req = Request.Builder().url("${Config.SERVER}$path").get().build()
        client.newCall(req).execute().use {
            if (!it.isSuccessful) {
                RideLog.write(ctx, "NET", "$path refused: HTTP ${it.code}")
                null
            } else JSONObject(it.body?.string().orEmpty())
        }
    }.getOrElse { err ->
        RideLog.write(ctx, "NET", "$path failed: ${err.javaClass.simpleName} ${err.message}")
        null
    }

    /** POST returning the relay's answer, for actions whose answer matters. */
    internal fun postJson(ctx: Context, path: String, body: JSONObject): JSONObject? = runCatching {
        val req = Request.Builder()
            .url("${Config.SERVER}$path")
            .post(body.toString().toRequestBody(JSON))
            .build()
        client.newCall(req).execute().use {
            val text = it.body?.string().orEmpty()
            if (!it.isSuccessful) {
                RideLog.write(ctx, "NET", "$path refused: HTTP ${it.code} $text")
                null
            } else if (text.isEmpty()) JSONObject() else JSONObject(text)
        }
    }.getOrElse { err ->
        RideLog.write(ctx, "NET", "$path failed: ${err.javaClass.simpleName} ${err.message}")
        null
    }

    private val TEXT = "text/plain; charset=utf-8".toMediaType()
    private val JSON = "application/json; charset=utf-8".toMediaType()

    private fun post(ctx: Context, path: String, body: JSONObject): Boolean = runCatching {
        val req = Request.Builder()
            .url("${Config.SERVER}$path")
            .post(body.toString().toRequestBody(JSON))
            .build()
        client.newCall(req).execute().use {
            if (!it.isSuccessful) RideLog.write(ctx, "NET", "$path refused: HTTP ${it.code}" +
                if (it.code == 401) " — this phone has no valid key; pair it again" else "")
            it.isSuccessful
        }
    }.getOrElse { err ->
        RideLog.write(ctx, "NET", "$path failed: ${err.javaClass.simpleName} ${err.message}")
        false
    }

    /**
     * Is the server actually reachable right now?
     *
     * Worth its own check rather than inferring from tick failures: on 2026-09-10
     * Meshnet stayed dead after a connectivity blip and needed a manual restart in
     * the NordVPN app, while every phone-side indicator looked healthy. The rider
     * needs to see a dead tunnel in the driveway, not discover it afterwards.
     *
     * Returns round-trip milliseconds, or null if unreachable.
     */
    fun ping(): Long? = probe().ms

    /** One /health request: round-trip ms, or the reason it failed. */
    class Probe(val ms: Long?, val error: String?)

    fun probe(c: OkHttpClient = client): Probe = try {
        val started = System.currentTimeMillis()
        val req = Request.Builder().url("${Config.SERVER}/health").get().build()
        c.newCall(req).execute().use {
            if (it.isSuccessful) Probe(System.currentTimeMillis() - started, null)
            else Probe(null, "HTTP ${it.code}")
        }
    } catch (e: Exception) {
        Probe(null, "${e.javaClass.simpleName} ${e.message}")
    }

    /**
     * "Reconnect to server": everything the phone itself can reset, in order.
     *
     *  1. Cancel every in-flight request and close every pooled connection, then
     *     replace the client outright, so nothing from before the switch survives.
     *  2. Try the server three times on the brand-new client.
     *  3. Try once more with the socket pinned to Android's current default
     *     network, in case a connection was still following the network that went away.
     *
     * Nothing here can reach NordVPN's tunnel or the Mini. If this fails while
     * mobile data works, the stuck state is somewhere the app cannot touch — which
     * is exactly what this button is here to find out. Blocking: call off the main thread.
     */
    fun reconnect(ctx: Context): String {
        val t0 = SystemClock.elapsedRealtime()
        RideLog.write(ctx, "RECONNECT", "pressed — link ${Link.describe()} — networks ${NetWatch.snapshot(ctx)}")

        val old = client
        runCatching { old.dispatcher.cancelAll() }
        runCatching { old.connectionPool.evictAll() }
        val fresh = newClient()
        client = fresh
        RideLog.write(ctx, "RECONNECT", "step 1: old connections closed, new client in place")

        for (attempt in 1..3) {
            val r = probe(fresh)
            if (r.ms != null) return reconnected(ctx, t0, "new client, attempt $attempt", r.ms)
            RideLog.write(ctx, "RECONNECT", "step 2: attempt $attempt failed: ${r.error}")
        }

        val active = ctx.getSystemService(ConnectivityManager::class.java).activeNetwork
        if (active != null) {
            // Own pool, so a pinned socket can never be reused after the next switch.
            val pinned = fresh.newBuilder()
                .socketFactory(active.socketFactory)
                .connectionPool(ConnectionPool())
                .build()
            val r = probe(pinned)
            runCatching { pinned.connectionPool.evictAll() }
            if (r.ms != null) return reconnected(ctx, t0, "socket pinned to default network", r.ms)
            RideLog.write(ctx, "RECONNECT", "step 3: pinned attempt failed: ${r.error}")
        } else {
            RideLog.write(ctx, "RECONNECT", "step 3: skipped — Android reports no default network")
        }

        val secs = (SystemClock.elapsedRealtime() - t0) / 1000.0
        RideLog.write(ctx, "RECONNECT", String.format(java.util.Locale.US,
            "FAILED after %.1f s — networks %s", secs, NetWatch.snapshot(ctx)))
        return String.format(java.util.Locale.US, "✗ still unreachable (tried for %.0f s)", secs)
    }

    private fun reconnected(ctx: Context, t0: Long, how: String, ms: Long): String {
        val secs = (SystemClock.elapsedRealtime() - t0) / 1000.0
        RideLog.write(ctx, "RECONNECT", String.format(java.util.Locale.US,
            "SUCCESS via %s — %d ms round trip, %.1f s after pressing", how, ms, secs))
        return String.format(java.util.Locale.US, "✓ reconnected (%s) in %.1f s", how, secs)
    }

    /**
     * Exchange a pairing code for this phone's device key. The key is stored and
     * never shown or logged; the rider it is bound to becomes this phone's identity.
     * Returns a line for the screen.
     */
    fun pair(ctx: Context, code: String): String = try {
        val body = JSONObject()
            .put("code", code.trim())
            .put("name", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
        val req = Request.Builder()
            .url("${Config.SERVER}/pair")
            .post(body.toString().toRequestBody(JSON))
            .build()
        client.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            when {
                resp.isSuccessful -> {
                    val j = JSONObject(text)
                    val rider = j.optString("rider").takeIf { it.isNotEmpty() && it != "null" }
                    DeviceKey.save(ctx, j.getString("key"), j.getString("role"), rider, j.getString("name"))
                    rider?.let { Identity.set(ctx, it) }
                    RideLog.write(ctx, "PAIR", "paired as ${j.getString("name")} (${j.getString("role")}${rider?.let { ", $it" } ?: ""})")
                    "✓ paired as ${j.getString("name")}" + (rider?.let { " — rider $it" } ?: "")
                }
                resp.code == 403 -> {
                    RideLog.write(ctx, "PAIR", "code refused (403)")
                    "✗ that code isn't valid — mistyped, already used, or older than 10 minutes"
                }
                resp.code == 429 -> "✗ too many attempts — wait ten minutes"
                else -> "✗ the relay answered HTTP ${resp.code}"
            }
        }
    } catch (e: javax.net.ssl.SSLException) {
        RideLog.write(ctx, "PAIR", "TLS failed: ${e.message}")
        "✗ secure connection failed (certificate): ${e.message}"
    } catch (e: Exception) {
        RideLog.write(ctx, "PAIR", "failed: ${e.javaClass.simpleName} ${e.message}")
        "✗ ${e.javaClass.simpleName}: ${e.message}"
    }

    /** Start riding. Opens a trip if none is open, else resumes within it. */
    fun rideStart(ctx: Context): Boolean =
        post(ctx, "/trip/start", JSONObject().put("rider", Identity.get(ctx)))

    /**
     * Opens the trip, saying whether the rider is on the bike. A relay from before
     * 2026-09-20 has no idea what `riding` means and opens every trip riding, so if
     * its answer says so, this puts it off the bike itself — the app works either
     * way round, deployed or not.
     */
    fun tripStart(ctx: Context, riding: Boolean): Boolean {
        val rider = Identity.get(ctx)
        val answer = postJson(ctx, "/trip/start",
            JSONObject().put("rider", rider).put("riding", riding)) ?: return false
        if (!riding && answer.optString("state") == "riding") {
            RideLog.write(ctx, "TRIP", "older relay opened it riding — putting it off the bike")
            return postJson(ctx, "/ride/offbike", JSONObject().put("rider", rider)) != null
        }
        return true
    }

    /**
     * Off the bike — arrived, or making a stop. The trip stays open and telemetry
     * keeps reporting, because an hour at a destination is part of the trip.
     */
    fun offBike(ctx: Context): Boolean =
        post(ctx, "/ride/offbike", JSONObject().put("rider", Identity.get(ctx)))

    /** End trip: closes the trip and sleeps. Pressed on reaching home. */
    fun tripEnd(ctx: Context): Boolean =
        post(ctx, "/trip/end", JSONObject().put("rider", Identity.get(ctx)))

    /** One 5-second packet, in the shape the real server expects. */
    fun postPosition(
        ctx: Context, lat: Double, lon: Double,
        accuracy: Float, speedKmh: Float, battery: Int, s: Sensors.Summary? = null
    ): Boolean = post(ctx, "/position", JSONObject()
        .apply {
            // Sensor summary for the window since the last tick. Sent every tick,
            // per design.md — the server must already hold the pre-crash context
            // before the signal drops, because after it drops nothing more can be.
            if (s != null) {
                put("peak_g", s.peakG); put("min_g", s.minG)
                put("mean_g", s.meanG); put("rms_g", s.rmsG)
                put("peak_rot", s.peakRot); put("mean_rot", s.meanRot)
                put("accel_n", s.n); put("gyro_n", s.gyroN)
                // Braking and cornering, which the figures above cannot show.
                put("peak_horiz_g", s.peakHorizG); put("mean_horiz_g", s.meanHorizG)
            }
        }
        .put("rider", Identity.get(ctx))
        // The phone's own clock, so the server can tell a tick that was generated
        // late (One UI throttling) from one that merely arrived late (network).
        // Those are the two things Friday's ride has to separate.
        .put("ts", isoNow())
        .put("lat", lat).put("lon", lon)
        .put("accuracy", accuracy.toDouble())
        .put("speed", speedKmh.toDouble())
        .put("battery", battery))

    private fun isoNow(): String =
        java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", java.util.Locale.US)
            .format(java.util.Date())

    /**
     * The ride's log, sent when the trip ends and cleared once it has landed
     * (Jack, 2026-09-16 — the spike needed a button; the app does not).
     *
     * Cleared only on success, so a trip that ends out of coverage keeps its log and
     * sends it the next time the relay can be reached. Each upload is therefore one
     * trip's worth, which is how the Mini files them.
     */
    fun uploadRideLog(ctx: Context): Boolean {
        val prefs = ctx.getSharedPreferences("relay", Context.MODE_PRIVATE)
        val body = RideLog.read(ctx)
        if (body.isEmpty()) {
            prefs.edit().putBoolean(PENDING_LOG, false).commit()
            return true
        }
        val sent = runCatching {
            val req = Request.Builder()
                .url("${Config.SERVER}/spike/log?build=${BuildConfig.VERSION_NAME}" +
                    "&rider=${Identity.get(ctx)}")
                .post(body.toRequestBody(TEXT))
                .build()
            client.newCall(req).execute().use { it.isSuccessful }
        }.getOrDefault(false)
        prefs.edit().putBoolean(PENDING_LOG, !sent).commit()
        if (sent) {
            RideLog.clear(ctx)
            RideLog.write(ctx, "LOG", "previous ride's log uploaded (${body.length} bytes)")
        } else {
            RideLog.write(ctx, "LOG", "no signal for the log — it will go when there is")
        }
        return sent
    }

    /** True when a ride's log is still waiting for a connection. */
    fun logWaiting(ctx: Context): Boolean =
        ctx.getSharedPreferences("relay", Context.MODE_PRIVATE).getBoolean(PENDING_LOG, false)

    private const val PENDING_LOG = "pending_log"

    /** The whole log file, on demand from diagnostics. */
    fun postLog(ctx: Context): String = runCatching {
        val body = RideLog.read(ctx)
        if (body.isEmpty()) return "log is empty"
        val req = Request.Builder()
            .url("${Config.SERVER}/spike/log?build=${BuildConfig.VERSION_NAME}&rider=${Identity.get(ctx)}")
            .post(body.toRequestBody(TEXT))
            .build()
        client.newCall(req).execute().use { resp ->
            if (resp.isSuccessful) "uploaded ${body.length} bytes" else "server said ${resp.code}"
        }
    }.getOrElse { err -> "failed: ${err.javaClass.simpleName} ${err.message}" }
}
