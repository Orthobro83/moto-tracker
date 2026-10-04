//
//  The Mac's half of moto-tracker, inside the app itself (design.md 2026-10-04).
//
//  Until 2026-10-04 this ran as a separate background service on 127.0.0.1:8089 and
//  the window only showed its page. Twice in two days the window sat at "Waiting for
//  the monitor…" — once because the service had hung, once because the window opened
//  before the service at login and never asked again. Jack's ruling: the app talks to
//  the relay itself, the way the phones do, and nothing on the Mac has to be running
//  first. So everything that service did happens in here:
//
//    state    /state from the relay every second (also the ping and the Live light)
//    events   /events as a stream: the running log and packets-per-minute
//    sync     /sync -> archive -> /sync/ack every five seconds
//    weather  Open-Meteo near each rider every ten minutes
//
//  Each runs on its own thread. Everything they share lives on one serial queue and is
//  only touched through onQ, which is safe to nest: the hang of 2026-10-02 was a thread
//  waiting on a lock it already held, and that cannot happen here. Where two queues
//  meet, the order is always archive, then state, then alarm — never the other way.
//
import Foundation

let VERSION = "app-4.0"
let LOG_LINES = 600                 // the running log the page can scroll back through
// How many polls must fail before the relay counts as unreachable. Polls are a second
// apart, so this is a three-second grace — long enough to ride out the source-address
// rotation that breaks every connection in flight, short enough to notice a real outage.
let MISSES_BEFORE_DOWN = 3
// An alarm nobody is there to silence should not sound all afternoon. After this it
// goes quiet HERE only — the incident stays open, the icon keeps flashing, and the
// relay still shows it unsilenced, because nobody has actually acknowledged it.
let ALARM_MAX_S = 900.0
// Weather this old is no longer worth showing. On 2026-09-20 the pane still had a
// reading from eleven hours earlier: every fetch since had failed, and nothing said so
// (Open-Meteo throttles, and the Mini goes out through a shared address).
let WEATHER_STALE_S = 2.0 * 3600
let DRILL_CONFIRM_S = 6.0           // how long a drill shows as "checking" before it escalates
let DRILL_MAX_S = 600.0             // a drill can never be left running longer than this

/// An answer the page gets instead of what it asked for, with the reason.
struct HTTPError: Error {
    let status: Int
    let detail: String
}

final class Monitor {
    let relay: Relay
    let devices: Devices
    let tiles: Tiles
    let alarm: Alarm
    let archive: Archive?
    let archiveError: String?
    /// When the state loop last came round. Kept apart from everything else so the
    /// watchdog can always read it, even if the rest were ever stuck.
    let pollBeat = Locked<TimeInterval>(Clock.uptime())
    /// The window's page asking for things: how often, and when last. Says from outside
    /// whether the window is alive. Only the main thread writes it.
    private let pageStats = Locked<(requests: Int, lastAt: String?)>((0, nil))
    /// What the page says about itself (its heartbeat, its errors) and what it is still
    /// waiting for. Only the main thread writes it.
    private let pageInfo = Locked<JSON>([:])

    /// How weather is fetched; the test swaps in one that never answers.
    var fetchWeather: (Double, Double) -> JSON? = { Weather.fetch(lat: $0, lon: $1, windyKmh: Config.windyKmh) }

    private let queue = DispatchQueue(label: "moto.state")
    private let queueKey = DispatchSpecificKey<Bool>()

    // Everything below is touched only through onQ.
    private var state: JSON = [:]
    private var server: JSON = ["up": false, "ping_ms": jnull, "checked_at": jnull, "error": jnull]
    private var log: [JSON] = []
    private var logSeq = 0
    private var ticks: [TimeInterval] = []          // position arrivals, for packets/minute
    private var streamUp = false
    private var weather: JSON?
    // Each rider's own weather: with both out at once, each pane shows what its rider
    // is riding in (Jack, 2026-09-24).
    private var weatherBy: [String: JSON] = [:]
    private var syncStatus: JSON = ["last_seq": 0, "pending": jnull, "at": jnull, "error": jnull]
    // Pairing and role. A second Mac (Dana's) pairs with a code like a phone does,
    // and is a viewer: it sees everything and can act as an Observer, but exactly one
    // Mac consumes the outbox and keeps the history.
    private var paired = false
    private var keyRejected = false
    private var archivist: Bool?
    private var archivistChecked = 0.0
    private var seenIncidents: [Int: JSON] = [:]
    private var signalChimed: [String: String] = [:]   // rider -> the signal loss already chimed for
    private var drill: JSON?
    private var baselines: JSON = [:]
    private var baselinesAt = 0.0
    private var attention: String?
    private var misses = 0
    private var downSince: TimeInterval?
    private var lastTripCache: [String: (TimeInterval, Any)] = [:]
    private var lastPollOK: String?

