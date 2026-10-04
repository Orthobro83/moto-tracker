/* moto-tracker monitor UI.
 *
 * Every counter on this screen runs on real time. Elapsed values are measured
 * against the relay's clock (the `now` field in each answer), corrected for this
 * machine's offset, and re-read from the real clock on every frame — nothing
 * accumulates, so nothing can run fast (design.md 2026-09-15).
 */
'use strict';

const POLL_MS = 1000, TICK_MS = 250, TRAIL_MS = 10000, TRAFFIC_MS = 120000;
const MOVING_KMH = 3;
// A signal loss (Jack, 2026-09-29). Unheard for a minute, the bubble says "Signal lost
// for m:ss" over the last known position — not how long the rider has been stopped: they
// are not known to be stopped, only not heard (2026-09-24). Unheard for two, a bar over
// the map says so too and the Mac chimes once. An alert, not a crash: nothing is raised.
// The relay decides both moments (relay-15, `signal` in /state), so every screen agrees
// and a Mac that has lost the relay itself cannot blame the rider. This is only the
// old rule, for an older relay.
const SIGNAL_LOST_S = 60;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const S = {                      // everything the screen is drawn from
  status: null,
  skew: 0,                       // this machine's clock minus the relay's
  layout: null,                  // 'pairing' | 'split' | 'replay:<rider>'
  logSeq: 0,
  halted: false,
  seenTrips: new Set(),
  liveTrips: new Set(),          // trips open at the last poll
  sides: {},                     // rider -> that rider's side of the window
  folded: null,                  // the rider whose side is folded away, if any
  stoppedSince: {},              // rider -> ISO time speed last fell below moving
  incidentOpen: null,            // {rider, id} of the incident whose box is on screen
  incidentSig: null,             // what the open box was drawn from
  announced: new Set(),
  modal: null,
  replay: null,          // a trip being watched again, instead of the live ride
};

const RATES = [1, 4, 16, 60];

const relayNow = () => Date.now() - S.skew;
const parse = (iso) => (iso ? Date.parse(iso) : NaN);

function since(iso) {            // seconds of real time since a relay timestamp
  const t = parse(iso);
  return isNaN(t) ? null : Math.max(0, (relayNow() - t) / 1000);
}

function clock(seconds, withHours) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
  const s = Math.floor(seconds), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h || withHours) return `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

function ago(seconds) {
  if (seconds === null) return '—';
  if (seconds < 90) return `${Math.floor(seconds)} s ago`;
  return `${clock(seconds)} ago`;
}

/** The running log wants seconds: a tick every five of them is meaningless without. */
const logTime = (iso) => {
  const t = parse(iso);
  return isNaN(t) ? '—' : new Date(t).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
};

const localTime = (iso) => {
  const t = parse(iso);
  return isNaN(t) ? '—' : new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};
const localDate = (iso) => {
  const t = parse(iso);
  return isNaN(t) ? '—' : new Date(t).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
};
const num = (v, digits, unit) =>
  (v === null || v === undefined) ? '—' : `${Number(v).toFixed(digits)}${unit || ''}`;

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Moto': '1' }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text }; }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${r.status}`);
  return data;
}

/** The rider's signal loss: {since, alert} while unheard, else null. */
function signalOf(r) {
  if (r.signal !== undefined) return r.signal;
  const gap = since(r.last_received_at);
  return gap !== null && gap >= SIGNAL_LOST_S ? { since: r.last_received_at, alert: false } : null;
}

const signalAlert = (r) => !!(r && (signalOf(r) || {}).alert);

/* ---------------------------------------------------------------- links out */

const mapsUrl = (lat, lon) => `https://www.google.com/maps?q=${lat.toFixed(6)},${lon.toFixed(6)}`;

function openExternal(url) {
  // In the app the page is a WKWebView: Swift opens links in the real browser.
  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.moto) {
    window.webkit.messageHandlers.moto.postMessage({ action: 'open', url });
  } else {
    window.open(url, '_blank', 'noopener');
  }
}

function copyText(text, button) {
  const done = () => {
    if (!button) return;
    const was = button.textContent;
    button.textContent = '✓';
    setTimeout(() => { button.textContent = was; }, 1200);
  };
  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.moto) {
    window.webkit.messageHandlers.moto.postMessage({ action: 'copy', text });
    done();
    return;
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}

