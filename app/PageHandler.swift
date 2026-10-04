//
//  How the window's page reaches the app: moto://app/... answered in this process.
//
import Foundation
import WebKit

/// Answers the page's requests inside the app. moto://app/api/status goes straight to
/// the API, in this process: no socket, no port, nothing that can be missing.
final class PageHandler: NSObject, WKURLSchemeHandler {
    let api: API
    private var live: [ObjectIdentifier: (path: String, since: TimeInterval)] = [:]     // main thread only

    init(api: API) {
        self.api = api
    }

    func webView(_ webView: WKWebView, start task: any WKURLSchemeTask) {
        let id = ObjectIdentifier(task as AnyObject)
        guard let url = task.request.url else {
            task.didFailWithError(URLError(.badURL))
            return
        }
        live[id] = (url.path + (url.query.map { "?" + $0 } ?? ""), Clock.uptime())
        api.m.pageRequested()
        noteWaiting()
        let request = APIRequest(method: task.request.httpMethod ?? "GET", target: url.absoluteString,
                                 headers: task.request.allHTTPHeaderFields ?? [:],
                                 body: task.request.httpBody ?? task.request.httpBodyStream.map(PageHandler.drain))
        DispatchQueue.global(qos: .userInitiated).async { [api] in
            api.handle(request) { response in
                DispatchQueue.main.async {
                    // The page may have moved on; a stopped task must not be answered.
                    guard self.live.removeValue(forKey: id) != nil else { return }
                    self.noteWaiting()
                    var fields = response.headers
                    fields["Content-Type"] = response.type
                    fields["Content-Length"] = String(response.body.count)
                    let head = HTTPURLResponse(url: url, statusCode: response.status, httpVersion: "HTTP/1.1",
                                               headerFields: fields)!
                    task.didReceive(head)
                    task.didReceive(response.body)
                    task.didFinish()
                }
            }
        }
    }

    func webView(_ webView: WKWebView, stop task: any WKURLSchemeTask) {
        live.removeValue(forKey: ObjectIdentifier(task as AnyObject))
        noteWaiting()
    }

    /// How many of the page's requests are still unanswered, and the oldest of them.
    private func noteWaiting() {
        api.m.pageNote("waiting", live.count)
        if let oldest = live.values.min(by: { $0.since < $1.since }) {
            api.m.pageNote("oldest_waiting", "\(oldest.path) for \(Int(Clock.uptime() - oldest.since)) s")
        } else {
            api.m.pageNote("oldest_waiting", jnull)
        }
    }

    static func drain(_ stream: InputStream) -> Data {
        var data = Data()
        stream.open()
        defer { stream.close() }
        var buffer = [UInt8](repeating: 0, count: 16384)
        while stream.hasBytesAvailable {
            let n = stream.read(&buffer, maxLength: buffer.count)
            if n <= 0 { break }
            data.append(buffer, count: n)
        }
        return data
    }
}