    init() {
        relay = Relay(url: Config.relayURL, keyFile: Config.keyFile, caFile: Config.caFile)
        devices = Devices(sshKey: Config.sshKey, target: Config.sshTarget, enabled: Config.devicesEnabled)
        tiles = Tiles(keyFile: Config.tomtomKeyFile, cache: Config.tileCache)
        alarm = Alarm(mute: Config.alarmMute)
        // The live view and the alarm matter more than the history: a broken archive
        // is reported and the rest carries on. The relay keeps the outbox meanwhile.
        do {
            archive = try Archive(path: Config.archive)
            archiveError = nil
        } catch {
            archive = nil
            archiveError = "\(error)"
        }
        queue.setSpecific(key: queueKey, value: true)
        paired = relay.hasKey()
    }

    @discardableResult
    func onQ<T>(_ work: () throws -> T) rethrows -> T {
        if DispatchQueue.getSpecific(key: queueKey) == true { return try work() }
        return try queue.sync(execute: work)
    }

    private var nowS: TimeInterval { Date().timeIntervalSince1970 }

    // ---------------------------------------------------------------- the running log

    func addLog(_ entry: JSON) {
        onQ {
            logSeq += 1
            var e = entry
            e["seq"] = logSeq
            log.append(e)
            if log.count > LOG_LINES { log.removeFirst(log.count - LOG_LINES) }
            if e["tag"] as? String == "position" {
                ticks.append(nowS)
                if ticks.count > 600 { ticks.removeFirst(ticks.count - 600) }
            }
        }
    }

    func note(_ level: String, _ message: String, _ rider: String? = nil, tag: String = "monitor") {
        addLog(["ts": Clock.now(), "level": level, "tag": tag, "rider": orNull(rider), "message": message])
    }

    func packetsPerMinute() -> Int {
        onQ {
            let cutoff = nowS - 60
            return ticks.filter { $0 >= cutoff }.count
        }
    }

    // ---------------------------------------------------------------- starting

    func start() {
        if let seq = try? archive?.cursor() { onQ { syncStatus["last_seq"] = seq } }
        let loops: [(String, () -> Void)] = [
            ("state", { [unowned self] in self.pollState() }),
            ("events", { [unowned self] in self.consumeEvents() }),
            ("sync", { [unowned self] in self.syncForever() }),
            ("weather", { [unowned self] in self.weatherForever() }),
            ("status", { [unowned self] in self.writeStatusForever() }),
        ]
        for (name, body) in loops {
            let thread = Thread(block: body)
            thread.name = name
            thread.qualityOfService = .userInitiated
            thread.start()
        }
        note("info", "moto-tracker \(VERSION) started — talking to the relay directly", tag: "monitor")
        if let problem = archiveError {
            note("alert", "the archive could not be opened (\(problem)). The live view and the alarm carry on,"
                 + " and the relay keeps everything until it can be archived.", tag: "archive")
        }
    }

    // ---------------------------------------------------------------- state

