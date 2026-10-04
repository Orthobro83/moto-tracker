//
//  Weather for the map inset: Open-Meteo, no account and no key.
//
//  Coordinates are rounded to two decimals (about a kilometre) before they leave the
//  Mac, so a rider's exact position is never sent to a weather service.
//
import Foundation

enum Weather {
    static let fields = "temperature_2m,apparent_temperature,precipitation,rain,weather_code,"
        + "cloud_cover,wind_speed_10m,wind_gusts_10m,is_day,relative_humidity_2m"

    // Below this the model has a trace, and nothing a rider would call rain.
    static let wetMM = 0.2

    // WMO weather codes → what a rider needs to know.
    static let codes: [Int: (String, String)] = [
        0: ("Clear", "clear"), 1: ("Mainly clear", "clear"), 2: ("Partly cloudy", "cloud"),
        3: ("Overcast", "cloud"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
        51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
        56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
        61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
        66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
        71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
        77: ("Snow grains", "snow"), 80: ("Light showers", "rain"), 81: ("Showers", "rain"),
        82: ("Violent showers", "rain"), 85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
        95: ("Thunderstorm", "storm"), 96: ("Thunderstorm, hail", "storm"),
        99: ("Thunderstorm, hail", "storm"),
    ]

    /// Label and icon. **Rain is only claimed when there is rain to claim** (Jack,
    /// 2026-09-20): the pane said "Light drizzle" over a dry, partly sunny ride with
    /// 0.1 mm behind it, and "Thunderstorm" over a dry one the day before. The code is
    /// the model's opinion; precipitation and cloud cover are quantities it carries.
    /// Fog is the exception — it is about seeing, not wetness.
    static func describe(code: Int, day: Bool, precip: Double, cloud: Int) -> (String, String) {
        if code == 45 || code == 48 { return (codes[code]!.0, "fog") }
        if precip >= wetMM {
            let (label, icon) = codes[code] ?? ("Rain", "rain")
            return (label, ["rain", "snow", "storm"].contains(icon) ? icon : "rain")
        }
        if cloud < 0 { return ("—", day ? "clear" : "night") }
        if cloud < 20 { return day ? ("Sunny", "clear") : ("Clear", "night") }
        if cloud < 60 { return day ? ("Partly cloudy", "clear") : ("Partly cloudy", "night") }
        if cloud < 88 { return ("Cloudy", "cloud") }
        return ("Overcast", "cloud")
    }

    /// Current conditions at (roughly) this position, or nil if unreachable.
    static func fetch(lat: Double, lon: Double, windyKmh: Double = 30, timeout: TimeInterval = 10) -> JSON? {
        let la = round2(lat), lo = round2(lon)
        guard var parts = URLComponents(string: Config.weatherURL) else { return nil }
        parts.queryItems = [
            URLQueryItem(name: "latitude", value: "\(la)"), URLQueryItem(name: "longitude", value: "\(lo)"),
            URLQueryItem(name: "current", value: fields), URLQueryItem(name: "wind_speed_unit", value: "kmh"),
            URLQueryItem(name: "timezone", value: "auto"),
        ]
        guard let url = parts.url else { return nil }
        let done = DispatchSemaphore(value: 0)
        let answer = Locked<Data?>(nil)
        let task = URLSession.shared.dataTask(with: URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData,
                                                               timeoutInterval: timeout)) { data, response, _ in
            if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) { answer.set(data) }
            done.signal()
        }
        task.resume()
        if done.wait(timeout: .now() + timeout + 5) == .timedOut {
            task.cancel()
            return nil
        }
        guard let data = Json.parse(answer.get()) as? JSON else { return nil }
        let cur = data["current"] as? JSON ?? [:]
        let code = int(cur["weather_code"]) ?? -1
        let day = isNull(cur["is_day"]) ? true : truthy(cur["is_day"])
        let wind = dbl(cur["wind_speed_10m"]) ?? 0
        let gust = dbl(cur["wind_gusts_10m"]) ?? 0
        let precip = dbl(cur["precipitation"]) ?? 0
        let cloud = int(cur["cloud_cover"])
        let (label, icon) = describe(code: code, day: day, precip: precip, cloud: cloud ?? -1)
        // A rider feels wind before they read a label, so it outranks a clear sky.
        var headline = label
        if !["rain", "storm", "snow"].contains(icon) && (wind >= windyKmh || gust >= windyKmh * 1.6) {
            headline = "Windy"
        }
        return [
            "headline": headline, "label": label, "icon": icon, "cloud_pct": orNull(cloud),
            "temp_c": orNull(cur["temperature_2m"]), "feels_c": orNull(cur["apparent_temperature"]),
            "wind_kmh": wind, "gust_kmh": gust, "humidity": orNull(cur["relative_humidity_2m"]),
            "precip_mm": orNull(cur["precipitation"]), "is_day": day,
            "at": orNull(cur["time"]), "code": code,
            // When this Mac actually got it, which is not the same as the model's own
            // timestamp — and is what says whether it is still worth showing.
            "fetched_at": Clock.isoSeconds(),
            "lat": la, "lon": lo,
        ]
    }
}
