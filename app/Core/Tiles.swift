//
//  TomTom raster tiles, fetched for the page.
//
//  The page asks for /tiles/...; the app adds the key and caches the answer. The key
//  never reaches the page, so it cannot leak through a screenshot, the web inspector
//  or a copied URL. Map tiles are cached for a month, traffic for two minutes —
//  traffic is the whole point of the overlay.
//
import CryptoKit
import Foundation

final class Tiles {
    static let mapTTL = 30 * 86400
    static let trafficTTL = 120
    // 1×1 transparent PNG: what a failed tile looks like, so the map never shows a hole.
    static let blank = Data(hex: "89504e470d0a1a0a0000000d494844520000000100000001080600000"
                               + "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
                               + "0000000049454e44ae426082")

    let keyFile: URL
    let cache: URL
    private let key = Locked<String?>(nil)
    private let session: URLSession

    init(keyFile: URL, cache: URL) {
        self.keyFile = keyFile
        self.cache = cache
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpMaximumConnectionsPerHost = 8
        session = URLSession(configuration: config)
    }

    private func readKey() -> String {
        if let k = key.get() { return k }
        let k = (try? String(contentsOf: keyFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        key.set(k)
        return k
    }

    func available() -> Bool { FileManager.default.fileExists(atPath: keyFile.path) && !readKey().isEmpty }

    /// Hands back (png, max-age seconds). Never fails: the map keeps working.
    func get(layer: String, z: Int, x: Int, y: Int, style: String, done: @escaping (Data, Int) -> Void) {
        var style = style
        let url: String
        let ttl: Int
        if layer == "traffic" {
            if !["relative0", "relative0-dark", "absolute", "relative-delay"].contains(style) { style = "relative0" }
            url = "https://api.tomtom.com/traffic/map/4/tile/flow/\(style)/\(z)/\(x)/\(y).png"
            ttl = Tiles.trafficTTL
        } else {
            if !["main", "night"].contains(style) { style = "main" }
            url = "https://api.tomtom.com/map/1/tile/basic/\(style)/\(z)/\(x)/\(y).png"
            ttl = Tiles.mapTTL
        }
        guard (0...22).contains(z), x >= 0, y >= 0, x < (1 << z), y < (1 << z) else {
            done(Tiles.blank, 60)
            return
        }
        let path = cachePath(layer, style, z, x, y)
        if let attributes = try? FileManager.default.attributesOfItem(atPath: path.path),
           let modified = attributes[.modificationDate] as? Date,
           Date().timeIntervalSince(modified) < Double(ttl),
           let data = try? Data(contentsOf: path) {
            done(data, ttl)
            return
        }
        let k = readKey()
        guard !k.isEmpty, let full = URL(string: url + "?key=" + k) else {
            done(Tiles.blank, 60)
            return
        }
        session.dataTask(with: URLRequest(url: full, timeoutInterval: 15)) { data, response, _ in
            guard let data = data, let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                // Stale beats blank: an old tile still shows the road.
                if let old = try? Data(contentsOf: path) { done(old, 30) } else { done(Tiles.blank, 30) }
                return
            }
            try? FileManager.default.createDirectory(at: path.deletingLastPathComponent(),
                                                     withIntermediateDirectories: true)
            let tmp = path.deletingPathExtension().appendingPathExtension("tmp")
            if (try? data.write(to: tmp)) != nil { rename(tmp.path, path.path) }
            done(data, ttl)
        }.resume()
    }

    private func cachePath(_ layer: String, _ style: String, _ z: Int, _ x: Int, _ y: Int) -> URL {
        // Two hashed levels keep any one directory small.
        let h = SHA256.hash(data: Data("\(layer)/\(style)/\(z)/\(x)/\(y)".utf8))
            .map { String(format: "%02x", $0) }.joined()
        let a = String(h.prefix(2)), b = String(h.dropFirst(2).prefix(2))
        return cache.appendingPathComponent(layer).appendingPathComponent(a).appendingPathComponent(b)
            .appendingPathComponent("\(h).png")
    }

    /// Drops the oldest tiles if the cache outgrows its budget.
    @discardableResult
    func sweep(maxBytes: Int = 1_500_000_000) -> Int {
        guard let walker = FileManager.default.enumerator(at: cache,
                                                          includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey]) else { return 0 }
        var files: [(Date, Int, URL)] = []
        for case let url as URL in walker where url.pathExtension == "png" {
            let values = try? url.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
            files.append((values?.contentModificationDate ?? .distantPast, values?.fileSize ?? 0, url))
        }
        var total = files.reduce(0) { $0 + $1.1 }
        var removed = 0
        for (_, size, url) in files.sorted(by: { $0.0 < $1.0 }) {
            if total <= maxBytes { break }
            if (try? FileManager.default.removeItem(at: url)) != nil {
                total -= size
                removed += 1
            }
        }
        return removed
    }
}

extension Data {
    init(hex: String) {
        var bytes: [UInt8] = []
        var i = hex.startIndex
        while i < hex.endIndex, let j = hex.index(i, offsetBy: 2, limitedBy: hex.endIndex) {
            if let b = UInt8(hex[i..<j], radix: 16) { bytes.append(b) }
            i = j
        }
        self.init(bytes)
    }
}