function fallbackCopy(text, done) {
  const ta = el('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { /* nothing more to try */ }
  ta.remove();
}

function coordLink(lat, lon) {
  const wrap = el('span', 'coords');
  const url = mapsUrl(lat, lon);
  const a = el('a', null, `${lat.toFixed(5)}, ${lon.toFixed(5)}`);
  a.href = url;
  a.title = 'Open in Google Maps';
  a.addEventListener('click', (e) => { e.preventDefault(); openExternal(url); });
  const copy = el('button', 'copy', '⧉');
  copy.title = 'Copy the Google Maps link';
  copy.addEventListener('click', () => copyText(url, copy));
  wrap.append(a, copy);
  return wrap;
}

/* ---------------------------------------------------------------- header */

function drawHeader(st) {
  const up = st.server.up;
  // An unpaired Mac has no key to ask with; that is not the relay being down.
  if (!st.paired) {
    $('srvdot').className = 'dot';
    $('srvtext').textContent = st.key_rejected ? 'Key rejected' : 'Not paired yet';
    $('srvtext').style.color = 'var(--dim)';
    $('ping').textContent = '—';
    $('ppm').textContent = '—';
    return;
  }
  $('srvdot').className = 'dot ' + (up ? 'go' : 'bad');
  $('srvtext').textContent = up ? 'Server live' : 'Server unreachable';
  $('srvtext').style.color = up ? '' : 'var(--bad)';
  $('ping').textContent = up && st.server.ping_ms !== null ? `${st.server.ping_ms} ms` : '—';
  $('ppm').textContent = st.packets_per_minute;
  const testing = st.alarm && st.alarm.sounding && st.alarm.reason === 'test';
  $('btn-test').textContent = testing ? 'Stop alarm' : 'Test alarm';
  $('btn-test').classList.toggle('danger', !!testing);
  // Only offered when there is a trip that could need ending. With both out, it
  // asks whose.
  const riding = Object.entries(st.riders || {}).filter(([, r]) => r.trip_id);
  $('btn-forceend').hidden = riding.length === 0;
  $('btn-forceend').textContent = riding.length === 1
    ? `Force end ${riding[0][1].name}'s trip` : 'Force end a trip…';
  $('btn-drill').textContent = st.drill ? 'End drill' : 'Test incident';
  $('btn-drill').classList.toggle('danger', !!st.drill);
  // The gear itself says when a test is running, since the menu is usually shut.
  $('gear').classList.toggle('alert', !!(testing || st.drill));
}

function openGear(open) {
  const menu = $('gearmenu');
  const show = open === undefined ? menu.hidden : open;
  menu.hidden = !show;
  $('gear').classList.toggle('open', show);
  $('gear').setAttribute('aria-expanded', String(show));
}

/* ---------------------------------------------------------------- pairing */

function buildPairing(st) {
  const main = $('main');
  main.innerHTML = '';
  const wrap = el('div', 'idle');
  const pane = el('div', 'pane');
  pane.style.maxWidth = '560px';
  pane.style.margin = 'auto';
  pane.append(el('h2', null, st.key_rejected ? 'This Mac’s key was rejected' : 'Pair this Mac'));
  pane.append(el('div', 'sub', st.key_rejected
    ? 'The key this Mac was using is gone — revoked, or the relay was rebuilt. Pair it again.'
    : `Ask for a pairing code on the other Mac: Devices → Monitor (this Mac) → Create pairing code. `
      + `Then type it here. It works once and lasts ten minutes.`));
  const fields = el('div', 'fields');
  const code = el('input');
  code.type = 'text';
  code.placeholder = 'ABCD-EFGH';
  code.autocapitalize = 'characters';
  const name = el('input');
  name.type = 'text';
  name.value = st.hostname || '';
  name.placeholder = 'this Mac';
  fields.append(el('div', null, 'Code'), code, el('div', null, 'Name'), name);
  pane.append(fields);
  const go = el('button', 'primary', 'Pair this Mac');
  go.style.marginTop = '16px';
  go.style.width = '100%';
  const err = el('div', 'err');
  err.style.display = 'none';
  const done = el('div', 'note');
  pane.append(go, err, done);
  pane.append(el('div', 'note', `Relay: ${st.relay || ''}`));

  const submit = async () => {
    err.style.display = 'none';
    go.disabled = true;
    try {
      const r = await api('/api/pair', { code: code.value, name: name.value });
      done.className = 'note ok';
      done.textContent = r.archivist
        ? 'Paired. This Mac keeps the history.'
        : 'Paired. This Mac is a viewer — the other Mac keeps the history.';
      S.layout = null;
    } catch (e) {
      err.style.display = '';
      err.textContent = String(e.message || e);
      go.disabled = false;
    }
  };
  go.addEventListener('click', submit);
  code.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  wrap.append(pane);
  main.append(wrap);
  code.focus();
}

/* ---------------------------------------------------------------- panes */

/*
 * Both riders can be out at once (Jack, 2026-09-24). The top of the window is split
 * in two, Jack on the left and Dana on the right, always. A rider on a trip gets
 * their own map, with their telemetry on a translucent panel over it; a rider at
 * home gets their last trip. Either side folds away to its own edge, leaving a slim
 * bar with a chevron to bring it back, and the other side takes the whole width.
 */
const SIDE_ORDER = ['jack', 'dana'];
const FOLD_KEY = 'moto.folded';

function ridersInOrder(st) {
  const ids = Object.keys(st.riders || {});
  return [...SIDE_ORDER.filter((id) => ids.includes(id)), ...ids.filter((id) => !SIDE_ORDER.includes(id))];
}

const ridingIds = (st) => Object.entries(st.riders || {}).filter(([, r]) => r.trip_id).map(([id]) => id);

/* Which side is folded away, remembered on this Mac only. */
function loadFolded() {
  try { return localStorage.getItem(FOLD_KEY) || null; } catch (e) { return null; }
}

function setFolded(id) {
  S.folded = id;
  try {
    if (id) localStorage.setItem(FOLD_KEY, id); else localStorage.removeItem(FOLD_KEY);
  } catch (e) { /* only a convenience */ }
  applyFold();
}

function applyFold() {
  const split = $('split');
  if (!split) return;
  for (const side of Object.values(S.sides)) {
    const hidden = S.folded === side.id && Object.keys(S.sides).length > 1;
    side.slot.hidden = hidden;
    side.bar.hidden = !hidden;
  }
  // Leaflet measures its box once; a side that just got wider has to be told.
  requestAnimationFrame(() => {
    for (const side of Object.values(S.sides)) if (side.map) side.map.invalidateSize();
  });
}

function buildSplit(st) {
  const main = $('main');
  main.innerHTML = '';
  for (const side of Object.values(S.sides)) if (side.map) side.map.remove();
  S.sides = {};
  const split = el('div', 'split');
  split.id = 'split';
  const ids = ridersInOrder(st);
  ids.forEach((id, i) => {
    const edge = i === 0 ? 'left' : 'right';
    const name = (st.riders[id] || {}).name || id;
    const bar = el('button', `restore ${edge}`);
    bar.title = `Show ${name}`;
    bar.append(el('span', 'chev', edge === 'left' ? '›' : '‹'), el('span', 'vname', name));
    bar.addEventListener('click', () => setFolded(null));
    const slot = el('div', `side-slot ${edge}`);
    S.sides[id] = { id, edge, slot, bar, mode: null, sig: null, map: null, trail: [],
                    trailTrip: null, trailAt: 0, following: true,
                    q: (k) => slot.querySelector(`[data-k="${k}"]`) };
  });
  const sides = ids.map((id) => S.sides[id]);
  if (sides.length) split.append(sides[0].bar);
  sides.forEach((s) => split.append(s.slot));
  if (sides.length > 1) split.append(sides[sides.length - 1].bar);
  main.append(split);
  applyFold();
}

/** The chevron that folds a side away towards its own edge. */
function foldButton(side) {
  const b = el('button', 'fold', side.edge === 'left' ? '‹' : '›');
  b.title = 'Hide this side';
  b.addEventListener('click', () => setFolded(side.id));
  if (Object.keys(S.sides).length < 2) b.hidden = true;
  return b;
}

function buildIdleSide(st, side) {
  const r = st.riders[side.id];
  side.slot.innerHTML = '';
  const pane = el('div', 'pane');
  const head = el('div', 'panehead');
  head.append(el('h2', null, r.name || side.id));
  if (side.edge === 'left') head.prepend(foldButton(side)); else head.append(foldButton(side));
  pane.append(head);
  pane.append(el('div', 'sub', 'Not riding'));
  const p = r.previous_trip;
  const dl = el('dl', 'stats');
  const add = (k, v) => { dl.append(el('dt', null, k), el('dd', null, v)); };
  if (p) {
    add('Last trip', localDate(p.started_at));
    add('Started', localTime(p.started_at));
    add('Ended', localTime(p.ended_at));
    add('Duration', clock(p.duration_s, true));
    add('Max speed', num(p.max_speed, 0, ' km/h'));
    add('Average speed', num(p.avg_speed, 0, ' km/h'));
    add('Peak G', num(p.max_g, 2, ' g'));
    add('Rotational force', num(p.max_rot, 2, ' rad/s'));
  } else {
    add('Last trip', 'no trips archived yet');
  }
  pane.append(dl);
  if (st.archivist !== false) {          // a viewer Mac holds no logs to open
    const foot = el('div', 'foot');
    const btn = el('button', null, 'Open logs');
    btn.addEventListener('click', () => showTripPicker(side.id));
    foot.append(btn);
    pane.append(foot);
  }
  side.slot.append(pane);
}

function buildRidingSide(st, side) {
  side.slot.innerHTML = '';
  if (side.map) { side.map.remove(); side.map = null; }
  const host = el('div', 'maphost');
  const mapDiv = el('div', 'map');
  const wx = el('div', 'wx');
  wx.innerHTML = '<div class="w1">—</div><div class="w2"></div>';
  const follow = el('button', 'follow', 'Follow rider');
  follow.style.display = 'none';
  follow.addEventListener('click', () => {
    side.following = true;
    follow.style.display = 'none';
    recentre(side);
  });
  const tele = el('div', 'tele');
  tele.innerHTML = `
    <div data-k="alertslot"></div>
    <div class="statusline"><span data-k="rname"></span><span class="badge" data-k="rstate"></span></div>
    <div class="big"><b data-k="speed">—</b><span>km/h</span></div>
    <dl class="kv">
      <dt>Trip started</dt><dd data-k="tstart">—</dd>
      <dt>Riding for</dt><dd data-k="telapsed">—</dd>
      <dt>Last packet</dt><dd data-k="tlast">—</dd>
      <dt>Position</dt><dd data-k="tpos">—</dd>
      <dt>Accuracy</dt><dd data-k="tacc">—</dd>
      <dt>Battery</dt><dd data-k="tbat">—</dd>
      <dt>G-force</dt><dd data-k="tg">—</dd>
      <dt>Max G<span data-k="gscope"></span></dt><dd data-k="tgmax">—</dd>
      <dt>Rotation</dt><dd data-k="trot">—</dd>
      <dt>Max rotation</dt><dd data-k="trotmax">—</dd>
    </dl>`;
  const line = tele.querySelector('.statusline');
  if (side.edge === 'left') line.prepend(foldButton(side)); else line.append(foldButton(side));
  if (!st || st.archivist !== false) {
    const foot = el('div', 'foot');
    const logs = el('button', null, 'Open logs');
    logs.addEventListener('click', () => showTripPicker(side.id));
    foot.append(logs);
    tele.append(foot);
  }
  const sigbar = el('div', 'sigbar');
  sigbar.dataset.k = 'sigbar';
  sigbar.hidden = true;
  host.append(mapDiv, sigbar, tele, wx, follow);
  side.slot.append(host);
  makeMap(side, mapDiv, follow);
}

/** Builds a side afresh only when what it is showing changes. */
function drawSide(st, id) {
  const side = S.sides[id];
  const r = st.riders[id];
  if (!side || !r) return;
  const mode = r.trip_id ? 'riding' : 'idle';
  if (mode === 'idle') {
    const sig = JSON.stringify([r.name, r.previous_trip]);
    if (side.mode !== 'idle' || side.sig !== sig) {
      if (side.map) { side.map.remove(); side.map = null; }
      side.mode = 'idle';
      side.sig = sig;
      buildIdleSide(st, side);
    }
    return;
  }
  if (side.mode !== 'riding') {
    side.mode = 'riding';
    side.sig = null;
    buildRidingSide(st, side);
  }
  drawRiding(st, side);
}

/* ---------------------------------------------------------------- map */

function makeMap(side, mapDiv, follow) {
  const map = L.map(mapDiv, { zoomControl: false, attributionControl: false, preferCanvas: true })
    .setView([13.69, -89.22], 14);
  // The telemetry panel sits at the side's outer edge, so the zoom goes inside.
  L.control.zoom({ position: side.edge === 'left' ? 'topright' : 'topleft' }).addTo(map);
  L.tileLayer('/tiles/map/{z}/{x}/{y}.png?style=night', { maxZoom: 19, minZoom: 3 }).addTo(map);
  side.traffic = L.tileLayer('/tiles/traffic/{z}/{x}/{y}.png?style=relative0-dark',
    { maxZoom: 19, minZoom: 3, opacity: 0.95 }).addTo(map);
  side.line = L.polyline([], { color: '#4da3ff', weight: 3, opacity: 0.75 }).addTo(map);
  side.marker = L.marker([13.69, -89.22], {
    icon: L.divIcon({ className: '', html: '<div class="rider-dot" style="width:16px;height:16px"></div>',
                      iconSize: [16, 16], iconAnchor: [8, 8] }),
  }).addTo(map);
  side.bubble = L.tooltip({ permanent: true, direction: 'top', offset: [0, -12], className: 'leaflet-tooltip-own' });
  side.marker.bindTooltip(side.bubble).openTooltip();
  side.following = true;
  side.trafficAt = Date.now();
  side.map = map;
  // The map is created the moment its side appears; Leaflet measures the container
  // on the next frame, or it loads tiles for a box the size it was born with.
  requestAnimationFrame(() => side.map && side.map.invalidateSize());
  map.on('dragstart', () => {
    side.following = false;
    follow.style.display = '';
  });
}

/**
 * Pans so the rider sits in the middle of the part of the map the telemetry panel
 * leaves uncovered, not behind the panel.
 */
function panToRider(side, here, opts) {
  const map = side.map;
  const tele = side.slot.querySelector('.tele');
  const w = map.getSize().x;
  let dx = 0;
  if (tele && tele.offsetWidth) {
    const covered = tele.offsetLeft + (side.edge === 'left' ? tele.offsetWidth : 0);
    const freeMid = side.edge === 'left' ? (covered + w) / 2 : tele.offsetLeft / 2;
    // Too narrow to dodge the panel: keep the rider centred.
    if (w - tele.offsetWidth > 160) dx = freeMid - w / 2;
  }
  const z = map.getZoom();
  const centre = map.unproject(map.project(L.latLng(here), z).subtract([dx, 0]), z);
  map.panTo(centre, opts);
}

function recentre(side) {
  const r = S.status && S.status.riders[side.id];
  if (r && r.lat !== null && side.map) panToRider(side, [r.lat, r.lon], { animate: true });
}

function drawMap(side, r) {
  if (!side.map || r.lat === null || r.lat === undefined) return;
  const here = [r.lat, r.lon];
  side.marker.setLatLng(here);
  const dot = side.marker.getElement() && side.marker.getElement().querySelector('.rider-dot');
  if (dot) dot.className = 'rider-dot' + (r.incident && r.incident.display === 'red' ? ' red' : '');
  if (side.following) panToRider(side, here, { animate: true, duration: 0.6 });
  // The trail comes from the archive; the live point is appended so the dot leads it.
  const pts = side.trail.map((p) => [p.lat, p.lon]);
  pts.push(here);
  side.line.setLatLngs(pts);
  if (Date.now() - side.trafficAt > TRAFFIC_MS) {      // traffic is only worth showing if it is current
    side.trafficAt = Date.now();
    side.traffic.setUrl(`/tiles/traffic/{z}/{x}/{y}.png?style=relative0-dark&t=${Math.floor(Date.now() / 1000)}`, false);
  }
}

function drawWeather(side, w) {
  const box = side.slot.querySelector('.wx');
  if (!box) return;
  if (!w) { box.style.display = 'none'; return; }
  box.style.display = '';
  // How old it is matters: a reading can be hours stale when the service is
  // refusing, and an hour-old "Thunderstorm" over a sunny ride is worse than nothing.
  const mins = w.fetched_at ? Math.round((Date.now() - Date.parse(w.fetched_at)) / 60000) : 0;
  box.querySelector('.w1').textContent = mins >= 20 ? `${w.headline} · ${mins} min ago` : w.headline;
  box.querySelector('.w2').textContent =
    `${num(w.temp_c, 0, '°C')}  wind ${num(w.wind_kmh, 0)} km/h`;
}

/* ---------------------------------------------------------------- a riding side */

function drawRiding(st, side) {
  const r = st.riders[side.id];
  const q = side.q;
  q('rname').textContent = r.name || side.id;
  const badge = q('rstate');
  badge.textContent = r.state === 'riding' ? 'Riding' : r.state === 'offbike' ? 'Off-bike' : 'Asleep';
  badge.className = 'badge ' + (r.state || 'sleep');
  q('speed').textContent = r.speed === null || r.speed === undefined ? '—' : Math.round(r.speed);
  q('tstart').textContent = localTime(r.trip_started);
  q('tpos').innerHTML = '';
  if (r.lat !== null && r.lat !== undefined) q('tpos').append(coordLink(r.lat, r.lon));
  else q('tpos').textContent = '—';
  q('tacc').textContent = num(r.accuracy, 0, ' m');
  q('tbat').textContent = r.battery === null || r.battery === undefined ? '—' : `${r.battery}%`;
  q('tg').textContent = num(r.peak_g, 2, ' g');
  q('trot').textContent = num(r.peak_rot, 2, ' rad/s');
  q('gscope').textContent = r.peaks_scope === 'trip' ? '' : ' (last trip)';
  const stamp = (v, at, digits, unit) => {
    const dd = el('span');
    dd.append(document.createTextNode(num(v, digits, unit)));
    if (at) dd.append(el('span', 'at', localTime(at)));
    return dd;
  };
  q('tgmax').innerHTML = '';
  q('tgmax').append(stamp(r.max_g, r.max_g_at, 2, ' g'));
  q('trotmax').innerHTML = '';
  q('trotmax').append(stamp(r.max_rot, r.max_rot_at, 2, ' rad/s'));

  const inc = r.incident;
  const look = inc ? (inc.display === 'pending' ? 'amber' : inc.display) : '';
  side.slot.querySelector('.tele').className = 'tele' + (look ? ' ' + look : '');
  // The signal-loss bar takes the top of the map; the panel and the zoom step down.
  const alert = signalAlert(r);
  side.slot.querySelector('.maphost').className = 'maphost' + (look ? ' ' + look : '') + (alert ? ' sig' : '');
  q('sigbar').hidden = !alert;
  if (alert) drawSignalBar(side, r);
  const slot = q('alertslot');
  if (inc) {
    if (!slot.firstChild || slot.dataset.inc !== String(inc.id) || slot.dataset.display !== inc.display) {
      slot.innerHTML = '';
      slot.dataset.inc = String(inc.id);
      slot.dataset.display = inc.display;
      const b = el('button', 'alertbtn' + (inc.display === 'pending' ? ' amber' : inc.display === 'yellow' ? ' yellow' : ''));
      b.textContent = incidentTitle(inc, true);
      b.addEventListener('click', () => showIncident(side.id));
      slot.append(b);
    }
  } else if (slot.firstChild) {
    slot.innerHTML = '';
    slot.dataset.inc = '';
  }
  drawMap(side, r);
  drawWeather(side, (st.weather_by || {})[side.id] || null);
}

/* The folded bar still says when its rider needs looking at. */
function drawBars(st) {
  for (const side of Object.values(S.sides)) {
    const r = st.riders[side.id] || {};
    const inc = r.incident;
    const look = inc ? (inc.display === 'pending' ? 'amber' : inc.display) : (signalAlert(r) ? 'sig' : '');
    side.bar.className = `restore ${side.edge}` + (look ? ' ' + look : '');
  }
}

/** What the speed bubble says: speed, how long stopped, or how long nothing has been heard. */
function bubbleHtml(r, riderId) {
  const lost = signalOf(r);
  if (lost) return `<div class="speedbub lost">Signal lost for ${clock(since(lost.since))}</div>`;
  const moving = (r.speed || 0) >= MOVING_KMH;
  const stoppedFor = moving ? null : since(S.stoppedSince[riderId] || r.last_received_at);
  return `<div class="speedbub${moving ? '' : ' stopped'}">` +
    (moving ? `${Math.round(r.speed)} km/h` : `Stopped for ${clock(stoppedFor)}`) + '</div>';
}

/** The bar across the top of the map, past two minutes unheard (Jack, 2026-09-29). */
function drawSignalBar(side, r) {
  side.q('sigbar').textContent =
    `Signal lost for ${clock(since(signalOf(r).since))}. Awaiting acquisition of signal.`;
}

/* counters that must run in real time, re-read from the clock every frame */
function tick() {
  if (S.replay) return;          // replay runs on its own clock
  const st = S.status;
  if (!st) return;
  for (const side of Object.values(S.sides)) {
    if (side.mode !== 'riding' || !side.q('telapsed')) continue;
    const r = st.riders[side.id];
    if (!r) continue;
    side.q('telapsed').textContent = clock(since(r.trip_started), true);
    const gap = since(r.last_received_at);
    const late = gap !== null && gap > 20;
    side.q('tlast').textContent = ago(gap);
    side.q('tlast').style.color = late ? 'var(--warn)' : '';
    if (side.bubble && r.lat !== null && r.lat !== undefined) {
      side.bubble.setContent(bubbleHtml(r, side.id));
    }
    if (signalAlert(r)) drawSignalBar(side, r);
  }
  const box = document.querySelector('.box.incident');
  if (box && S.incidentOpen) {
    const inc = currentIncident(S.incidentOpen.rider);
    const still = box.querySelector('[data-still]');
    if (still && inc) {
      still.textContent = inc.stationary_since ? clock(since(inc.stationary_since)) : 'moving';
    }
    const open = box.querySelector('[data-open]');
    if (open && inc) open.textContent = clock(since(inc.raised_at), true);
  }
}

/* ---------------------------------------------------------------- incident box */

function currentIncident(rider) {
  const r = S.status && S.status.riders && S.status.riders[rider];
  return (r && r.incident) || null;
}

/* What to call an incident, which depends on what raised it. */
function incidentTitle(inc, short) {
  const pending = inc.display === 'pending';
  if (inc.drill) return pending ? 'DRILL — CHECKING' : 'DRILL — INCIDENT DETECTED';
  if (inc.kind === 'help') return short ? 'RIDER NEEDS HELP' : 'RIDER PRESSED “I NEED HELP”';
  if (inc.kind === 'silence') {
    return pending ? 'SIGNAL LOST AFTER AN IMPACT' : 'SIGNAL LOST AFTER AN IMPACT';
  }
  if (pending) return short ? 'POSSIBLE CRASH — CHECKING…' : 'POSSIBLE CRASH — CHECKING';
  return 'INCIDENT DETECTED';
}

function incidentSignature(inc) {
  return inc && [inc.id, inc.state, inc.display, inc.silenced_at, inc.rider_closed_at,
                 inc.observer_closed_at].join('|');
}

function showIncident(riderId, phrase) {
  const inc = currentIncident(riderId);
  if (!inc) return;
  const rider = S.status.riders[riderId];
  closeModal();
  S.incidentOpen = { rider: riderId, id: inc.id };
  S.incidentSig = incidentSignature(inc);

  const scrim = el('div', 'scrim');
  const box = el('div', 'box incident ' + (inc.display === 'red' ? 'red' : inc.display === 'yellow' ? 'yellow' : ''));
  const title = incidentTitle(inc, false);
  box.append(el('h3', null, `${title} — ${rider ? rider.name : ''}`));
  const when = el('div', 'when');
  when.append(document.createTextNode(`Raised ${localTime(inc.raised_at)} · open for `));
  const openFor = el('span');
  openFor.dataset.open = '1';
  when.append(openFor);
  box.append(when);

  const row = (k, valueNode) => {
    const r = el('div', 'row');
    r.append(el('div', 'k', k));
    const v = el('div', 'v');
    if (typeof valueNode === 'string') v.textContent = valueNode; else v.append(valueNode);
    r.append(v);
    box.append(r);
  };

  if (inc.last_position) {
    row('Last known position', coordLink(inc.last_position.lat, inc.last_position.lon));
    row('Fixed at', `${localTime(inc.last_position.at)} · ±${num(inc.last_position.accuracy, 0, ' m')}`);
  } else {
    row('Last known position', 'no position received');
  }
  if (inc.kind !== 'help') {
    row('Deceleration', `${num(inc.speed_before, 0, ' km/h')} → ${num(inc.speed_after, 0, ' km/h')}`);
  }
  const still = el('span');
  still.dataset.still = '1';
  row('Rider stationary for', still);
  if (inc.kind !== 'help') {
    row('G-force recorded', num(inc.g, 2, ' g'));
    row('Rotational force recorded', num(inc.rot, 2, ' rad/s'));
  }
  if (inc.silenced_at) row('Alarm silenced', `${localTime(inc.silenced_at)} · ${inc.silenced_by || ''}`);
  if (inc.rider_closed_at) {
    row('Rider closed it', `${localTime(inc.rider_closed_at)} · ` +
      (inc.rider_resolution === 'false_alarm' ? 'false alarm' : 'I’m OK'));
  }
  if (inc.observer_closed_at) {
    row('Observer closed it', `${localTime(inc.observer_closed_at)} · ${inc.observer_closed_by || ''}`);
  }

  const actions = el('div', 'actions');
  const silence = el('button', 'primary', 'Silence alarm');
  silence.disabled = !!inc.silenced_at;
  if (inc.silenced_at) silence.textContent = 'Alarm silenced';
  silence.addEventListener('click', () => act(silence, `/api/incident/${inc.id}/silence`));
  const close = el('button', null, inc.drill ? 'End drill' : 'Close incident');
  close.disabled = !!inc.observer_closed_at || inc.state !== 'sos';
  if (inc.observer_closed_at) close.textContent = 'You closed it';
  close.addEventListener('click', () => act(close, `/api/incident/${inc.id}/close`));
  const dismiss = el('button', 'ghost', 'Back');
  dismiss.addEventListener('click', closeModal);
  actions.append(silence, close, dismiss);
  box.append(actions);

  const note = el('div', 'note');
  if (inc.kind === 'silence' && inc.display === 'pending') {
    note.className = 'note warn';
    note.textContent = 'Something hard was recorded and then the phone went quiet. It clears itself '
      + 'if the ride carries on as normal.';
  } else if (inc.drill) {
    note.className = 'note warn';
    note.textContent = 'This is a drill on this Mac only. Nothing was sent to the relay, nothing was '
      + 'archived, and neither rider was told anything. The alarm behaves exactly as it would for real.';
  } else if (inc.display === 'pending') {
    note.textContent = 'The phone is still confirming. No alarm yet — this becomes an incident if the rider does not retract it.';
  } else if (inc.rider_closed_at && !inc.observer_closed_at) {
    note.textContent = 'The rider says they are OK. It stays open until you close it too.';
  } else if (inc.observer_closed_at && !inc.rider_closed_at) {
    note.className = 'note warn';
    note.textContent = 'Waiting for the rider to confirm. Make contact.';
  } else {
    note.textContent = 'An incident closes only when both the rider and an Observer have closed it.';
  }
  box.append(note);

  let force = null;
  if (inc.state === 'sos' && !inc.rider_closed_at && !inc.drill) {
    force = el('button', 'ghost danger', 'Close without rider confirmation');
    force.style.marginTop = '12px';
    force.style.width = '100%';
    force.addEventListener('click', () => forceCloseStep(box, force, inc));
    box.append(force);
  }

  const err = el('div', 'err');
  err.style.display = 'none';
  box.append(err);
  scrim.append(box);
  document.body.append(scrim);
  S.modal = scrim;
  tick();
  // Redrawn while the phrase was being typed: it comes back exactly as it was.
  if (phrase !== undefined && force) forceCloseStep(box, force, inc, phrase);

  async function act(button, path) {
    button.disabled = true;
    try {
      await api(path, {});
      // The relay is the authority; the next poll redraws the box with its answer.
      S.incidentSig = null;
    } catch (e) {
      err.style.display = '';
      err.textContent = String(e.message || e);
      button.disabled = false;
    }
  }
}

function forceCloseStep(box, trigger, inc, typed = '') {
  trigger.remove();
  const phrase = 'close without rider confirmation';
  const wrap = el('div');
  wrap.style.marginTop = '14px';
  // The phrase is written out, not only left as the box's placeholder: a placeholder
  // vanishes at the first keystroke, and on 2026-09-18 that left Jack being asked for
  // a phrase he had never been shown.
  const how = el('div', 'note warn',
    'This records the incident as closed WITHOUT the rider confirming. Only do this when the rider ' +
    'cannot answer at all. To confirm, type ');
  how.append(el('b', 'phrase', phrase), ' below.');
  wrap.append(how);
  const input = el('input');
  input.type = 'text';
  input.placeholder = phrase;
  input.value = typed;
  input.style.marginTop = '8px';
  const go = el('button', 'danger', 'Close without rider confirmation');
  go.style.marginTop = '8px';
  go.style.width = '100%';
  const matches = () => input.value.trim().toLowerCase() === phrase;
  go.disabled = !matches();
  const err = el('div', 'err');
  err.style.display = 'none';
  input.addEventListener('input', () => { go.disabled = !matches(); });
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      await api(`/api/incident/${inc.id}/force-close`, { confirm: input.value.trim() });
      closeModal();
      await refresh();
    } catch (e) {
      err.style.display = '';
      err.textContent = String(e.message || e);
      go.disabled = false;
    }
  });
  wrap.append(input, go, err);
  box.append(wrap);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function closeModal() {
  if (S.modal) { S.modal.remove(); S.modal = null; }
  S.incidentOpen = null;
}