    private func pollState() {
        while true {
            pollBeat.set(Clock.uptime())
            if !relay.hasKey() {
                onQ { paired = false }
                Thread.sleep(forTimeInterval: 2)
                continue
            }
            let (status, body, ms) = relay.call("GET", "/state", timeout: 8)
            if status == 401 {
                // The key was revoked, or this Mac was never paired. Say so plainly.
                onQ {
                    paired = true
                    keyRejected = true
                    server = ["up": false, "ping_ms": jnull, "checked_at": Clock.now(),
                              "error": "this Mac's key was rejected"]
                }
                Thread.sleep(forTimeInterval: 5)
                continue
            }
            onQ {
                paired = true
                keyRejected = false
            }
            if status == 200, let fresh = body as? JSON {
                let back: TimeInterval? = onQ {
                    defer {
                        downSince = nil
                        misses = 0
                    }
                    return downSince.map { nowS - $0 }
                }
                if let seconds = back {
                    note("info", String(format: "relay back after %.0f s", seconds), tag: "server")
                }
                let current = applyDrill(fresh)
                onQ {
                    state = current
                    server = ["up": true, "ping_ms": Int(ms.rounded()), "checked_at": Clock.now(), "error": jnull]
                    lastPollOK = Clock.now()
                }
                followIncidents(current)
                followSignal(current)
            } else {
                // One failed poll is not an outage. The Mini's route to the VPS is
                // carried by a tunnel whose source address rotates through a pool, and
                // every rotation kills the connections in flight. That costs one poll and
                // heals itself, so it is not worth a red light or a line in the log.
                let (count, wentDown): (Int, Bool) = onQ {
                    misses += 1
                    if misses < MISSES_BEFORE_DOWN { return (misses, false) }
                    let wasUp = server["up"] as? Bool ?? false
                    server = ["up": false, "ping_ms": jnull, "checked_at": Clock.now(),
                              "error": status != 0 ? "HTTP \(status)" : "unreachable",
                              "why": orNull(status == 0 ? body as? String : nil)]
                    if wasUp { downSince = nowS }
                    return (misses, wasUp)
                }
                if wentDown { note("warn", "relay unreachable — \(count) polls in a row", tag: "server") }
            }
            Thread.sleep(forTimeInterval: Config.stateEvery)
        }
    }

    /// The alarm follows the relay, never a local guess: silence is shared, so a
    /// silence on the Observer phone stops this Mac's alarm on the next poll.
    private func followIncidents(_ current: JSON) {
        var sounding = false
        var level: String?
        for rider in Monitor.riderOrder(current.keys) {
            guard let s = current[rider] as? JSON, let inc = s["incident"] as? JSON else { continue }
            let display = inc["display"] as? String
            // Fifteen minutes is long enough for anyone in the house to have heard it.
            if let raised = Clock.parse(inc["raised_at"]), Date().timeIntervalSince(raised) > ALARM_MAX_S {
                if alarm.sounding && alarm.reason == "incident" {
                    note("warn", "alarm quieted here after \(Int(ALARM_MAX_S) / 60) minutes —"
                         + " incident #\(pyStr(inc["id"])) is still open and unsilenced", rider, tag: "alarm")
                    alarm.set(false)
                }
                if display == "red" {
                    level = "red"
                } else if level != "red" {
                    level = display
                }
                continue
            }
            let key = int(inc["id"]) ?? 0
            let before: JSON? = onQ {
                let b = seenIncidents[key]
                seenIncidents[key] = inc
                return b
            }
            if inc["state"] as? String == "sos" && !truthy(inc["silenced_at"]) { sounding = true }
            if display == "red" {
                level = "red"
            } else if display == "yellow" && level != "red" {
                level = "yellow"
            } else if display == "pending" && level == nil {
                level = "amber"
            }
            // A new incident, or one that just escalated, brings the Mac to life.
            if inc["state"] as? String == "sos" && (before == nil || before?["state"] as? String != "sos") {
                alarm.attention()
            }
        }
        // This follower owns the INCIDENT alarm and nothing else. Calling set(false)
        // unconditionally here is what once made a test alarm stop after about a
        // second: the next poll silenced it. A test stops when it is stopped.
        if sounding {
            alarm.set(true, "incident")
        } else if alarm.reason == "incident" {
            alarm.set(false)
        }
        onQ { attention = level }
    }

    /// A rider unheard for two minutes with nothing violent before it: one chime, once
    /// per signal loss (Jack, 2026-09-29). The relay says when; this only follows a
    /// fresh answer from it, so a Mac that has lost the relay itself can never chime
    /// about a rider.
    private func followSignal(_ current: JSON) {
        for rider in Monitor.riderOrder(current.keys) {
            let signal = (current[rider] as? JSON)?["signal"] as? JSON
            guard let sig = signal, !sig.isEmpty else {
                onQ { _ = signalChimed.removeValue(forKey: rider) }
                continue
            }
            let since = sig["since"] as? String
            let chime: Bool = onQ {
                guard truthy(sig["alert"]), signalChimed[rider] != since else { return false }
                signalChimed[rider] = since
                return true
            }
            if chime { alarm.chime() }
        }
    }

    /// Jack first, then Dana, then anyone else: the order the relay sends and the
    /// page lays out.
    static func riderOrder<S: Sequence>(_ keys: S) -> [String] where S.Element == String {
        let all = Set(keys)
        let known = ["jack", "dana"].filter { all.contains($0) }
        return known + all.subtracting(known).sorted()
    }

