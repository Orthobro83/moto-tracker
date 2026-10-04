//
//  Test only: the app's own code with no window, for app/e2e_test.py.
//
//  The test speaks to it over stdin and stdout, one JSON line each way, so it never
//  listens on a port either. It is built into the test's temporary folder and is never
//  installed.
//
//    moto-harness                          the whole app, driven over stdin/stdout
//    moto-harness probe describe C D P K   one weather label (code, day 0/1, mm, cloud %)
//    moto-harness probe stale-weather      weather unanswered for hours must not hang it
//    moto-harness probe prune              what pruning removes from MONITOR_ARCHIVE
//    moto-harness probe relay              one GET /state, to see how the relay answers
//    moto-harness probe sounds             the alarm and chime decode and play (silently)
//
import AVFoundation
import Foundation

func probe(_ args: [String]) -> Int32 {
    switch args.first {
    case "describe" where args.count == 5:
        let (label, icon) = Weather.describe(code: Int(args[1]) ?? -1, day: args[2] == "1",
                                             precip: Double(args[3]) ?? 0, cloud: Int(args[4]) ?? -1)
        print(Json.text(["label": label, "icon": icon]))
    case "stale-weather":
        // The real Monitor, its weather round, Open-Meteo silent, a reading three hours old.
        let m = Monitor()
        m.fetchWeather = { _, _ in nil }
        m.seedForProbe(state: ["dana": ["lat": 13.69, "lon": -89.22] as JSON],
                       weatherBy: ["dana": ["headline": "Sunny",
                                               "fetched_at": Clock.isoSeconds(Date().addingTimeInterval(-3 * 3600))]])
        let finished = DispatchSemaphore(value: 0)
        Thread { m.weatherOnce(); finished.signal() }.start()
        let returned = finished.wait(timeout: .now() + 3) == .success
        let free = m.stateAnswers(within: 2)
        guard returned && free else {
            print(Json.text(["free": false, "dropped": jnull, "logged": jnull]))
            return 0
        }
        print(Json.text(["free": true, "dropped": !m.hasWeather("dana"), "logged": m.logged(tag: "weather")]))
    case "relay":
        let relay = Relay(url: Config.relayURL, keyFile: Config.keyFile, caFile: Config.caFile)
        let (status, body, ms) = relay.call("GET", "/state", timeout: 10)
        print(Json.text(["status": status, "anchors": relay.anchors.count, "ms": Int(ms),
                         "answer": status == 200 ? "state for \((body as? JSON)?.count ?? 0) riders" : orNull(body)]))
    case "sounds":
        // Decoded by the same player the alarm uses, but never started: nothing is heard.
        let alarm = try? AVAudioPlayer(data: Alarm.wav(Alarm.alarmFrames()))
        let chime = try? AVAudioPlayer(data: Alarm.wav(Alarm.chimeFrames()))
        print(Json.text(["alarm_s": orNull(alarm.map { round1($0.duration) }),
                         "chime_s": orNull(chime.map { round1($0.duration) }),
                         "ready": (alarm?.prepareToPlay() ?? false) && (chime?.prepareToPlay() ?? false)]))
    case "prune":
        do {
            let a = try Archive(path: Config.archive)
            print(Json.text(["removed": try a.prune()]))
        } catch {
            print(Json.text(["error": "\(error)"]))
        }
    default:
        FileHandle.standardError.write(Data("unknown probe\n".utf8))
        return 2
    }
    return 0
}

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments.first == "probe" {
    exit(probe(Array(arguments.dropFirst())))
}

let monitor = Monitor()
monitor.start()
let api = API(monitor: monitor, uiDir: Config.uiDir)
let output = FileHandle.standardOutput

while let line = readLine(strippingNewline: true) {
    guard let message = Json.parse(Data(line.utf8)) as? JSON else { continue }
    let request = APIRequest(method: message["method"] as? String ?? "GET",
                             target: message["path"] as? String ?? "/",
                             headers: message["headers"] as? [String: String] ?? [:],
                             body: (message["body"] as? String).map { Data($0.utf8) })
    let answered = DispatchSemaphore(value: 0)
    let reply = Locked<APIResponse?>(nil)
    DispatchQueue.global().async {
        api.handle(request) { response in
            reply.set(response)
            answered.signal()
        }
    }
    answered.wait()
    let response = reply.get()!
    let out: JSON = ["id": orNull(message["id"]), "status": response.status, "type": response.type,
                     "headers": response.headers, "body_b64": response.body.base64EncodedString()]
    output.write(Json.encode(out) + Data("\n".utf8))
}
exit(0)