/* ---------------------------------------------------------------- force end */

/**
 * For a phone that can never send "end trip" — a flat battery, a broken screen, a
 * lift home. Deliberately behind a question, because it ends a real ride's record.
 */
function askForceEnd(which) {
  const st = S.status;
  const riding = Object.entries((st && st.riders) || {}).filter(([, r]) => r.trip_id);
  if (!riding.length) return;
  if (!which && riding.length > 1) { askWhoseTrip(riding); return; }
  const found = riding.find(([id]) => id === which) || riding[0];
  const [id, rider] = found;
  closeModal();
  const scrim = el('div', 'scrim');
  const box = el('div', 'box');
  box.append(el('h3', null, `End ${rider.name}'s trip?`));
  box.append(el('div', 'when',
    `Trip #${rider.trip_id}, last packet ${rider.last_seen_s === null ? '—' :
      Math.round(rider.last_seen_s) + ' s'} ago.`));
  box.append(el('div', 'note',
    'Use this when their phone cannot end the trip itself — flat battery, broken '
    + 'phone, or they got a lift home. It closes the trip and stops the relay waiting '
    + 'for telemetry that is not coming. It does not close an open incident.'));
  const actions = el('div', 'actions');
  const go = el('button', 'primary', 'Yes, end the trip');
  const no = el('button', 'ghost', 'Cancel');
  no.addEventListener('click', closeModal);
  const err = el('div', 'err');
  err.style.display = 'none';
  go.addEventListener('click', async () => {
    go.disabled = true;
    try {
      await api('/api/trip/force-end', { rider: id });
      closeModal();
      await refresh();
    } catch (e) {
      err.style.display = '';
      err.textContent = String(e.message || e);
      go.disabled = false;
    }
  });
  actions.append(go, no);
  box.append(actions, err);
  scrim.append(box);
  document.body.append(scrim);
  S.modal = scrim;
}

