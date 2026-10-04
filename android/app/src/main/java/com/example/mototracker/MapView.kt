package com.example.mototracker

import android.annotation.SuppressLint
import android.content.Context
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.foundation.shape.RoundedCornerShape
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.json.JSONTokener

/** Where the maps open, and whose sun they follow until a rider has a position: San Salvador. */
internal const val HOME_LAT = 13.6929
internal const val HOME_LON = -89.2182

/**
 * The Observer's map: Leaflet in a WebView, with TomTom's tiles and traffic flow drawn
 * straight from TomTom to this phone — the light day style from sunrise, the night
 * style from sunset (Jack, 2026-09-29; see Sun).
 *
 * The key is fetched from the relay and kept in private storage, so it is not in the
 * APK and no tile crosses the VPS (Jack, 2026-09-16). The page itself ships in the
 * app's assets — no CDN, so the map still draws on a bad connection as long as the
 * tiles come.
 *
 * **It says what it is doing (2026-09-20).** On a ride it drew nothing at all — no
 * tiles, no rider dot, not even the zoom control — and the log had not one word
 * about the map, so there was no way to tell a missing key from a page that never
 * started. Every step is now written to the ride log, a key that does not come is
 * asked for again, and a map that cannot draw says so on screen instead of showing
 * black.
 */
@Composable
fun RiderMap(
    lat: Double?,
    lon: Double?,
    speedKmh: Double,
    stoppedLabel: String?,
    alarm: Boolean,
    height: Dp = 260.dp,
    /** How long the rider has been unheard, "m:ss", while the relay says so; else null. */
    signalLostFor: String? = null,
    /** Past two minutes: the app's bar is over the top of the map. */
    signalAlert: Boolean = false,
    /** The sun is up where the rider is: TomTom's light day style instead of night. */
    light: Boolean = false,
    modifier: Modifier = Modifier,
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    var web by remember { mutableStateOf<WebView?>(null) }
    var ready by remember { mutableStateOf(false) }
    var key by remember { mutableStateOf<String?>(null) }
    var started by remember { mutableStateOf(false) }
    var note by remember { mutableStateOf<String?>(null) }

    // One refused request must not mean no map for the rest of the ride.
    LaunchedEffect(Unit) {
        var wait = 5_000L
        while (true) {
            val fetched = withContext(Dispatchers.IO) { Relay.mapKey(context) }
            if (fetched != null) {
                key = fetched
                if (!started) note = null
                break
            }
            note = "No map key from the relay yet"
            delay(wait)
            wait = (wait * 2).coerceAtMost(60_000L)
        }
    }

    // Hand the key in once the page has loaded and the key is known.
    LaunchedEffect(ready, key, web) {
        val view = web ?: return@LaunchedEffect
        val k = key ?: return@LaunchedEffect
        if (!ready || started) return@LaunchedEffect
        // Its height goes with the key: see map.html for why the page cannot work
        // it out on its own.
        view.evaluateJavascript(
            "moto.start(${JSONObject.quote(k)}, ${height.value.toInt()}, $light);") { answer ->
            val said = unquote(answer)
            RideLog.write(context, "MAP", "start: $said")
            if (said == "ok" || said == "already started") {
                started = true
                note = null
            } else {
                note = "The map could not start"
            }
        }
    }

    // Once it is up, say how it went — which is the difference between "no tiles"
    // and "no map".
    LaunchedEffect(started) {
        if (!started) return@LaunchedEffect
        delay(8_000)
        web?.evaluateJavascript("moto.status();") { answer ->
            val text = unquote(answer)
            RideLog.write(context, "MAP", "status $text")
            val status = runCatching { JSONObject(text) }.getOrNull() ?: return@evaluateJavascript
            if (status.optInt("loaded") == 0 && status.optInt("failed") > 0) {
                note = "TomTom would not serve the map tiles"
            }
        }
    }

    // A page that has not started fifteen seconds after the key arrived is not going
    // to on its own. One reload, written down, beats a black rectangle for a ride.
    LaunchedEffect(started, key) {
        if (started || key == null) return@LaunchedEffect
        delay(15_000)
        val view = web
        if (!started && view != null) {
            RideLog.write(context, "MAP", "nothing after 15 s — reloading the page")
            ready = false
            view.reload()
        }
    }

    // Each change of style is written down with the day it was worked out from, so a
    // map that went light at the wrong time can be checked against the almanac.
    LaunchedEffect(light, started) {
        if (!started) return@LaunchedEffect
        RideLog.write(context, "MAP", (if (light) "day style (light)" else "night style (dark)") +
            " — sun up " + Sun.describe(System.currentTimeMillis(), lat ?: HOME_LAT, lon ?: HOME_LON) +
            String.format(java.util.Locale.US, " at %.2f,%.2f", lat ?: HOME_LAT, lon ?: HOME_LON))
    }

    LaunchedEffect(lat, lon, speedKmh, alarm, signalLostFor, signalAlert, light, started) {
        val view = web ?: return@LaunchedEffect
        if (!started || lat == null || lon == null) return@LaunchedEffect
        val state = JSONObject()
            .put("lat", lat).put("lon", lon)
            .put("speed", speedKmh)
            .put("moving", speedKmh >= 3)
            .put("stopped", stoppedLabel ?: "stopped")
            .put("alarm", alarm)
            .put("lost", signalLostFor != null)
            .put("lostFor", signalLostFor ?: "")
            .put("alert", signalAlert)
            .put("light", light)
        view.evaluateJavascript("moto.update($state);", null)
    }

    DisposableEffect(Unit) {
        onDispose { web?.destroy() }
    }

    Box(modifier.fillMaxWidth().height(height).clip(RoundedCornerShape(14.dp))) {
        AndroidView(
            factory = { ctx ->
                makeWebView(ctx, report = { RideLog.write(ctx, "MAP", it) }) { ready = true }
                    .also { web = it }
            },
            modifier = Modifier.fillMaxWidth().height(height))
        note?.takeIf { !started }?.let {
            Text(it, color = Color(0xFF6B7480), fontSize = 13.sp, textAlign = TextAlign.Center,
                modifier = Modifier.align(Alignment.Center).padding(24.dp))
        }
    }
}

/** evaluateJavascript hands back JSON; a string comes back quoted and escaped. */
private fun unquote(answer: String?): String =
    runCatching { JSONTokener(answer ?: "").nextValue() as? String }.getOrNull() ?: (answer ?: "")

@SuppressLint("SetJavaScriptEnabled")
internal fun makeWebView(ctx: Context, report: (String) -> Unit = {}, onReady: () -> Unit): WebView =
    WebView(ctx).apply {
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        // Tiles are cacheable for a long time; using the cache is what keeps the
        // TomTom request count — and the phone's data — small on familiar roads.
        settings.cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
        settings.setGeolocationEnabled(false)
        setBackgroundColor(0xFF11141A.toInt())
        webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                if (m.messageLevel() == ConsoleMessage.MessageLevel.ERROR) {
                    report("js error: ${m.message()} (line ${m.lineNumber()})")
                }
                return true
            }
        }
        webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                report("page loaded")
                onReady()
            }

            override fun onReceivedError(view: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (req.isForMainFrame) report("page failed: ${err.description}")
            }
        }
        loadUrl("file:///android_asset/map.html")
    }