    // ---------------------------------------------------------------- the drill

    /// A rehearsal: a fabricated incident fed into this Mac exactly where a real one
    /// arrives, so the alarm, the flashing icon, the red pane, the box and its counters
    /// all behave for real. Nothing is sent to the relay, nothing is archived, and no
    /// rider is ever told anything.
    func startDrill(_ rider: String) throws -> Any {
        let current: JSON = onQ { state }
        guard current[rider] != nil else { throw HTTPError(status: 404, detail: "no rider '\(rider)'") }
        if current.values.contains(where: { truthy(($0 as? JSON)?["incident"]) }) {
            throw HTTPError(status: 409, detail: "there is a real incident open — not now")
        }
        if current.values.contains(where: { truthy(($0 as? JSON)?["trip_id"]) }) {
            throw HTTPError(status: 409, detail: "someone is riding — a drill would hide the real screen")
        }
        if onQ({ drill }) != nil { return orNull(drillView()) }
        let seen = current[rider] as? JSON ?? [:]
        let started = Date()
        let raised = Clock.iso(started)
        let fresh: JSON = [
            "id": -Int(started.timeIntervalSince1970),        // negative: never a relay id
            "rider": rider,
            "started": started,
            "raised_at": raised,
            "escalate_at": Clock.iso(started.addingTimeInterval(DRILL_CONFIRM_S)),
            "silenced_at": jnull, "silenced_by": jnull,
            "lat": dbl(seen["lat"]) ?? 13.6929,
            "lon": dbl(seen["lon"]) ?? -89.2182,
            "speed_before": truthy(seen["speed"]) ? (dbl(seen["speed"]) ?? 78.0) : 78.0,
            "g": 9.8, "rot": 6.4,
        ]
        onQ { drill = fresh }
        note("warn", "DRILL — a practice incident was raised for \(seen["name"] as? String ?? rider);"
             + " nothing was sent to the relay", rider, tag: "drill")
        return orNull(drillView())
    }

    func stopDrill(_ why: String = "ended") {
        let ended: JSON? = onQ {
            let d = drill
            drill = nil
            return d
        }
        guard let d = ended else { return }
        // Stop the menu-bar icon flashing now, not on the next poll. The last poll left
        // the drill in the stored state, so take it out of there too.
        onQ {
            if let id = int(d["id"]) { seenIncidents[id] = nil }
            for (rider, seen) in state {
                if var s = seen as? JSON, let inc = s["incident"] as? JSON, (int(inc["id"]) ?? 0) < 0 {
                    s["incident"] = jnull
                    state[rider] = s
                }
            }
            if !state.values.contains(where: { truthy(($0 as? JSON)?["incident"]) }) { attention = nil }
        }
        note("info", "DRILL \(why)", d["rider"] as? String, tag: "drill")
    }

    /// The same shape the relay gives for a real incident, so every screen, counter and
    /// button works on it unchanged.
    func drillView() -> JSON? {
        guard let d = onQ({ drill }), let started = d["started"] as? Date else { return nil }
        let age = Date().timeIntervalSince(started)
        if age > DRILL_MAX_S {
            stopDrill("ended by itself after ten minutes")
            return nil
        }
        let escalated = age >= DRILL_CONFIRM_S
        return [
            "id": orNull(d["id"]), "kind": "candidate", "drill": true,
            "state": escalated ? "sos" : "pending",
            "display": escalated ? "red" : "pending",
            "raised_at": orNull(d["raised_at"]), "escalate_at": orNull(d["escalate_at"]),
            "rider_closed_at": jnull, "rider_resolution": jnull,
            "observer_closed_at": jnull, "observer_closed_by": jnull,
            "silenced_at": orNull(d["silenced_at"]), "silenced_by": orNull(d["silenced_by"]),
            "last_position": ["lat": orNull(d["lat"]), "lon": orNull(d["lon"]), "accuracy": 6.0,
                              "at": orNull(d["raised_at"])],
            "speed_before": orNull(d["speed_before"]), "speed_after": 0.0,
            "stationary_since": orNull(d["raised_at"]),
            "g": orNull(d["g"]), "rot": orNull(d["rot"]),
        ]
    }

