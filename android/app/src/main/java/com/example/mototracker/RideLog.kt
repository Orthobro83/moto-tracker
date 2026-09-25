package com.example.mototracker

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The local log is the ground truth for the go/no-go gate.
 *
 * Ticks are also shipped to the server, but a gap in the server log cannot
 * separate One UI throttling from a cellular dropout — and separating those is
 * the entire point of Phase 1. Only the on-device file says whether the service
 * actually ran.
 *
 * With no adb logcat available (design.md "Diagnostics without logcat"), this
 * file is also the only place a crash can be seen.
 */
object RideLog {
    private val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US)
    private val lock = Any()

    fun file(ctx: Context): File = File(ctx.getExternalFilesDir(null), Config.LOG_FILE)

    fun write(ctx: Context, tag: String, msg: String) {
        val line = "${stamp.format(Date())}\t$tag\t$msg\n"
        synchronized(lock) {
            runCatching { file(ctx).appendText(line) }
        }
    }

    fun read(ctx: Context): String =
        runCatching { file(ctx).readText() }.getOrDefault("")

    fun sizeBytes(ctx: Context): Long =
        runCatching { file(ctx).length() }.getOrDefault(0L)

    fun lineCount(ctx: Context): Int =
        runCatching { file(ctx).readLines().size }.getOrDefault(0)

    fun clear(ctx: Context) {
        synchronized(lock) { runCatching { file(ctx).delete() } }
    }
}
