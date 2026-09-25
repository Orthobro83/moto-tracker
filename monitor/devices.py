"""
The Devices panel's bridge to the relay.

Pairing and revocation stay off the network API deliberately: the relay has no
admin endpoint, so a stolen device key can never mint another one. The Mini does
this over its own SSH key instead, running relayctl on the VPS.
"""
import json
import shlex
import subprocess
from pathlib import Path
from typing import Optional


class Devices:
    def __init__(self, ssh_key: Path, target: str, enabled: bool = True):
        self.ssh_key = ssh_key
        self.target = target
        self.enabled = enabled

    def _run(self, args: list, timeout: float = 25) -> dict:
        if not self.enabled:
            return {"ok": False, "error": "device management is off in this instance"}
        if not self.ssh_key.exists():
            return {"ok": False, "error": f"no SSH key at {self.ssh_key}"}
        remote = "sudo /usr/local/bin/relayctl " + " ".join(shlex.quote(a) for a in args)
        cmd = ["/usr/bin/ssh", "-i", str(self.ssh_key), "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", self.target, remote]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the VPS did not answer in time"}
        except Exception as e:
            return {"ok": False, "error": repr(e)}
        if p.returncode != 0:
            return {"ok": False, "error": (p.stderr or p.stdout).strip()[:400]}
        return {"ok": True, "stdout": p.stdout.strip()}

    def list(self) -> dict:
        r = self._run(["devices", "--json"])
        if not r["ok"]:
            return r
        try:
            return {"ok": True, "devices": json.loads(r["stdout"] or "[]")}
        except ValueError:
            return {"ok": False, "error": "unexpected answer from relayctl"}

    def pair(self, role: str, name: str, rider: Optional[str] = None) -> dict:
        # Two roles: a phone belongs to its rider and observes the other one.
        if role not in ("rider", "monitor"):
            return {"ok": False, "error": "unknown role"}
        if role == "rider" and rider not in ("jack", "dana"):
            return {"ok": False, "error": "a rider key needs a rider"}
        name = (name or "").strip()[:80]
        if not name:
            return {"ok": False, "error": "give the device a name"}
        args = ["pair", "--role", role, "--name", name, "--json"]
        if rider and role == "rider":
            args += ["--rider", rider]
        r = self._run(args)
        if not r["ok"]:
            return r
        try:
            out = json.loads(r["stdout"])
        except ValueError:
            return {"ok": False, "error": "unexpected answer from relayctl"}
        out["ok"] = True
        return out

    def revoke(self, device_id: int) -> dict:
        r = self._run(["revoke", str(int(device_id))])
        return r if not r["ok"] else {"ok": True, "message": r["stdout"]}