    /// Puts the drill where a real incident would be, before anything reads it.
    func applyDrill(_ current: JSON) -> JSON {
        guard let view = drillView(), let rider = onQ({ drill?["rider"] as? String }),
              var seen = current[rider] as? JSON else { return current }
        var out = current
        seen["incident"] = view
        out[rider] = seen
        return out
    }

    var drillId: Int? { onQ { int(drill?["id"]) } }
    var drillRider: String? { onQ { drill?["rider"] as? String } }

    func silenceDrill() {
        onQ {
            drill?["silenced_at"] = Clock.now()
            drill?["silenced_by"] = "Mac Mini monitor"
        }
    }

    // ---------------------------------------------------------------- events

    private func consumeEvents() {
        var backoff = 1.0
        while true {
            if relay.hasKey() {
                let opened = Locked(false)
                relay.stream("/events", onOpen: { [unowned self] in
                    opened.set(true)
                    self.onQ { self.streamUp = true }
                }, onEvent: { [unowned self] payload in
                    self.addLog(payload)
                })
                if opened.get() { backoff = 1.0 }
            }
            onQ { streamUp = false }
            Thread.sleep(forTimeInterval: backoff)
            backoff = min(backoff * 2, 15)
        }
    }

    // ---------------------------------------------------------------- the archive

