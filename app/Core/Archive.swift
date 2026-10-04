//
//  The Mac's archive: the permanent history the relay does not keep.
//
//  The relay appends every accepted write to a numbered outbox. This pulls that outbox
//  in order, stores each entry in moto.db, and only then acknowledges it, so nothing
//  can be lost by acknowledging early. Entries are idempotent: ids come from the relay,
//  so storing an entry twice changes nothing.
//
//  It also writes the per-trip log files behind the pane's "Open logs" button, and
//  works out the previous-trip statistics the idle panes show. Same database, same
//  tables and same files as the Python monitor before it, so the archive carries
//  straight on.
//
import Foundation
import SQLite3

private let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

struct DBError: Error, CustomStringConvertible {
    let description: String
}

/// A thin layer over SQLite. Rows come back as dictionaries, NULL as NSNull.
final class DB {
    private var handle: OpaquePointer?

    init(path: String) throws {
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        guard sqlite3_open_v2(path, &handle, flags, nil) == SQLITE_OK else {
            throw DBError(description: "cannot open \(path): \(message())")
        }
        sqlite3_busy_timeout(handle, 15000)
        try exec("PRAGMA journal_mode = WAL")
        try exec("PRAGMA foreign_keys = ON")
    }

    deinit { sqlite3_close_v2(handle) }

    private func message() -> String { String(cString: sqlite3_errmsg(handle)) }

    func exec(_ sql: String) throws {
        var error: UnsafeMutablePointer<CChar>?
        if sqlite3_exec(handle, sql, nil, nil, &error) != SQLITE_OK {
            let text = error.map { String(cString: $0) } ?? message()
            sqlite3_free(error)
            throw DBError(description: text)
        }
    }

    private func prepare(_ sql: String, _ args: [Any?]) throws -> OpaquePointer {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK, let s = statement else {
            throw DBError(description: "\(message()) in: \(sql)")
        }
        for (i, value) in args.enumerated() {
            bind(s, Int32(i + 1), value)
        }
        return s
    }

    private func bind(_ s: OpaquePointer, _ i: Int32, _ value: Any?) {
        switch value {
        case nil, is NSNull:
            sqlite3_bind_null(s, i)
        case let n as NSNumber where isBool(n):
            sqlite3_bind_int64(s, i, n.boolValue ? 1 : 0)
        case let n as NSNumber where CFNumberIsFloatType(n):
            sqlite3_bind_double(s, i, n.doubleValue)
        case let n as NSNumber:
            sqlite3_bind_int64(s, i, n.int64Value)
        case let text as String:
            sqlite3_bind_text(s, i, text, -1, SQLITE_TRANSIENT)
        case let data as Data:
            _ = data.withUnsafeBytes { sqlite3_bind_blob(s, i, $0.baseAddress, Int32(data.count), SQLITE_TRANSIENT) }
        default:
            // A nested object from a newer relay: kept as its JSON text, never refused.
            sqlite3_bind_text(s, i, Json.text(value), -1, SQLITE_TRANSIENT)
        }
    }

    func query(_ sql: String, _ args: [Any?] = []) throws -> [JSON] {
        let s = try prepare(sql, args)
        defer { sqlite3_finalize(s) }
        var rows: [JSON] = []
        while true {
            let rc = sqlite3_step(s)
            if rc == SQLITE_DONE { break }
            guard rc == SQLITE_ROW else { throw DBError(description: "\(message()) in: \(sql)") }
            var row: JSON = [:]
            for c in 0..<sqlite3_column_count(s) {
                let name = String(cString: sqlite3_column_name(s, c))
                switch sqlite3_column_type(s, c) {
                case SQLITE_INTEGER: row[name] = Int(sqlite3_column_int64(s, c))
                case SQLITE_FLOAT: row[name] = sqlite3_column_double(s, c)
                case SQLITE_TEXT: row[name] = String(cString: sqlite3_column_text(s, c))
                case SQLITE_BLOB:
                    let n = Int(sqlite3_column_bytes(s, c))
                    row[name] = n > 0 ? Data(bytes: sqlite3_column_blob(s, c), count: n) : Data()
                default: row[name] = jnull
                }
            }
            rows.append(row)
        }
        return rows
    }