/** Both out at once: which trip is it that will not end? */
function askWhoseTrip(riding) {
  closeModal();
  const scrim = el('div', 'scrim');
  const box = el('div', 'box');
  box.append(el('h3', null, 'Whose trip?'));
  box.append(el('div', 'when', 'Both riders have a trip open. End the one whose phone cannot.'));
  const actions = el('div', 'actions');
  for (const [id, r] of riding) {
    const b = el('button', null, `${r.name}'s trip`);
    b.addEventListener('click', () => askForceEnd(id));
    actions.append(b);
  }
  const no = el('button', 'ghost', 'Cancel');
  no.addEventListener('click', closeModal);
  actions.append(no);
  box.append(actions);
  scrim.append(box);
  document.body.append(scrim);
  S.modal = scrim;
}

/* ---------------------------------------------------------------- replay */

/**
 * Pick a day, then a ride from it. Opened by the pane's "Open logs" button, and
 * it shows that pane's rider only — their days, their rides, their log folder
 * (Jack, 2026-09-20).
 */
async function showTripPicker(rider) {
  closeModal();
  const whose = rider ? `?rider=${encodeURIComponent(rider)}` : '';
  const name = (((S.status || {}).riders || {})[rider] || {}).name || rider;
  const scrim = el('div', 'scrim');
  const box = el('div', 'box');
  box.append(el('h3', null, name ? `Watch ${name} ride again` : 'Watch a ride again'));
  box.append(el('div', 'when', name
    ? `Every one of ${name}'s rides is kept. Pick a day, then the ride.`
    : 'Every ride is kept. Pick a day, then the ride.'));
  const body = el('div', null, 'loading…');
  box.append(body);
  const actions = el('div', 'actions');
  const files = el('button', 'ghost', name ? `Show ${name}'s log files` : 'Show the log files');
  files.addEventListener('click', () => api('/api/open-logs', { rider: rider || '' }).catch(() => {}));
  const close = el('button', 'ghost', 'Close');
  close.addEventListener('click', closeModal);
  actions.append(files, close);
  box.append(actions);
  scrim.append(box);
  document.body.append(scrim);
  S.modal = scrim;

  async function showDays() {
    body.innerHTML = '';
    const { days } = await api('/api/trip-days' + whose);
    if (!days.length) {
      body.append(el('div', 'note', 'Nothing archived yet.'));
      return;
    }
    for (const d of days) {
      const b = el('button', 'daybtn');
      b.append(el('span', null, localDate(d.day + 'T12:00:00')));
      b.append(el('span', 'sub', `${d.trips} ride${d.trips === 1 ? '' : 's'}`));
      b.addEventListener('click', () => showTrips(d.day));
      body.append(b);
    }
  }

  async function showTrips(day) {
    body.innerHTML = '';
    const back = el('button', 'ghost', '← other days');
    back.style.marginBottom = '10px';
    back.addEventListener('click', showDays);
    body.append(back);
    const { trips } = await api(`/api/trips?day=${day}${whose.replace('?', '&')}`);
    if (!trips.length) body.append(el('div', 'note', 'No finished rides that day.'));
    for (const t of trips) {
      const b = el('button', 'triprow');
      const left = el('span');
      left.append(el('div', null, (rider ? '' : `${t.rider} · `) +
        `${localTime(t.started_at)}–${localTime(t.ended_at)}`));
      left.append(el('div', 'sub',
        `${clock(t.duration_s, true)}   ${num(t.max_speed, 0, ' km/h')} top   ${t.points} points`));
      b.append(left);
      b.append(el('span', 'sub', '▶'));
      b.addEventListener('click', () => startReplay(t.trip_id));
      body.append(b);
    }
  }

  showDays().catch((e) => { body.innerHTML = ''; body.append(el('div', 'err', String(e.message || e))); });
}

