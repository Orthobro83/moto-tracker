"""
Settings for the moto-tracker monitor on the Mac Mini (design.md 2026-09-15).

Every value can be overridden from the environment. That is how the end-to-end
test runs a throwaway monitor against a throwaway relay without touching the real
archive, the real relay or the Mac's speakers.
"""
import os
from pathlib import Path

SUPPORT = Path(os.environ.get("MONITOR_SUPPORT", str(Path.home() / "Library/Application Support/moto-tracker")))

# The relay, and how the monitor proves who it is.
RELAY_URL = os.environ.get("MONITOR_RELAY_URL", "https://203.0.113.10")
CA_FILE = Path(os.environ.get("MONITOR_CA", str(SUPPORT / "pki/ca.crt")))
KEY_FILE = Path(os.environ.get("MONITOR_KEY", str(SUPPORT / "relay/monitor.key")))

# The history lives here, not on the VPS.
ARCHIVE = Path(os.environ.get("MONITOR_ARCHIVE", str(SUPPORT / "moto.db")))
LOGS = Path(os.environ.get("MONITOR_LOGS", str(SUPPORT / "logs")))

# Code, UI, the alarm sound and the tile cache.
RUNTIME = Path(os.environ.get("MONITOR_RUNTIME", str(Path(__file__).resolve().parent)))
TOMTOM_KEY_FILE = Path(os.environ.get("MONITOR_TOMTOM_KEY", str(SUPPORT / "monitor-secrets/tomtom.key")))
TILE_CACHE = Path(os.environ.get("MONITOR_TILE_CACHE", str(SUPPORT / "tile-cache")))

# The local interface. Loopback only: nothing on the Mini listens on the network.
HOST = "127.0.0.1"
PORT = int(os.environ.get("MONITOR_PORT", "8089"))

# Alarm behaviour. Tests set MUTE and OPEN_APP=0.
ALARM_MUTE = os.environ.get("MONITOR_ALARM_MUTE") == "1"
OPEN_APP = os.environ.get("MONITOR_OPEN_APP", "1") == "1"
APP_PATH = os.environ.get("MONITOR_APP", "/Applications/moto-tracker.app")

# Pairing and revocation go over the Mini's SSH key, so the relay needs no admin API.
DEVICES_ENABLED = os.environ.get("MONITOR_DEVICES", "1") == "1"
SSH_KEY = Path(os.environ.get("MONITOR_SSH_KEY", str(Path.home() / ".ssh/moto_vps_ed25519")))
SSH_TARGET = os.environ.get("MONITOR_SSH_TARGET", "moto@203.0.113.10")

# Cadences.
STATE_EVERY_S = 1.0
SYNC_EVERY_S = 5.0
WEATHER_EVERY_S = 600          # design: poll weather every 10–15 minutes
WINDY_KMH = 30                 # sustained wind at or above this reads "Windy"
