package com.example.mototracker

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

// The app's colours, the same ones the Mini's monitor uses.
private val BG = Color(0xFF0F1115)
private val PANEL = Color(0xFF171A20)
private val PANEL2 = Color(0xFF1C2027)
private val LINE = Color(0xFF2A2F37)
private val INK = Color(0xFFE8EAED)
private val DIM = Color(0xFFA3ACB6)
private val FAINT = Color(0xFF6B7480)
private val GO = Color(0xFF3DDC84)
private val WARN = Color(0xFFFFB020)
private val BAD = Color(0xFFFF4D4F)
private val ALARM = Color(0xFFA1121F)

/** The collapsed drawer: tall enough to hit with a gloved thumb. */
private val DRAWER_BAR = 52.dp

/**
 * Moto Tracker.
 *
 * One app, and the relay decides which face it shows: home when nobody has a trip
 * open, the rider's own trip, the other rider's Observer screen, or Hybrid when both
 * are out at once (Jack, 2026-09-24) — your own speed on top, theirs on a map below.
 * Observer and Hybrid keep this phone's trip controls in a drawer at the bottom, so
 * the map gets the room. The other rider's alarm reaches this phone whichever it is.
 */
class MainActivity : ComponentActivity() {

    private var resumeCount by mutableIntStateOf(0)

    /**
     * The diagnostics panel holds two buttons that raise real incidents. Left open,
     * a screen left on in a pocket pressed one five times in ten seconds
     * (2026-09-18). So it never outlives the screen: it closes whenever the app
     * leaves the foreground, and whenever a trip starts or changes state.
     */
    private var showDiagnostics by mutableStateOf(false)

    override fun onResume() {
        super.onResume()
        resumeCount++
    }

    override fun onStop() {
        super.onStop()
        showDiagnostics = false
    }

