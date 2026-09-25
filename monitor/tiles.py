"""
TomTom raster tiles, proxied.

The UI asks the Mini for tiles; the Mini adds the key and caches the answer. The
key never reaches the page, so it cannot leak through a screenshot, the web
inspector or a copied URL. Map tiles are cached for a month, traffic for two
minutes — traffic is the whole point of the overlay.
"""
import hashlib
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

MAP_URL = "https://api.tomtom.com/map/1/tile/basic/{style}/{z}/{x}/{y}.png"
TRAFFIC_URL = "https://api.tomtom.com/traffic/map/4/tile/flow/{style}/{z}/{x}/{y}.png"
MAP_TTL = 30 * 86400
TRAFFIC_TTL = 120
# 1×1 transparent PNG: what a failed tile looks like, so the map never shows a hole.
BLANK = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


class Tiles:
    def __init__(self, key_file: Path, cache_dir: Path):
        self.key_file = key_file
        self.cache = cache_dir
        self._key: Optional[str] = None

    def available(self) -> bool:
        return self.key_file.exists() and bool(self._read_key())

    def _read_key(self) -> str:
        if self._key is None:
            try:
                self._key = self.key_file.read_text().strip()
            except OSError:
                self._key = ""
        return self._key

    def get(self, layer: str, z: int, x: int, y: int, style: str = "main") -> Tuple[bytes, int]:
        """Returns (png bytes, max-age seconds). Never raises: the map keeps working."""
        if layer == "traffic":
            url, ttl = TRAFFIC_URL, TRAFFIC_TTL
            style = style if style in ("relative0", "relative0-dark", "absolute", "relative-delay") else "relative0"
        else:
            url, ttl = MAP_URL, MAP_TTL
            style = style if style in ("main", "night") else "main"
        if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            return BLANK, 60
        path = self._cache_path(layer, style, z, x, y)
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            try:
                return path.read_bytes(), ttl
            except OSError:
                pass
        key = self._read_key()
        if not key:
            return BLANK, 60
        full = url.format(style=style, z=z, x=x, y=y) + "?key=" + key
        try:
            with urllib.request.urlopen(full, timeout=15) as r:
                data = r.read()
        except Exception:
            # Stale beats blank: an old tile still shows the road.
            if path.exists():
                try:
                    return path.read_bytes(), 30
                except OSError:
                    pass
            return BLANK, 30
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            pass
        return data, ttl

    def _cache_path(self, layer: str, style: str, z: int, x: int, y: int) -> Path:
        # Two hashed levels keep any one directory small.
        h = hashlib.sha256(f"{layer}/{style}/{z}/{x}/{y}".encode()).hexdigest()
        return self.cache / layer / h[:2] / h[2:4] / f"{h}.png"

    def sweep(self, max_bytes: int = 1_500_000_000) -> int:
        """Drops the oldest tiles if the cache outgrows its budget."""
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.cache.rglob("*.png")]
        total = sum(f[1] for f in files)
        removed = 0
        for _, size, p in sorted(files):
            if total <= max_bytes:
                break
            try:
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
        return removed
