package com.example.mototracker

import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import kotlinx.coroutines.flow.first
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min

/**
 * The state of the ride, drawn rather than described (Jack, 2026-09-16).
 *
 * Riding is the road from just behind the rider, its markings running towards you at
 * the speed actually being ridden — still when stopped, full pace at 100 km/h — so a
 * glance says both "this is riding mode" and roughly how fast. Off the bike is that same bike from the same place, parked and
 * empty: the camera never moves, the rider simply gets off.
 *
 * Drawn with vectors in the app's own palette rather than shipped as artwork: it
 * scales to any screen, weighs nothing, and cannot drift out of step with the theme.
 */

private val ROAD = Color(0xFF191C22)
private val PAINT = Color(0xFFC8CDD4)
private val MARK = Color(0xFFE8EAED)
private val RIDER = Color(0xFF4DA3FF)
private val RIDER_DARK = Color(0xFF2C6DB5)
private val BIKE = Color(0xFF8E98A4)
private val BIKE_DARK = Color(0xFF3A4048)
private val TAIL = Color(0xFFB0343A)
private val INDICATOR = Color(0xFF8E7A45)
private val SHADOW = Color(0x33000000)

/** Where the rider sits across the frame, and how wide their lane is in those terms. */
private const val RIDER_X = 0.57f
private const val LANE_FRACTION = 0.98f

/** One easing for the whole scene: 0 at the horizon, 1 at the bottom of the frame. */
private fun ease(t: Float) = t * t * 0.78f + t * 0.22f

/** Crossfades between the two scenes as the rider's state changes. */
@Composable
fun StateScene(riding: Boolean, speedKmh: Double, modifier: Modifier = Modifier) {
    // Compose does not clip a Canvas to its own bounds, and this scene deliberately
    // draws past them — the road runs off the sides of the frame. Without this the
    // lane lines carry on across the screen (Jack, 2026-09-16). Rounded to match the
    // cards above it.
    Crossfade(
        targetState = riding, label = "scene",
        animationSpec = tween(durationMillis = 650, easing = LinearEasing),
        modifier = modifier.clip(RoundedCornerShape(14.dp))
    ) { isRiding ->
        if (isRiding) RidingScene(speedKmh, Modifier.fillMaxSize())
        else ParkedScene(Modifier.fillMaxSize())
    }
}

/**
 * How fast the road runs, from 0 to 1, in 5% steps (Jack, 2026-09-24): still at a
 * standstill, full speed at 100 km/h, and no faster beyond it. Each step is 5 km/h,
 * floored, so the 1-3 km/h a stationary GPS reports keeps the road still.
 */
internal fun roadRate(speedKmh: Double): Float {
    if (speedKmh.isNaN() || speedKmh <= 0.0) return 0f
    return floor(min(speedKmh, 100.0) / 5.0).toInt() * 0.05f
}

/** At full speed a marking crosses the frame in this long. */
private const val FULL_SPEED_CROSSING_S = 0.7f

