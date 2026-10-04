//
//  Everything the page asks for: /, /ui/..., /api/... and /tiles/....
//
//  The page is the one Jack specified on 2026-09-15 and has used since, unchanged. It
//  still asks for these paths, but nothing travels over a network to answer them: in
//  the app, a moto:// handler hands each request straight to `handle` inside the same
//  process. Nothing on the Mac listens on a port.
//
import Foundation

struct APIRequest {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]          // names in lower case
    let body: Data?

    /// `target` is a path with its query ("/api/trips?day=2026-10-04") or a whole URL.
    init(method: String, target: String, headers: [String: String], body: Data?) {
        let parts = URLComponents(string: target.hasPrefix("/") ? "moto://app" + target : target)
        self.method = method.uppercased()
        let p = parts?.path ?? "/"
        path = p.isEmpty ? "/" : p
        var q: [String: String] = [:]
        for item in parts?.queryItems ?? [] { q[item.name] = item.value ?? "" }
        query = q
        var h: [String: String] = [:]
        for (k, v) in headers { h[k.lowercased()] = v }
        self.headers = h
        self.body = body
    }
}

struct APIResponse {
    var status: Int
    var type: String
    var body: Data
    var headers: [String: String] = [:]

    static func json(_ value: Any, status: Int = 200) -> APIResponse {
        APIResponse(status: status, type: "application/json", body: Json.encode(value))
    }

    static func detail(_ status: Int, _ detail: String) -> APIResponse {
        .json(["detail": detail], status: status)
    }
}

final class API {
    let m: Monitor
    let uiDir: URL
    // Only our own page acts. Nothing outside the app can reach these paths at all; the
    // check stays as a second lock on the door (design.md 2026-09-15).
    static let allowedOrigins: Set<String> = ["moto://app"]

    init(monitor: Monitor, uiDir: URL) {
        m = monitor
        self.uiDir = uiDir
    }

    /// Answers one request. Some of these wait on the relay, so it is called off the
    /// main thread, and `done` is called exactly once.
    func handle(_ r: APIRequest, _ done: @escaping (APIResponse) -> Void) {
        let finish: (APIResponse) -> Void = { response in
            var out = response
            if out.headers["Cache-Control"] == nil { out.headers["Cache-Control"] = "no-store" }
            out.headers["X-Content-Type-Options"] = "nosniff"
            done(out)
        }
        if r.method != "GET" {
            if r.headers["x-moto"] != "1" {
                finish(.json(["error": "not from this monitor"], status: 403))
                return
            }
            if let origin = r.headers["origin"], !API.allowedOrigins.contains(origin) {
                finish(.json(["error": "bad origin"], status: 403))
                return
            }
        }
        if r.method == "GET" && r.path.hasPrefix("/tiles/") {
            tile(r, finish)
            return
        }
        do {
            finish(try route(r))
        } catch let e as HTTPError {
            finish(.detail(e.status, e.detail))
        } catch {
            finish(.detail(500, "\(error)"))
        }
    }

    // ---------------------------------------------------------------- helpers

    private func body(_ r: APIRequest) throws -> JSON {
        guard let data = r.body, !data.isEmpty else { return [:] }
        guard let parsed = Json.parse(data) as? JSON else { throw HTTPError(status: 422, detail: "expected a JSON object") }
        return parsed
    }

    private func intQuery(_ r: APIRequest, _ name: String, required: Bool = false) throws -> Int? {
        guard let raw = r.query[name] else {
            if required { throw HTTPError(status: 422, detail: "\(name) is required") }
            return nil
        }
        guard let value = Int(raw) else { throw HTTPError(status: 422, detail: "\(name) must be a whole number") }
        return value
    }

    /// The number in a path like /api/incident/12/silence.
    private func between(_ path: String, _ prefix: String, _ suffix: String) -> Int? {
        guard path.hasPrefix(prefix), path.hasSuffix(suffix), path.count > prefix.count + suffix.count else { return nil }
        return Int(path.dropFirst(prefix.count).dropLast(suffix.count))
    }