async function startReplay(tripId) {
  closeModal();
  const data = await api(`/api/replay?trip_id=${tripId}`);
  const frames = (data.frames || []).filter((f) => f.lat !== null);
  if (!frames.length) return;
  S.replay = {
    trip: data.trip,
    frames,
    events: data.events || [],
    t0: parse(frames[0].received_at),
    t1: parse(frames[frames.length - 1].received_at),
    at: parse(frames[0].received_at),
    playing: true,
    rate: 4,
    lastStep: performance.now(),
    shownEvents: -1,
  };
  S.layout = null;               // force the riding layout to be rebuilt for replay
  $('replaybar').hidden = false;
  $('replaywho').textContent =
    `${data.trip.rider} · ${localDate(data.trip.started_at)}`;
  $('replayrate').textContent = `${S.replay.rate}×`;
  $('replayplay').textContent = '⏸';
  $('log').innerHTML = '';
  S.halted = false;
  drawReplay(true);
}

function exitReplay() {
  if (!S.replay) return;
  S.replay = null;
  S.layout = null;
  $('replaybar').hidden = true;
  $('log').innerHTML = '';
  refresh().catch(() => {});
}

/** The frame at the current replay time, and everything drawn from it. */
function drawReplay(rebuild) {
  const r = S.replay;
  if (!r) return;
  let i = 0;
  while (i < r.frames.length - 1 && parse(r.frames[i + 1].received_at) <= r.at) i++;
  const f = r.frames[i];
  const rider = r.trip.rider;

  // The same shape the live poll produces, so every existing drawing function works.
  const upTo = r.frames.slice(0, i + 1);
  const peakG = Math.max(...upTo.map((x) => x.peak_g || 0), 0);
  const peakRot = Math.max(...upTo.map((x) => x.peak_rot || 0), 0);
  const synthetic = {
    server: { up: true, ping_ms: null, checked_at: r.frames[i].received_at, error: null },
    stream_up: true,
    packets_per_minute: 12,
    weather: null,
    attention: null,
    alarm: { sounding: false, muted: true, reason: null },
    drill: false,
    paired: true,
    devices_available: false,
    archivist: true,
    now: f.received_at,
    riders: {},
  };
  synthetic.riders[rider] = {
    name: rider.charAt(0).toUpperCase() + rider.slice(1),
    state: f.state || 'riding',
    trip_id: r.trip.trip_id,
    trip_started: r.trip.started_at,
    last_seen_s: 0,
    speed: f.speed, battery: f.battery, accuracy: f.accuracy,
    peak_g: f.peak_g, mean_g: f.mean_g, peak_rot: f.peak_rot, accel_n: f.accel_n,
    max_g: peakG, max_rot: peakRot, max_g_at: null, max_rot_at: null,
    peaks_scope: 'trip', lat: f.lat, lon: f.lon,
    last_received_at: f.received_at,
    incident: null,
    signal: null,
  };
  S.status = synthetic;

  // One side, the whole width: the ride being watched again, and nothing else.
  if (rebuild || S.layout !== `replay:${rider}`) {
    S.layout = `replay:${rider}`;
    buildSplit(synthetic);
    drawSide(synthetic, rider);
    const host = S.sides[rider].slot.querySelector('.maphost');
    if (host) {
      const exit = el('button', null, 'Exit replay');
      exit.id = 'exitreplay';
      exit.addEventListener('click', exitReplay);
      host.append(exit);
    }
  }
  const side = S.sides[rider];
  side.trail = upTo.map((x) => ({ lat: x.lat, lon: x.lon, speed: x.speed, at: x.received_at }));
  drawSide(synthetic, rider);

  // Counters would otherwise be measured against the real clock.
  side.q('telapsed').textContent = clock((r.at - parse(r.trip.started_at)) / 1000, true);
  side.q('tlast').textContent = 'replay';
  side.q('tlast').style.color = '';
  if (side.bubble) {
    const moving = (f.speed || 0) >= MOVING_KMH;
    side.bubble.setContent(`<div class="speedbub${moving ? '' : ' stopped'}">` +
      (moving ? `${Math.round(f.speed)} km/h` : 'stopped') + '</div>');
  }

  // The log, up to this moment.
  const due = r.events.filter((e) => parse(e.ts) <= r.at);
  if (due.length !== r.shownEvents) {
    r.shownEvents = due.length;
    $('log').innerHTML = '';
    drawLog(due);
  }

  const span = Math.max(1, r.t1 - r.t0);
  $('scrub').value = String(Math.round(((r.at - r.t0) / span) * 1000));
  $('replayclock').textContent =
    `${logTime(f.received_at)}  ·  ${clock((r.at - r.t0) / 1000, true)} of ${clock(span / 1000, true)}`;
}

