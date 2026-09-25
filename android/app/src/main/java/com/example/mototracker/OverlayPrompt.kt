package com.example.mototracker

import android.content.Context
import android.graphics.Color
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The stop prompt as a window overlay rather than a full-screen intent.
 *
 * Why this exists: the full-screen intent test failed on 2026-09-08 even with the
 * permission reporting GRANTED. That is Android working as documented — a
 * full-screen intent only launches immediately when the screen is off or locked.
 * On an unlocked phone in active use it degrades to a heads-up notification, which
 * is precisely the rider's situation: awake, unlocked, Waze in front.
 *
 * SYSTEM_ALERT_WINDOW ("Appear on top", "Popup" in One UI) has no such rule. An
 * overlay draws over whatever is in the foreground, Waze included.
 *
 * design.md forbade overlays on 2026-08-16, then narrowed that on 2026-09-08 to
 * allow the stop prompt to take the screen. **Jack widened it again on 2026-09-16:**
 * an open incident does take the screen, because a rider who has crashed needs one
 * obvious way back into the app — and the pop-up, not a notification, is what draws
 * over Waze.
 */
object OverlayPrompt {

    /** Only ever read or written on the main thread. */
    private var view: View? = null

    /**
     * What the pop-up on screen is: "stop" for the stop prompt, or an incident's key
     * ("mine:<id>", "theirs:<id>"). Main thread only, like the view. An incident
     * replaces a stop prompt or a different incident; a stop prompt never replaces an
     * incident (Jack, 2026-09-24: with both riding, either can arrive while the other
     * is up).
     */
    private var showing: String? = null

    private val main = Handler(Looper.getMainLooper())

    /**
     * Windows and views belong to the main thread, and the incident watcher decides
     * on a network thread. Adding the pop-up from there failed every single time with
     * "Can't create handler inside thread ... Looper.prepare()", so until 2026-09-18
     * the pop-up had never once appeared: the phone vibrated and showed nothing.
     * Every entry point goes through here, whoever calls it.
     *
     * The application context, not the caller's: the open screen passes itself, and
     * a pop-up that must outlive whatever is in front cannot belong to an activity.
     */
    private fun onMain(ctx: Context, block: (Context) -> Unit) {
        val app = ctx.applicationContext
        if (Looper.myLooper() == Looper.getMainLooper()) block(app) else main.post { block(app) }
    }

    fun canDraw(ctx: Context): Boolean =
        if (Build.VERSION.SDK_INT >= 23) Settings.canDrawOverlays(ctx) else true

    fun show(ctx: Context, stoppedFor: String = "5:00") = onMain(ctx) { showNow(it, stoppedFor) }

    private fun showNow(ctx: Context, stoppedFor: String) {
        if (!canDraw(ctx)) {
            RideLog.write(ctx, "OVERLAY", "cannot draw overlays — permission not granted")
            return
        }
        if (view != null) return

        val wm = ctx.getSystemService(WindowManager::class.java)
        val type = if (Build.VERSION.SDK_INT >= 26)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            type,
            // Focusable, so the buttons take taps. Not FLAG_NOT_TOUCHABLE.
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON,
            android.graphics.PixelFormat.OPAQUE
        ).apply { gravity = Gravity.CENTER }

