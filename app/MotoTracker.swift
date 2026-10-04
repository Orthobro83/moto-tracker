//
//  moto-tracker — the Mac's window on the two riders (design.md 2026-10-04).
//
//  One app, and nothing else that has to be running. It talks to the relay itself,
//  over HTTPS with this Mac's own key, the way the phones do. It keeps the archive,
//  sounds the alarm and flashes the menu-bar icon, and it draws the page it always
//  drew from inside itself: the page still asks for /api/... and /tiles/..., but a
//  moto:// handler answers those requests in this same process. Nothing on the Mac
//  listens on a port, so there is nothing to wait for and nothing to start first.
//
//  Closing the window only hides it — the alarm and the archive need the app, not the
//  window. Quitting asks first. A crash, or a hang the watchdog notices, ends the app
//  and launchd opens it again at once.
//
import AppKit
import WebKit

@main
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }

    let monitor = Monitor()
    lazy var api = API(monitor: monitor, uiDir: Config.uiDir)
    lazy var pages = PageHandler(api: api)
    var window: NSWindow!
    var web: WKWebView!
    var status: NSStatusItem!
    var flashTimer: Timer?
    var flashOn = false
    var attention: String?          // nil | amber | yellow | red
    var announced = false
    var activity: NSObjectProtocol?
    /// When the main thread last came round, for the watchdog.
    let mainBeat = Locked<TimeInterval>(Clock.uptime())

    static let home = URL(string: "moto://app/")!

    // ---------------------------------------------------------------- launch

    func applicationDidFinishLaunching(_ note: Notification) {
        // One copy at a time: two would both sound the alarm and both keep the archive.
        if !Config.isolated, let other = otherCopy() {
            other.activate(options: [])
            NSApp.terminate(nil)
            return
        }
        // Never napped: the alarm and the relay must keep time with the window hidden.
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep],
                                                         reason: "watching the riders and holding the alarm")
        NSApp.setActivationPolicy(.regular)
        monitor.alarm.bringForward = { [weak self] in self?.showWindow() }
        monitor.start()
        noteAnyRestart()
        buildMenu()
        buildWindow()
        buildStatusItem()
        let beat = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.mainBeat.set(Clock.uptime())
            self.apply(self.monitor.attentionLevel)
        }
        // Common modes: the beat keeps going through menus, drags and the quit question.
        RunLoop.main.add(beat, forMode: .common)
        startWatchdog()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    // Logging out, shutting down and the installer all quit without asking.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply { .terminateNow }

    private func otherCopy() -> NSRunningApplication? {
        guard let id = Bundle.main.bundleIdentifier else { return nil }
        let me = ProcessInfo.processInfo.processIdentifier
        return NSRunningApplication.runningApplications(withBundleIdentifier: id)
            .first { $0.processIdentifier != me && !$0.isTerminated }
    }

    // ---------------------------------------------------------------- the watchdog

    /// A hang the process survives is the failure that bit on 2026-10-02: everything
    /// stopped and nothing noticed. If the relay loop or the main thread stops coming
    /// round for a minute and a half, the app ends itself, and launchd opens it again.
    /// The clock used stops while the Mac sleeps, so waking up is never a hang.
    private func startWatchdog() {
        let monitor = self.monitor, mainBeat = self.mainBeat
        let thread = Thread {
            while true {
                Thread.sleep(forTimeInterval: 10)
                let now = Clock.uptime()
                let relayLoop = now - monitor.pollBeat.get(), mainThread = now - mainBeat.get()
                guard relayLoop > 90 || mainThread > 90 else { continue }
                let what = relayLoop > 90 ? "the relay loop" : "the window"
                let line = "\(what) was stuck for \(Int(max(relayLoop, mainThread))) s — restarted"
                AppLog.write("watchdog: " + line)
                try? line.write(to: Config.support.appendingPathComponent("restarted-after-hang"),
                                atomically: true, encoding: .utf8)
                _exit(70)       // not exit(): nothing may wait on whatever is stuck
            }
        }
        thread.name = "watchdog"
        thread.start()
    }

    private func noteAnyRestart() {
        let marker = Config.support.appendingPathComponent("restarted-after-hang")
        guard let why = try? String(contentsOf: marker, encoding: .utf8) else { return }
        try? FileManager.default.removeItem(at: marker)
        monitor.note("warn", "this app restarted itself: \(why)", tag: "monitor")
    }

    // ---------------------------------------------------------------- the window

    // The window is the app's face, not the app: closing it leaves everything running
    // and keeps the menu-bar icon watching.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.add(self, name: "moto")
        config.setURLSchemeHandler(pages, forURLScheme: "moto")
        // The page reports that it is alive, and any error it hits, so the status file
        // can say from outside whether the window is working.
        config.userContentController.addUserScript(WKUserScript(source: """
            (function () {
              const say = (m) => { try { window.webkit.messageHandlers.moto.postMessage(m); } catch (e) {} };
              addEventListener('error', (e) => say({action: 'page-error',
                text: String(e.message || e) + ' ' + (e.filename || '') + ':' + (e.lineno || '')}));
              addEventListener('unhandledrejection', (e) => say({action: 'page-error', text: 'rejected: ' + String(e.reason)}));
              setInterval(() => say({action: 'page-beat', visibility: document.visibilityState}), 5000);
            })();
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = Config.isolated ? "moto-tracker — demo" : "moto-tracker"
        window.titlebarAppearsTransparent = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = NSColor(red: 0.059, green: 0.067, blue: 0.082, alpha: 1)
        window.minSize = NSSize(width: 1100, height: 680)
        window.contentView = web
        window.delegate = self
        window.setFrameAutosaveName(Config.isolated ? "motoDemoWindow" : "motoMonitorWindow")
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        load()
    }

    func load() {
        web.load(URLRequest(url: AppDelegate.home, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
    }

    /// The page comes from inside the app, so it cannot be "not there yet". If a load
    /// fails anyway, it is simply tried again — never left on an error screen.
    private func retry(_ error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
        if e.domain == "WebKitErrorDomain" && e.code == 102 { return }      // a link handed elsewhere
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.load() }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        retry(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        retry(error)
    }

    // If the page's own process dies, the window would stay blank: draw it again.
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        AppLog.write("the page's process ended; reloading it")
        load()
    }

    // ---------------------------------------------------------------- links and clipboard

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, url.scheme != "moto", url.scheme != "about" {
            if url.scheme == "https" || url.scheme == "http" {
                NSWorkspace.shared.open(url)        // Google Maps opens in the real browser
            }
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, url.scheme == "https" || url.scheme == "http" {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "open":
            if let s = body["url"] as? String, let url = URL(string: s), url.scheme == "https" || url.scheme == "http" {
                NSWorkspace.shared.open(url)
            }
        case "copy":
            if let text = body["text"] as? String {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            }
        case "page-beat":
            monitor.pageNote("beat_at", Clock.now())
            monitor.pageNote("visibility", body["visibility"] as? String ?? "?")
        case "page-error":
            let text = String((body["text"] as? String ?? "?").prefix(300))
            monitor.pageNote("last_error", "\(Clock.now()) \(text)")
            AppLog.write("page error: \(text)")
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
        menu.addItem(withTitle: "Show moto-tracker", action: #selector(showWindow), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit moto-tracker…", action: #selector(confirmQuit(_:)), keyEquivalent: "").target = self
        status.menu = menu
        status.button?.performClick(nil)
        status.menu = nil               // back to click-to-show-window
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reload() { load() }

    /// Quitting stops the alarm and the archive, so it is asked about, never assumed.
    @objc func confirmQuit(_ sender: Any?) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Quit moto-tracker?"
        alert.informativeText = "While it is closed this Mac will not sound the alarm, flash the menu-bar icon"
            + " or keep the archive. It opens again at the next login, or from Applications."
        alert.addButton(withTitle: "Keep running")
        alert.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertSecondButtonReturn {
            NSApp.terminate(nil)
        }
    }

    // ---------------------------------------------------------------- the incident light

    func apply(_ level: String?) {
        if level != attention {
            attention = level
            level == nil ? stopFlashing() : startFlashing()
        }
        // A raised incident pulls the Mac forward: the window comes to the front, and
        // the Dock icon keeps asking until it is looked at.
        if level == "red" && !announced {
            announced = true
            showWindow()
            NSApp.requestUserAttention(.criticalRequest)
        }
        if level == nil { announced = false }
    }

    func startFlashing() {
        guard flashTimer == nil else { return }
        let timer = Timer(timeInterval: 0.55, repeats: true) { [weak self] _ in
            guard let self = self, let button = self.status.button else { return }
            self.flashOn.toggle()
            button.image = self.icon(alert: self.flashOn)
            button.contentTintColor = self.flashOn
                ? (self.attention == "yellow" || self.attention == "amber" ? .systemYellow : .systemRed)
                : nil
        }
        RunLoop.main.add(timer, forMode: .common)
        flashTimer = timer
        timer.fire()
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
        appMenu.addItem(withTitle: "About moto-tracker", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide moto-tracker", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit moto-tracker…", action: #selector(confirmQuit(_:)), keyEquivalent: "q").target = self
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