    private func archive() throws -> Archive {
        guard let a = m.archive else {
            throw HTTPError(status: 503, detail: "the archive is unavailable: \(m.archiveError ?? "unknown")")
        }
        return a
    }

    private func relayError(_ status: Int, _ body: Any?) -> HTTPError {
        let text: String
        if let s = body as? String { text = s } else { text = Json.text(body) }
        return HTTPError(status: status == 0 ? 502 : status, detail: text)
    }

    // ---------------------------------------------------------------- routes

    private func route(_ r: APIRequest) throws -> APIResponse {
        let p = r.path
        switch (r.method, p) {

        // the page
        case ("GET", "/"):
            return try file("index.html")

        // state
        case ("GET", "/api/status"):
            return .json(m.status())
        case ("GET", "/api/events"):
            return .json(m.events(after: try intQuery(r, "after") ?? 0, limit: try intQuery(r, "limit") ?? 400))
        case ("GET", "/api/attention"):
            return .json(["attention": orNull(m.attentionLevel), "sounding": m.alarm.sounding,
                          "server_up": m.relayUp] as JSON)
        case ("POST", "/api/pair"):
            return .json(try pairThisMac(try body(r)))

        // replay
        case ("GET", "/api/trip-days"):
            // Which days have a ride to watch, so the picker offers them rather than
            // asking for a date to be guessed (2026-09-16). Scoped to one rider when asked.
            let rider = r.query["rider"] ?? ""
            return .json(["rider": rider, "days": try archive().daysWithTrips(rider.isEmpty ? nil : rider)] as JSON)
        case ("GET", "/api/trips"):
            guard let day = r.query["day"] else { throw HTTPError(status: 422, detail: "day is required") }
            guard day.count == 10 else { throw HTTPError(status: 400, detail: "day must be YYYY-MM-DD") }
            let rider = r.query["rider"] ?? ""
            return .json(["day": day, "rider": rider,
                          "trips": try archive().tripsOn(day, rider.isEmpty ? nil : rider)] as JSON)
        case ("GET", "/api/replay"):
            let id = try intQuery(r, "trip_id", required: true)!
            guard let data = try archive().replay(id) else {
                throw HTTPError(status: 404, detail: "no trip #\(id) in the archive")
            }
            return .json(data)
        case ("GET", "/api/trail"):
            let id = try intQuery(r, "trip_id", required: true)!
            return .json(["trail": try archive().trail(id, limit: try intQuery(r, "limit") ?? 3000)])

        // drills and tests
        case ("POST", "/api/drill"):
            // Rehearse an incident on this Mac. Refused while anyone is riding or a real
            // incident is open, and it never reaches the relay or the archive.
            let payload = try body(r)
            guard let rider = (payload["rider"] as? String).flatMap({ $0.isEmpty ? nil : $0 }) ?? m.firstRider else {
                throw HTTPError(status: 503, detail: "no rider state yet")
            }
            return .json(["ok": true, "incident": try m.startDrill(rider)] as JSON)
        case ("POST", "/api/drill/stop"):
            m.alarm.set(false)
            m.stopDrill("stopped")
            return .json(["ok": true])
        case ("POST", "/api/alarm/test"):
            // Starts or stops the test alarm. Refused while a real incident is sounding —
            // that one is stopped by silencing the incident, which the Observer phone shares.
            if m.alarm.sounding && m.alarm.reason != "test" {
                throw HTTPError(status: 409, detail: "an incident is sounding — silence it in the incident box")
            }
            let sounding = m.alarm.test()
            m.note("info", sounding ? "test alarm started" : "test alarm stopped", tag: "alarm")
            return .json(["ok": true, "sounding": sounding])
        case ("POST", "/api/alarm/stop"):
            if m.alarm.sounding && m.alarm.reason != "test" {
                throw HTTPError(status: 409, detail: "an incident is sounding — silence it in the incident box")
            }
            m.alarm.set(false)
            return .json(["ok": true])

        // ending a trip the phone cannot
        case ("POST", "/api/trip/force-end"):
            // An Observer's decision, made deliberately behind a confirmation (Jack, 2026-09-16).
            guard let rider = (try body(r))["rider"] as? String, !rider.isEmpty else {
                throw HTTPError(status: 400, detail: "which rider?")
            }
            let (status, answer, _) = m.relay.call("POST", "/trip/force-end", ["rider": rider])
            guard status == 200 else { throw relayError(status, answer) }
            m.note("warn", "force-ended \(rider)'s trip — their phone was not going to", rider, tag: "trip")
            return .json(orNull(answer))

        // devices and logs
        case ("GET", "/api/devices"):
            return .json(m.devices.list())
        case ("POST", "/api/devices/pair"):
            let b = try body(r)
            return .json(m.devices.pair(role: b["role"] as? String ?? "", name: b["name"] as? String ?? "",
                                        rider: b["rider"] as? String))
        case ("POST", "/api/devices/revoke"):
            guard let id = int((try body(r))["id"]) else { throw HTTPError(status: 400, detail: "which device?") }
            return .json(m.devices.revoke(id))
        case ("POST", "/api/open-logs"):
            return .json(try openLogs(try body(r)))

        default:
            break
        }

        if r.method == "GET", p.hasPrefix("/ui/") {
            return try file(String(p.dropFirst(4)))
        }
        if r.method == "POST", let id = between(p, "/api/incident/", "/silence") {
            return .json(try silence(id))
        }
        if r.method == "POST", let id = between(p, "/api/incident/", "/close") {
            return .json(try close(id))
        }
        if r.method == "POST", let id = between(p, "/api/incident/", "/force-close") {
            return .json(try forceClose(id, try body(r)))
        }
        throw HTTPError(status: 404, detail: "Not Found")
    }

