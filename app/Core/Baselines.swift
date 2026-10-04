//
//  What normal riding looks like, per rider, computed from this Mac's archive and
//  pushed to the relay.
//
//  One source of truth, deliberately. The phone judges a ride against these numbers,
//  and the relay uses the same ones to decide whether a silence followed something
//  violent — if the two disagreed, a crash could be a crash on one machine and
//  ordinary riding on the other.
//
//  The archivist Mac is the only machine that can compute them: it is the only one
//  that holds the history. They are recomputed as that history grows, so a new bike,
//  a new mount or a different riding style moves the thresholds with it. Mirrors
//  analysis/baseline.py, the readable version for looking at the data by hand.
//
import Foundation

enum Baselines {
    static let movingKmh = 20.0          // below this a rider is filtering, parking or stopped
    // design.md, "Baseline epoch": everything before this date was recorded while the
    // pipeline itself was being debugged and does not describe riding.
    static let epoch = "2026-09-11"
    // A real incident is not ordinary riding. If its windows were counted as normal,
    // the bar would rise by exactly the amount of the crash. Retracted candidates stay
    // in: those WERE ordinary riding.
    static let incidentMarginS = 120.0
    static let minMovingSamples = 200    // ~17 minutes of riding before a rider's own numbers count
    static let impactGFloor = 11.0       // the app's floors, repeated here so both ends agree
    static let impactRotFloor = 8.0
    static let margin = 1.5              // how far outside their own worst riding an impact must sit
    // Below riding speed, only a vehicle-sized blow counts (Jack, 2026-09-16).
    static let rearEndMargin = 1.8
    static let rearEndGFloor = 20.0

    // Until a rider has ridden enough, they are judged by the harder of the two known
    // riders' baselines.
    static let unknown: JSON = ["impact_g": 13.2, "impact_rot": 9.1, "rearend_g": 23.8, "decel_kmh_s": 6.7,
                                "moving_h": 0.0, "source": "default (not enough riding yet)"]

    static func pct(_ values: [Double], _ p: Double) -> Double? {
        guard !values.isEmpty else { return nil }
        let k = max(0, min(values.count - 1, Int((p / 100 * Double(values.count)).rounded(.up)) - 1))
        return values[k]
    }

    private static func shift(_ iso: Any?, _ seconds: Double) -> String? {
        Clock.parse(iso).map { Clock.iso($0.addingTimeInterval(seconds)) }
    }

    /// This rider's thresholds, from every archived ride of theirs since the epoch,
    /// with confirmed incidents left out.
    static func compute(_ archive: Archive, _ rider: String) throws -> JSON {
        try archive.use { db in
            var rows = try db.query("SELECT p.speed, p.peak_g, p.peak_rot, p.received_at FROM positions p"
                                    + " WHERE p.rider_id=? AND p.speed >= ? AND p.received_at >= ?",
                                    [rider, movingKmh, epoch])
            var skip: [(String, String)] = []
            for r in try db.query("SELECT raised_at, COALESCE(closed_at, resolved_at, raised_at) AS ended"
                                  + " FROM incidents WHERE rider_id=? AND state NOT IN ('retracted')", [rider]) {
                if let start = shift(r["raised_at"], -incidentMarginS), let end = shift(r["ended"], incidentMarginS) {
                    skip.append((start, end))
                }
            }
            if !skip.isEmpty {
                rows = rows.filter { row in
                    let at = row["received_at"] as? String ?? ""
                    return !skip.contains { $0.0 <= at && at <= $0.1 }
                }
            }
            let gs = rows.compactMap { dbl($0["peak_g"]) }.sorted()
            let rots = rows.compactMap { dbl($0["peak_rot"]) }.sorted()
            if gs.count < minMovingSamples || rots.count < minMovingSamples { return unknown }

            let worstG = max(gs.last!, pct(gs, 99.9) ?? 0)
            let worstRot = max(rots.last!, pct(rots, 99.9) ?? 0)
            // Hardest ordinary braking, km/h lost per second, over consecutive windows.
            var decel: [Double] = []
            let trips = try db.query("SELECT DISTINCT trip_id AS id FROM positions WHERE rider_id=?"
                                     + " AND trip_id IS NOT NULL", [rider]).compactMap { int($0["id"]) }
            for trip in trips {
                let speeds = try db.query("SELECT speed FROM positions WHERE trip_id=? ORDER BY id", [trip])
                    .map { dbl($0["speed"]) ?? 0 }
                for i in speeds.indices.dropFirst() {
                    let a = speeds[i - 1], b = speeds[i]
                    if a >= movingKmh && a > b { decel.append((a - b) / 5.0) }
                }
            }
            decel.sort()
            let impactG = max(worstG * margin, impactGFloor)
            return [
                "impact_g": round2(impactG),
                "impact_rot": round2(max(worstRot * margin, impactRotFloor)),
                "rearend_g": round2(max(impactG * rearEndMargin, rearEndGFloor)),
                "decel_kmh_s": orNull(decel.last.map(round2)),
                "moving_h": round2(Double(gs.count) * 5 / 3600),
                "source": "\(gs.count) moving windows since \(epoch)"
                    + (skip.isEmpty ? "" : ", \(skip.count) incident(s) left out"),
            ]
        }
    }

    /// Computes and sends. Returns what was sent, for the log.
    static func push(_ archive: Archive, _ relay: Relay, riders: [String] = ["jack", "dana"]) throws -> JSON {
        var sent: JSON = [:]
        for rider in riders {
            var b = try compute(archive, rider)
            var body = b
            body["rider"] = rider
            let (status, _, _) = relay.call("POST", "/baseline", body, timeout: 15)
            b["ok"] = status == 200
            b["status"] = status
            sent[rider] = b
        }
        return sent
    }

    static func describe(_ b: JSON) -> String {
        String(format: "impact %.1f g / %.1f rad/s, %.1f g below riding speed, from %.1f h of riding",
               dbl(b["impact_g"]) ?? 0, dbl(b["impact_rot"]) ?? 0, dbl(b["rearend_g"]) ?? 0,
               dbl(b["moving_h"]) ?? 0)
    }
}
