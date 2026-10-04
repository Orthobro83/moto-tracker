package com.example.mototracker

import android.view.ViewGroup
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebView
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.verticalScroll
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The Observer's map page, in the app's own WebView (2026-09-20: it drew nothing on
 * the phone — no tiles, no rider dot, not even the zoom control, which is what a
 * page that never started looks like).
 *
 * No key here: with a made-up one TomTom refuses the tiles, but Leaflet still lays
 * them out, so this says whether the page, the shipped Leaflet and `moto.start`
 * work — not whether the key is good.
 */
@RunWith(AndroidJUnit4::class)
class MapPageTest {

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val problems = mutableListOf<String>()

    @Test
    fun theMapStartsAndDrawsItsControlsAndTheRider() {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            lateinit var web: WebView
            val loaded = CountDownLatch(1)
            scenario.onActivity { activity ->
                web = makeWebView(activity) { loaded.countDown() }
                web.webChromeClient = object : WebChromeClient() {
                    override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                        if (m.messageLevel() == ConsoleMessage.MessageLevel.ERROR) {
                            problems += "${m.message()} (${m.sourceId()}:${m.lineNumber()})"
                        }
                        return true
                    }
                }
                activity.addContentView(web, ViewGroup.LayoutParams(1000, 800))
            }
            assertTrue("the page never finished loading", loaded.await(20, TimeUnit.SECONDS))

            assertEquals("the shipped Leaflet did not load $problems", "\"function\"", js(web, "typeof L.map"))
            assertEquals("the page's own script did not run $problems", "\"object\"", js(web, "typeof moto"))

            js(web, "moto.start('not-a-real-key');")
            waitFor("the map to be built") { js(web, "!!window.__motoReady") == "true" }
            waitFor("the zoom control to be drawn") {
                js(web, "document.querySelectorAll('.leaflet-control-zoom').length") == "1"
            }
            waitFor("tiles to be laid out") {
                (js(web, "document.querySelectorAll('.leaflet-tile').length").toIntOrNull() ?: 0) > 0
            }