    // ---------------------------------------------------------------- the page's files

    private func file(_ name: String) throws -> APIResponse {
        // One level only, and nothing hidden: the page's own files and nothing else.
        guard !name.isEmpty, !name.contains("/"), !name.hasPrefix(".") else {
            throw HTTPError(status: 404, detail: "no such file")
        }
        let url = uiDir.appendingPathComponent(name)
        guard let data = try? Data(contentsOf: url) else { throw HTTPError(status: 404, detail: "no such file") }
        let types = ["html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                     "css": "text/css; charset=utf-8", "png": "image/png", "svg": "image/svg+xml",
                     "wav": "audio/wav"]
        return APIResponse(status: 200, type: types[url.pathExtension] ?? "application/octet-stream", body: data)
    }

    // ---------------------------------------------------------------- map tiles

    private func tile(_ r: APIRequest, _ done: @escaping (APIResponse) -> Void) {
        let parts = r.path.split(separator: "/").map(String.init)      // tiles, layer, z, x, y.png
        guard parts.count == 5, parts[4].hasSuffix(".png"),
              let z = Int(parts[2]), let x = Int(parts[3]), let y = Int(parts[4].dropLast(4)) else {
            done(.detail(404, "Not Found"))
            return
        }
        let layer = parts[1]
        guard layer == "map" || layer == "traffic" else {
            done(.detail(404, "no such layer"))
            return
        }
        m.tiles.get(layer: layer, z: z, x: x, y: y, style: r.query["style"] ?? "main") { data, ttl in
            done(APIResponse(status: 200, type: "image/png", body: data,
                             headers: ["Cache-Control": "private, max-age=\(ttl)"]))
        }
    }

    // ---------------------------------------------------------------- acting on an incident

    private func silence(_ id: Int) throws -> Any {
        if let drill = m.drillId, drill == id {
            m.silenceDrill()
            m.alarm.set(false)
            m.note("info", "DRILL — alarm silenced; the practice incident is still open", m.drillRider, tag: "drill")
            return ["ok": true, "silenced": true, "drill": true]
        }
        let (status, answer, _) = m.relay.call("POST", "/incident/silence", ["incident_id": id])
        guard status == 200 else { throw relayError(status, answer) }
        m.alarm.set(false)
        return orNull(answer)
    }