    private func syncOnce(_ a: Archive) throws -> (ok: Bool, stored: Int, more: Bool) {
        let after = try a.cursor()
        let (status, body, _) = relay.call("GET", "/sync?after=\(after)&limit=500", timeout: 30)
        if status == 403 {
            // Another Mac keeps the history. Checked again now and then, in case it moves.
            let first: Bool = onQ {
                let was = archivist
                archivist = false
                archivistChecked = nowS
                syncStatus["error"] = jnull
                syncStatus["at"] = Clock.now()
                syncStatus["pending"] = 0
                return was != false
            }
            if first { note("info", "this Mac is a viewer — another Mac keeps the history", tag: "archive") }
            return (true, 0, false)
        }
        if status == 200 { onQ { archivist = true } }
        guard status == 200, let page = body as? JSON else {
            onQ { syncStatus["error"] = status != 0 ? "HTTP \(status)" : "unreachable" }
            return (false, 0, false)
        }
        let entries = page["entries"] as? [JSON] ?? []
        if entries.isEmpty {
            onQ {
                syncStatus["error"] = jnull
                syncStatus["at"] = Clock.now()
                syncStatus["pending"] = 0
            }
            return (true, 0, false)
        }
        let result = try a.apply(entries)
        for payload in result.logs {
            let path = try Archive.writePhoneLog(payload, logsDir: Config.logs)
            note("info", "phone log stored: \(path.lastPathComponent)", payload["rider"] as? String, tag: "archive")
        }
        for trip in result.closedTrips { try a.writeTripLog(trip, logsDir: Config.logs) }
        // Only now may the relay drop them.
        let top = int(entries.last?["seq"]) ?? 0
        let (ackStatus, _, _) = relay.call("POST", "/sync/ack", ["upto": top], timeout: 15)
        if ackStatus != 200 {
            onQ { syncStatus["error"] = "ack failed: HTTP \(ackStatus)" }
            return (false, entries.count, false)
        }
        let more = truthy(page["more"])
        onQ {
            syncStatus = ["last_seq": top, "error": jnull, "at": Clock.now(), "pending": more ? 1 : 0]
            // A trip that just ended changes what the idle pane must show.
            if result.counts["trip"] != nil { lastTripCache.removeAll() }
        }
        if result.counts.keys.contains(where: { $0 != "position" }) {
            let kinds = result.counts.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: ", ")
            note("info", "archived \(entries.count) (\(kinds))", tag: "archive")
        }
        return (true, entries.count, more)
    }

    private func syncForever() {
        var lastPrune = 0.0
        while true {
            let (isPaired, rejected, role, checked) = onQ { (paired, keyRejected, archivist, archivistChecked) }
            if !isPaired || rejected || (role == false && nowS - checked < 300) {
                Thread.sleep(forTimeInterval: Config.syncEvery)
                continue
            }
            guard let a = archive else {
                onQ { syncStatus["error"] = "archive unavailable: \(archiveError ?? "unknown")" }
                Thread.sleep(forTimeInterval: Config.syncEvery)
                continue
            }
            do {
                var result = try syncOnce(a)
                while result.more { result = try syncOnce(a) }
            } catch {
                onQ { syncStatus["error"] = "\(error)" }
            }
            let (isArchivist, last) = onQ { (archivist, baselinesAt) }
            if isArchivist != false && nowS - last > 3600 {
                onQ { baselinesAt = nowS }
                do {
                    let sent = try Baselines.push(a, relay)
                    onQ { baselines = sent }
                    for rider in Monitor.riderOrder(sent.keys) {
                        if let b = sent[rider] as? JSON, b["ok"] as? Bool == true {
                            note("info", "baseline for \(rider): \(Baselines.describe(b))", rider, tag: "baseline")
                        }
                    }
                } catch {
                    note("warn", "could not publish baselines: \(error)", tag: "baseline")
                }
            }
            if nowS - lastPrune > 3600 {
                lastPrune = nowS
                _ = try? a.prune()
                tiles.sweep()
            }
            Thread.sleep(forTimeInterval: Config.syncEvery)
        }
    }

    func previousTrip(_ rider: String) -> Any? {
        let now = nowS
        if let (at, value) = onQ({ lastTripCache[rider] }), now - at <= 10 {
            return value is NSNull ? nil : value
        }
        // Read outside onQ: the archive is never touched while holding the state.
        var fresh: Any = jnull
        if let a = archive, let trip = try? a.lastTrip(rider) { fresh = trip }
        onQ { lastTripCache[rider] = (now, fresh) }
        return fresh is NSNull ? nil : fresh
    }

    // ---------------------------------------------------------------- weather

    /// One round of weather, riders on a trip first; with nobody out, the last known
    /// position of each, so an idle pane is not left without it. Returns how long to wait.
    @discardableResult
    func weatherOnce() -> TimeInterval {
        let spots: [(String, Double, Double)] = onQ {
            func located(_ onTrip: Bool) -> [(String, Double, Double)] {
                Monitor.riderOrder(state.keys).compactMap { rider in
                    guard let s = state[rider] as? JSON, let lat = dbl(s["lat"]), let lon = dbl(s["lon"]),
                          !onTrip || truthy(s["trip_id"]) else { return nil }
                    return (rider, lat, lon)
                }
            }
            let riding = located(true)
            return riding.isEmpty ? located(false) : riding
        }
        for (rider, lat, lon) in spots {
            let fetched = fetchWeather(lat, lon)
            let dropped: Bool = onQ {
                if let w = fetched {
                    weatherBy[rider] = w
                    return false
                }
                guard let have = weatherBy[rider] else { return false }
                let taken = Clock.parse(have["fetched_at"])
                let old = taken.map { Date().timeIntervalSince($0) > WEATHER_STALE_S } ?? true
                if old { weatherBy[rider] = nil }
                return old
            }
            if dropped { note("info", "no weather — the service has not answered for hours", rider, tag: "weather") }
        }
        return onQ {
            // The single reading older screens and tests read: the first rider out.
            weather = spots.lazy.compactMap { self.weatherBy[$0.0] }.first
            return weather != nil ? Config.weatherEvery : 60
        }
    }

    private func weatherForever() {
        while true { Thread.sleep(forTimeInterval: weatherOnce()) }
    }

    // ---------------------------------------------------------------- what the page reads

    func status() -> Ordered {
        let snap = onQ {
            (state: state, server: server, weather: weather, weatherBy: weatherBy, attention: attention,
             streamUp: streamUp, paired: paired, keyRejected: keyRejected, archivist: archivist,
             sync: syncStatus, baselines: baselines)
        }
        // A drill is shown the moment it is raised, not on the next poll.
        let current = applyDrill(snap.state)
        var level = snap.attention
        if let rider = drillRider, level == nil {
            level = ((current[rider] as? JSON)?["incident"] as? JSON)?["display"] as? String
        }
        var riders: [(String, Any)] = []
        for rider in Monitor.riderOrder(current.keys) {
            guard var s = current[rider] as? JSON else { continue }
            // The archivist has the whole history; a viewer keeps what the relay sends.
            if !truthy(s["trip_id"]) && snap.archivist != false {
                let relayPrevious = isNull(s["previous_trip"]) ? nil : s["previous_trip"]
                s["previous_trip"] = orNull(previousTrip(rider) ?? relayPrevious)
            }
            riders.append((rider, s))
        }
        return Ordered(pairs: [
            ("version", VERSION),
            ("server", snap.server),
            ("stream_up", snap.streamUp),
            ("packets_per_minute", packetsPerMinute()),
            ("riders", Ordered(pairs: riders)),
            ("weather", orNull(snap.weather)),
            ("weather_by", snap.weatherBy),
            ("attention", orNull(level)),
            ("alarm", ["sounding": alarm.sounding, "muted": alarm.mute, "reason": orNull(alarm.reason),
                       "chimes": alarm.chimes] as JSON),
            ("drill", drillId != nil),
            ("baselines", snap.baselines),
            ("sync", snap.sync),
            ("tiles", tiles.available()),
            ("paired", snap.paired && !snap.keyRejected),
            ("key_rejected", snap.keyRejected),
            ("archivist", orNull(snap.archivist)),
            ("devices_available", devices.available),
            ("hostname", Monitor.hostname()),
            ("relay", Config.relayURL),
            ("now", Clock.now()),
        ])
    }

    func events(after: Int, limit: Int) -> JSON {
        onQ {
            let rows = log.filter { (int($0["seq"]) ?? 0) > after }
            return ["entries": Array(rows.suffix(max(1, min(limit, LOG_LINES)))), "seq": logSeq]
        }
    }

    /// What the menu-bar icon needs, and nothing else.
    var attentionLevel: String? { onQ { attention } }
    var relayUp: Bool { onQ { server["up"] as? Bool ?? false } }
    var firstRider: String? { onQ { Monitor.riderOrder(state.keys).first } }

    /// Records a key the relay just gave this Mac, and what kind of Mac it is.
    func paired(asArchivist: Bool) {
        onQ {
            paired = true
            keyRejected = false
            archivist = asArchivist
            archivistChecked = nowS
        }
    }

    func pageRequested() {
        let seen = pageStats.get()
        pageStats.set((seen.requests + 1, Clock.now()))
    }

    func pageNote(_ key: String, _ value: Any) {
        var info = pageInfo.get()
        info[key] = value
        pageInfo.set(info)
    }

    static func hostname() -> String {
        var buffer = [CChar](repeating: 0, count: 256)
        gethostname(&buffer, buffer.count)
        return String(cString: buffer).replacingOccurrences(of: ".local", with: "")
    }

    // ---------------------------------------------------------------- seen from outside

    /// Every few seconds the app says how it is, in a small file beside the archive:
    /// the installer reads it to prove the new app is talking to the relay, and it can
    /// be read without opening the window.
    private func writeStatusForever() {
        while true {
            let snapshot: JSON = onQ {
                [
                    "version": VERSION, "pid": Int(getpid()), "written_at": Clock.now(),
                    "relay_up": server["up"] ?? false, "relay_error": orNull(server["error"]),
                    "relay_why": orNull(server["why"]),
                    "ping_ms": orNull(server["ping_ms"]), "last_poll_ok": orNull(lastPollOK),
                    "stream_up": streamUp, "paired": paired && !keyRejected, "key_rejected": keyRejected,
                    "archivist": orNull(archivist), "sync": syncStatus, "attention": orNull(attention),
                    "archive_error": orNull(archiveError),
                ]
            }
            var out = snapshot
            out["alarm"] = ["sounding": alarm.sounding, "reason": orNull(alarm.reason)] as JSON
            let page = pageStats.get()
            var seen = pageInfo.get()
            seen["requests"] = page.requests
            seen["last_at"] = orNull(page.lastAt)
            out["page"] = seen
            try? FileManager.default.createDirectory(at: Config.statusFile.deletingLastPathComponent(),
                                                     withIntermediateDirectories: true)
            try? Json.encode(out).write(to: Config.statusFile, options: .atomic)
            Thread.sleep(forTimeInterval: 5)
        }
    }

    // ---------------------------------------------------------------- for the test probes

    func seedForProbe(state seeded: JSON, weatherBy seededWeather: [String: JSON]) {
        onQ {
            state = seeded
            weatherBy = seededWeather
        }
    }

    /// Whether the shared state can still be reached at all, within a deadline.
    func stateAnswers(within seconds: TimeInterval) -> Bool {
        let reached = DispatchSemaphore(value: 0)
        queue.async { reached.signal() }
        return reached.wait(timeout: .now() + seconds) == .success
    }

    func hasWeather(_ rider: String) -> Bool { onQ { weatherBy[rider] != nil } }
    func logged(tag: String) -> Bool { onQ { log.contains { $0["tag"] as? String == tag } } }
}