    @discardableResult
    func run(_ sql: String, _ args: [Any?] = []) throws -> Int {
        let s = try prepare(sql, args)
        defer { sqlite3_finalize(s) }
        let rc = sqlite3_step(s)
        guard rc == SQLITE_DONE || rc == SQLITE_ROW else { throw DBError(description: "\(message()) in: \(sql)") }
        return Int(sqlite3_changes(handle))
    }

    func transaction<T>(_ work: () throws -> T) throws -> T {
        try exec("BEGIN IMMEDIATE")
        do {
            let result = try work()
            try exec("COMMIT")
            return result
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }

    func columns(_ table: String) throws -> Set<String> {
        Set(try query("PRAGMA table_info(\(table))").compactMap { $0["name"] as? String })
    }
}

final class Archive {
    // Columns the relay-2 incident model added; an archive from the Mini's first
    // server predates all of them.
    static let incidentColumns: [(String, String)] = [
        ("rider_closed_at", "TEXT"), ("rider_resolution", "TEXT"),
        ("observer_closed_at", "TEXT"), ("observer_closed_by", "TEXT"),
        ("silenced_at", "TEXT"), ("silenced_by", "TEXT"),
        ("closed_at", "TEXT"), ("forced", "INTEGER NOT NULL DEFAULT 0"),
    ]
    static let positionColumns: [(String, String)] = [
        ("state", "TEXT"), ("min_g", "REAL"), ("mean_g", "REAL"), ("rms_g", "REAL"),
        ("peak_rot", "REAL"), ("mean_rot", "REAL"), ("accel_n", "INTEGER"), ("gyro_n", "INTEGER"),
        ("decel", "REAL"),
        // Braking and leaning, measured from 2026-09-16 (Jack's brake tests were
        // invisible in everything recorded before it).
        ("peak_horiz_g", "REAL"), ("mean_horiz_g", "REAL"),
    ]
    // Positions are kept for ever (Jack, 2026-09-16). A full 84-minute ride is about
    // 130 KB, and deleting one would make a ride unreplayable, which is the one thing
    // the archive is for.
    static let retainDays = 0
    // The calendar day a ride belongs to, in the timezone the Mac is sitting in.
    static let localDay = "substr(datetime(started_at, 'localtime'), 1, 10)"

    static let schema = """
        CREATE TABLE IF NOT EXISTS riders (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'sleep');
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rider_id TEXT NOT NULL REFERENCES riders(id),
            started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL DEFAULT 'riding');
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER REFERENCES trips(id),
            rider_id TEXT NOT NULL, ts TEXT NOT NULL, received_at TEXT NOT NULL,
            lat REAL, lon REAL, accuracy REAL, speed REAL, battery INTEGER,
            peak_g REAL, decel REAL);
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trip_id INTEGER REFERENCES trips(id),
            rider_id TEXT NOT NULL, raised_at TEXT NOT NULL, kind TEXT NOT NULL,
            state TEXT NOT NULL, escalate_at TEXT, resolved_at TEXT, resolved_by TEXT,
            resolution TEXT, evidence TEXT);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
            tag TEXT NOT NULL, rider_id TEXT, message TEXT NOT NULL);
        -- Where the relay's outbox has been read up to. One row.
        CREATE TABLE IF NOT EXISTS sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1), last_seq INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT);
        INSERT OR IGNORE INTO sync_state (id, last_seq) VALUES (1, 0);
        INSERT OR IGNORE INTO riders (id, name) VALUES ('jack','Jack'), ('dana','Dana');
        CREATE INDEX IF NOT EXISTS idx_pos_trip ON positions(trip_id, ts);
        CREATE INDEX IF NOT EXISTS idx_pos_recv ON positions(received_at);
        CREATE INDEX IF NOT EXISTS idx_trips_rider ON trips(rider_id, started_at DESC);
        """

