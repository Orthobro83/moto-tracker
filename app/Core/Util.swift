//
//  Small shared pieces: JSON in and out, the relay's timestamp format, and the idea
//  of "empty" that the relay's answers were written against (Python's).
//
import Foundation

typealias JSON = [String: Any]

/// JSON null. A Swift dictionary cannot hold nil, and the page and the relay both
/// expect the key to be there with null in it.
let jnull = NSNull()

func orNull(_ value: Any?) -> Any { value ?? jnull }

func isNull(_ value: Any?) -> Bool { value == nil || value is NSNull }

func isBool(_ value: Any?) -> Bool {
    guard let n = value as? NSNumber else { return false }
    return CFGetTypeID(n) == CFBooleanGetTypeID()
}

func dbl(_ value: Any?) -> Double? {
    guard let n = value as? NSNumber, !isBool(n) else { return nil }
    return n.doubleValue
}

func int(_ value: Any?) -> Int? {
    if let n = value as? NSNumber, !isBool(n) { return n.intValue }
    if let s = value as? String { return Int(s.trimmingCharacters(in: .whitespaces)) }
    return nil
}

/// Python's truth test: None, 0, "", [] and {} are all false.
func truthy(_ value: Any?) -> Bool {
    switch value {
    case nil, is NSNull: return false
    case let n as NSNumber: return n.doubleValue != 0
    case let s as String: return !s.isEmpty
    case let a as [Any]: return !a.isEmpty
    case let d as [String: Any]: return !d.isEmpty
    default: return true
    }
}

/// Python's str() of a value, for the log files, which Python always wrote.
func pyStr(_ value: Any?) -> String {
    switch value {
    case nil, is NSNull: return "None"
    case let n as NSNumber where isBool(n): return n.boolValue ? "True" : "False"
    case let n as NSNumber:
        guard CFNumberIsFloatType(n) else { return "\(n.int64Value)" }
        let d = n.doubleValue
        return d == d.rounded() && abs(d) < 1e16 ? String(format: "%.1f", d) : "\(d)"
    case let s as String: return s
    default: return "\(value!)"
    }
}

func ljust(_ s: String, _ width: Int) -> String {
    s.count >= width ? s : s + String(repeating: " ", count: width - s.count)
}

func round2(_ x: Double) -> Double { (x * 100).rounded() / 100 }
func round1(_ x: Double) -> Double { (x * 10).rounded() / 10 }

/// A value several threads read and write, behind its own small lock. Used only for
/// things that must stay readable even if everything else is stuck — the watchdog's
/// heartbeats — and for one-off results handed between threads.
final class Locked<T> {
    private var value: T
    private let lock = NSLock()
    init(_ value: T) { self.value = value }
    func get() -> T { lock.lock(); defer { lock.unlock() }; return value }
    func set(_ newValue: T) { lock.lock(); value = newValue; lock.unlock() }
}

/// An object whose keys must keep their order on the way out: the riders, so the page
/// sees Jack before Dana exactly as the relay sends them.
struct Ordered {
    var pairs: [(String, Any)]
}

enum Json {
    static func encode(_ value: Any?) -> Data { Data(text(value).utf8) }

    static func text(_ value: Any?) -> String {
        var out = ""
        write(value, into: &out)
        return out
    }

    static func parse(_ data: Data?) -> Any? {
        guard let data = data, !data.isEmpty else { return nil }
        return try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
    }

    private static func write(_ value: Any?, into out: inout String) {
        switch value {
        case nil, is NSNull:
            out += "null"
        case let o as Ordered:
            out += "{"
            for (i, pair) in o.pairs.enumerated() {
                if i > 0 { out += "," }
                quote(pair.0, into: &out)
                out += ":"
                write(pair.1, into: &out)
            }
            out += "}"
        case let d as [String: Any]:
            out += "{"
            for (i, key) in d.keys.sorted().enumerated() {
                if i > 0 { out += "," }
                quote(key, into: &out)
                out += ":"
                write(d[key], into: &out)
            }
            out += "}"
        case let a as [Any]:
            out += "["
            for (i, v) in a.enumerated() {
                if i > 0 { out += "," }
                write(v, into: &out)
            }
            out += "]"
        case let s as String:
            quote(s, into: &out)
        case let n as NSNumber:
            if isBool(n) {
                out += n.boolValue ? "true" : "false"
            } else if CFNumberIsFloatType(n) {
                let d = n.doubleValue
                out += d.isFinite ? "\(d)" : "null"
            } else {
                out += "\(n.int64Value)"
            }
        case let date as Date:
            quote(Clock.iso(date), into: &out)
        default:
            quote("\(value!)", into: &out)
        }
    }

    private static func quote(_ s: String, into out: inout String) {
        out += "\""
        for u in s.unicodeScalars {
            switch u {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if u.value < 0x20 || u.value == 0x2028 || u.value == 0x2029 {
                    out += String(format: "\\u%04x", u.value)
                } else {
                    out.unicodeScalars.append(u)
                }
            }
        }
        out += "\""
    }
}

enum Clock {
    private static func formatter(_ format: String, local: Bool = false) -> DateFormatter {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = local ? TimeZone.current : TimeZone(identifier: "UTC")
        f.dateFormat = format
        return f
    }

    private static let millis = formatter("yyyy-MM-dd'T'HH:mm:ss.SSSxxx")
    private static let whole = formatter("yyyy-MM-dd'T'HH:mm:ssxxx")
    private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// The relay's own format: UTC, to the millisecond, "+00:00".
    static func iso(_ date: Date = Date()) -> String { millis.string(from: date) }
    static func isoSeconds(_ date: Date = Date()) -> String { whole.string(from: date) }
    static func now() -> String { iso() }

    static func parse(_ value: Any?) -> Date? {
        guard var s = value as? String, s.count >= 19 else { return nil }
        // No offset at all: read it as UTC, which is what the relay always means.
        if !(s.hasSuffix("Z") || s.range(of: #"[+-]\d\d:?\d\d$"#, options: .regularExpression) != nil) {
            s += "Z"
        }
        // Python writes microseconds unless told otherwise; three places are plenty.
        if let r = s.range(of: #"\.\d{4,}"#, options: .regularExpression) {
            s.replaceSubrange(r, with: String(s[r].prefix(4)))
        }
        return fractional.date(from: s) ?? plain.date(from: s)
    }

    /// A time as this Mac's clock shows it, for log files and file names.
    static func local(_ date: Date, _ format: String) -> String {
        formatter(format, local: true).string(from: date)
    }

    /// Seconds on a clock that stops while the Mac sleeps — for heartbeats, so waking
    /// up is never mistaken for a hang.
    static func uptime() -> TimeInterval { Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000 }
}

/// The app's own log, for what happens to the app itself (a restart after a hang).
/// The running log the page shows is the monitor's, and lives in memory.
enum AppLog {
    private static let lock = NSLock()
    static let file = Config.home.appendingPathComponent("Library/Logs/moto-tracker-app.log")

    static func write(_ line: String) {
        lock.lock()
        defer { lock.unlock() }
        let text = "\(Clock.now())  \(line)\n"
        FileHandle.standardError.write(Data(text.utf8))
        guard !Config.isolated else { return }
        if let handle = try? FileHandle(forWritingTo: file) {
            handle.seekToEndOfFile()
            handle.write(Data(text.utf8))
            try? handle.close()
        } else {
            try? text.write(to: file, atomically: true, encoding: .utf8)
        }
    }
}