        val root = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#181b20"))
            setPadding(48, 96, 48, 64)
        }
        root.addView(TextView(ctx).apply {
            text = "STOPPED FOR"
            textSize = 13f
            setTextColor(Color.parseColor("#6b7480"))
        })
        root.addView(TextView(ctx).apply {
            text = stoppedFor
            textSize = 40f
            setTextColor(Color.parseColor("#ffb020"))
        })
        root.addView(TextView(ctx).apply {
            text = "\nTell Dana what's happening.\n"
            textSize = 15f
            setTextColor(Color.parseColor("#9aa3ad"))
        })

        // Glove-sized. The entire reason the notification actions were not enough.
        fun bigButton(label: String, bg: String, fg: String, tag: String) =
            Button(ctx).apply {
                text = label
                textSize = 19f
                setTextColor(Color.parseColor(fg))
                setBackgroundColor(Color.parseColor(bg))
                setPadding(24, 56, 24, 56)
                setOnClickListener {
                    RideLog.write(ctx, "OVERLAY", "answered: $tag")
                    hide(ctx)
                }
            }

        root.addView(bigButton("Arrived / Stopping", "#3ddc84", "#06210f", "arrived"))
        root.addView(TextView(ctx).apply { textSize = 6f })
        root.addView(bigButton("Stuck in traffic", "#ffb020", "#2a1a00", "traffic"))
        root.addView(TextView(ctx).apply { textSize = 6f })
        root.addView(bigButton("I need help", "#ff4d4f", "#ffffff", "help"))

        runCatching { wm.addView(root, params) }
            .onSuccess {
                view = root
                showing = "stop"
                RideLog.write(ctx, "OVERLAY", "shown — this drew OVER whatever was in front")
            }
            .onFailure { RideLog.write(ctx, "OVERLAY", "addView failed: ${it.message}") }
    }

    /**
     * The incident pop-up, for this rider's own incident or the other rider's. One
     * button, because someone who has just come off a bike should not be reading a
     * menu. The alarm sound is not part of this: IncidentWatch decides it, and the
     * rider's own phone stays quiet (Jack, 2026-09-16).
     */
    fun incident(ctx: Context, key: String, title: String, body: String) =
        onMain(ctx) { incidentNow(it, key, title, body) }

    /** Takes down an incident pop-up, leaving a stop prompt where it is. */
    fun hideIncident(ctx: Context) = onMain(ctx) {
        if (showing != null && showing != "stop") hideNow(it)
    }

    private fun incidentNow(ctx: Context, key: String, title: String, body: String) {
        if (!canDraw(ctx)) {
            RideLog.write(ctx, "OVERLAY", "cannot draw the incident pop-up — no permission")
            return
        }
        if (showing == key) return
        if (view != null) hideNow(ctx)

        val wm = ctx.getSystemService(WindowManager::class.java)
        val type = if (Build.VERSION.SDK_INT >= 26)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON,
            android.graphics.PixelFormat.OPAQUE
        ).apply { gravity = Gravity.CENTER }

        val root = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#1b1114"))
            setPadding(52, 48, 52, 48)
        }
        root.addView(TextView(ctx).apply {
            text = "MOTO TRACKER"
            textSize = 12f
            letterSpacing = 0.18f
            setTextColor(Color.parseColor("#ff4d4f"))
        })
        root.addView(TextView(ctx).apply {
            text = title
            textSize = 26f
            setTextColor(Color.parseColor("#e8eaed"))
            setPadding(0, 18, 0, 0)
        })
        root.addView(TextView(ctx).apply {
            text = body + "\n"
            textSize = 15f
            setTextColor(Color.parseColor("#a3acb6"))
            setPadding(0, 10, 0, 22)
        })
        root.addView(Button(ctx).apply {
            text = "Go to app"
            textSize = 20f
            setTextColor(Color.parseColor("#06210f"))
            setBackgroundColor(Color.parseColor("#3ddc84"))
            setPadding(24, 56, 24, 56)
            setOnClickListener {
                RideLog.write(ctx, "OVERLAY", "incident pop-up: go to app")
                hide(ctx)
                runCatching {
                    ctx.startActivity(
                        android.content.Intent(ctx, MainActivity::class.java).addFlags(
                            android.content.Intent.FLAG_ACTIVITY_NEW_TASK or
                                android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP or
                                android.content.Intent.FLAG_ACTIVITY_CLEAR_TOP))
                }
            }
        })

        runCatching { wm.addView(root, params) }
            .onSuccess {
                view = root
                showing = key
                RideLog.write(ctx, "OVERLAY", "incident pop-up ($key) shown over whatever was in front")
            }
            .onFailure { RideLog.write(ctx, "OVERLAY", "addView failed: ${it.message}") }
    }

    fun hide(ctx: Context) = onMain(ctx) { hideNow(it) }

    private fun hideNow(ctx: Context) {
        val v = view ?: return
        runCatching { ctx.getSystemService(WindowManager::class.java).removeView(v) }
        view = null
        showing = null
        RideLog.write(ctx, "OVERLAY", "dismissed")
    }
}