    private let db: DB
    private let queue = DispatchQueue(label: "moto.archive")
    private let queueKey = DispatchSpecificKey<Bool>()
    private var columnCache: [String: Set<String>] = [:]

    init(path: URL) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        db = try DB(path: path.path)
        queue.setSpecific(key: queueKey, value: true)
        try migrate()
    }

    /// Every use of the database goes through here, one at a time. Safe to nest.
    func use<T>(_ work: (DB) throws -> T) throws -> T {
        if DispatchQueue.getSpecific(key: queueKey) == true { return try work(db) }
        return try queue.sync { try work(db) }
    }

    /// Brings an older archive up to the current shape. Only ever adds: no archived
    /// column or row is changed or dropped.
    private func migrate() throws {
        try use { db in
            try db.transaction {
                try db.exec(Archive.schema)
                for (table, columns) in [("incidents", Archive.incidentColumns),
                                         ("positions", Archive.positionColumns)] {
                    let have = try db.columns(table)
                    for (name, type) in columns where !have.contains(name) {
                        try db.exec("ALTER TABLE \(table) ADD COLUMN \(name) \(type)")
                    }
                }
            }
            columnCache = [:]
        }
    }

    func cursor() throws -> Int {
        try use { db in int(try db.query("SELECT last_seq FROM sync_state WHERE id=1").first?["last_seq"]) ?? 0 }
    }

    /// Stores a relay row snapshot, ignoring any column this archive lacks.
    private func insert(_ db: DB, _ table: String, _ payload: JSON, _ mode: String = "OR REPLACE") throws {
        if columnCache[table] == nil { columnCache[table] = try db.columns(table) }
        let have = columnCache[table] ?? []
        let cols = payload.keys.filter { have.contains($0) }.sorted()
        guard !cols.isEmpty else { return }
        let marks = Array(repeating: "?", count: cols.count).joined(separator: ",")
        try db.run("INSERT \(mode) INTO \(table) (\(cols.joined(separator: ","))) VALUES (\(marks))",
                   cols.map { payload[$0] })
    }

    /// Stores a batch of outbox entries and advances the cursor, in ONE transaction.
    func apply(_ entries: [JSON]) throws -> (counts: [String: Int], logs: [JSON], closedTrips: [Int]) {
        try use { db in
            try db.transaction {
                var counts: [String: Int] = [:]
                var logs: [JSON] = []
                var closed = Set<Int>()
                for entry in entries {
                    let kind = entry["kind"] as? String ?? ""
                    let p = entry["payload"] as? JSON ?? [:]
                    counts[kind, default: 0] += 1
                    switch kind {
                    case "trip":
                        try insert(db, "trips", p)
                        if truthy(p["ended_at"]), let id = int(p["id"]) { closed.insert(id) }
                    case "rider":
                        try db.run("INSERT INTO riders (id, name, state) VALUES (?,?,?)"
                                   + " ON CONFLICT(id) DO UPDATE SET name=excluded.name, state=excluded.state",
                                   [p["id"], p["name"], (p["state"] as? String) ?? "sleep"])
                    case "position":
                        try insert(db, "positions", p, "OR IGNORE")
                    case "incident":
                        try insert(db, "incidents", p)
                    case "event":
                        try insert(db, "events", p, "OR IGNORE")
                    case "spike_log":
                        logs.append(p)
                    default:
                        // An unknown kind is still acknowledged: it came from a newer
                        // relay, and holding the outbox open for it would stall the archive.
                        break
                    }
                }
                let last = try entries.last.flatMap { int($0["seq"]) }
                    ?? int(db.query("SELECT last_seq FROM sync_state WHERE id=1").first?["last_seq"]) ?? 0
                try db.run("UPDATE sync_state SET last_seq=?, updated_at=? WHERE id=1", [last, Clock.now()])
                return (counts, logs, closed.sorted())
            }
        }
    }

    // ---------------------------------------------------------------- trip statistics

