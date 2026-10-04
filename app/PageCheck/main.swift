//
//  Test only: the real page, in real WebKit, answered by the app's own moto:// handler.
//
//  What the harness cannot show: that WebKit loads the page from inside the app with no
//  server at all, runs it without a script error, and that the page's GETs and POSTs —
//  bodies, headers, Origin and all — arrive at the API and come back. The window is
//  never shown. Prints one JSON line and exits; app/e2e_test.py reads it.
//
import AppKit
import WebKit

final class PageCheck: NSObject, WKNavigationDelegate {
    let monitor = Monitor()
    lazy var api = API(monitor: monitor, uiDir: Config.uiDir)
    lazy var pages = PageHandler(api: api)
    var web: WKWebView!
    var window: NSWindow!

    func run() {
        monitor.start()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.setURLSchemeHandler(pages, forURLScheme: "moto")
        // Catch every script error the page raises from its very first line.
        config.userContentController.addUserScript(WKUserScript(
            source: "window.__errors = [];"
                + " addEventListener('error', e => __errors.push(String(e.message || e)));"
                + " addEventListener('unhandledrejection', e => __errors.push('rejected: ' + String(e.reason)));",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1440, height: 900), configuration: config)
        web.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900), styleMask: [.borderless],
                          backing: .buffered, defer: false)
        window.contentView = web
        web.load(URLRequest(url: URL(string: "moto://app/")!))
        DispatchQueue.main.asyncAfter(deadline: .now() + 40) { finish(["error": "the page did not finish in 40 s"]) }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Let the page run a few of its own polls first.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { self.probe() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        finish(["error": "the page did not load: \(error.localizedDescription)"])
    }

    func probe() {
        let script = """
            const out = {};
            const s = await fetch('/api/status');
            out.status = s.status;
            out.version = (await s.json()).version;
            // A POST the way the page sends one. Its body names a rider who does not exist,
            // so the answer proves the body arrived: 404 for that rider, not 503 for none.
            const d = await fetch('/api/drill', {method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Moto': '1'},
                body: JSON.stringify({rider: 'nobody'})});
            out.post = d.status;
            out.postBody = await d.json();
            // Without the page's own header, refused.
            const u = await fetch('/api/alarm/test', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: '{}'});
            out.unsigned = u.status;
            const t = await fetch('/tiles/map/3/4/3.png?style=night');
            out.tile = t.status;
            out.tileType = t.headers.get('Content-Type');
            out.tileBytes = (await t.arrayBuffer()).byteLength;
            out.title = document.title;
            out.leaflet = typeof L !== 'undefined';
            out.map = document.querySelectorAll('.leaflet-container').length;
            out.srvtext = (document.getElementById('srvtext') || {}).textContent || null;
            out.errors = window.__errors;
            return out;
            """
        web.callAsyncJavaScript(script, arguments: [:], in: nil, in: .page) { result in
            switch result {
            case .success(let value): finish(value as? [String: Any] ?? ["error": "no answer from the page"])
            case .failure(let error): finish(["error": "\(error)"])
            }
        }
    }
}

func finish(_ result: [String: Any]) {
    print(Json.text(result))
    fflush(stdout)
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let check = PageCheck()
check.run()
app.run()
