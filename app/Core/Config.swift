//
//  Where everything lives (design.md 2026-10-04).
//
//  Every value can be overridden from the environment. That is how the end-to-end
//  test and the demo run this same code against a throwaway relay without touching the
//  real archive, the real relay or the Mac's speakers. The installed app sets none of
//  them.
//
import Foundation

enum Config {
    static let env = ProcessInfo.processInfo.environment
    static let home = FileManager.default.homeDirectoryForCurrentUser

    private static func path(_ key: String, _ fallback: URL) -> URL {
        if let value = env[key], !value.isEmpty { return URL(fileURLWithPath: value) }
        return fallback
    }

    static let support = path("MONITOR_SUPPORT",
                              home.appendingPathComponent("Library/Application Support/moto-tracker"))

    // The relay, and how this Mac proves who it is.
    static let relayURL = env["MONITOR_RELAY_URL"] ?? "https://203.0.113.10"
    static let caFile = path("MONITOR_CA", support.appendingPathComponent("pki/ca.crt"))
    static let keyFile = path("MONITOR_KEY", support.appendingPathComponent("relay/monitor.key"))

    // The history lives here, not on the VPS.
    static let archive = path("MONITOR_ARCHIVE", support.appendingPathComponent("moto.db"))
    static let logs = path("MONITOR_LOGS", support.appendingPathComponent("logs"))

    // The map key (never given to the page) and the tile cache.
    static let tomtomKeyFile = path("MONITOR_TOMTOM_KEY",
                                    support.appendingPathComponent("monitor-secrets/tomtom.key"))
    static let tileCache = path("MONITOR_TILE_CACHE", support.appendingPathComponent("tile-cache"))

    // What the app says about itself every few seconds, for the installer and for a
    // look from outside without opening the window.
    static let statusFile = path("MONITOR_STATUS_FILE", support.appendingPathComponent("app-status.json"))

    // The page. Inside the app, from its own Resources; the test points at the source.
    static let uiDir = path("MONITOR_UI",
                            (Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")).appendingPathComponent("ui"))

    // Alarm behaviour. Tests set MUTE and OPEN_APP=0.
    static let alarmMute = env["MONITOR_ALARM_MUTE"] == "1"
    static let openApp = (env["MONITOR_OPEN_APP"] ?? "1") == "1"

    // Pairing and revocation go over the Mini's SSH key, so the relay needs no admin API.
    static let devicesEnabled = (env["MONITOR_DEVICES"] ?? "1") == "1"
    static let sshKey = path("MONITOR_SSH_KEY", home.appendingPathComponent(".ssh/moto_vps_ed25519"))
    static let sshTarget = env["MONITOR_SSH_TARGET"] ?? "moto@203.0.113.10"

    static let weatherURL = env["MONITOR_WEATHER_URL"] ?? "https://api.open-meteo.com/v1/forecast"

    /// A demo or a test runs on its own data beside the real app, never instead of it.
    static let isolated = env["MONITOR_SUPPORT"] != nil

    // Cadences.
    static let stateEvery: TimeInterval = 1.0
    static let syncEvery: TimeInterval = 5.0
    static let weatherEvery: TimeInterval = 600     // design: poll weather every 10–15 minutes
    static let windyKmh = 30.0                      // sustained wind at or above this reads "Windy"
}
