package com.example.mototracker

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest

/**
 * Which networks the phone has, logged every time that changes.
 *
 * spike-12 exists to watch wifi-to-mobile switch-overs, so each failure in the
 * log has to sit next to the switch that caused it. Without this the log shows
 * a dead link but not whether wifi had just gone, mobile had just arrived, or
 * NordVPN's tunnel had just been rebuilt.
 *
 * Summary format: one entry per network, e.g. "WIFI✓ CELL✓ VPN✓".
 * ✓ = Android has validated internet on it, ? = connected but not validated.
 */
object NetWatch {
    private var users = 0
    private var callback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var last = ""

    @Suppress("DEPRECATION") // allNetworks: fine for a throwaway spike, and it sees the VPN too
    fun snapshot(ctx: Context): String {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val parts = cm.allNetworks.mapNotNull { net ->
            val caps = cm.getNetworkCapabilities(net) ?: return@mapNotNull null
            val name = when {
                caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "VPN"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "WIFI"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "CELL"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ETH"
                else -> "OTHER"
            }
            val ok = if (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) "✓" else "?"
            name + ok
        }.sorted()
        return if (parts.isEmpty()) "NONE" else parts.joinToString(" ")
    }

    /** Reference-counted: the ride service and the network test can both hold it. */
    @Synchronized
    fun start(ctx: Context) {
        users++
        if (callback != null) return
        val app = ctx.applicationContext
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = changed(app)
            override fun onLost(network: Network) = changed(app)
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = changed(app)
        }
        // Default requests exclude VPNs; removing NOT_VPN lets NordVPN's tunnel show up.
        val request = NetworkRequest.Builder()
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()
        runCatching {
            app.getSystemService(ConnectivityManager::class.java).registerNetworkCallback(request, cb)
            callback = cb
            last = snapshot(app)
            RideLog.write(app, "NETCHG", "watching — networks now: $last")
        }.onFailure { RideLog.write(app, "NETCHG", "could not watch networks: ${it.message}") }
    }

    @Synchronized
    fun stop(ctx: Context) {
        if (users > 0) users--
        if (users > 0) return
        val cb = callback ?: return
        runCatching {
            ctx.applicationContext.getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(cb)
        }
        callback = null
    }

    private fun changed(ctx: Context) {
        val now = snapshot(ctx)
        if (now == last) return
        RideLog.write(ctx, "NETCHG", "$last → $now")
        last = now
    }
}
