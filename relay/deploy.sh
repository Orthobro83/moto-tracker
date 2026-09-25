#!/usr/bin/env bash
# Deploy the relay from the Mini to the VPS (design.md 2026-09-15; checklist
# Phase 2b, M1).
#
#   ./deploy.sh            normal deploy
#   ./deploy.sh --force    deploy even while a trip is open — only when told to
#
# 1. Refuses while any real trip is open, or while a real position arrived in the
#    last 5 minutes. It reads the relay's database directly over SSH, so it works
#    even when the relay itself is down.
# 2. Installs the release beside the previous ones (the five newest are kept) and
#    switches to it.
# 3. Runs smoke.py from the Mini over HTTPS. If any check fails, switches back to
#    the previous release and restarts it.
set -euo pipefail
cd "$(dirname "$0")"

VPS="moto@***.*.***.**"
URL="https://***.*.***.**"
SSH_KEY="$HOME/.ssh/****_***_*******"
SUPPORT="$HOME/Library/Application Support/moto-tracker"
CA="$SUPPORT/pki/ca.crt"
SMOKE_KEY="$SUPPORT/relay/smoke.key"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

vps() { ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 "$VPS" "$@"; }

echo "== 1/5 trip guard"
guard=$(vps 'sudo bash -s' <<'EOF'
db=/var/lib/moto-relay/relay.db
if [ ! -f "$db" ]; then echo "none"; exit 0; fi
sqlite3 -readonly "$db" "SELECT
  (SELECT COUNT(*) FROM trips WHERE ended_at IS NULL AND rider_id <> '__smoketest__'),
  COALESCE((SELECT CAST((julianday('now') - julianday(MAX(received_at))) * 86400 AS INTEGER)
            FROM positions WHERE rider_id <> '__smoketest__'), -1);"
EOF
)
if [ "$guard" = "none" ]; then
  echo "   first deploy: no relay database yet"