/** Advances replay on the real clock, at whatever rate is chosen. */
function stepReplay() {
  const r = S.replay;
  if (!r) return;
  const now = performance.now();
  const elapsed = now - r.lastStep;
  r.lastStep = now;
  if (!r.playing) return;
  r.at += elapsed * r.rate;
  if (r.at >= r.t1) {
    r.at = r.t1;
    r.playing = false;
    $('replayplay').textContent = '▶';
  }
  drawReplay(false);
}

/* ---------------------------------------------------------------- devices */

async function showDevices() {
  closeModal();
  const scrim = el('div', 'scrim');
  const box = el('div', 'box');
  box.append(el('h3', null, 'Devices'));
  box.append(el('div', 'when', 'Keys the relay accepts. A new phone needs a pairing code; a lost phone is revoked here. '
    + 'A rider’s phone is also the other rider’s Observer — it switches by itself when a trip opens.'));
  const list = el('div', null, 'loading…');
  box.append(list);

  const fields = el('div', 'fields');
  const role = el('select');
  // Two roles only (Jack, 2026-09-15). A phone is paired to its rider; it becomes
  // an Observer by itself whenever the other rider has a trip open.
  [['rider', 'Rider phone'], ['monitor', 'Monitor (this Mac)']]
    .forEach(([v, label]) => { const o = el('option', null, label); o.value = v; role.append(o); });
  const who = el('select');
  [['jack', 'Jack'], ['dana', 'Dana']].forEach(([v, label]) => {
    const o = el('option', null, label); o.value = v; who.append(o);
  });
  const name = el('input');
  name.type = 'text';
  name.placeholder = 'e.g. Jack’s S21 FE';
  fields.append(el('div', null, 'Role'), role, el('div', null, 'Rider'), who, el('div', null, 'Name'), name);
  box.append(fields);
  role.addEventListener('change', () => { who.disabled = role.value !== 'rider'; });

  const out = el('div');
  box.append(out);
  const actions = el('div', 'actions');
  const pair = el('button', 'primary', 'Create pairing code');
  const done = el('button', 'ghost', 'Close');
  done.addEventListener('click', closeModal);
  actions.append(pair, done);
  box.append(actions);
  const err = el('div', 'err');
  err.style.display = 'none';
  box.append(err);

  pair.addEventListener('click', async () => {
    err.style.display = 'none';
    pair.disabled = true;
    try {
      const r = await api('/api/devices/pair',
        { role: role.value, rider: role.value === 'rider' ? who.value : null, name: name.value });
      if (!r.ok) throw new Error(r.error || 'could not create a code');
      out.innerHTML = '';
      out.append(el('div', 'code', r.code));
      out.append(el('div', 'note', 'Type this on the phone within ten minutes. It works once.'));
      load();
    } catch (e) {
      err.style.display = '';
      err.textContent = String(e.message || e);
    }
    pair.disabled = false;
  });

  scrim.append(box);
  document.body.append(scrim);
  S.modal = scrim;

  async function load() {
    try {
      const r = await api('/api/devices');
      list.innerHTML = '';
      if (!r.ok) { list.append(el('div', 'err', r.error || 'the VPS did not answer')); return; }
      const table = el('table', 'dev');
      const head = el('tr');
      ['#', 'Role', 'Rider', 'Name', 'Last seen', ''].forEach((h) => head.append(el('th', null, h)));
      table.append(head);
      for (const d of r.devices) {
        if (d.role === 'smoke') continue;
        const tr = el('tr', d.revoked_at ? 'revoked' : '');
        [d.id, d.role, d.rider_id || '—', d.name,
         d.revoked_at ? 'revoked' : (d.last_seen_at ? localTime(d.last_seen_at) : 'never')]
          .forEach((v) => tr.append(el('td', null, String(v))));
        const td = el('td');
        if (!d.revoked_at) {
          const rev = el('button', 'ghost danger', 'Revoke');
          rev.style.padding = '2px 8px';
          rev.addEventListener('click', async () => {
            rev.disabled = true;
            await api('/api/devices/revoke', { id: d.id }).catch(() => {});
            load();
          });
          td.append(rev);
        }
        tr.append(td);
        table.append(tr);
      }
      list.append(table);
    } catch (e) {
      list.innerHTML = '';
      list.append(el('div', 'err', String(e.message || e)));
    }
  }
  load();
}