            js(web, "moto.update({lat:13.6929,lon:-89.2182,speed:40,moving:true,alarm:false});")
            waitFor("the rider dot to appear") {
                js(web, "document.querySelectorAll('.rider-dot').length") == "1"
            }
            assertEquals("the speed bubble is wrong $problems", "\"40 km/h\"",
                js(web, "document.querySelector('.speedbub').textContent"))
            // A minute unheard is "Signal lost for m:ss", never "stopped" (2026-09-24),
            // outlined in yellow (Jack, 2026-09-29).
            js(web, "moto.update({lat:13.6929,lon:-89.2182,speed:0,moving:false,stopped:'off the bike',alarm:false,lost:true,lostFor:'1:05',alert:false});")
            assertEquals("a lost signal still says stopped $problems", "\"Signal lost for 1:05\"",
                js(web, "document.querySelector('.speedbub').textContent"))
            assertEquals("\"speedbub lost\"", js(web, "document.querySelector('.speedbub').className"))
            assertEquals("the zoom stepped down for no bar", "false", js(web, "document.body.classList.contains('sig')"))
            // Past two minutes the app's bar takes the top of the map; the zoom steps down.
            js(web, "moto.update({lat:13.6929,lon:-89.2182,speed:0,moving:false,alarm:false,lost:true,lostFor:'2:01',alert:true});")
            assertEquals("\"Signal lost for 2:01\"", js(web, "document.querySelector('.speedbub').textContent"))
            assertEquals("the zoom did not step down for the bar", "true", js(web, "document.body.classList.contains('sig')"))
            // Light by day, dark by night (Jack, 2026-09-29): the app says which, and the
            // page swaps TomTom's style in place.
            assertEquals("the map did not start dark", "false", js(web, "document.documentElement.classList.contains('light')"))
            js(web, "moto.update({lat:13.6929,lon:-89.2182,speed:40,moving:true,alarm:false,light:true});")
            assertEquals("the page did not turn light", "true", js(web, "document.documentElement.classList.contains('light')"))
            waitFor("TomTom's day tiles to be asked for") {
                js(web, "[...document.querySelectorAll('.leaflet-tile')].some(t => t.src.includes('/basic/main/'))") == "true"
            }
            js(web, "moto.update({lat:13.6929,lon:-89.2182,speed:40,moving:true,alarm:false,light:false});")
            assertEquals("the page did not turn dark again", "false", js(web, "document.documentElement.classList.contains('light')"))
            waitFor("TomTom's night tiles to be asked for again") {
                js(web, "[...document.querySelectorAll('.leaflet-tile')].some(t => t.src.includes('/basic/night/'))") == "true"
            }
            assertTrue("the page reported errors: $problems", problems.isEmpty())
        }
    }

    /**
     * The same page, but driven the way the app drives it: through the RiderMap
     * composable, with a key already in the phone's own storage so nothing has to be
     * fetched. This is the path that drew nothing on the phone.
     */
    @Test
    fun riderMapStartsTheMapByItself() {
        val ctx = instrumentation.targetContext
        ctx.getSharedPreferences("relay", android.content.Context.MODE_PRIVATE).edit()
            .putString("map_key", "not-a-real-key")
            .putLong("map_key_at", System.currentTimeMillis()).commit()
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            lateinit var host: androidx.compose.ui.platform.ComposeView
            scenario.onActivity { activity ->
                host = androidx.compose.ui.platform.ComposeView(activity).apply {
                    setContent {
                        // Exactly where the Observer screen puts it: in a column that
                        // scrolls. On the phone that left the map 376 x 0 — started,
                        // tiles loaded, dot drawn, and nothing on screen.
                        androidx.compose.foundation.layout.Column(
                            androidx.compose.ui.Modifier
                                .fillMaxWidth()
                                .verticalScroll(androidx.compose.foundation.rememberScrollState())
                        ) {
                            RiderMap(lat = 13.6929, lon = -89.2182, speedKmh = 40.0,
                                stoppedLabel = "stopped", alarm = false,
                                height = androidx.compose.ui.unit.Dp(280f))
                        }
                    }
                }
                activity.addContentView(host, ViewGroup.LayoutParams(1000, 900))
            }
            var web: WebView? = null
            waitFor("the map's WebView to appear") {
                instrumentation.runOnMainSync { web = findWebView(host) }
                web != null
            }
            val view = web!!
            waitFor("RiderMap to start the map") { js(view, "!!window.__motoReady") == "true" }
            waitFor("the rider dot to appear") {
                js(view, "document.querySelectorAll('.rider-dot').length") == "1"
            }
            assertEquals("the speed bubble is wrong $problems", "\"40 km/h\"",
                js(view, "document.querySelector('.speedbub').textContent"))
            // And it must have a size. Everything above is true of a map drawn into
            // a box no one can see.
            val reported = jsonString(js(view, "moto.status();"))
            RideLog.write(ctx, "MAP", "test measured $reported")
            val size = org.json.JSONObject(reported).getJSONArray("size")
            // 280dp was asked for, so anything much short of that is the same bug
            // wearing a different number.
            assertTrue("the map is ${size[0]} x ${size[1]}, and 280 high was asked for",
                size.getInt(0) > 200 && size.getInt(1) >= 250)
        }
    }

    /**
     * With no key and no relay to ask, the map cannot draw — and must say so in the
     * ride log rather than leaving a black rectangle and no explanation.
     */
    @Test
    fun withoutAKeyItSaysSoInTheLog() {
        val ctx = instrumentation.targetContext
        ctx.getSharedPreferences("relay", android.content.Context.MODE_PRIVATE).edit()
            .remove("map_key").remove("map_key_at").commit()
        RideLog.clear(ctx)
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                val host = androidx.compose.ui.platform.ComposeView(activity).apply {
                    setContent {
                        RiderMap(lat = 13.6929, lon = -89.2182, speedKmh = 0.0,
                            stoppedLabel = "stopped", alarm = false,
                            height = androidx.compose.ui.unit.Dp(280f))
                    }
                }
                activity.addContentView(host, ViewGroup.LayoutParams(1000, 900))
            }
            waitFor("the log to say there is no map key", 20_000) {
                RideLog.read(ctx).contains("no map key")
            }
        }
    }

    /** evaluateJavascript hands back JSON; a string comes back quoted and escaped. */
    private fun jsonString(answer: String): String =
        org.json.JSONTokener(answer).nextValue() as String

    private fun findWebView(v: android.view.View): WebView? = when {
        v is WebView -> v
        v is ViewGroup -> (0 until v.childCount).firstNotNullOfOrNull { findWebView(v.getChildAt(it)) }
        else -> null
    }

    /** Runs one expression in the page and gives back its JSON result. */
    private fun js(web: WebView, script: String): String {
        val done = CountDownLatch(1)
        var answer = ""
        instrumentation.runOnMainSync {
            web.evaluateJavascript(script) { value ->
                answer = value ?: ""
                done.countDown()
            }
        }
        check(done.await(10, TimeUnit.SECONDS)) { "the page never answered: $script" }
        return answer
    }

    private fun waitFor(what: String, timeoutMs: Long = 10_000, done: () -> Boolean) {
        val until = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < until) {
            if (done()) return
            Thread.sleep(200)
        }
        assertTrue("timed out waiting for $what — page errors: $problems", done())
    }
}