@Composable
fun RidingScene(speedKmh: Double, modifier: Modifier = Modifier) {
    // The road is advanced frame by frame at the current rate, rather than by an
    // animation with a speed-dependent duration: changing a running animation's
    // duration restarts it, and the markings would jump at every change of speed.
    // (The old duration also worked out above its own ceiling at every speed, so
    // the road always ran at the standstill pace, and never stopped.)
    val rate = rememberUpdatedState(roadRate(speedKmh))
    var phase by remember { mutableFloatStateOf(0f) }
    LaunchedEffect(Unit) {
        while (true) {
            // Stopped: draw nothing new until the rider moves again.
            if (rate.value == 0f) snapshotFlow { rate.value }.first { it > 0f }
            var last = 0L
            while (rate.value > 0f) {
                withFrameNanos { now ->
                    if (last != 0L) {
                        phase = (phase + (now - last) / 1e9f * rate.value / FULL_SPEED_CROSSING_S) % 1f
                    }
                    last = now
                }
            }
        }
    }

    Canvas(modifier.fillMaxWidth()) {
        val w = size.width
        val h = size.height
        val horizon = h * 0.10f
        // Everything converges on the point straight ahead of the rider, because
        // that is what a straight road does. The rider sits in the right-hand lane,
        // so that point is above them, not above the middle of the frame.
        val vanishX = w * RIDER_X
        fun yAt(t: Float) = horizon + (h - horizon) * ease(t)
        fun xAt(t: Float, bottomX: Float) = vanishX + (bottomX - vanishX) * ease(t)

        // The road is the whole frame: a trapezoid would leave black wedges in the
        // corners and read as a tent rather than a road.
        drawRect(ROAD, size = size)

        // The dashed line is the middle of the road and the two solid lines are its
        // edges, so each sits the same distance from it — at every depth, since both
        // gaps scale by the same easing (Jack, 2026-09-16). The rider rides in the
        // middle of the right-hand lane, which puts them half a lane off the centre.
        val laneWidth = 0.98f * w
        val centre = (RIDER_X - 0.5f * LANE_FRACTION) * w
        val left = centre - laneWidth
        val right = centre + laneWidth

        // Edge lines thicken as they approach and fade into the distance instead of
        // meeting in a point — two lines converging to an apex look like a tent.
        val start = 0.055f
        val fade = Brush.verticalGradient(
            0f to Color.Transparent,
            0.35f to PAINT.copy(alpha = 0.30f),
            1f to PAINT.copy(alpha = 0.55f),
            startY = yAt(start), endY = h)
        for (bottomX in listOf(left, right)) {
            val topWidth = 1.0f
            val bottomWidth = w * 0.030f
            val edge = Path().apply {
                moveTo(xAt(start, bottomX) - topWidth / 2, yAt(start))
                lineTo(xAt(start, bottomX) + topWidth / 2, yAt(start))
                lineTo(xAt(1f, bottomX) + bottomWidth / 2, h)
                lineTo(xAt(1f, bottomX) - bottomWidth / 2, h)
                close()
            }
            drawPath(edge, fade)
        }

        // The dashed lane line, to the rider's left, marching towards the viewer.
        val dashes = 7
        for (i in 0 until dashes) {
            val t = ((i.toFloat() / dashes) + phase) % 1f
            if (t < 0.015f) continue
            val tEnd = min(1f, t + 0.075f + 0.04f * t)
            val wTop = max(0.9f, w * 0.026f * ease(t) + 1.0f)
            val wBottom = max(1.2f, w * 0.026f * ease(tEnd) + 1.2f)
            val dash = Path().apply {
                moveTo(xAt(t, centre) - wTop / 2, yAt(t))
                lineTo(xAt(t, centre) + wTop / 2, yAt(t))
                lineTo(xAt(tEnd, centre) + wBottom / 2, yAt(tEnd))
                lineTo(xAt(tEnd, centre) - wBottom / 2, yAt(tEnd))
                close()
            }
            drawPath(dash, MARK.copy(alpha = 0.20f + 0.65f * ease(t)))
        }

        drawRider(w, h)
    }
}

/** The rider, from behind, sitting in the lane. They never move: the road does. */
private fun DrawScope.drawRider(w: Float, h: Float) {
    val cx = w * RIDER_X
    val base = h * 0.86f
    val u = min(w, h) * 0.052f

    drawOval(SHADOW, topLeft = Offset(cx - u * 2.3f, base - u * 0.5f),
        size = Size(u * 4.6f, u * 1.25f))
    // Rear tyre, then the tail of the bike with its light.
    drawRoundRect(BIKE_DARK, topLeft = Offset(cx - u * 0.42f, base - u * 2.6f),
        size = Size(u * 0.84f, u * 2.7f), cornerRadius = CornerRadius(u * 0.4f))
    drawRoundRect(BIKE, topLeft = Offset(cx - u * 0.95f, base - u * 3.7f),
        size = Size(u * 1.9f, u * 1.5f), cornerRadius = CornerRadius(u * 0.5f))
    drawRoundRect(TAIL, topLeft = Offset(cx - u * 0.34f, base - u * 3.4f),
        size = Size(u * 0.68f, u * 0.34f), cornerRadius = CornerRadius(u * 0.16f))

    // Knees, out either side of the tank so they read as legs.
    for (side in listOf(-1f, 1f)) {
        drawRoundRect(RIDER_DARK,
            topLeft = Offset(cx + side * u * 1.30f - u * 0.38f, base - u * 4.6f),
            size = Size(u * 0.76f, u * 2.2f), cornerRadius = CornerRadius(u * 0.34f))
    }

    val torso = Path().apply {
        moveTo(cx - u * 1.30f, base - u * 6.6f)
        lineTo(cx + u * 1.30f, base - u * 6.6f)
        lineTo(cx + u * 1.00f, base - u * 3.9f)
        lineTo(cx - u * 1.00f, base - u * 3.9f)
        close()
    }
    drawPath(torso, RIDER)

    for (side in listOf(-1f, 1f)) {
        val arm = Path().apply {
            moveTo(cx + side * u * 1.15f, base - u * 6.4f)
            lineTo(cx + side * u * 2.45f, base - u * 5.2f)
            lineTo(cx + side * u * 2.15f, base - u * 4.8f)
            lineTo(cx + side * u * 0.90f, base - u * 5.95f)
            close()
        }
        drawPath(arm, RIDER_DARK)
        drawRoundRect(BIKE_DARK,
            topLeft = Offset(cx + side * u * 2.62f - u * 0.42f, base - u * 5.45f),
            size = Size(u * 0.84f, u * 0.44f), cornerRadius = CornerRadius(u * 0.2f))
    }

    drawCircle(RIDER, radius = u * 1.18f, center = Offset(cx, base - u * 7.65f))
    drawArc(RIDER_DARK, startAngle = 205f, sweepAngle = 130f, useCenter = false,
        topLeft = Offset(cx - u * 1.18f, base - u * 8.83f),
        size = Size(u * 2.36f, u * 2.36f), style = Stroke(width = u * 0.2f))
}

