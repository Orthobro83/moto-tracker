package com.example.mototracker

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Target of the full-screen-intent test — the screen that must appear over Waze.
 *
 * This is the second platform assumption Phase 1 exists to check. Android 14+
 * restricts USE_FULL_SCREEN_INTENT to calling and alarm apps by default; if a
 * sideloaded app cannot seize the screen, the five-minute stop escalation in the
 * real design silently degrades to an ordinary notification and the glove problem
 * comes back unsolved.
 */
class TakeoverActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        RideLog.write(this, "TAKEOVER", "activity shown — full-screen intent WORKED")

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#8c0f11"))
            setPadding(48, 96, 48, 48)
        }
        root.addView(TextView(this).apply {
            text = "Full-screen intent fired"
            textSize = 26f
            setTextColor(Color.WHITE)
        })
        root.addView(TextView(this).apply {
            text = "\nIf you are reading this over Waze, the five-minute stop escalation is viable on this phone.\n"
            textSize = 15f
            setTextColor(Color.parseColor("#f0d0d0"))
        })
        root.addView(Button(this).apply {
            text = "Dismiss"
            setOnClickListener {
                RideLog.write(this@TakeoverActivity, "TAKEOVER", "dismissed by user")
                finish()
            }
        })
        setContentView(root)
    }
}
