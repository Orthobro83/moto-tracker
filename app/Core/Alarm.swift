//
//  The Mac's alarm.
//
//  An open, unsilenced incident sounds through the Mac's speakers until someone
//  silences it. It belongs to the app, not the window, so it is heard whether or not
//  the window is open. Raising the alarm also wakes the display and brings the window
//  forward.
//
//  Silence is shared: the relay records it, so silencing on the Observer phone stops
//  this too. This only follows what /state reports.
//
//  It also owns the signal-loss chime (Jack, 2026-09-29): a rider unheard for two
//  minutes with nothing violent before it is an alert, not an alarm — one soft chime
//  at the Mac's own volume, nothing repeated, nothing woken or brought forward.
//
import AVFoundation
import Foundation
import IOKit.pwr_mgt

final class Alarm {
    // What the alarm is sounding for. An incident outranks a test: a test can never
    // take the alarm away from a real incident, and an incident sounding over a test
    // takes ownership of it, so the test's own timeout cannot silence the incident.
    static let priority: [String: Int] = ["test": 1, "incident": 2]
    static let sampleRate = 22050
    static let minVolume = 70           // percent: an alarm at 10% volume is not an alarm

    let mute: Bool
    /// Set by the app: brings the window to the front.
    var bringForward: (() -> Void)?

    private let queue = DispatchQueue(label: "moto.alarm")
    private let queueKey = DispatchSpecificKey<Bool>()
    private var isSounding = false
    private var currentReason: String?
    private var reasonAt = 0.0
    private var played = 0
    private var player: AVAudioPlayer?
    private var chimePlayer: AVAudioPlayer?
    private var priorVolume: Int?
    private lazy var alarmSound = Alarm.wav(Alarm.alarmFrames())
    private lazy var chimeSound = Alarm.wav(Alarm.chimeFrames())

    init(mute: Bool) {
        self.mute = mute
        queue.setSpecific(key: queueKey, value: true)
    }

    private func onQueue<T>(_ work: () -> T) -> T {
        if DispatchQueue.getSpecific(key: queueKey) == true { return work() }
        return queue.sync(execute: work)
    }

    var sounding: Bool { onQueue { isSounding } }
    var reason: String? { onQueue { currentReason } }
    /// How many chimes have been played, for the tests.
    var chimes: Int { onQueue { played } }

    // ---------------------------------------------------------------- control

    /// Sounds while on. Calling it again with the same answer does nothing new.
    func set(_ on: Bool, _ reason: String? = nil) {
        onQueue {
            if on && !isSounding {
                isSounding = true
                currentReason = reason
                start()
            } else if on && (Alarm.priority[reason ?? ""] ?? 0) > (Alarm.priority[currentReason ?? ""] ?? 0) {
                // Already sounding for something lesser — an incident arriving during a
                // test takes it over, so only a silence can stop it now.
                currentReason = reason
            } else if !on && isSounding {
                isSounding = false
                currentReason = nil
                stopSound()
            }
        }
    }

    /// The Test alarm button. It sounds the way a real alarm sounds — over and over
    /// until someone stops it — because a single beep proves only the speaker. Pressing
    /// it again stops it, and it stops itself after two minutes so a test can never be
    /// left running. It will not touch an alarm sounding for an incident. Returns
    /// whether it is now sounding.
    func test(limit: TimeInterval = 120) -> Bool {
        var started = 0.0
        let sounding: Bool = onQueue {
            if isSounding && currentReason != "test" { return true }
            if isSounding {
                set(false)
                return false
            }
            set(true, "test")
            started = Date().timeIntervalSince1970
            reasonAt = started
            return true
        }
        guard sounding, started > 0 else { return sounding }
        let mine = started
        DispatchQueue.global().async { [weak self] in
            let deadline = Date().addingTimeInterval(limit)
            while Date() < deadline {
                guard let self = self else { return }
                let still = self.onQueue { self.isSounding && self.currentReason == "test" && self.reasonAt == mine }
                if !still { return }
                Thread.sleep(forTimeInterval: 0.25)
            }
            // Only ever stops its own test — never an incident that took over.
            guard let self = self else { return }
            self.onQueue {
                if self.isSounding && self.currentReason == "test" && self.reasonAt == mine { self.set(false) }
            }
        }
        return true
    }

    /// One signal-loss chime. Never raises the volume, never loops, and never touches
    /// the alarm, which may be sounding for something else entirely.
    func chime() {
        onQueue {
            played += 1
            guard !mute else { return }
            let p = try? AVAudioPlayer(data: chimeSound)
            p?.play()
            chimePlayer = p
        }
    }

