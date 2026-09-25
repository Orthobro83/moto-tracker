package com.example.mototracker

import android.graphics.Bitmap
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * Screenshots of Observer and Hybrid on made-up relay data (Jack, 2026-09-24), so the
 * layout can be looked at without a relay or a second rider. The pictures land in the
 * app's external files, under shots/:
 *
 *     adb pull /sdcard/Android/data/com.example.mototracker/files/shots
 *
 * Unpaired, so the map says it has no key; the layout around it is what this is for.
 * Drawn from the view itself, not captured from the screen, so the map's WebView
 * (which draws through the GPU) may come out blank.
 */
@RunWith(AndroidJUnit4::class)
class ScreensTest {

    private val instrumentation = InstrumentationRegistry.getInstrumentation()

    private fun rider(id: String, name: String, state: String, speed: Double, lastSeen: Double) =
        Relay.Rider(id, JSONObject()
            .put("name", name).put("state", state).put("trip_id", 120)
            .put("speed", speed).put("battery", 81).put("last_seen_s", lastSeen)
            .put("lat", 13.6929).put("lon", -89.2182))

    private fun shoot(name: String, tripState: Trip.State, drawerOpen: Boolean, meState: String = "riding",
                      solo: Boolean = false) {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity {
                it.showForTest(rider("jack", "Jack", meState, 63.0, 2.1),
                    rider("dana", "Dana", if (solo) "idle" else "riding", 47.0, 3.4), tripState, drawerOpen, solo)
            }
            Thread.sleep(2500)
            // The view draws itself into a bitmap: the emulator's screen captures come
            // back black here, and this does not go through the screen at all.
            var shot: Bitmap? = null
            scenario.onActivity {
                val root = it.window.decorView
                shot = Bitmap.createBitmap(root.width, root.height, Bitmap.Config.ARGB_8888)
                    .also { b -> root.draw(android.graphics.Canvas(b)) }
            }
            assertNotNull("no screenshot", shot)
            val dir = File(instrumentation.targetContext.getExternalFilesDir(null), "shots").apply { mkdirs() }
            File(dir, "$name.png").outputStream().use { shot!!.compress(Bitmap.CompressFormat.PNG, 100, it) }
        }
    }

    @Test fun rider() = shoot("0-rider", Trip.State.RIDING, drawerOpen = false, solo = true)
    @Test fun observer() = shoot("1-observer", Trip.State.IDLE, drawerOpen = false)
    @Test fun observerDrawer() = shoot("2-observer-drawer", Trip.State.IDLE, drawerOpen = true)
    @Test fun hybridRiding() = shoot("3-hybrid-riding", Trip.State.RIDING, drawerOpen = false)
    @Test fun hybridRidingDrawer() = shoot("4-hybrid-riding-drawer", Trip.State.RIDING, drawerOpen = true)
    @Test fun hybridOffBikeDrawer() =
        shoot("5-hybrid-offbike-drawer", Trip.State.OFF_BIKE, drawerOpen = true, meState = "offbike")
}