/** The same bike, from the same place, parked and empty. */
@Composable
fun ParkedScene(modifier: Modifier = Modifier) {
    Canvas(modifier.fillMaxWidth()) {
        val w = size.width
        val h = size.height
        val horizon = h * 0.30f
        val cx = w * 0.5f

        drawRect(ROAD, size = size)

        // The bay: two lines running towards the viewer, either side of the bike.
        for (side in listOf(-1f, 1f)) {
            val bay = Path().apply {
                moveTo(cx + side * w * 0.20f, horizon + h * 0.05f)
                lineTo(cx + side * w * 0.23f, horizon + h * 0.05f)
                lineTo(cx + side * w * 0.46f, h)
                lineTo(cx + side * w * 0.40f, h)
                close()
            }
            drawPath(bay, PAINT.copy(alpha = 0.22f))
        }

        val base = h * 0.86f
        val u = min(w, h) * 0.064f

        // On its side stand, so the shadow falls to one side.
        drawOval(SHADOW, topLeft = Offset(cx - u * 2.0f, base - u * 0.6f),
            size = Size(u * 5.0f, u * 1.3f))

        drawRoundRect(BIKE_DARK, topLeft = Offset(cx - u * 0.46f, base - u * 2.8f),
            size = Size(u * 0.92f, u * 2.9f), cornerRadius = CornerRadius(u * 0.42f))
        drawRoundRect(BIKE, topLeft = Offset(cx - u * 1.0f, base - u * 4.0f),
            size = Size(u * 2.0f, u * 1.5f), cornerRadius = CornerRadius(u * 0.5f))
        drawRoundRect(TAIL, topLeft = Offset(cx - u * 0.36f, base - u * 3.7f),
            size = Size(u * 0.72f, u * 0.36f), cornerRadius = CornerRadius(u * 0.17f))
        for (side in listOf(-1f, 1f)) {
            drawCircle(INDICATOR, radius = u * 0.18f,
                center = Offset(cx + side * u * 1.15f, base - u * 3.55f))
        }

        // The empty seat — the whole point of the picture.
        drawRoundRect(BIKE_DARK, topLeft = Offset(cx - u * 1.25f, base - u * 5.3f),
            size = Size(u * 2.5f, u * 1.5f), cornerRadius = CornerRadius(u * 0.7f))
        drawRoundRect(BIKE.copy(alpha = 0.85f),
            topLeft = Offset(cx - u * 1.05f, base - u * 6.2f),
            size = Size(u * 2.1f, u * 1.2f), cornerRadius = CornerRadius(u * 0.55f))

        // Bars, grips, and the mirrors on their stalks.
        drawRoundRect(BIKE_DARK, topLeft = Offset(cx - u * 1.95f, base - u * 6.55f),
            size = Size(u * 3.9f, u * 0.26f), cornerRadius = CornerRadius(u * 0.13f))
        for (side in listOf(-1f, 1f)) {
            drawRoundRect(BIKE_DARK,
                topLeft = Offset(cx + side * u * 1.62f - u * 0.36f, base - u * 6.62f),
                size = Size(u * 0.72f, u * 0.4f), cornerRadius = CornerRadius(u * 0.18f))
            drawLine(BIKE,
                Offset(cx + side * u * 1.80f, base - u * 6.6f),
                Offset(cx + side * u * 2.12f, base - u * 7.25f),
                strokeWidth = u * 0.14f)
            drawOval(BIKE,
                topLeft = Offset(cx + side * u * 2.12f - u * 0.42f, base - u * 7.62f),
                size = Size(u * 0.84f, u * 0.56f))
        }
    }
}