    /// The Observer's half. The incident stays open until the rider closes it too.
    private func close(_ id: Int) throws -> Any {
        if let drill = m.drillId, drill == id {
            m.alarm.set(false)
            m.stopDrill("closed")
            return ["ok": true, "state": "closed", "drill": true]
        }
        let (status, answer, _) = m.relay.call("POST", "/incident/observer-close", ["incident_id": id])
        guard status == 200 else { throw relayError(status, answer) }
        m.alarm.set(false)
        return orNull(answer)
    }

    /// Deliberately awkward: the exact phrase must be typed, and the close is recorded
    /// as made without the rider's confirmation.
    private func forceClose(_ id: Int, _ payload: JSON) throws -> Any {
        if let drill = m.drillId, drill == id {
            throw HTTPError(status: 400, detail: "this is a drill; close it with Close incident")
        }
        let (status, answer, _) = m.relay.call("POST", "/incident/force-close",
                                               ["incident_id": id, "confirm": payload["confirm"] as? String ?? ""])
        guard status == 200 else { throw relayError(status, answer) }
        m.alarm.set(false)
        m.note("warn", "incident #\(id) closed without rider confirmation", tag: "incident")
        return orNull(answer)
    }

    // ---------------------------------------------------------------- pairing this Mac

    /// Pairs this Mac with the relay using a code from the other Mac's Devices panel.
    /// Nobody needs the VPS: the code is exchanged for this Mac's own device key.
    private func pairThisMac(_ payload: JSON) throws -> JSON {
        let raw = payload["code"].map { $0 as? String ?? "\($0)" } ?? ""
        let code = String(raw.uppercased().filter { $0.isLetter || $0.isNumber })
        var name = (payload["name"] as? String) ?? ""
        if name.isEmpty { name = Monitor.hostname() }
        name = String(name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        guard code.count == 8 else { throw HTTPError(status: 400, detail: "a pairing code is eight characters") }
        let (status, answer) = m.relay.pair(code: code, name: name)
        guard status == 200, let reply = answer as? JSON, let key = reply["key"] as? String else {
            let detail = (answer as? JSON)?["detail"] ?? answer
            throw HTTPError(status: status == 0 ? 502 : status,
                            detail: truthy(detail) ? (detail as? String ?? Json.text(detail)) : "pairing failed")
        }
        guard reply["role"] as? String == "monitor" else {
            throw HTTPError(status: 400, detail: "that code is for a \(pyStr(reply["role"])) device, not a Mac")
        }
        try m.relay.saveKey(key)
        // The relay has just said which kind of Mac this is; no need to discover it.
        let archivist = truthy(reply["archivist"])
        m.paired(asArchivist: archivist)
        m.note("info", "this Mac paired as \(pyStr(reply["name"]))"
               + (archivist ? " — it keeps the history" : " — viewer"), tag: "device")
        return ["ok": true, "name": orNull(reply["name"]), "archivist": archivist]
    }

    // ---------------------------------------------------------------- logs

    private func openLogs(_ payload: JSON) throws -> JSON {
        let rider = String((payload["rider"] as? String ?? "").filter { $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" })
        let folder = rider.isEmpty ? Config.logs : Config.logs.appendingPathComponent(rider)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        // Make sure what Finder shows is current before it opens.
        let a = try archive()
        let trips = try a.recentTrips(rider.isEmpty ? nil : rider)
        for trip in trips { try a.writeTripLog(trip, logsDir: Config.logs) }
        let open = Process()
        open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        open.arguments = [folder.path]
        open.standardOutput = FileHandle.nullDevice
        open.standardError = FileHandle.nullDevice
        do { try open.run() } catch { throw HTTPError(status: 500, detail: "\(error)") }
        return ["ok": true, "folder": folder.path, "logs": trips.count]
    }
}
