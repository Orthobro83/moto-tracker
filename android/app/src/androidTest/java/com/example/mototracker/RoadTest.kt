package com.example.mototracker

import android.graphics.Bitmap
import android.graphics.Canvas
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * The road stands still when the rider does, and runs when they ride (Jack,
 * 2026-09-24). Two drawings of the scene 400 ms apart: identical at 0 km/h,
 * different at 60. The pace itself is `SceneTest`'s job.
 */
@RunWith(AndroidJUnit4::class)
class RoadTest {

    private fun twoFrames(kmh: Double): Pair<Bitmap, Bitmap> {
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { it.setContent { RidingScene(kmh, Modifier.fillMaxSize()) } }
            Thread.sleep(1500)
            fun draw(): Bitmap {
                var b: Bitmap? = null
                scenario.onActivity {
                    val root = it.window.decorView
                    b = Bitmap.createBitmap(root.width, root.height, Bitmap.Config.ARGB_8888)
                        .also { bmp -> root.draw(Canvas(bmp)) }
                }
                return b!!
            }
            val first = draw()
            Thread.sleep(400)
            return first to draw()
        }
    }

    @Test fun stoppedRoadDoesNotMove() {
        val (a, b) = twoFrames(0.0)
        assertTrue("the road moved at 0 km/h", a.sameAs(b))
    }

    @Test fun ridingRoadMoves() {
        val (a, b) = twoFrames(60.0)
        assertFalse("the road stood still at 60 km/h", a.sameAs(b))
    }
}