/* ---------------------------------------------------------------- log */

/*
 * What colour a log line gets (Jack, 2026-09-16):
 *
 *   blue    the rider's state changed — trip started, ride resumed, off the bike,
 *           the signal back
 *   yellow  hard braking, or a possible incident that has not escalated
 *   red     an incident, open or closed
 *
 * Matched on what the line says, not on the relay's level: a closed incident is
 * written at `info` and must not look like an ordinary tick, while a retraction is
 * written at the same level as a crash and must not look like one.
 */
const LOG_RED = [
  'crash detected', 'no signal from', 'i need help', 'pressed i need help',
  'closed', 'closes it', 'silenced', 'waiting for the rider', 'incident still open',
];
const LOG_YELLOW = [
  'possible', 'candidate', 'retract', 'cleared itself', 'signal lost', 'hard brak',
  'checking', 'watching',
];

function logClass(e) {
  const tag = e.tag || '';
  const text = (e.message || '').toLowerCase();
  if (tag === 'incident' || tag === 'drill' || tag === 'signal') {
    // Red first: "candidate #7 was not retracted in time" is a crash, not a retraction.
    if (LOG_RED.some((w) => text.includes(w))) return 'incident';
    if (LOG_YELLOW.some((w) => text.includes(w))) return 'possible';
    return 'incident';
  }
  if (tag === 'trip' || tag === 'rider' || tag === 'stop' || tag === 'signal-ok') return 'state';
  if (LOG_YELLOW.some((w) => text.includes(w))) return 'possible';
  if (e.level === 'alert') return 'incident';
  if (e.level === 'warn') return 'possible';
  return '';
}

