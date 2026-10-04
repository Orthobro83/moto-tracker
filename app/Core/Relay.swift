//
//  This Mac's connection to the relay: HTTPS with this Mac's own device key, trusting
//  only our own CA — exactly what the phones do. Nothing in between.
//
import Foundation
import Security

/// Trusts only our own CA, never the system's list of authorities, so no certificate a
/// public authority might issue for that address could stand in for the relay.
class RelayTrust: NSObject, URLSessionDelegate {
    let anchors: [SecCertificate]

    init(anchors: [SecCertificate]) {
        self.anchors = anchors
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        guard !anchors.isEmpty else {
            completionHandler(.cancelAuthenticationChallenge, nil)     // no CA: trust nothing
            return
        }
        // The chain must end at our CA, and the certificate must name this address —
        // the same two checks the phones make. Apple's own TLS policy is not used: it
        // also refuses any server certificate valid for more than 825 days, a rule for
        // public authorities, and the relay's runs three years (to 2029-09-14).
        SecTrustSetPolicies(trust, SecPolicyCreateBasicX509())
        SecTrustSetAnchorCertificates(trust, anchors as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var error: CFError?
        guard SecTrustEvaluateWithError(trust, &error),
              let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first,
              RelayTrust.names(leaf).contains(challenge.protectionSpace.host) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    /// The addresses and names a certificate is issued for: its subject alternative names.
    static func names(_ cert: SecCertificate) -> Set<String> {
        guard let values = SecCertificateCopyValues(cert, [kSecOIDSubjectAltName] as CFArray, nil) as? [String: Any],
              let san = values[kSecOIDSubjectAltName as String] as? [String: Any] else { return [] }
        var found = Set<String>()
        func collect(_ value: Any?) {
            if let s = value as? String {
                found.insert(s)
            } else if let list = value as? [Any] {
                list.forEach(collect)
            } else if let entry = value as? [String: Any] {
                collect(entry[kSecPropertyKeyValue as String])
            }
        }
        collect(san[kSecPropertyKeyValue as String])
        return found
    }
}

/// The relay's server-sent events, one `data:` line at a time.
final class EventReader: RelayTrust, URLSessionDataDelegate {
    private let onOpen: () -> Void
    private let onEvent: (JSON) -> Void
    private var buffer = Data()
    let finished = DispatchSemaphore(value: 0)
    private(set) var failure: String?

    init(anchors: [SecCertificate], onOpen: @escaping () -> Void, onEvent: @escaping (JSON) -> Void) {
        self.onOpen = onOpen
        self.onEvent = onEvent
        super.init(anchors: anchors)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 200 {
            onOpen()
            completionHandler(.allow)
        } else {
            failure = "HTTP \(status)"
            completionHandler(.cancel)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            var line = buffer.subdata(in: buffer.startIndex..<newline)
            buffer.removeSubrange(buffer.startIndex...newline)
            if line.last == 0x0D { line.removeLast() }
            guard let text = String(data: line, encoding: .utf8), text.hasPrefix("data:") else { continue }
            let payload = text.dropFirst(5).trimmingCharacters(in: .whitespaces)
            if let event = Json.parse(Data(payload.utf8)) as? JSON {
                onEvent(event)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error = error, failure == nil { failure = error.localizedDescription }
        finished.signal()
    }
}

final class Relay {
    let base: String
    let keyFile: URL
    private let trust: RelayTrust
    private let session: URLSession

    init(url: String, keyFile: URL, caFile: URL) {
        base = url.hasSuffix("/") ? String(url.dropLast()) : url
        self.keyFile = keyFile
        trust = RelayTrust(anchors: Relay.loadCA(caFile))
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.waitsForConnectivity = false
        session = URLSession(configuration: config, delegate: trust, delegateQueue: nil)
    }

    /// Our CA, from its PEM file. An http:// relay (the test's) needs none.
    static func loadCA(_ file: URL) -> [SecCertificate] {
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { return [] }
        var certs: [SecCertificate] = []
        for block in text.components(separatedBy: "-----BEGIN CERTIFICATE-----").dropFirst() {
            guard let body = block.components(separatedBy: "-----END CERTIFICATE-----").first else { continue }
            let b64 = body.components(separatedBy: .whitespacesAndNewlines).joined()
            if let der = Data(base64Encoded: b64), let cert = SecCertificateCreateWithData(nil, der as CFData) {
                certs.append(cert)
            }
        }
        return certs
    }

    var anchors: [SecCertificate] { trust.anchors }

    func key() -> String {
        (try? String(contentsOf: keyFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    func hasKey() -> Bool { !key().isEmpty }

    /// (status, parsed body, round-trip ms). Status 0 means unreachable. Waits for the
    /// answer, so it is only ever called from a background thread.
    func call(_ method: String, _ path: String, _ body: JSON? = nil,
              timeout: TimeInterval = 10) -> (Int, Any?, Double) {
        guard let url = URL(string: base + path) else { return (0, "bad path \(path)", 0) }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer " + key(), forHTTPHeaderField: "Authorization")
        if let body = body {
            request.httpBody = Json.encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let started = Clock.uptime()
        let (status, data, failure) = perform(request, timeout: timeout)
        let ms = (Clock.uptime() - started) * 1000
        if status == 0 { return (0, failure, ms) }
        guard let data = data, !data.isEmpty else { return (status, nil, ms) }
        if let parsed = Json.parse(data) { return (status, parsed, ms) }
        return (status, String(decoding: data, as: UTF8.self), ms)
    }

    /// Exchanges a pairing code for this Mac's device key. No key needed: this is how
    /// a second Mac joins without anyone touching the VPS.
    func pair(code: String, name: String, timeout: TimeInterval = 20) -> (Int, Any?) {
        var request = URLRequest(url: URL(string: base + "/pair")!, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.httpBody = Json.encode(["code": code, "name": name])
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (status, data, failure) = perform(request, timeout: timeout)
        if status == 0 { return (0, failure) }
        return (status, Json.parse(data) ?? String(decoding: data ?? Data(), as: UTF8.self))
    }

    func saveKey(_ key: String) throws {
        try FileManager.default.createDirectory(at: keyFile.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        let old = umask(0o077)
        defer { umask(old) }
        try key.write(to: keyFile, atomically: true, encoding: .utf8)
        chmod(keyFile.path, 0o600)
    }

    /// Reads the event stream until the connection drops. The relay sends a keepalive
    /// every 15 s, so a minute with nothing at all means the stream is dead. Returns why
    /// it ended.
    @discardableResult
    func stream(_ path: String, onOpen: @escaping () -> Void, onEvent: @escaping (JSON) -> Void) -> String? {
        guard let url = URL(string: base + path) else { return "bad path" }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 60)
        request.setValue("Bearer " + key(), forHTTPHeaderField: "Authorization")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let reader = EventReader(anchors: trust.anchors, onOpen: onOpen, onEvent: onEvent)
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 60
        config.urlCache = nil
        let streamSession = URLSession(configuration: config, delegate: reader, delegateQueue: nil)
        streamSession.dataTask(with: request).resume()
        reader.finished.wait()
        streamSession.invalidateAndCancel()
        return reader.failure
    }

    private func perform(_ request: URLRequest, timeout: TimeInterval) -> (Int, Data?, String?) {
        let done = DispatchSemaphore(value: 0)
        let result = Locked<(Int, Data?, String?)>((0, nil, "no answer"))
        let task = session.dataTask(with: request) { data, response, error in
            if let http = response as? HTTPURLResponse {
                result.set((http.statusCode, data, nil))
            } else {
                result.set((0, nil, error.map { $0.localizedDescription } ?? "no answer"))
            }
            done.signal()
        }
        task.resume()
        // URLSession's own timeout is the gap between packets; this bounds the whole
        // call, so nothing waiting on the relay can wait for ever.
        if done.wait(timeout: .now() + timeout + 5) == .timedOut {
            task.cancel()
            _ = done.wait(timeout: .now() + 2)
            return (0, nil, "timed out")
        }
        return result.get()
    }
}
