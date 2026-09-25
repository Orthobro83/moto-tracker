//
//  moto-tracker — the Mac Mini's monitor window (design.md 2026-09-15).
//
//  A plain AppKit app: one window holding the monitor's page, and a menu-bar icon
//  that flashes while an incident is open and brings the window to the front when
//  clicked. The monitor itself runs in the background under launchd, so the alarm
//  sounds and the archive keeps filling whether or not this window is open —
//  closing the window only hides it.
//
import AppKit
import WebKit

let monitorURL = URL(string: ProcessInfo.processInfo.environment["MONITOR_URL"] ?? "http://127.0.0.1:8089")!
let attentionURL = monitorURL.appendingPathComponent("api/attention")

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSStatusItem!
    var flashTimer: Timer?
    var pollTimer: Timer?
    var flashOn = false
    var attention: String?          // nil | amber | yellow | red
    var announced = false
    var loaded = false

    // ---------------------------------------------------------------- launch

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        buildWindow()
        buildStatusItem()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.poll()
        }
        poll()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    // The window is the monitor's face, not the monitor: closing it leaves the
    // background service running and keeps the menu-bar icon watching.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.add(self, name: "moto")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "moto-tracker"
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.059, green: 0.067, blue: 0.082, alpha: 1)
        window.minSize = NSSize(width: 1100, height: 680)
        window.contentView = web
        window.delegate = self
        window.setFrameAutosaveName("motoMonitorWindow")
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        load()
    }

    func load() {
        web.load(URLRequest(url: monitorURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { loaded = true }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        waiting()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        waiting()
    }

    /// The background monitor may still be starting (after a reboot, say).
    func waiting() {
        loaded = false
        web.loadHTMLString("""
            <body style="background:#0f1115;color:#a3acb6;font:14px -apple-system;display:flex;
                         align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center">
              <div style="font-size:16px;color:#e8eaed">Waiting for the monitor…</div>
              <div style="margin-top:8px">\(monitorURL.absoluteString)</div>
            </div></body>
            """, baseURL: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self = self, !self.loaded else { return }
            self.load()
        }
    }

    // ---------------------------------------------------------------- links and clipboard

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated,
           url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)        // Google Maps opens in the real browser
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "open":
            if let s = body["url"] as? String, let url = URL(string: s),
               url.scheme == "https" || url.scheme == "http" {
                NSWorkspace.shared.open(url)
            }
        case "copy":
            if let text = body["text"] as? String {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            }
        default:
            break
        }
    }

    // ---------------------------------------------------------------- menu bar

    func buildStatusItem() {
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = status.button {
            button.image = icon(alert: false)
            button.image?.isTemplate = true
            button.toolTip = "moto-tracker"
            button.target = self
            button.action = #selector(statusClicked(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    func icon(alert: Bool) -> NSImage? {
        let name = alert ? "exclamationmark.triangle.fill" : "location.north.circle"
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "moto-tracker")
        image?.isTemplate = !alert
        return image
    }

    @objc func statusClicked(_ sender: NSStatusBarButton) {
        if NSApp.currentEvent?.type == .rightMouseUp {
            showMenu()
        } else {
            showWindow()
        }
    }

    func showMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: "Show monitor", action: #selector(showWindow), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit moto-tracker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        status.menu = menu
        status.button?.performClick(nil)
        status.menu = nil               // back to click-to-show-window
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reload() { load() }

    // ---------------------------------------------------------------- the incident light

    func poll() {
        var request = URLRequest(url: attentionURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5)
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            guard let self = self else { return }
            var level: String?
            if let data = data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                level = json["attention"] as? String
            }
            DispatchQueue.main.async { self.apply(level) }
        }.resume()
    }

    func apply(_ level: String?) {
        if level != attention {
            attention = level
            level == nil ? stopFlashing() : startFlashing()
        }
        // A raised incident pulls the Mini forward: the display wakes, the window
        // comes to the front, and the Dock icon keeps asking until it is looked at.
        if level == "red" && !announced {
            announced = true
            showWindow()
            NSApp.requestUserAttention(.criticalRequest)
        }
        if level == nil { announced = false }
    }

    func startFlashing() {
        guard flashTimer == nil else { return }
        flashTimer = Timer.scheduledTimer(withTimeInterval: 0.55, repeats: true) { [weak self] _ in
            guard let self = self, let button = self.status.button else { return }
            self.flashOn.toggle()
            button.image = self.icon(alert: self.flashOn)
            button.contentTintColor = self.flashOn
                ? (self.attention == "yellow" || self.attention == "amber" ? .systemYellow : .systemRed)
                : nil
        }
        flashTimer?.fire()
    }

    func stopFlashing() {
        flashTimer?.invalidate()
        flashTimer = nil
        flashOn = false
        status.button?.image = icon(alert: false)
        status.button?.contentTintColor = nil
    }

    // ---------------------------------------------------------------- app menu

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About moto-tracker", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide moto-tracker", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit moto-tracker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