    /// Wakes the display and puts the window in front of whatever is on screen.
    func attention() {
        guard !mute else { return }
        var assertion: IOPMAssertionID = 0
        if IOPMAssertionDeclareUserActivity("moto-tracker: an incident" as CFString, kIOPMUserActiveLocal,
                                            &assertion) == kIOReturnSuccess {
            DispatchQueue.global().asyncAfter(deadline: .now() + 10) { IOPMAssertionRelease(assertion) }
        }
        if Config.openApp {
            DispatchQueue.main.async { [weak self] in self?.bringForward?() }
        }
    }

    // ---------------------------------------------------------------- sound

    private func start() {
        guard !mute else { return }
        if let volume = Alarm.outputVolume(), volume < Alarm.minVolume {
            priorVolume = volume
            Alarm.setOutputVolume(Alarm.minVolume)
        }
        let p = try? AVAudioPlayer(data: alarmSound)
        p?.numberOfLoops = -1
        p?.play()
        player = p
    }

    private func stopSound() {
        player?.stop()
        player = nil
        if let volume = priorVolume {
            Alarm.setOutputVolume(volume)
            priorVolume = nil
        }
    }

    private static func osascript(_ script: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let done = DispatchSemaphore(value: 0)
        let data = Locked(Data())
        DispatchQueue.global().async {
            data.set(out.fileHandleForReading.readDataToEndOfFile())
            done.signal()
        }
        if done.wait(timeout: .now() + 5) == .timedOut {
            p.terminate()
            return nil
        }
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        return String(decoding: data.get(), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func outputVolume() -> Int? {
        osascript("output volume of (get volume settings)").flatMap { Int($0) }
    }

    private static func setOutputVolume(_ percent: Int) {
        _ = osascript("set volume output volume \(percent)")
    }

    // ---------------------------------------------------------------- the two sounds

    /// The alarm: a square-edged two-tone, about a second per cycle.
    static func alarmFrames(cycles: Int = 6) -> Data {
        let tone: [(Double, Double)] = [(880, 0.35), (0, 0.06), (660, 0.35), (0, 0.24)]
        var out = Data()
        for _ in 0..<cycles {
            for (freq, seconds) in tone {
                let n = Int(Double(sampleRate) * seconds)
                for i in 0..<n {
                    var v: Int16 = 0
                    if freq > 0 {
                        // A short fade at each end keeps the tone from clicking.
                        let edge = min(1.0, Double(i) / 220, Double(n - i) / 220)
                        v = Int16(26000 * edge * sin(2 * Double.pi * freq * Double(i) / Double(sampleRate)))
                    }
                    withUnsafeBytes(of: v.littleEndian) { out.append(contentsOf: $0) }
                }
            }
        }
        return out
    }

    /// The chime: two bell strikes, A5 then F5, each ringing out — nothing like the
    /// alarm, so the two can never be mistaken for each other. The phone ships the very
    /// same sound.
    static func chimeFrames() -> Data {
        let strikes: [(Double, Double)] = [(880.0, 0.0), (698.46, 0.26)]
        let partials: [(Double, Double, Double)] = [(1.0, 1.0, 0.95), (2.0, 0.42, 0.55), (3.0, 0.18, 0.32),
                                                    (4.16, 0.08, 0.2)]   // ratio, level, decay s
        let rate = Double(sampleRate)
        let n = Int(rate * 1.9)
        var wave = [Double](repeating: 0, count: n)
        for (freq, start) in strikes {
            let first = Int(rate * start)
            for i in first..<n {
                let t = Double(i - first) / rate
                let attack = min(1.0, t / 0.004)             // 4 ms, so the strike does not click
                var sum = 0.0
                for (ratio, level, decay) in partials {
                    sum += level * exp(-t / decay) * sin(2 * Double.pi * freq * ratio * t)
                }
                wave[i] += attack * sum
            }
        }
        let peak = wave.map(abs).max() ?? 1.0
        let tail = Int(rate * 0.3)                           // the last 300 ms fade to nothing
        var out = Data()
        for (i, v) in wave.enumerated() {
            let fade = min(1.0, Double(n - i) / Double(tail))
            let sample = Int16(32767 * 0.45 * fade * v / (peak == 0 ? 1 : peak))
            withUnsafeBytes(of: sample.littleEndian) { out.append(contentsOf: $0) }
        }
        return out
    }

    /// 16-bit mono PCM wrapped as a WAV file.
    static func wav(_ frames: Data) -> Data {
        func le32(_ v: UInt32) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
        func le16(_ v: UInt16) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
        var d = Data("RIFF".utf8)
        d += le32(UInt32(36 + frames.count))
        d += Data("WAVEfmt ".utf8)
        d += le32(16) + le16(1) + le16(1)
        d += le32(UInt32(sampleRate)) + le32(UInt32(sampleRate * 2)) + le16(2) + le16(16)
        d += Data("data".utf8) + le32(UInt32(frames.count)) + frames
        return d
    }
}