function drawLog(entries) {
  const box = $('log');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const e of entries) {
    const line = el('div', 'l ' + logClass(e));
    line.append(el('span', 't', logTime(e.ts)));
    line.append(el('span', 'who', e.rider || ''));
    line.append(el('span', 'm', e.message));
    box.append(line);
  }
  while (box.childElementCount > 900) box.firstElementChild.remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function noteInLog(text) {
  const line = el('div', 'l halted');
  line.append(el('span', 't', logTime(new Date().toISOString())));
  line.append(el('span', 'who', ''));
  line.append(el('span', 'm', text));
  $('log').append(line);
  $('log').scrollTop = $('log').scrollHeight;
}

async function pollLog() {
  try {
    const r = await api(`/api/events?after=${S.logSeq}`);
    S.logSeq = r.seq;
    if (!S.halted && r.entries.length) drawLog(r.entries);
  } catch (e) { /* the next poll will catch up */ }
}

/* ---------------------------------------------------------------- main loop */

async function refresh() {
  const st = await api('/api/status');
  if (S.replay) {
    // Still listening: a real incident outranks whatever is being watched again.
    const live = Object.values(st.riders || {}).find((r) => r.incident
      && r.incident.state === 'sos');
    // (Either rider's: the replay stops for whoever needs looking at.)
    if (live) {
      exitReplay();
    } else {
      drawHeader(st);
      return;
    }
  }
  S.status = st;
  const t = parse(st.now);
  if (!isNaN(t)) S.skew = Date.now() - t;          // counters run on the relay's clock
  drawHeader(st);

  // A Mac with no key of its own can only ask for one.
  if (!st.paired) {
    $('btn-devices').hidden = true;
    if (S.layout !== 'pairing') {
      S.layout = 'pairing';
      for (const side of Object.values(S.sides)) if (side.map) side.map.remove();
      S.sides = {};
      buildPairing(st);
    }
    return;
  }
  $('btn-devices').hidden = !st.devices_available;

  const ids = ridersInOrder(st);
  if (S.layout !== 'split' || ids.some((id) => !S.sides[id])) {
    S.layout = 'split';
    buildSplit(st);
  }

  // Trip boundaries drive the log. It clears when a trip starts with nobody else out,
  // and halts once every trip has ended; a second rider setting off only says so,
  // because the first rider's ride is still being written (Jack, 2026-09-24).
  const riding = ridingIds(st);
  let fresh = S.liveTrips.size === 0;      // nobody was out: this starts a new log
  for (const id of riding) {
    const r = st.riders[id];
    if (!S.seenTrips.has(r.trip_id)) {
      S.seenTrips.add(r.trip_id);
      if (fresh) {
        fresh = false;                     // once per poll, or the second start wipes the first
        $('log').innerHTML = '';
        S.halted = false;
      }
      noteInLog(`— trip #${r.trip_id} started: ${r.name} —`);
    }
    const speed = r.speed || 0;
    if (speed >= MOVING_KMH) S.stoppedSince[id] = null;
    else if (!S.stoppedSince[id]) S.stoppedSince[id] = r.last_received_at;
    const side = S.sides[id];
    if (side && (Date.now() - side.trailAt > TRAIL_MS || side.trailTrip !== r.trip_id)) {
      side.trailAt = Date.now();
      side.trailTrip = r.trip_id;
      const trip = r.trip_id;
      api(`/api/trail?trip_id=${trip}`)
        .then((t) => { if (side.trailTrip === trip) side.trail = t.trail || []; }).catch(() => {});
    }
  }
  if (!riding.length && !S.halted && S.seenTrips.size) {
    S.halted = true;
    noteInLog('— trip ended; the log stops here until the next trip —');
  }
  S.liveTrips = new Set(riding.map((id) => st.riders[id].trip_id));

  // A rider with something open is never left folded away out of sight.
  if (S.folded && currentIncident(S.folded)) setFolded(null);
  for (const id of ids) drawSide(st, id);
  drawBars(st);

  // The incident box follows the relay: it opens itself when an incident is raised
  // and closes when the incident is closed. With both riders out, one box at a time:
  // a second incident opens once the first box is shut, and its side is red meanwhile.
  if (S.incidentOpen) {
    const inc = currentIncident(S.incidentOpen.rider);
    if (!inc || inc.id !== S.incidentOpen.id) {
      closeModal();
    } else if (incidentSignature(inc) !== S.incidentSig) {
      // Silenced, or closed by one side: redraw it. This used to wait while the phrase
      // was being typed, so the box went stale — still offering Silence and Close after
      // both had been done (2026-09-18). Now the half-typed phrase is carried across.
      const typing = document.querySelector('.box.incident input');
      showIncident(S.incidentOpen.rider, typing ? typing.value : undefined);
    }
  }
  if (!S.incidentOpen) {
    for (const id of ids) {
      const inc = currentIncident(id);
      if (inc && inc.state === 'sos' && !S.announced.has(inc.id)) {
        S.announced.add(inc.id);
        showIncident(id);
        break;
      }
    }
  }
  $('logstate').textContent = st.stream_up ? '' : 'stream down';
}

function loop() {
  refresh().catch(() => {
    $('srvdot').className = 'dot bad';
    $('srvtext').textContent = 'app not answering';
  });
  pollLog();
}

/* ---------------------------------------------------------------- start */

$('loghead').addEventListener('click', () => $('logwrap').classList.toggle('collapsed'));
$('gear').addEventListener('click', (e) => { e.stopPropagation(); openGear(); });
document.addEventListener('click', (e) => {
  if (!$('gearmenu').hidden && !$('gearmenu').contains(e.target) && e.target !== $('gear')) openGear(false);
});
$('btn-test').addEventListener('click', () => {
  openGear(false);
  api('/api/alarm/test', {}).then(refresh).catch(() => {});
});
$('btn-drill').addEventListener('click', async () => {
  openGear(false);
  const running = S.status && S.status.drill;
  try {
    await api(running ? '/api/drill/stop' : '/api/drill', {});
    await refresh();
  } catch (e) {
    alert(String(e.message || e));      // "someone is riding", most likely
  }
});
$('btn-devices').addEventListener('click', () => { openGear(false); showDevices(); });
$('btn-forceend').addEventListener('click', () => { openGear(false); askForceEnd(null); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { openGear(false); closeModal(); }
});
window.addEventListener('resize', () => {
  for (const side of Object.values(S.sides)) if (side.map) side.map.invalidateSize();
});

$('replayexit').addEventListener('click', exitReplay);
$('replayplay').addEventListener('click', () => {
  const r = S.replay;
  if (!r) return;
  r.playing = !r.playing;
  r.lastStep = performance.now();
  $('replayplay').textContent = r.playing ? '⏸' : '▶';
});
$('replayrate').addEventListener('click', () => {
  const r = S.replay;
  if (!r) return;
  r.rate = RATES[(RATES.indexOf(r.rate) + 1) % RATES.length];
  $('replayrate').textContent = `${r.rate}×`;
});
$('scrub').addEventListener('input', () => {
  const r = S.replay;
  if (!r) return;
  r.at = r.t0 + ((Number($('scrub').value) / 1000) * (r.t1 - r.t0));
  r.lastStep = performance.now();
  drawReplay(false);
});
setInterval(stepReplay, 100);

S.folded = loadFolded();
loop();
setInterval(loop, POLL_MS);
setInterval(tick, TICK_MS);