    private val permissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        RideLog.write(this, "PERM", granted.entries.joinToString {
            "${it.key.substringAfterLast('.')}=${it.value}"
        })
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        askForWhatWeNeed()
        setContent { MaterialTheme(colorScheme = darkColorScheme(background = BG, surface = PANEL)) { App() } }
    }

    private fun askForWhatWeNeed() {
        val wanted = listOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.POST_NOTIFICATIONS,
        ).filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (wanted.isNotEmpty()) permissions.launch(wanted.toTypedArray())
    }

    private fun openMaps(lat: Double, lon: Double) {
        val url = mapsUrl(lat, lon)
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
    }

    private fun copyMaps(lat: Double, lon: Double) {
        val clip = getSystemService(ClipboardManager::class.java)
        clip.setPrimaryClip(ClipData.newPlainText("position", mapsUrl(lat, lon)))
    }

    private fun call(rider: String) {
        // A number is not something this app knows; hand the name to the dialer and
        // let the phone's own contacts do the rest.
        runCatching {
            startActivity(Intent(Intent.ACTION_DIAL).apply { data = Uri.parse("tel:") })
        }
    }

    // ---------------------------------------------------------------- the app

    @Composable
    private fun App() {
        val ctx = this
        val scope = rememberCoroutineScope()
        var paired by remember { mutableStateOf(DeviceKey.current != null) }
        var snap by remember { mutableStateOf(Relay.snapshot) }
        var tripState by remember { mutableStateOf(Trip.state(ctx)) }
        var reach by remember { mutableStateOf<Long?>(null) }
        var busy by remember { mutableStateOf(false) }
        var status by remember { mutableStateOf("") }
        var weather by remember { mutableStateOf(Weather.current()) }
        var tick by remember { mutableLongStateOf(System.currentTimeMillis()) }

        LaunchedEffect(resumeCount) {
            paired = DeviceKey.current != null
            tripState = Trip.state(ctx)
        }
        LaunchedEffect(tripState) {
            if (tripState != Trip.State.IDLE) showDiagnostics = false
        }
        // The screen polls only while it IS the screen (Jack, 2026-09-16): nothing
        // sits in the background waiting for a ride to begin. Coming back to the
        // foreground asks the relay straight away — is anyone riding? — and the answer
        // decides whether this is the home screen or Observer mode.
        LaunchedEffect(Unit) {
            lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
                while (true) {
                    if (DeviceKey.current != null) {
                        snap = withContext(Dispatchers.IO) { Relay.refresh(ctx) } ?: snap
                        // A log that could not be sent when the trip ended goes now.
                        if (snap != null && Uploader.logWaiting(ctx)) {
                            withContext(Dispatchers.IO) { runCatching { Uploader.uploadRideLog(ctx) } }
                        }
                        tripState = Trip.state(ctx)
                        val fresh = snap
                        val theirTrip = fresh?.other(ctx)?.takeIf { it.tripId != null }
                        if (tripState == Trip.State.IDLE && theirTrip != null) {
                            // Observer mode keeps listening with the app in the
                            // background, so it is handed to a service that outlives
                            // this screen. It retires itself when their trip ends.
                            ObserverService.watching = theirTrip.name
                            if (!ObserverService.running) ObserverService.start(ctx)
                        } else if (tripState == Trip.State.IDLE && fresh != null) {
                            withContext(Dispatchers.IO) {
                                runCatching { IncidentWatch.decide(ctx, fresh) }
                            }
                        }
                    }
                    delay(1500)
                }
            }
        }
        LaunchedEffect(Unit) {
            while (true) {
                reach = withContext(Dispatchers.IO) { Uploader.probe().ms }
                delay(5000)
            }
        }
        LaunchedEffect(Unit) {
            while (true) {
                tick = System.currentTimeMillis()
                delay(1000)
            }
        }
        // Weather follows whoever is being watched.
        val watching = snap?.other(ctx)
        LaunchedEffect(watching?.lat, watching?.lon) {
            val lat = watching?.lat
            val lon = watching?.lon
            if (lat != null && lon != null) {
                weather = withContext(Dispatchers.IO) { Weather.fetch(ctx, lat, lon) }
            }
        }

        fun act(what: () -> Boolean, said: String) {
            busy = true
            scope.launch {
                val ok = withContext(Dispatchers.IO) { what() }
                status = if (ok) said else "the relay did not answer — it will catch up"
                // Decided here, after the action: ending a trip sends its log, and a
                // log stranded by a dead spot is worth saying out loud.
                if (Uploader.logWaiting(ctx)) status += " · log waiting for signal"
                tripState = Trip.state(ctx)
                snap = withContext(Dispatchers.IO) { Relay.refresh(ctx) } ?: snap
                busy = false
            }
        }

        Surface(Modifier.fillMaxSize(), color = BG) {
            Column(Modifier.fillMaxSize().padding(horizontal = 18.dp)) {
                Header(reach, snap)
                val me = snap?.me(ctx)
                val other = snap?.other(ctx)
                val mine = me?.incident
                val theirs = other?.incident

                // The screens get the space between the header and the footer, and
                // place their own buttons at the bottom of it.
                Column(Modifier.weight(1f).fillMaxWidth()) {
                when {
                    !paired -> PairScreen { paired = DeviceKey.current != null }

                    // The other rider needs help. This is the only screen that shouts.
                    theirs != null && theirs.state == "sos" ->
                        AlarmScreen(other!!, theirs, tick, busy, ::act)

                    mine != null && mine.state == "sos" && !mine.riderClosed ->
                        MyIncidentScreen(mine, other, busy, ::act)

                    // Both out at once: Hybrid. It follows the relay, so either rider
                    // opening a trip flips both phones into it (Jack, 2026-09-24).
                    tripState != Trip.State.IDLE && other?.tripId != null ->
                        WithDrawer(tripState, busy, ::act, status) {
                            HybridScreen(me, other, theirs, weather, tripState, tick)
                        }

                    tripState == Trip.State.RIDING -> RidingScreen(me, tick, busy, ::act, status)
                    tripState == Trip.State.OFF_BIKE -> OffBikeScreen(tick, busy, ::act, status)

                    // Not riding, and the other one is: this phone is the Observer.
                    other?.tripId != null ->
                        WithDrawer(tripState, busy, ::act, status) {
                            ObserverScreen(other, theirs, weather, tick)
                        }

                    else -> HomeScreen(snap, busy, ::act, status)
                }
                }

                Footer(showDiagnostics) { showDiagnostics = !showDiagnostics }
                if (showDiagnostics) Diagnostics(paired) { paired = DeviceKey.current != null }
            }
        }
    }

    // ---------------------------------------------------------------- pieces

    @Composable
    private fun Header(reach: Long?, snap: Relay.Snapshot?) {
        val ctx = this        // inside Row {} `this` is the RowScope, not the activity
        Row(
            Modifier.fillMaxWidth().padding(top = 14.dp, bottom = 10.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("MOTO TRACKER", color = DIM, fontSize = 13.sp, letterSpacing = 2.sp)
            Spacer(Modifier.weight(1f))
            Box(
                Modifier.size(8.dp).clip(CircleShape)
                    .background(if (reach != null) GO else BAD)
            )
            Spacer(Modifier.width(7.dp))
            Text(
                if (reach != null) "relay ok" else "no relay",
                color = if (reach != null) DIM else BAD, fontSize = 12.sp
            )
            snap?.me(ctx)?.battery?.let {
                Text(" · $it%", color = DIM, fontSize = 12.sp)
            }
        }
    }

    @Composable
    private fun Pill(text: String, colour: Color) {
        Row(
            Modifier.clip(RoundedCornerShape(999.dp))
                .background(colour.copy(alpha = 0.15f))
                .padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(colour))
            Spacer(Modifier.width(8.dp))
            Text(text, color = colour, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
                letterSpacing = 1.sp)
        }
    }

    @Composable
    private fun Card(content: @Composable ColumnScope.() -> Unit) {
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(PANEL)
                .padding(16.dp),
            content = content
        )
    }

    @Composable
    private fun KeyValue(key: String, value: String, valueColour: Color = INK) {
        Row(Modifier.fillMaxWidth().padding(vertical = 5.dp)) {
            Text(key, color = DIM, fontSize = 14.sp)
            Spacer(Modifier.weight(1f))
            Text(value, color = valueColour, fontSize = 14.sp,
                fontFamily = FontFamily.Monospace, fontWeight = FontWeight.SemiBold)
        }
    }

    @Composable
    private fun BigButton(
        label: String, colour: Color, enabled: Boolean = true,
        text: Color = Color(0xFF07130C), onClick: () -> Unit,
    ) {
        Button(
            onClick, Modifier.fillMaxWidth().height(62.dp),
            enabled = enabled,
            shape = RoundedCornerShape(12.dp),
            colors = ButtonDefaults.buttonColors(containerColor = colour, contentColor = text)
        ) { Text(label, fontSize = 18.sp, fontWeight = FontWeight.SemiBold) }
    }

    @Composable
    private fun QuietButton(label: String, enabled: Boolean = true, onClick: () -> Unit) {
        OutlinedButton(
            onClick, Modifier.fillMaxWidth().height(56.dp),
            enabled = enabled,
            shape = RoundedCornerShape(12.dp),
            colors = ButtonDefaults.outlinedButtonColors(contentColor = INK)
        ) { Text(label, fontSize = 16.sp) }
    }

    /** Coordinates that open in Maps, with a copy button beside them. */
    @Composable
    private fun Coordinates(lat: Double, lon: Double, onLight: Boolean = false) {
        var copied by remember { mutableStateOf(false) }
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton({ openMaps(lat, lon) }, contentPadding = PaddingValues(0.dp)) {
                Text(
                    String.format(java.util.Locale.US, "%.5f, %.5f", lat, lon),
                    color = if (onLight) Color.White else Color(0xFF4DA3FF),
                    fontSize = 15.sp, fontFamily = FontFamily.Monospace
                )
            }
            TextButton({ copyMaps(lat, lon); copied = true }, contentPadding = PaddingValues(6.dp, 0.dp)) {
                Text(if (copied) "✓" else "⧉",
                    color = if (onLight) Color.White else DIM, fontSize = 16.sp)
            }
        }
    }

    // ---------------------------------------------------------------- rider screens

    @Composable
    private fun ColumnScope.HomeScreen(
        snap: Relay.Snapshot?, busy: Boolean,
        act: (() -> Boolean, String) -> Unit, status: String,
    ) {
        val ctx = this@MainActivity
        val previous = remember(snap) { LastTrip.remember(ctx, snap?.me(ctx)?.previousTrip) }
        Column(Modifier.fillMaxWidth()) {
            Spacer(Modifier.height(10.dp))
            Text(snap?.me(ctx)?.name ?: Identity.get(ctx).replaceFirstChar { it.uppercase() },
                color = INK, fontSize = 30.sp, fontWeight = FontWeight.Bold)
            Text("not riding", color = FAINT, fontSize = 15.sp)
            Spacer(Modifier.height(20.dp))
            if (previous != null) {
                Card {
                    KeyValue("Last trip", previous.day)
                    KeyValue("Started", previous.started)
                    KeyValue("Ended", previous.ended)
                    KeyValue("Duration", previous.duration)
                    KeyValue("Max speed", previous.maxSpeed)
                    KeyValue("Average speed", previous.avgSpeed)
                    KeyValue("Peak G", previous.peakG)
                    KeyValue("Rotational force", previous.peakRot)
                }
            } else {
                Text("No trip recorded yet.", color = FAINT, fontSize = 14.sp)
            }
        }
        // Start trip sits at the bottom, under the thumb (Jack, 2026-09-16).
        Spacer(Modifier.weight(1f))
        if (status.isNotEmpty()) {
            Text(status, color = DIM, fontSize = 13.sp, modifier = Modifier.padding(bottom = 8.dp))
        }
        if (!Trip.autoResume(ctx)) {
            Text("Auto-resume is off — the ride will not start by itself, and nothing is "
                + "watched for until you press Start ride.",
                color = WARN, fontSize = 12.sp, modifier = Modifier.padding(bottom = 8.dp))
        }
        BigButton("Start trip", GO, enabled = !busy) {
            act({ Trip.startTrip(ctx) }, "trip open — press Start ride when you set off")
        }
        Spacer(Modifier.height(8.dp))
    }

    @Composable
    private fun ColumnScope.RidingScreen(
        me: Relay.Rider?, tick: Long, busy: Boolean,
        act: (() -> Boolean, String) -> Unit, status: String,
    ) {
        val ctx = this@MainActivity
        Column(Modifier.fillMaxWidth()) {
            Pill("RIDING", GO)
            Spacer(Modifier.height(18.dp))
            Text("SPEED", color = DIM, fontSize = 13.sp, letterSpacing = 2.sp)
            Text(
                ((me?.speed ?: 0.0).takeIf { !it.isNaN() } ?: 0.0).toInt().toString(),
                color = INK, fontSize = 76.sp, fontWeight = FontWeight.Bold
            )
            Text("KM/H", color = DIM, fontSize = 14.sp, letterSpacing = 2.sp)
            Spacer(Modifier.height(20.dp))
            Card {
                KeyValue("Ride time", elapsed(Trip.startedAt(ctx), tick))
                KeyValue("Last packet",
                    me?.lastSeenS?.takeIf { !it.isNaN() }?.let { "${it.toInt()} s ago" } ?: "—",
                    if ((me?.lastSeenS ?: 0.0) > 20) WARN else INK)
                me?.battery?.let { KeyValue("Battery", "$it%") }
            }
        }
        // The road runs at the speed actually being ridden.
        StateScene(riding = true, speedKmh = me?.speed?.takeIf { !it.isNaN() } ?: 0.0,
            modifier = Modifier.weight(1f).fillMaxWidth().padding(vertical = 10.dp))
        if (status.isNotEmpty()) {
            Text(status, color = DIM, fontSize = 13.sp, modifier = Modifier.padding(bottom = 8.dp))
        }
        BigButton("Pause ride", WARN, enabled = !busy, text = Color(0xFF1A1206)) {
            act({ Trip.pauseRide(ctx) }, "off the bike — still sharing location")
        }
        Spacer(Modifier.height(10.dp))
        QuietButton("End trip", enabled = !busy) {
            act({ Trip.endTrip(ctx) }, "trip ended")
        }
        Spacer(Modifier.height(8.dp))
    }

    @Composable
    private fun ColumnScope.OffBikeScreen(
        tick: Long, busy: Boolean, act: (() -> Boolean, String) -> Unit, status: String,
    ) {
        val ctx = this@MainActivity
        // The same screen serves the start of a trip and a stop in the middle of
        // one; only the words change (Jack, 2026-09-20).
        val ridden = Trip.hasRidden(ctx)
        Column(Modifier.fillMaxWidth()) {
            Pill(if (ridden) "OFF THE BIKE" else "NOT RIDING YET", Color(0xFF4DA3FF))
            Spacer(Modifier.height(18.dp))
            Text("TRIP CONTINUES · SINCE", color = DIM, fontSize = 12.sp, letterSpacing = 1.5.sp)
            Text(clockTime(Trip.startedAt(ctx)), color = INK, fontSize = 40.sp,
                fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace)
            Spacer(Modifier.height(18.dp))
            Card {
                KeyValue("Location", "sharing", GO)
                KeyValue("Sensors", "off", DIM)
                KeyValue("Crash detection", "off", DIM)
                KeyValue(if (ridden) "Off the bike for" else "Trip open for",
                    elapsed(Trip.offBikeSince(ctx), tick))
                if (!Trip.autoResume(ctx)) KeyValue("Auto-resume", "off", WARN)
            }
            Spacer(Modifier.height(10.dp))
            Text(
                if (!Trip.autoResume(ctx))
                    "Auto-resume is off: this stays off the bike however fast it moves, and "
                        + "nothing is watched for. Press Start ride to change that."
                else "Ride away and it ${if (ridden) "resumes" else "starts"} by itself — " +
                    "${Trip.RESUME_KMH.toInt()} km/h for ${Trip.RESUME_S} seconds is riding, " +
                    "whatever the app was told.",
                color = if (Trip.autoResume(ctx)) FAINT else WARN, fontSize = 12.sp
            )
        }
        StateScene(riding = false, speedKmh = 0.0,
            modifier = Modifier.weight(1f).fillMaxWidth().padding(vertical = 10.dp))
        if (status.isNotEmpty()) {
            Text(status, color = DIM, fontSize = 13.sp, modifier = Modifier.padding(bottom = 8.dp))
        }
        BigButton(if (ridden) "Resume ride" else "Start ride", GO, enabled = !busy) {
            act({ Trip.resumeRide(ctx) }, "riding")
        }
        Spacer(Modifier.height(10.dp))
        QuietButton("End trip", enabled = !busy) {
            act({ Trip.endTrip(ctx) }, "trip ended")
        }
        Spacer(Modifier.height(8.dp))
    }

    /** The rider's own open incident: what was seen, and the three ways out of it. */
    @Composable
    private fun ColumnScope.MyIncidentScreen(
        inc: Relay.Incident, other: Relay.Rider?, busy: Boolean,
        act: (() -> Boolean, String) -> Unit,
    ) {
        val ctx = this@MainActivity
        val watcher = other?.name ?: "Your Observer"
        Column(Modifier.fillMaxWidth()) {
            Spacer(Modifier.height(6.dp))
            Pill("INCIDENT OPEN", BAD)
            Spacer(Modifier.height(16.dp))
            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp))
                    .background(Color(0xFF2A1015)).padding(16.dp)
            ) {
                KeyValue("Raised", timeOf(inc.raisedAt))
                if (!inc.g.isNaN() && inc.g > 0)
                    KeyValue("Peak", String.format(java.util.Locale.US, "%.1f g", inc.g))
                if (!inc.rot.isNaN() && inc.rot > 0)
                    KeyValue("Rotation", String.format(java.util.Locale.US, "%.1f rad/s", inc.rot))
                if (!inc.speedBefore.isNaN() && inc.speedBefore > 0)
                    KeyValue("Speed", String.format(java.util.Locale.US, "%.0f → %.0f km/h",
                        inc.speedBefore, if (inc.speedAfter.isNaN()) 0.0 else inc.speedAfter))
                Spacer(Modifier.height(8.dp))
                Text(
                    if (inc.silenced) "$watcher was alerted and has silenced it."
                    else "$watcher was alerted at ${timeOf(inc.raisedAt)}.",
                    color = Color(0xFFE0A9AE), fontSize = 13.sp
                )
            }
            if (inc.lat != null && inc.lon != null) {
                Spacer(Modifier.height(8.dp))
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("Position", color = DIM, fontSize = 14.sp)
                    Spacer(Modifier.weight(1f))
                    Coordinates(inc.lat, inc.lon)
                }
            }
        }
        Spacer(Modifier.weight(1f))
        BigButton("I'm OK", GO, enabled = !busy) {
            act({ Relay.resolve(ctx, inc.id, "ok") }, "told them you are OK")
        }
        Spacer(Modifier.height(10.dp))
        QuietButton("False alarm", enabled = !busy) {
            act({ Relay.resolve(ctx, inc.id, "false_alarm") }, "marked a false alarm")
        }
        Spacer(Modifier.height(10.dp))
        // No window, no waiting, no automatic retraction: this one wins outright.
        Button(
            { act({ Relay.escalate(ctx, inc.id) }, "help is being called for") },
            Modifier.fillMaxWidth().height(62.dp), enabled = !busy,
            shape = RoundedCornerShape(12.dp),
            colors = ButtonDefaults.buttonColors(containerColor = BAD, contentColor = Color.White)
        ) { Text("I need help", fontSize = 18.sp, fontWeight = FontWeight.Bold) }
        Spacer(Modifier.height(8.dp))
    }

    // ---------------------------------------------------------------- observer screens

    @Composable
    private fun ObserverScreen(
        other: Relay.Rider, theirs: Relay.Incident?, weather: Weather.Now?, tick: Long,
    ) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(other.name, color = INK, fontSize = 26.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.width(12.dp))
                StatePill(other.state)
            }
            Spacer(Modifier.height(12.dp))
            PendingBanner(theirs)
            Spacer(Modifier.height(2.dp))
            WatchedMap(other, theirs, weather, 280.dp)
            Spacer(Modifier.height(16.dp))
            Row(verticalAlignment = Alignment.Bottom) {
                Text(
                    ((other.speed.takeIf { !it.isNaN() }) ?: 0.0).toInt().toString(),
                    color = INK, fontSize = 58.sp, fontWeight = FontWeight.Bold
                )
                Text(" KM/H", color = DIM, fontSize = 14.sp,
                    modifier = Modifier.padding(bottom = 12.dp))
                Spacer(Modifier.weight(1f))
            }
            Spacer(Modifier.height(16.dp))
            Card {
                KeyValue("Last packet",
                    other.lastSeenS.takeIf { !it.isNaN() }?.let { "${it.toInt()} s ago" } ?: "—",
                    if (other.lastSeenS > 20) WARN else INK)
                other.battery?.let { KeyValue("Battery", "$it%") }
                weather?.let { KeyValue("Weather", it.label + (it.tempC?.let { t -> "  ${t.toInt()}°C" } ?: "")) }
                if (other.lat != null && other.lon != null) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text("Position", color = DIM, fontSize = 14.sp)
                        Spacer(Modifier.weight(1f))
                        Coordinates(other.lat, other.lon)
                    }
                }
            }
            Spacer(Modifier.height(14.dp))
        }
    }

    /** The other rider on a map, with the weather where they are sitting on it. */
    @Composable
    private fun WatchedMap(
        other: Relay.Rider, theirs: Relay.Incident?, weather: Weather.Now?,
        height: androidx.compose.ui.unit.Dp,
    ) {
        Box {
            RiderMap(
                lat = other.lat, lon = other.lon,
                speedKmh = other.speed.takeIf { !it.isNaN() } ?: 0.0,
                stoppedLabel = if (other.state == "offbike") "off the bike" else "stopped",
                alarm = theirs != null && theirs.state == "sos",
                height = height,
                signalLost = !other.lastSeenS.isNaN() && other.lastSeenS >= Config.SIGNAL_LOST_S,
            )
            // The weather sits on the map, as it does on the Mini: one glyph for
            // what it is doing outside, the wind underneath (Jack, 2026-09-16).
            weather?.let {
                Column(
                    Modifier.align(Alignment.BottomEnd).padding(10.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .background(Color(0xE6171A20)).padding(horizontal = 12.dp, vertical = 8.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Text(it.icon, fontSize = 26.sp)
                    Text(it.wind, color = DIM, fontSize = 11.sp,
                        fontFamily = FontFamily.Monospace)
                }
            }
        }
    }

    /** The other rider's candidate: nothing raised yet, but worth knowing about. */
    @Composable
    private fun PendingBanner(theirs: Relay.Incident?) {
        if (theirs == null || theirs.state != "pending") return
        Column(
            Modifier.fillMaxWidth().padding(bottom = 10.dp).clip(RoundedCornerShape(12.dp))
                .background(WARN.copy(alpha = 0.12f)).padding(14.dp)
        ) {
            Text(
                if (theirs.kind == "lost") "No signal — watching"
                else "Possible crash — checking",
                color = WARN, fontSize = 16.sp, fontWeight = FontWeight.SemiBold
            )
            Text("Nothing has been raised. It clears itself if the ride carries on.",
                color = DIM, fontSize = 13.sp)
        }
    }

    @Composable
    private fun StatePill(state: String) = when (state) {
        "riding" -> Pill("RIDING", GO)
        "offbike" -> Pill("OFF THE BIKE", Color(0xFF4DA3FF))
        else -> Pill("ASLEEP", FAINT)
    }

    /**
     * Hybrid: both riders out at once (Jack, 2026-09-24). This rider's own speed on
     * top, where the riding screen has it; the other rider's map and speed below, in
     * the space the riding cartoon would otherwise take. The trip controls are in
     * the drawer, not here.
     */
    @Composable
    private fun ColumnScope.HybridScreen(
        me: Relay.Rider?, other: Relay.Rider, theirs: Relay.Incident?, weather: Weather.Now?,
        tripState: Trip.State, tick: Long,
    ) {
        val ctx = this@MainActivity
        // Mine.
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            if (tripState == Trip.State.RIDING) Pill("RIDING", GO)
            else Pill("OFF THE BIKE", Color(0xFF4DA3FF))
            Spacer(Modifier.weight(1f))
            Text(elapsed(Trip.startedAt(ctx), tick), color = DIM, fontSize = 14.sp,
                fontFamily = FontFamily.Monospace)
        }
        Row(verticalAlignment = Alignment.Bottom) {
            Text(
                ((me?.speed ?: 0.0).takeIf { !it.isNaN() } ?: 0.0).toInt().toString(),
                color = INK, fontSize = 64.sp, fontWeight = FontWeight.Bold
            )
            Text(" KM/H", color = DIM, fontSize = 14.sp, modifier = Modifier.padding(bottom = 14.dp))
            Spacer(Modifier.weight(1f))
            Column(horizontalAlignment = Alignment.End, modifier = Modifier.padding(bottom = 12.dp)) {
                Text(me?.lastSeenS?.takeIf { !it.isNaN() }?.let { "sent ${it.toInt()} s ago" } ?: "not sent yet",
                    color = if ((me?.lastSeenS ?: 0.0) > 20) WARN else FAINT, fontSize = 12.sp)
                me?.battery?.let { Text("battery $it%", color = FAINT, fontSize = 12.sp) }
            }
        }
        Spacer(Modifier.height(4.dp))
        Box(Modifier.fillMaxWidth().height(1.dp).background(LINE))
        Spacer(Modifier.height(10.dp))

        // Theirs.
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(other.name, color = INK, fontSize = 20.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.width(10.dp))
            StatePill(other.state)
            Spacer(Modifier.weight(1f))
            Text(((other.speed.takeIf { !it.isNaN() }) ?: 0.0).toInt().toString(),
                color = INK, fontSize = 30.sp, fontWeight = FontWeight.Bold)
            Text(" KM/H", color = DIM, fontSize = 12.sp)
        }
        Text(
            other.lastSeenS.takeIf { !it.isNaN() }?.let { "last packet ${it.toInt()} s ago" } ?: "no packet yet",
            color = if (other.lastSeenS > 20) WARN else FAINT, fontSize = 12.sp
        )
        Spacer(Modifier.height(8.dp))
        PendingBanner(theirs)
        // The map takes whatever is left. Its height is fixed when it starts, so it
        // is measured once here, and the drawer slides over it rather than moving it.
        BoxWithConstraints(Modifier.weight(1f).fillMaxWidth().padding(bottom = 8.dp)) {
            val h = maxHeight
            if (h > 80.dp) key(h) { WatchedMap(other, theirs, weather, h) }
        }
    }

    /**
     * Observer and Hybrid put this phone's trip controls in a drawer (Jack,
     * 2026-09-24): a slim bar at the bottom with a chevron, which slides the buttons
     * up over the screen when tapped. The screen above keeps its size either way.
     */
    @Composable
    private fun ColumnScope.WithDrawer(
        tripState: Trip.State, busy: Boolean, act: (() -> Boolean, String) -> Unit, status: String,
        startOpen: Boolean = false,
        content: @Composable ColumnScope.() -> Unit,
    ) {
        Box(Modifier.weight(1f).fillMaxWidth()) {
            Column(Modifier.fillMaxSize().padding(bottom = DRAWER_BAR)) { content() }
            TripDrawer(tripState, busy, act, status, startOpen, Modifier.align(Alignment.BottomCenter))
        }
    }

    @Composable
    private fun TripDrawer(
        tripState: Trip.State, busy: Boolean, act: (() -> Boolean, String) -> Unit, status: String,
        startOpen: Boolean, modifier: Modifier,
    ) {
        val ctx = this
        // Closes itself once the trip changes state, so the buttons never outlive
        // the state they were drawn for.
        var open by remember(tripState) { mutableStateOf(startOpen) }
        val then: (() -> Boolean, String) -> Unit = { what, said -> open = false; act(what, said) }
        Column(
            modifier.fillMaxWidth().clip(RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp))
                .background(PANEL2)
        ) {
            Row(
                Modifier.fillMaxWidth().height(DRAWER_BAR).clickable { open = !open }
                    .padding(horizontal = 16.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    when (tripState) {
                        Trip.State.IDLE -> "Your trip · not started"
                        Trip.State.OFF_BIKE -> "You · off the bike"
                        Trip.State.RIDING -> "You · riding"
                    },
                    color = INK, fontSize = 14.sp
                )
                if (status.isNotEmpty()) {
                    Text("  ·  $status", color = FAINT, fontSize = 12.sp, maxLines = 1,
                        modifier = Modifier.weight(1f, fill = false))
                }
                Spacer(Modifier.weight(1f))
                // One glyph, turned over when open, so it sits in the same place both ways.
                Text("⌃", color = INK, fontSize = 24.sp,
                    modifier = Modifier.rotate(if (open) 180f else 0f))
            }
            AnimatedVisibility(open, enter = expandVertically(), exit = shrinkVertically()) {
                Column(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, bottom = 12.dp)) {
                    when (tripState) {
                        Trip.State.IDLE -> {
                            if (!Trip.autoResume(ctx)) {
                                Text("Auto-resume is off — the ride will not start by itself.",
                                    color = WARN, fontSize = 12.sp, modifier = Modifier.padding(bottom = 8.dp))
                            }
                            BigButton("Start trip", GO, enabled = !busy) {
                                then({ Trip.startTrip(ctx) }, "trip open — press Start ride when you set off")
                            }
                        }
                        Trip.State.OFF_BIKE -> {
                            BigButton(if (Trip.hasRidden(ctx)) "Resume ride" else "Start ride", GO,
                                enabled = !busy) { then({ Trip.resumeRide(ctx) }, "riding") }
                            Spacer(Modifier.height(10.dp))
                            QuietButton("End trip", enabled = !busy) { then({ Trip.endTrip(ctx) }, "trip ended") }
                        }
                        Trip.State.RIDING -> {
                            BigButton("Pause ride", WARN, enabled = !busy, text = Color(0xFF1A1206)) {
                                then({ Trip.pauseRide(ctx) }, "off the bike — still sharing location")
                            }
                            Spacer(Modifier.height(10.dp))
                            QuietButton("End trip", enabled = !busy) { then({ Trip.endTrip(ctx) }, "trip ended") }
                        }
                    }
                }
            }
        }
    }

    /**
     * The screenshot test's way in (androidTest ScreensTest): Rider, Observer or Hybrid
     * on made-up relay data (solo = this phone riding with the other at home), so the layout can be looked at without a relay or a ride.
     * The buttons do nothing here.
     */
    @androidx.annotation.VisibleForTesting
    internal fun showForTest(me: Relay.Rider?, other: Relay.Rider, tripState: Trip.State, drawerOpen: Boolean,
                             solo: Boolean = false) {
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(background = BG, surface = PANEL)) {
                Surface(Modifier.fillMaxSize(), color = BG) {
                    Column(Modifier.fillMaxSize().padding(horizontal = 18.dp)) {
                        Header(120L, null)
                        Column(Modifier.weight(1f).fillMaxWidth()) {
                            if (solo) RidingScreen(me, System.currentTimeMillis(), false, { _, _ -> }, "")
                            else WithDrawer(tripState, false, { _, _ -> }, "", drawerOpen) {
                                if (tripState == Trip.State.IDLE)
                                    ObserverScreen(other, other.incident, null, System.currentTimeMillis())
                                else HybridScreen(me, other, other.incident, null, tripState,
                                    System.currentTimeMillis())
                            }
                        }
                        Footer(false) {}
                    }
                }
            }
        }
    }

    /** The screen nobody wants: the other rider is in trouble. */
    @Composable
    private fun AlarmScreen(
        other: Relay.Rider, inc: Relay.Incident, tick: Long, busy: Boolean,
        act: (() -> Boolean, String) -> Unit,
    ) {
        val ctx = this
        val yellow = inc.riderClosed
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp))
                .background(if (yellow) Color(0xFF4A3A10) else ALARM)
                .padding(18.dp)
        ) {
            Text(
                if (inc.silenced) "ALARM · SILENCED" else "ALARM · REPEATING",
                color = Color.White.copy(alpha = 0.75f), fontSize = 12.sp, letterSpacing = 2.sp
            )
            Spacer(Modifier.height(10.dp))
            Text(
                if (yellow) "${other.name} says they are OK" else "Rider in trouble",
                color = Color.White, fontSize = 30.sp, fontWeight = FontWeight.Bold
            )
            Spacer(Modifier.height(8.dp))
            val detail = when {
                inc.kind == "help" -> "${other.name} pressed I need help"
                inc.kind == "lost" -> "${other.name} · no signal since " +
                    java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US)
                        .format(java.util.Date(Time.parse(inc.raisedAt) ?: 0))
                else -> buildString {
                    append(other.name)
                    if (!inc.g.isNaN() && inc.g > 0)
                        append(String.format(java.util.Locale.US, " · impact %.1f g", inc.g))
                    if (!inc.speedBefore.isNaN())
                        append(String.format(java.util.Locale.US, "\n%.0f → %.0f km/h",
                            inc.speedBefore, if (inc.speedAfter.isNaN()) 0.0 else inc.speedAfter))
                }
            }
            Text(detail, color = Color.White, fontSize = 15.sp, textAlign = TextAlign.Start)
            if (inc.lat != null && inc.lon != null) {
                Spacer(Modifier.height(6.dp))
                Text("Last known position", color = Color.White.copy(alpha = 0.75f), fontSize = 12.sp)
                Coordinates(inc.lat, inc.lon, onLight = true)
            }
            Spacer(Modifier.height(18.dp))
            Button(
                { call(other.name) }, Modifier.fillMaxWidth().height(58.dp),
                shape = RoundedCornerShape(10.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Color.White,
                    contentColor = ALARM)
            ) { Text("Call ${other.name}", fontSize = 18.sp, fontWeight = FontWeight.Bold) }
            Spacer(Modifier.height(10.dp))
            if (!inc.silenced) {
                OutlinedButton(
                    { act({ Relay.silence(ctx, inc.id) }, "alarm silenced everywhere") },
                    Modifier.fillMaxWidth().height(54.dp), enabled = !busy,
                    shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Color.White)
                ) { Text("Silence alarm", fontSize = 16.sp) }
            } else if (!inc.observerClosed) {
                OutlinedButton(
                    { act({ Relay.observerClose(ctx, inc.id) }, "you closed your half") },
                    Modifier.fillMaxWidth().height(54.dp), enabled = !busy,
                    shape = RoundedCornerShape(10.dp),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Color.White)
                ) { Text("Close incident", fontSize = 16.sp) }
            }
            Spacer(Modifier.height(12.dp))
            Text(
                if (inc.observerClosed)
                    "You have closed your half. It stays open until ${other.name} confirms."
                else "Silencing stops the sound only. This stays open until ${other.name} " +
                    "clears it too.",
                color = Color.White.copy(alpha = 0.8f), fontSize = 12.sp
            )
        }
    }

    // ---------------------------------------------------------------- pairing, footer

    @Composable
    private fun PairScreen(onPaired: () -> Unit) {
        val ctx = this
        val scope = rememberCoroutineScope()
        var code by remember { mutableStateOf("") }
        var result by remember { mutableStateOf("") }
        var busy by remember { mutableStateOf(false) }
        Column(Modifier.fillMaxWidth()) {
            Spacer(Modifier.height(12.dp))
            Text("Pair this phone", color = INK, fontSize = 26.sp, fontWeight = FontWeight.Bold)
            Text("Ask for an eight-character code on the Mac, then type it here.",
                color = DIM, fontSize = 14.sp)
            Spacer(Modifier.height(18.dp))
            OutlinedTextField(
                value = code,
                onValueChange = { v ->
                    code = v.uppercase().filter { it.isLetterOrDigit() || it == '-' }.take(11)
                },
                label = { Text("pairing code") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(
                    capitalization = KeyboardCapitalization.Characters,
                    keyboardType = KeyboardType.Ascii
                ),
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(14.dp))
            BigButton("Pair this phone", GO,
                enabled = !busy && code.count { it.isLetterOrDigit() } == 8) {
                busy = true
                result = "pairing…"
                scope.launch {
                    result = withContext(Dispatchers.IO) { Uploader.pair(ctx, code) }
                    busy = false
                    onPaired()
                }
            }
            if (result.isNotEmpty()) {
                Spacer(Modifier.height(10.dp))
                Text(result, color = DIM, fontSize = 13.sp)
            }
        }
    }

    @Composable
    private fun Footer(open: Boolean, toggle: () -> Unit) {
        Row(
            Modifier.fillMaxWidth().padding(vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("moto tracker", color = FAINT, fontSize = 11.sp)
            Spacer(Modifier.weight(1f))
            TextButton(toggle, contentPadding = PaddingValues(4.dp)) {
                Text(if (open) "hide diagnostics" else "diagnostics",
                    color = FAINT, fontSize = 11.sp)
            }
        }
    }

    /** Everything that is useful to a builder and to nobody else. */
    @Composable
    private fun Diagnostics(paired: Boolean, onChanged: () -> Unit) {
        val ctx = this
        val scope = rememberCoroutineScope()
        var line by remember { mutableStateOf("") }
        var confirmUnpair by remember { mutableStateOf(false) }
        var confirmRaise by remember { mutableStateOf<String?>(null) }
        Column(
            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(PANEL2)
                .padding(14.dp).verticalScroll(rememberScrollState())
        ) {
            Text("build ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
                color = DIM, fontSize = 12.sp)
            Text("rider ${Identity.get(ctx)} · ${DeviceKey.describe(ctx)}",
                color = DIM, fontSize = 12.sp)
            Text("battery exemption: ${if (Power.exempt(ctx)) "granted" else "DENIED"}",
                color = if (Power.exempt(ctx)) DIM else WARN, fontSize = 12.sp)
            Spacer(Modifier.height(8.dp))
            // For a trip that is not on the bike at all (Jack, 2026-09-20).
            var auto by remember { mutableStateOf(Trip.autoResume(ctx)) }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f).padding(end = 10.dp)) {
                    Text("Disable auto-resume", color = if (auto) INK else WARN, fontSize = 13.sp)
                    Text("For a trip on foot, on a bicycle or in a car: the ride never starts "
                        + "by itself, so no sensors and no crash detection.",
                        color = FAINT, fontSize = 11.sp)
                }
                Switch(checked = !auto, onCheckedChange = { off ->
                    auto = !off
                    Trip.setAutoResume(ctx, auto)
                })
            }
            Spacer(Modifier.height(8.dp))
            TextButton({
                scope.launch {
                    line = withContext(Dispatchers.IO) { Uploader.postLog(ctx) }
                }
            }) { Text("Upload log", color = INK, fontSize = 13.sp) }
            // The map only appears while watching the other rider, so this is the one
            // way to ask whether it would work without waiting for a ride (2026-09-20).
            TextButton({
                scope.launch {
                    line = "checking the map…"
                    line = withContext(Dispatchers.IO) {
                        val k = Relay.mapKey(ctx)
                        if (k == null) "no map key — the relay refused it or could not be reached"
                        else "map key ok · tile: " + Uploader.probeTile(k)
                    }
                }
            }) { Text("Check the map", color = INK, fontSize = 13.sp) }
            TextButton({ confirmRaise = "crash" }) {
                Text("Simulate crash (real incident)", color = WARN, fontSize = 13.sp)
            }
            TextButton({ confirmRaise = "help" }) { Text("I need help", color = BAD, fontSize = 13.sp) }
            TextButton({
                runCatching {
                    startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:$packageName")))
                }
            }) { Text("Battery exemption", color = INK, fontSize = 13.sp) }
            if (paired) {
                TextButton({ confirmUnpair = true }) {
                    Text("Unpair this phone", color = DIM, fontSize = 13.sp)
                }
            }
            if (line.isNotEmpty()) Text(line, color = DIM, fontSize = 12.sp)
        }
        if (confirmUnpair) {
            AlertDialog(
                onDismissRequest = { confirmUnpair = false },
                title = { Text("Unpair this phone?") },
                text = { Text("The key is deleted. Pairing again needs a new code.") },
                confirmButton = {
                    TextButton({
                        DeviceKey.clear(this)
                        confirmUnpair = false
                        onChanged()
                    }) { Text("Unpair") }
                },
                dismissButton = { TextButton({ confirmUnpair = false }) { Text("Cancel") } }
            )
        }
        // Both of these alarm every Observer, so neither goes on one tap. A pocket
        // pressing the same spot again lands on the dialog's scrim, which only
        // dismisses it.
        confirmRaise?.let { what ->
            val help = what == "help"
            AlertDialog(
                onDismissRequest = { confirmRaise = null },
                title = { Text(if (help) "Send “I need help”?" else "Simulate a crash?") },
                text = {
                    Text(if (help) "This opens a real incident straight away. Both Macs and the other phone alarm."
                         else "This raises a real incident. In ${Config.CONFIRM_WINDOW_S} seconds both Macs " +
                              "and the other phone alarm.")
                },
                confirmButton = {
                    TextButton({
                        confirmRaise = null
                        scope.launch {
                            line = if (help) {
                                val ok = withContext(Dispatchers.IO) { Relay.needHelp(ctx) }
                                if (ok) "I need help sent" else "the relay refused that"
                            } else {
                                withContext(Dispatchers.IO) { Relay.simulateCrash(ctx) }
                            }
                        }
                    }) { Text(if (help) "Send it" else "Simulate", color = BAD) }
                },
                dismissButton = { TextButton({ confirmRaise = null }) { Text("Cancel") } }
            )
        }
    }
}

// ---------------------------------------------------------------- small helpers

private fun mapsUrl(lat: Double, lon: Double) =
    String.format(java.util.Locale.US, "https://www.google.com/maps?q=%.6f,%.6f", lat, lon)

/** HH:mm:ss from a relay timestamp. */
private fun timeOf(iso: String?): String {
    val ms = Time.parse(iso) ?: return "—"
    return java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US).format(java.util.Date(ms))
}

private fun clockTime(ms: Long): String =
    if (ms <= 0) "--:--"
    else java.text.SimpleDateFormat("HH:mm", java.util.Locale.US).format(java.util.Date(ms))

/** h:mm:ss since a wall-clock moment, ticking on the real clock. */
private fun elapsed(sinceMs: Long, nowMs: Long): String {
    if (sinceMs <= 0) return "—"
    val s = ((nowMs - sinceMs) / 1000).coerceAtLeast(0)
    return String.format(java.util.Locale.US, "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
}
