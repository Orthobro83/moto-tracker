# WARNING: UNTESTED SAFETY LOGIC. DO NOT RELY ON THIS.
#
# This crash detection has never been tested against a real crash, because no
# crash data exists. It has only been tuned to stay quiet during normal riding.
# It may miss a real crash entirely, fire when nothing happened, or fail
# silently because of a dead battery, lost signal, OS power management or a bug.
# It is not a safety device, emergency service or substitute for one. Nobody
# should rely on it, ever, for anyone's safety. See the README.
"""
The Mini's alarm.

An open, unsilenced incident sounds through the Mac's speakers until an Observer
silences it — the background monitor owns the sound, so it is heard whether or
not the window is open (design.md 2026-09-15). Raising the alarm also wakes the
display and brings the app forward.

Silence is shared: the relay records it, so silencing on the Observer phone stops
this too. This module only follows what /state reports.
"""
import math
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Optional

# What the alarm is sounding for. An incident outranks a test: a test can never
# take the alarm away from a real incident, and an incident sounding over a test
# takes ownership of it, so the test's own timeout cannot silence the incident.
PRIORITY = {None: 0, "test": 1, "incident": 2}

SAMPLE_RATE = 22050
MIN_VOLUME = 0.7          # an alarm at 10% volume is not an alarm
TONE = [(880, 0.35), (0, 0.06), (660, 0.35), (0, 0.24)]   # two-tone, ~1 s per cycle


def build_sound(path: Path, cycles: int = 6) -> Path:
    """Writes the alarm WAV once. No asset to ship, nothing to go missing."""
    if path.exists() and path.stat().st_size > 1000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for _ in range(cycles):
        for freq, secs in TONE:
            n = int(SAMPLE_RATE * secs)
            for i in range(n):
                if freq:
                    # A short fade at each end keeps the tone from clicking.
                    edge = min(1.0, i / 220, (n - i) / 220)
                    v = int(26000 * edge * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
                else:
                    v = 0
                frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return path


def _osascript(script: str) -> Optional[str]:
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


class Alarm:
    """Sounds while `set(True)`. Idempotent: calling set repeatedly does nothing new."""

    def __init__(self, sound: Path, mute: bool = False, open_app: Optional[str] = None):
        self.sound = sound
        self.mute = mute
        self.open_app = open_app
        self.sounding = False
        self.reason = None
        self.reason_at = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._prior_volume: Optional[str] = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- control

    def set(self, on: bool, reason: str = None) -> None:
        with self._lock:
            if on and not self.sounding:
                self.sounding, self.reason = True, reason
                self._start()
            elif on and PRIORITY.get(reason, 0) > PRIORITY.get(self.reason, 0):
                # Already sounding for something lesser — an incident arriving during
                # a test takes it over, so only a silence can stop it now.
                self.reason = reason
            elif not on and self.sounding:
                self.sounding, self.reason = False, None
                self._stop_sound()

    def test(self, limit: float = 120.0) -> bool:
        """The Test alarm button. It sounds the way a real alarm sounds — over and
        over until someone stops it — because a single beep proves only the speaker.
        Pressing it again stops it, and it stops itself after two minutes so a test
        can never be left running. Returns whether it is now sounding.

        It will not touch an alarm that is sounding for an incident: that one stops
        only by being silenced, which is recorded and shared with the Observer phone.
        """
        if self.sounding and self.reason != "test":
            return True
        if self.sounding:
            self.set(False)
            return False
        self.set(True, "test")
        started = self.reason_at = time.time()

        def timeout():
            while time.time() - started < limit:
                if not self.sounding or self.reason != "test" or self.reason_at != started:
                    return
                time.sleep(0.25)
            # Only ever stops its own test — never an incident that took over.
            if self.sounding and self.reason == "test":
                self.set(False)
        threading.Thread(target=timeout, daemon=True).start()
        return True

    def attention(self) -> None:
        """Wake the display and put the window in front of whatever is on screen."""
        if self.mute:
            return
        try:
            subprocess.Popen(["/usr/bin/caffeinate", "-u", "-t", "10"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if self.open_app:
            try:
                subprocess.Popen(["/usr/bin/open", "-a", self.open_app],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    # ---------------------------------------------------------------- sound

    def _start(self) -> None:
        if self.mute:
            return
        current = _osascript("output volume of (get volume settings)")
        if current and current.isdigit() and int(current) < MIN_VOLUME * 100:
            self._prior_volume = current
            _osascript(f"set volume output volume {int(MIN_VOLUME * 100)}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(["/usr/bin/afplay", str(self.sound)],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while self._proc.poll() is None:
                    if self._stop.wait(0.2):
                        self._proc.terminate()
                        return
            except Exception:
                self._stop.wait(1.0)

    def _stop_sound(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self._prior_volume is not None:
            _osascript(f"set volume output volume {self._prior_volume}")
            self._prior_volume = None