else
  open=${guard%%|*}
  since=${guard##*|}
  echo "   open trips: $open   last real position: $([ "$since" -lt 0 ] && echo never || echo "${since}s ago")"
  if [ "$open" -gt 0 ] || { [ "$since" -ge 0 ] && [ "$since" -lt 300 ]; }; then
    if [ "$FORCE" -eq 1 ]; then
      echo "   --force given: deploying anyway"
    else
      echo "REFUSED: a trip is open or telemetry is arriving. Deploy when nobody is riding, or pass --force."
      exit 2
    fi
  fi
fi

REL=$(date -u +%Y%m%d-%H%M%S)
echo "== 2/5 upload release $REL"
vps 'rm -rf /tmp/moto-relay-stage && mkdir -p /tmp/moto-relay-stage'
scp -q -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
  relay.py relayctl.py schema.sql requirements.txt moto-relay.service "$VPS:/tmp/moto-relay-stage/"

echo "== 3/5 install and restart"
out=$(vps "sudo REL=$REL bash -s" <<'EOF'
set -euo pipefail
id moto-relay >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin moto-relay
install -d -m 755 /opt/moto-relay /opt/moto-relay/releases
dest=/opt/moto-relay/releases/$REL
install -d -m 755 "$dest"
install -m 644 /tmp/moto-relay-stage/{relay.py,relayctl.py,schema.sql,requirements.txt,moto-relay.service} "$dest/"
rm -rf /tmp/moto-relay-stage

# The venv is shared by releases and rebuilt only when requirements change.
[ -x /opt/moto-relay/venv/bin/python ] || python3 -m venv /opt/moto-relay/venv
want=$(sha256sum "$dest/requirements.txt" | cut -d' ' -f1)
have=$(cat /opt/moto-relay/venv/.requirements.sha256 2>/dev/null || true)
if [ "$want" != "$have" ]; then
  /opt/moto-relay/venv/bin/pip install -q --disable-pip-version-check -r "$dest/requirements.txt"
  echo "$want" > /opt/moto-relay/venv/.requirements.sha256
  echo "   dependencies installed"
fi

prev=$(readlink /opt/moto-relay/current 2>/dev/null || true)
echo "PREVIOUS=${prev##*/}"
ln -sfn "$dest" /opt/moto-relay/current.new
mv -Tf /opt/moto-relay/current.new /opt/moto-relay/current

if ! cmp -s "$dest/moto-relay.service" /etc/systemd/system/moto-relay.service; then
  install -m 644 "$dest/moto-relay.service" /etc/systemd/system/moto-relay.service
  systemctl daemon-reload
fi
systemctl enable -q moto-relay

cat > /usr/local/bin/relayctl <<'W'
#!/bin/sh
# moto-tracker relay admin, installed by relay/deploy.sh. Run with sudo.
exec runuser -u moto-relay -- env RELAY_DB=/var/lib/moto-relay/relay.db /opt/moto-relay/venv/bin/python /opt/moto-relay/current/relayctl.py "$@"
W
chmod 755 /usr/local/bin/relayctl

systemctl restart moto-relay
for i in $(seq 1 40); do
  curl -fsS http://127.0.0.1:8088/livez >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -fsS http://127.0.0.1:8088/livez >/dev/null 2>&1; then
  echo "RELAY DID NOT START"
  journalctl -u moto-relay -n 30 --no-pager
  exit 3
fi
echo "   relay up on 127.0.0.1:8088"
ls -1dt /opt/moto-relay/releases/* | tail -n +6 | xargs -r rm -rf
EOF
) || { echo "$out"; echo "INSTALL FAILED"; exit 3; }
echo "$out" | grep -v '^PREVIOUS='
prev=$(printf '%s\n' "$out" | sed -n 's/^PREVIOUS=//p')

rollback() {
  if [ -n "$prev" ]; then
    vps "sudo bash -c 'ln -sfn /opt/moto-relay/releases/$prev /opt/moto-relay/current.new && mv -Tf /opt/moto-relay/current.new /opt/moto-relay/current && systemctl restart moto-relay'"
    echo "ROLLED BACK to $prev"
  else
    echo "first deploy: nothing to roll back to; relay left on $REL for inspection"
  fi
}

echo "== 4/5 map key"
# The phones fetch this from the relay and then talk to TomTom directly, so no tile
# ever crosses the VPS. It is piped over SSH straight into place — never written to
# /tmp, never echoed, never an argument on a command line.
MAP_KEY="$SUPPORT/monitor-secrets/tomtom.key"
if [ -s "$MAP_KEY" ]; then
  if vps 'sudo test -s /var/lib/moto-relay/tomtom.key' 2>/dev/null; then
    echo "   already installed on the relay (not read, not compared)"
  else
    vps "sudo bash -c 'umask 077; cat > /var/lib/moto-relay/tomtom.key;
         chown moto-relay:moto-relay /var/lib/moto-relay/tomtom.key;
         chmod 600 /var/lib/moto-relay/tomtom.key'" < "$MAP_KEY"
    echo "   installed on the relay, owned by the service account"
  fi
else
  echo "   no map key on this Mac; the phones will have no tiles until there is one"
fi

echo "== 4/5 smoke-test key"
if [ ! -s "$SMOKE_KEY" ]; then
  mkdir -p "$(dirname "$SMOKE_KEY")" && chmod 700 "$(dirname "$SMOKE_KEY")"
  (umask 077; vps 'sudo relayctl smoke-key' > "$SMOKE_KEY")
  echo "   new smoke key stored at $SMOKE_KEY"
else
  echo "   using $SMOKE_KEY"
fi

echo "== 5/5 smoke test"
set +e
/usr/bin/python3 smoke.py --url "$URL" --ca "$CA" --token-file "$SMOKE_KEY"
rc=$?
if [ "$rc" -eq 3 ]; then
  echo "   the relay does not know the stored smoke key: rotating it and retrying"
  (umask 077; vps 'sudo relayctl smoke-key' > "$SMOKE_KEY")
  /usr/bin/python3 smoke.py --url "$URL" --ca "$CA" --token-file "$SMOKE_KEY"
  rc=$?
fi
set -e
if [ "$rc" -ne 0 ]; then
  echo "SMOKE TEST FAILED"
  rollback
  exit 1
fi
echo "DEPLOYED $REL"
