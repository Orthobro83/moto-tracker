"""
The monitor's connection to the relay: plain HTTPS with the Mini's monitor key,
trusting only our own CA. Standard library only.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional, Tuple


class Relay:
    def __init__(self, url: str, key_file: Path, ca_file: Path):
        self.url = url.rstrip("/")
        self.key_file = key_file
        self.ctx = ssl.create_default_context(cafile=str(ca_file)) if self.url.startswith("https") else None

    def has_key(self) -> bool:
        return self.key_file.exists() and bool(self._key())

    def _key(self) -> str:
        try:
            return self.key_file.read_text().strip()
        except OSError:
            return ""            # not paired yet: the relay answers 401 and the page says so

    def call(self, method: str, path: str, body: Optional[dict] = None,
             timeout: float = 10) -> Tuple[int, object, float]:
        """Returns (status, parsed body, round-trip ms). Status 0 means unreachable."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self._key()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                text, status = r.read().decode("utf-8", "replace"), r.status
        except urllib.error.HTTPError as e:
            text, status = e.read().decode("utf-8", "replace"), e.code
        except Exception as e:  # unreachable, timed out, TLS failure
            return 0, repr(e), (time.monotonic() - started) * 1000
        ms = (time.monotonic() - started) * 1000
        try:
            return status, (json.loads(text) if text else None), ms
        except ValueError:
            return status, text, ms

    def pair(self, code: str, name: str, timeout: float = 20) -> Tuple[int, object]:
        """Exchanges a pairing code for this Mac's device key. No key needed: this is
        how a second Mac joins without anyone touching the VPS."""
        data = json.dumps({"code": code, "name": name}).encode()
        req = urllib.request.Request(self.url + "/pair", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, body
        except Exception as e:
            return 0, repr(e)

    def save_key(self, key: str) -> None:
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        try:
            self.key_file.write_text(key)
            self.key_file.chmod(0o600)
        finally:
            os.umask(old)

    def stream(self, path: str = "/events") -> Iterator[dict]:
        """Server-sent events, parsed. Ends (or raises) when the connection drops.
        The relay sends a keepalive every 15 s, so a 60 s read timeout means dead."""
        req = urllib.request.Request(self.url + path, headers={"Authorization": "Bearer " + self._key()})
        with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except ValueError:
                        continue
