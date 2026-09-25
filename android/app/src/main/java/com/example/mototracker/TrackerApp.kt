package com.example.mototracker

import android.app.Application
import java.io.PrintWriter
import java.io.StringWriter

/**
 * Installs the uncaught-exception handler before anything else can run.
 *
 * Without adb logcat this is the only crash visibility that exists. An app that
 * dies on launch would otherwise be completely silent — you would return from a
 * two-hour ride with an empty log and no way to tell "Samsung killed it" from
 * "it crashed in the first second".
 */
class TrackerApp : Application() {
    override fun onCreate() {
        super.onCreate()

        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            val trace = StringWriter().also { error.printStackTrace(PrintWriter(it)) }.toString()
            RideLog.write(this, "CRASH", "thread=${thread.name} ${trace.replace('\n', '|')}")
            previous?.uncaughtException(thread, error)
        }

        RideLog.write(this, "APP", "process start build=${BuildConfig.VERSION_NAME}")
        DeviceKey.load(this)
        RideLog.write(this, "PAIR", if (DeviceKey.current != null) "device key present" else "not paired")
    }
}
