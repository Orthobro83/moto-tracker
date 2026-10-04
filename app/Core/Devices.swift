//
//  The Devices panel's bridge to the relay.
//
//  Pairing and revocation stay off the network API deliberately: the relay has no
//  admin endpoint, so a stolen device key can never mint another one. The Mini does
//  this over its own SSH key instead, running relayctl on the VPS.
//
import Foundation

final class Devices {
    let sshKey: URL
    let target: String
    let enabled: Bool

    init(sshKey: URL, target: String, enabled: Bool = true) {
        self.sshKey = sshKey
        self.target = target
        self.enabled = enabled
    }

    var available: Bool { enabled && FileManager.default.fileExists(atPath: sshKey.path) }

    private static func quote(_ s: String) -> String {
        if s.isEmpty { return "''" }
        let safe = CharacterSet(charactersIn: "@%+=:,./-_").union(.alphanumerics)
        if s.unicodeScalars.allSatisfy({ safe.contains($0) }) { return s }
        return "'" + s.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
    }

    private func run(_ args: [String], timeout: TimeInterval = 25) -> JSON {
        guard enabled else { return ["ok": false, "error": "device management is off in this instance"] }
        guard FileManager.default.fileExists(atPath: sshKey.path) else {
            return ["ok": false, "error": "no SSH key at \(sshKey.path)"]
        }
        let remote = "sudo /usr/local/bin/relayctl " + args.map(Devices.quote).joined(separator: " ")
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        p.arguments = ["-i", sshKey.path, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                       "-o", "ConnectTimeout=10", target, remote]
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return ["ok": false, "error": "\(error)"] }
        let outData = Locked(Data()), errData = Locked(Data())
        let readers = DispatchGroup()
        readers.enter()
        DispatchQueue.global().async { outData.set(out.fileHandleForReading.readDataToEndOfFile()); readers.leave() }
        readers.enter()
        DispatchQueue.global().async { errData.set(err.fileHandleForReading.readDataToEndOfFile()); readers.leave() }
        if readers.wait(timeout: .now() + timeout) == .timedOut {
            p.terminate()
            return ["ok": false, "error": "the VPS did not answer in time"]
        }
        p.waitUntilExit()
        let stdout = String(decoding: outData.get(), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let stderr = String(decoding: errData.get(), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        if p.terminationStatus != 0 {
            return ["ok": false, "error": String((stderr.isEmpty ? stdout : stderr).prefix(400))]
        }
        return ["ok": true, "stdout": stdout]
    }

    func list() -> JSON {
        let r = run(["devices", "--json"])
        guard r["ok"] as? Bool == true else { return r }
        let text = r["stdout"] as? String ?? ""
        guard let devices = Json.parse(Data((text.isEmpty ? "[]" : text).utf8)) else {
            return ["ok": false, "error": "unexpected answer from relayctl"]
        }
        return ["ok": true, "devices": devices]
    }

    func pair(role: String, name: String, rider: String?) -> JSON {
        // Two roles: a phone belongs to its rider and observes the other one.
        guard role == "rider" || role == "monitor" else { return ["ok": false, "error": "unknown role"] }
        if role == "rider" && !(rider == "jack" || rider == "dana") {
            return ["ok": false, "error": "a rider key needs a rider"]
        }
        let name = String(name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        guard !name.isEmpty else { return ["ok": false, "error": "give the device a name"] }
        var args = ["pair", "--role", role, "--name", name, "--json"]
        if let rider = rider, role == "rider" { args += ["--rider", rider] }
        let r = run(args)
        guard r["ok"] as? Bool == true else { return r }
        guard var out = Json.parse(Data((r["stdout"] as? String ?? "").utf8)) as? JSON else {
            return ["ok": false, "error": "unexpected answer from relayctl"]
        }
        out["ok"] = true
        return out
    }

    func revoke(_ deviceId: Int) -> JSON {
        let r = run(["revoke", String(deviceId)])
        guard r["ok"] as? Bool == true else { return r }
        return ["ok": true, "message": r["stdout"] ?? ""]
    }
}
