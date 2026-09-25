package com.example.mototracker

import android.os.ParcelFileDescriptor
import android.provider.Settings
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.After
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import kotlin.concurrent.thread

/**
 * The rider's incident pop-up, raised exactly the way the incident watcher raises
 * it: from a network thread, never the main one. Until 2026-09-18 every such call
 * failed with "Can't create handler inside thread ... Looper.prepare()", and the
 * pop-up never appeared. Only a real Android shows this, so it runs on a device or
 * an emulator: ./gradlew connectedDebugAndroidTest
 *
 * The window manager is the judge of "on screen", not a screenshot: the headless
 * test images draw the window but hand back black screenshots.
 */
@RunWith(AndroidJUnit4::class)
class OverlayPromptTest {

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val ctx = instrumentation.targetContext

    @Before
    fun allowDrawingOverOtherApps() {
        // "Appear on top", granted the way a test can grant it.
        shell("appops set ${ctx.packageName} SYSTEM_ALERT_WINDOW allow")
        waitFor("the overlay permission to take") { Settings.canDrawOverlays(ctx) }
        RideLog.clear(ctx)
    }

    /** A test that fails part-way must not leave its pop-up in front of the next one. */
    @After
    fun takeDownWhateverIsLeft() {
        thread { OverlayPrompt.hide(ctx) }.join()
        waitFor("a clean screen") { overlay() == null }
    }

    @Test
    fun popUpAppearsWhenRaisedFromABackgroundThread() {
        thread(name = "network") {
            OverlayPrompt.incident(ctx, "mine:1", "Rider incident open", "Moto Tracker detected a potential incident.")
        }.join()
        waitFor("the pop-up to be logged") { RideLog.read(ctx).contains("incident pop-up (mine:1) shown") }
        val log = RideLog.read(ctx)
        assertFalse(log, log.contains("addView failed"))

        waitFor("the pop-up to be drawn on screen") {
            overlay()?.let { it.contains("mDrawState=HAS_DRAWN") && it.contains("isOnScreen=true") } == true
        }
        // Held up for a look when asked: am instrument -e holdMs 10000 ...
        InstrumentationRegistry.getArguments().getString("holdMs")?.toLongOrNull()?.let { Thread.sleep(it) }

        thread(name = "network") { OverlayPrompt.hide(ctx) }.join()
        waitFor("the pop-up to be dismissed") { RideLog.read(ctx).contains("dismissed") }
        waitFor("the pop-up to leave the screen") { overlay() == null }
    }

    /**
     * Both riding (Jack, 2026-09-24): the other rider's crash can arrive while this
     * rider's stop prompt is up. The alarm's pop-up must replace it, not wait behind it.
     */
    @Test
    fun theOtherRidersAlarmReplacesAStopPrompt() {
        thread(name = "ride") { OverlayPrompt.show(ctx) }.join()
        waitFor("the stop prompt") { overlay() != null && RideLog.read(ctx).contains("OVERLAY") }
        thread(name = "network") {
            OverlayPrompt.incident(ctx, "theirs:7", "Dana may have crashed", "Moto Tracker raised an alarm for Dana.")
        }.join()
        waitFor("the alarm pop-up to replace it") { RideLog.read(ctx).contains("incident pop-up (theirs:7) shown") }
        waitFor("exactly one pop-up on screen") { overlays() == 1 }
        // A stop prompt now must not cover the alarm.
        thread(name = "ride") { OverlayPrompt.show(ctx) }.join()
        Thread.sleep(300)
        assertTrue("the stop prompt covered the alarm", overlays() == 1)
        thread(name = "network") { OverlayPrompt.hideIncident(ctx) }.join()
        waitFor("the alarm pop-up to leave") { overlay() == null }
    }

    @Test
    fun takingDownAnIncidentLeavesTheStopPromptAlone() {
        thread(name = "ride") { OverlayPrompt.show(ctx) }.join()
        waitFor("the stop prompt") { overlay() != null }
        thread(name = "network") { OverlayPrompt.hideIncident(ctx) }.join()
        Thread.sleep(300)
        assertTrue("hideIncident took the stop prompt down", overlay() != null)
        thread(name = "ride") { OverlayPrompt.hide(ctx) }.join()
        waitFor("the stop prompt to leave") { overlay() == null }
    }

    private fun overlays(): Int =
        shellOut("dumpsys window windows").split(Regex("\n(?=  Window #)")).count {
            it.contains("package=${ctx.packageName}") && it.contains("ty=APPLICATION_OVERLAY")
        }

    /** This app's overlay in the window manager's dump, or null when there is none. */
    private fun overlay(): String? =
        shellOut("dumpsys window windows").split(Regex("\n(?=  Window #)")).firstOrNull {
            it.contains("package=${ctx.packageName}") && it.contains("ty=APPLICATION_OVERLAY")
        }

    private fun shell(command: String) {
        shellOut(command)
    }

    private fun shellOut(command: String): String {
        val out = instrumentation.uiAutomation.executeShellCommand(command)
        // Reading to the end is what waits for the command to finish.
        return ParcelFileDescriptor.AutoCloseInputStream(out).use { String(it.readBytes()) }
    }

    private fun waitFor(what: String, timeoutMs: Long = 5_000, done: () -> Boolean) {
        val until = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < until) {
            if (done()) return
            Thread.sleep(100)
        }
        assertTrue("timed out waiting for $what — the log says:\n${RideLog.read(ctx)}", done())
    }
}