    private func tripStats(_ db: DB, _ tripId: Int) throws -> JSON? {
        guard let trip = try db.query("SELECT * FROM trips WHERE id=?", [tripId]).first else { return nil }
        let rows = try db.query("SELECT ts, received_at, speed, state, peak_g, peak_rot FROM positions"
                                + " WHERE trip_id=? ORDER BY id", [tripId])
        let started = Clock.parse(trip["started_at"]), ended = Clock.parse(trip["ended_at"])
        var duration: Any = jnull
        if let s = started, let e = ended { duration = e.timeIntervalSince(s) }
        // Average speed counts riding time only: a coffee stop is not a slow ride.
        let riding = rows.filter { (($0["state"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "riding") == "riding" }
        var ridingS = 0.0
        var previous: Date?
        for r in riding {
            let at = Clock.parse(r["received_at"])
            if let p = previous, let a = at {
                let gap = a.timeIntervalSince(p)
                if gap > 0 && gap <= 30 { ridingS += gap }
            }
            previous = at
        }
        let speeds = riding.compactMap { dbl($0["speed"]) }
        let gs = rows.compactMap { dbl($0["peak_g"]) }
        let rots = rows.compactMap { dbl($0["peak_rot"]) }
        return [
            "trip_id": tripId, "rider": orNull(trip["rider_id"]),
            "started_at": orNull(trip["started_at"]), "ended_at": orNull(trip["ended_at"]),
            "duration_s": duration, "riding_s": round1(ridingS),
            "max_speed": orNull(speeds.max()),
            "avg_speed": speeds.isEmpty ? jnull : speeds.reduce(0, +) / Double(speeds.count),
            "max_g": orNull(gs.max()),
            "max_rot": orNull(rots.max()),
            "points": rows.count,
        ]
    }

    func lastTrip(_ rider: String) throws -> JSON? {
        try use { db in
            guard let row = try db.query("SELECT id FROM trips WHERE rider_id=? AND ended_at IS NOT NULL"
                                         + " ORDER BY started_at DESC LIMIT 1", [rider]).first,
                  let id = int(row["id"]) else { return nil }
            return try tripStats(db, id)
        }
    }

    /// Dates that have a finished trip, newest first, for the replay picker. One
    /// rider's when asked: the button belongs to a rider's pane (Jack, 2026-09-20).
    /// The day is the day it was ridden, here, not in UTC, and only rides that can
    /// actually be opened count.
    func daysWithTrips(_ rider: String?) throws -> [JSON] {
        try use { db in
            let mine = rider == nil ? "" : " AND rider_id=?"
            let rows = try db.query(
                "SELECT \(Archive.localDay) AS day, COUNT(*) AS trips FROM trips t"
                + " WHERE ended_at IS NOT NULL\(mine) AND EXISTS"
                + "  (SELECT 1 FROM positions p WHERE p.trip_id = t.id)"
                + " GROUP BY day ORDER BY day DESC", rider == nil ? [] : [rider])
            return rows.map { ["day": orNull($0["day"]), "trips": orNull($0["trips"])] }
        }
    }

    /// Every finished trip that started on a given day, with enough to choose by.
    func tripsOn(_ day: String, _ rider: String?) throws -> [JSON] {
        try use { db in
            let mine = rider == nil ? "" : " AND rider_id=?"
            let rows = try db.query(
                "SELECT id FROM trips t WHERE ended_at IS NOT NULL AND \(Archive.localDay)=?"
                + "\(mine) ORDER BY started_at", rider == nil ? [day] : [day, rider])
            var out: [JSON] = []
            for r in rows {
                if let id = int(r["id"]), let stats = try tripStats(db, id), (int(stats["points"]) ?? 0) > 0 {
                    out.append(stats)
                }
            }
            return out
        }
    }

    /// Everything needed to watch a trip again: every position, and the events that
    /// happened while it was open.
    func replay(_ tripId: Int) throws -> JSON? {
        try use { db in
            guard let stats = try tripStats(db, tripId) else { return nil }
            let frames = try db.query(
                "SELECT received_at, ts, lat, lon, speed, accuracy, battery, state,"
                + " peak_g, mean_g, peak_rot, accel_n, peak_horiz_g"
                + " FROM positions WHERE trip_id=? ORDER BY id", [tripId])
            let ended = truthy(stats["ended_at"]) ? stats["ended_at"] : Clock.now()
            let events = try db.query(
                "SELECT ts, level, tag, rider_id AS rider, message FROM events"
                + " WHERE ts BETWEEN ? AND ? ORDER BY id", [stats["started_at"], ended])
            let incidents = try db.query("SELECT * FROM incidents WHERE trip_id=? ORDER BY id", [tripId])
            return ["trip": stats, "frames": frames, "events": events, "incidents": incidents]
        }
    }

    /// The trip's track, for the map. Newest last.
    func trail(_ tripId: Int, limit: Int = 3000) throws -> [JSON] {
        try use { db in
            let rows = try db.query("SELECT lat, lon, speed, received_at FROM positions WHERE trip_id=?"
                                    + " AND lat IS NOT NULL ORDER BY id DESC LIMIT ?", [tripId, limit])
            return rows.reversed().map {
                ["lat": orNull($0["lat"]), "lon": orNull($0["lon"]), "speed": orNull($0["speed"]),
                 "at": orNull($0["received_at"])]
            }
        }
    }

    /// The ids of the last finished trips, one rider's or everyone's, for Open logs.
    func recentTrips(_ rider: String?, limit: Int = 20) throws -> [Int] {
        try use { db in
            try db.query("SELECT id FROM trips WHERE ended_at IS NOT NULL"
                         + (rider == nil ? "" : " AND rider_id=?")
                         + " ORDER BY started_at DESC LIMIT \(limit)", rider == nil ? [] : [rider])
                .compactMap { int($0["id"]) }
        }
    }

    // ---------------------------------------------------------------- log files

    private static func stamp(_ value: Any?) -> String {
        guard let d = Clock.parse(value) else { return "--:--:--" }
        return Clock.local(d, "HH:mm:ss")
    }

    /// Regenerates one trip's log file from the archive. Safe to call repeatedly.
    @discardableResult
    func writeTripLog(_ tripId: Int, logsDir: URL) throws -> URL? {
        try use { db in
            guard let stats = try tripStats(db, tripId) else { return nil }
            let rider = stats["rider"] as? String ?? "unknown"
            let day = Clock.parse(stats["started_at"]).map { Clock.local($0, "yyyy-MM-dd") } ?? "unknown"
            let out = logsDir.appendingPathComponent(rider).appendingPathComponent("\(day)-trip-\(tripId).log")
            try FileManager.default.createDirectory(at: out.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            var lines = [
                "moto-tracker trip #\(tripId) — \(rider)",
                "started  \(pyStr(stats["started_at"]))",
                "ended    \(truthy(stats["ended_at"]) ? pyStr(stats["ended_at"]) : "(open)")",
                "duration \(Archive.fmtDuration(dbl(stats["duration_s"])))   riding \(Archive.fmtDuration(dbl(stats["riding_s"])))",
                String(format: "max %.0f km/h   avg %.0f km/h   peak %.2f g   rot %.2f rad/s",
                       dbl(stats["max_speed"]) ?? 0, dbl(stats["avg_speed"]) ?? 0,
                       dbl(stats["max_g"]) ?? 0, dbl(stats["max_rot"]) ?? 0),
                "\(pyStr(stats["points"])) positions",
                "",
            ]
            for r in try db.query("SELECT * FROM positions WHERE trip_id=? ORDER BY id", [tripId]) {
                var sensors = ""
                if let g = dbl(r["peak_g"]) {
                    sensors = String(format: "  g %.2f/%.2f  rot %.2f", g, dbl(r["mean_g"]) ?? 0, dbl(r["peak_rot"]) ?? 0)
                        + "  n \(truthy(r["accel_n"]) ? pyStr(r["accel_n"]) : "0")/\(truthy(r["gyro_n"]) ? pyStr(r["gyro_n"]) : "0")"
                }
                let state = (r["state"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "riding"
                let battery = isNull(r["battery"]) ? "?" : pyStr(r["battery"])
                lines.append("\(Archive.stamp(r["received_at"]))  \(ljust(state, 8))"
                             + String(format: "  %.5f,%.5f  %5.1f km/h  ±%3.0f m",
                                      dbl(r["lat"]) ?? 0, dbl(r["lon"]) ?? 0, dbl(r["speed"]) ?? 0,
                                      dbl(r["accuracy"]) ?? 0)
                             + "  batt \(battery)%\(sensors)")
            }
            let incidents = try db.query("SELECT * FROM incidents WHERE trip_id=? ORDER BY id", [tripId])
            if !incidents.isEmpty {
                lines += ["", "incidents:"]
                for i in incidents {
                    var line = "  #\(pyStr(i["id"])) \(pyStr(i["kind"])) \(pyStr(i["state"])) raised \(pyStr(i["raised_at"]))"
                    if truthy(i["rider_closed_at"]) {
                        line += " rider \(pyStr(i["rider_resolution"])) at \(pyStr(i["rider_closed_at"]))"
                    }
                    if truthy(i["observer_closed_at"]) {
                        line += " observer \(pyStr(i["observer_closed_by"])) at \(pyStr(i["observer_closed_at"]))"
                    }
                    if truthy(i["forced"]) { line += " FORCED" }
                    lines.append(line)
                }
            }
            let ended = truthy(stats["ended_at"]) ? stats["ended_at"] : Clock.now()
            let events = try db.query("SELECT * FROM events WHERE rider_id=? AND ts BETWEEN ? AND ? ORDER BY id",
                                      [stats["rider"], stats["started_at"], ended])
            if !events.isEmpty {
                lines += ["", "events:"]
                for e in events {
                    lines.append("  \(Archive.stamp(e["ts"])) \(ljust(pyStr(e["level"]), 5)) \(ljust(pyStr(e["tag"]), 9)) \(pyStr(e["message"]))")
                }
            }
            try (lines.joined(separator: "\n") + "\n").write(to: out, atomically: true, encoding: .utf8)
            return out
        }
    }

    /// A log uploaded from a phone, kept beside that rider's trip logs.
    static func writePhoneLog(_ payload: JSON, logsDir: URL) throws -> URL {
        var rider = String(((payload["rider"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "unknown")
            .filter { $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" })
        if rider.isEmpty { rider = "unknown" }
        var name = URL(fileURLWithPath: (payload["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "phone.log")
            .lastPathComponent
        if name == "." || name == ".." || name.isEmpty { name = "phone.log" }
        let out = logsDir.appendingPathComponent(rider).appendingPathComponent("phone").appendingPathComponent(name)
        try FileManager.default.createDirectory(at: out.deletingLastPathComponent(), withIntermediateDirectories: true)
        try ((payload["text"] as? String) ?? "").write(to: out, atomically: true, encoding: .utf8)
        return out
    }

    /// Nothing is pruned while retainDays is 0, the setting Jack chose on 2026-09-16: the
    /// archive is small, and a deleted position is a ride that can never be replayed.
    /// Kept as a switch rather than deleted, in case that ever changes.
    @discardableResult
    func prune(days: Int = Archive.retainDays) throws -> Int {
        guard days > 0 else { return 0 }
        let cutoff = Clock.iso(Date().addingTimeInterval(-Double(days) * 86400))
        return try use { db in
            try db.transaction {
                try db.run("DELETE FROM positions WHERE received_at < ?"
                           + " AND trip_id IN (SELECT id FROM trips WHERE ended_at IS NOT NULL)", [cutoff])
            }
        }
    }

    static func fmtDuration(_ seconds: Double?) -> String {
        guard let seconds = seconds else { return "—" }
        let s = Int(seconds)
        let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }
}
