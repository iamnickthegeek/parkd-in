/**
 * Parkd-In | Map Implementation v2
 *
 * Single-sheet redesign: one bottom drawer (Level 0 / 1 / 2) replaces the
 * two separate panels.  All scoring logic is unchanged; only presentation
 * and state management are new.
 *
 * Data sources:
 *   /segments.json — road centroids + base probabilities (served by Vercel CDN)
 *   /api/v1/parking/event — crowd reports (gracefully skipped if API offline)
 */

mapboxgl.accessToken =
  "pk.eyJ1Ijoibmlja3RoZWdlZWsiLCJhIjoiY21udTZ5YTVkMDhuejJxc2N6MjZzb2dwNSJ9.GNQEyAD5HwhNb42cznksOQ";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const API_BASE        = "/api/v1";
const SEGMENTS_URL    = "/segments.json";
const TILE_BASE       = window.location.origin;
const TILES_URL       = `${TILE_BASE}/tiles/{z}/{x}/{y}.pbf`;
const SEARCH_RADIUS_M = 500;
const MAX_RECS        = 5;
const REFRESH_MS      = 60_000;

// Time-window probability multipliers (mirror engine.py WINDOW_MULTIPLIERS).
const WINDOW_MULT = { 5: 0.75, 10: 1.0, 15: 1.20 };

// ---------------------------------------------------------------------------
// Tier system — replaces raw probStyle()
// ---------------------------------------------------------------------------

/** Inline SVG icons per probability tier (no external dependency). */
const TIER_ICONS = {
  A: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
  B: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`,
  C: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
  D: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
};

/** Map probability [0,1] → tier descriptor. */
function probTier(p) {
  if (p >= 0.65) return { tier: "A", label: "Very likely", color: "#22c55e" };
  if (p >= 0.50) return { tier: "B", label: "Likely",      color: "#84cc16" };
  if (p >= 0.30) return { tier: "C", label: "Mixed",       color: "#f59e0b" };
  return               { tier: "D", label: "Unlikely",     color: "#ef4444" };
}

// ---------------------------------------------------------------------------
// App state machine
// ---------------------------------------------------------------------------

const S = {
  LOADING:       "loading",       // segments/GPS not yet ready
  NO_CENTER:     "noCenter",      // GPS denied or not set — waiting for tap
  HAS_CENTER:    "hasCenter",     // results available
  OUT_OF_BOUNDS: "outOfBounds",   // outside Camden data area
};

let appState = S.LOADING;
let sheetLvl = -1;  // -1 = hidden

/** Set CSS class on the sheet, driving the translateY level. */
function setSheet(level) {
  sheetLvl = level;
  const el = document.getElementById("bottom-sheet");
  if (!el) return;
  el.className = level < 0 ? "sheet-hidden" : `level${level}`;

  // Belt-and-braces: explicitly control visibility so CSS cascade can't bleed
  // level content between states during the translateY transition.
  const levels = [
    document.querySelector(".sheet-l0"),
    document.querySelector(".sheet-l1"),
    document.querySelector(".sheet-l2"),
  ];
  levels.forEach((node, i) => {
    if (!node) return;
    node.style.display = i === level ? "flex" : "none";
  });
}

/** Update the Level-0 glance card with the top result. */
function updateGlanceCard(recs) {
  const tierEl   = document.getElementById("glance-tier");
  const streetEl = document.getElementById("glance-street");
  const subEl    = document.getElementById("glance-sub");
  const walkEl   = document.getElementById("glance-walk");

  if (!recs || recs.length === 0) {
    tierEl.removeAttribute("data-tier");
    tierEl.innerHTML     = "";
    streetEl.textContent = "No results nearby";
    subEl.textContent    = "";
    walkEl.textContent   = "";
    return;
  }

  const top = recs[0];
  const t   = probTier(top.prob);
  const wm  = walkMin(top.walkM);

  tierEl.dataset.tier  = t.tier;
  tierEl.innerHTML     = TIER_ICONS[t.tier];
  streetEl.textContent = top.name;
  subEl.textContent    = t.label;
  walkEl.textContent   = `${wm} min`;
}

/** Drive glance-card content and (optionally) sheet level based on app state. */
function setAppState(state, data = {}) {
  appState = state;

  const tierEl   = document.getElementById("glance-tier");
  const streetEl = document.getElementById("glance-street");
  const subEl    = document.getElementById("glance-sub");
  const walkEl   = document.getElementById("glance-walk");

  switch (state) {

    case S.LOADING:
      tierEl.removeAttribute("data-tier");
      tierEl.innerHTML     = `<span style="font-size:20px;opacity:0.45">&#8943;</span>`;
      streetEl.textContent = "Finding parking near you\u2026";
      subEl.textContent    = "Loading road data";
      walkEl.textContent   = "";
      setSheet(0);
      break;

    case S.NO_CENTER:
      tierEl.removeAttribute("data-tier");
      tierEl.innerHTML     =
        `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="10" r="3"/><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z"/></svg>`;
      streetEl.textContent = "Tap the map to find parking";
      subEl.textContent    = "GPS off \u00b7 or try Demo";
      walkEl.textContent   = "";
      setSheet(0);
      break;

    case S.HAS_CENTER:
      updateGlanceCard(data.recs);
      // Don't collapse an already-open sheet on GPS refresh
      if (sheetLvl < 0) setSheet(0);
      break;

    case S.OUT_OF_BOUNDS:
      tierEl.removeAttribute("data-tier");
      tierEl.innerHTML     =
        `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`;
      streetEl.textContent = "No Camden data here";
      subEl.textContent    = "Tap Demo to explore Camden";
      walkEl.textContent   = "";
      setSheet(0);
      break;
  }
}

// ---------------------------------------------------------------------------
// Core state
// ---------------------------------------------------------------------------

/** @type {mapboxgl.Map} */
let map;

/** Full segments array from segments.json */
let allSegments    = [];
let segmentsLoaded = false;

/** @type {{ lat: number, lng: number } | null} */
let searchCenter   = null;

/** @type {mapboxgl.Marker[]} */
let parkingMarkers = [];

/** @type {mapboxgl.Marker | null} */
let destinationMarker = null;

/** @type {number | null} auto-refresh interval handle */
let refreshTimer = null;

/** Promise for the in-flight loadSegments call */
let segmentsPromise = null;

/** watchPosition ID — non-null while continuous GPS tracking is active */
let watchId = null;

/** Last GPS position used as search centre (movement threshold) */
let lastWatchPos = null;

/** Currently open detail record */
let activeRec = null;

/** Current prediction window in minutes */
let windowMin = 10;

// ---------------------------------------------------------------------------
// Camden time-of-day factor
// ---------------------------------------------------------------------------

function camdenTimeFactor() {
  const now = new Date();
  const h   = now.getHours() + now.getMinutes() / 60;
  const dow = now.getDay();

  if (dow === 0) return 1.0;

  if (dow === 6) {
    if (h < 9)    return 1.0;
    if (h < 10)   return 0.80;
    if (h < 13)   return 0.70;
    if (h < 16)   return 0.75;
    if (h < 18.5) return 0.70;
    return 0.95;
  }

  // Weekday
  if (h < 7)    return 1.0;
  if (h < 8.5)  return 0.70;
  if (h < 9.5)  return 0.45;
  if (h < 10.5) return 0.60;
  if (h < 17.5) return 0.70;
  if (h < 19)   return 0.45;
  if (h < 19.5) return 0.80;
  return 0.95;
}

function adjustedProb(baseProb, win = 10) {
  return Math.min(1.0, baseProb * camdenTimeFactor() * (WINDOW_MULT[win] ?? 1.0));
}

/** Walking minutes at 80 m/min. */
function walkMin(metres) {
  return Math.max(1, Math.round(metres / 80));
}

// ---------------------------------------------------------------------------
// Haversine distance (metres)
// ---------------------------------------------------------------------------

function haversineM(lat1, lon1, lat2, lon2) {
  const R  = 6371000;
  const r1 = lat1 * Math.PI / 180;
  const r2 = lat2 * Math.PI / 180;
  const dl = (lat2 - lat1) * Math.PI / 180;
  const dg = (lon2 - lon1) * Math.PI / 180;
  const a  =
    Math.sin(dl / 2) ** 2 +
    Math.cos(r1) * Math.cos(r2) * Math.sin(dg / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// ---------------------------------------------------------------------------
// Load segments
// ---------------------------------------------------------------------------

function loadSegments() {
  if (segmentsPromise) return segmentsPromise;
  segmentsPromise = (async () => {
    try {
      const res = await fetch(SEGMENTS_URL);
      if (!res.ok) {
        console.warn(`[Parkd-In] segments.json: HTTP ${res.status}`);
        return;
      }
      const data = await res.json();
      allSegments    = data.segments || [];
      segmentsLoaded = true;
      console.log(`[Parkd-In] Loaded ${allSegments.length} segments`);
    } catch (e) {
      console.warn("[Parkd-In] Could not load segments.json:", e);
    }
  })();
  return segmentsPromise;
}

// ---------------------------------------------------------------------------
// Best nearby — pure client-side scoring
// ---------------------------------------------------------------------------

function bestNearby(win = 10) {
  if (!searchCenter || allSegments.length === 0) return [];

  const tf       = camdenTimeFactor();
  const scored   = [];
  const seenNames = new Map();

  for (const seg of allSegments) {
    const dist = haversineM(searchCenter.lat, searchCenter.lng, seg.lat, seg.lon);
    if (dist > SEARCH_RADIUS_M) continue;

    const name = seg.n || "";
    if (name && seenNames.has(name) && seenNames.get(name) <= dist) continue;
    if (name) seenNames.set(name, dist);

    const baseProb  = seg.p / 100;
    const adjProb   = Math.min(1.0, baseProb * tf * (WINDOW_MULT[win] ?? 1.0));
    const proximity = 1.0 - dist / SEARCH_RADIUS_M;
    const score     = adjProb * 0.70 + proximity * 0.30;

    scored.push({ score, adjProb, dist, seg });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, MAX_RECS).map((item, i) => ({
    rank:      i + 1,
    name:      item.seg.n || "Unnamed street",
    lat:       item.seg.lat,
    lon:       item.seg.lon,
    baseProb:  item.seg.p / 100,
    prob:      item.adjProb,
    walkM:     item.dist,
    roadClass: item.seg.c,
  }));
}

// ---------------------------------------------------------------------------
// Set search centre and trigger refresh
// ---------------------------------------------------------------------------

function setSearchCenter(lngLat) {
  searchCenter = lngLat;

  if (destinationMarker) {
    destinationMarker.setLngLat(lngLat);
  } else {
    const el = document.createElement("div");
    el.className = "destination-pin";
    destinationMarker = new mapboxgl.Marker({ element: el, anchor: "bottom" })
      .setLngLat(lngLat)
      .addTo(map);
  }

  refreshRecommendations();

  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshRecommendations, REFRESH_MS);
}

// ---------------------------------------------------------------------------
// Continuous GPS tracking — hands-free driver mode
// ---------------------------------------------------------------------------

const GPS_MIN_MOVE_M = 30;

function startGpsTracking() {
  if (!navigator.geolocation || watchId !== null) return;
  watchId = navigator.geolocation.watchPosition(
    (pos) => {
      const lat     = pos.coords.latitude;
      const lng     = pos.coords.longitude;
      const isFirst = lastWatchPos === null;

      if (
        !isFirst &&
        haversineM(lastWatchPos.lat, lastWatchPos.lng, lat, lng) < GPS_MIN_MOVE_M
      ) return;

      lastWatchPos = { lat, lng };
      if (isFirst) map.flyTo({ center: [lng, lat], zoom: 15, speed: 1.2 });
      setSearchCenter({ lat, lng });
    },
    (err) => {
      console.warn("[Parkd-In] GPS:", err.message);
      // PERMISSION_DENIED — stop watching and surface tap-mode prompt
      if (err.code === 1) {
        stopGpsTracking();
        if (appState === S.LOADING || appState === S.NO_CENTER) {
          setAppState(S.NO_CENTER);
        }
      }
    },
    { enableHighAccuracy: true, maximumAge: 10_000, timeout: 15_000 }
  );
}

function stopGpsTracking() {
  if (watchId !== null) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }
  lastWatchPos = null;
}

// ---------------------------------------------------------------------------
// Refresh recommendations
// ---------------------------------------------------------------------------

function refreshRecommendations() {
  const recs = bestNearby(windowMin);

  renderMarkers(recs);
  renderList(recs);

  if (recs.length === 0) {
    if (segmentsLoaded) setAppState(S.OUT_OF_BOUNDS);
    // else still loading — stay on LOADING state
    return;
  }

  setAppState(S.HAS_CENTER, { recs });
}

// ---------------------------------------------------------------------------
// Render markers — circular ranked badges
// ---------------------------------------------------------------------------

function renderMarkers(recs) {
  parkingMarkers.forEach((m) => m.remove());
  parkingMarkers = [];

  recs.forEach((rec) => {
    const t  = probTier(rec.prob);
    const el = document.createElement("div");
    el.className = "parking-marker";
    // Higher rank number = lower z-index (rank 1 always on top)
    el.style.zIndex = String(MAX_RECS - rec.rank + 1);

    el.innerHTML = `
      <div class="marker-pin${rec.rank === 1 ? " pulse" : ""}"
           style="border-color:${t.color}">
        <span class="marker-rank">${rec.rank}</span>
      </div>
    `;

    el.addEventListener("click", (e) => {
      e.stopPropagation();
      navigator.vibrate?.(10);
      openDetail(rec);
    });

    const marker = new mapboxgl.Marker({ element: el, anchor: "center" })
      .setLngLat([rec.lon, rec.lat])
      .addTo(map);
    parkingMarkers.push(marker);
  });
}

// ---------------------------------------------------------------------------
// Render list — Level 1 content
// ---------------------------------------------------------------------------

function renderList(recs) {
  const list = document.getElementById("rec-list");
  if (!list) return;
  list.innerHTML = "";

  if (recs.length === 0) {
    if (!segmentsLoaded) {
      list.innerHTML = `<li class="rec-empty">Loading road data\u2026</li>`;
    } else {
      list.innerHTML =
        `<li class="rec-empty">No Camden parking data within 500 m.<br>` +
        `<a id="empty-demo-link">Try the Demo</a> to explore Camden Town.</li>`;
      document.getElementById("empty-demo-link")?.addEventListener("click", () =>
        demoMode(51.5391, -0.1426, "Showing parking near Camden Town")
      );
    }
    return;
  }

  recs.forEach((rec) => {
    const t  = probTier(rec.prob);
    const wm = walkMin(rec.walkM);

    const li = document.createElement("li");
    li.className = "result-item";
    li.innerHTML = `
      <span class="rank-badge">${rec.rank}</span>
      <div class="result-info">
        <div class="result-name">${rec.name}</div>
        <div class="result-meta">
          <span class="result-tier-label" style="color:${t.color}">${t.label}</span>
          <span class="result-walk">\u00b7 ${wm} min walk</span>
        </div>
      </div>
      <span class="result-walk-right">${wm} min</span>
    `;
    li.addEventListener("click", () => {
      navigator.vibrate?.(10);
      openDetail(rec);
    });
    list.appendChild(li);
  });
}

// ---------------------------------------------------------------------------
// Detail view — Level 2
// ---------------------------------------------------------------------------

const CLASS_HINTS = {
  street:         "Residential street. Usually free evenings & Sundays.",
  street_limited: "Limited parking. Check signs for hours.",
  service:        "Service road or car park. Check for access restrictions.",
  tertiary:       "Quieter road. May have single yellow lines.",
  secondary:      "Busy road. Single or double yellow lines likely.",
  primary:        "Main road. Likely double yellow \u2014 check before stopping.",
  primary_link:   "Main road junction. Likely double yellow.",
  trunk:          "Trunk road. Stopping almost certainly restricted.",
};

function openDetail(rec) {
  activeRec = rec;

  document.getElementById("sheet-street").textContent = rec.name;
  document.getElementById("sheet-walk").textContent =
    `${walkMin(rec.walkM)} min walk \u00b7 ${Math.round(rec.walkM)} m`;

  document.getElementById("sheet-hint-text").textContent =
    CLASS_HINTS[rec.roadClass] || "";

  // Prediction grid — tier label + % for each window
  [5, 10, 15].forEach((w) => {
    const p   = adjustedProb(rec.baseProb, w);
    const t   = probTier(p);
    const pct = Math.round(p * 100);

    const tierEl = document.getElementById(`sheet-${w}min-tier`);
    const pctEl  = document.getElementById(`sheet-${w}min`);
    if (tierEl) { tierEl.textContent = t.label; tierEl.style.color = t.color; }
    if (pctEl)  { pctEl.textContent  = `${pct}%`; }
  });

  document.getElementById("sheet-navigate").href =
    `https://www.google.com/maps/dir/?api=1` +
    `&destination=${rec.lat},${rec.lon}&travelmode=driving`;

  // Navigate haptic
  document.getElementById("sheet-navigate").onclick = () =>
    navigator.vibrate?.([10, 30, 10]);

  // Reset report expander to closed state
  const toggle = document.getElementById("report-toggle");
  const body   = document.getElementById("report-body");
  if (toggle && body) {
    toggle.setAttribute("aria-expanded", "false");
    body.hidden = true;
  }

  document.getElementById("report-status").textContent = "";

  setSheet(2);
}

// ---------------------------------------------------------------------------
// Reset — close sheet and clear all state
// ---------------------------------------------------------------------------

function resetSearch() {
  stopGpsTracking();
  parkingMarkers.forEach((m) => m.remove());
  parkingMarkers = [];
  if (destinationMarker) { destinationMarker.remove(); destinationMarker = null; }
  searchCenter = null;
  clearInterval(refreshTimer);
  activeRec = null;
  setAppState(S.NO_CENTER);
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

let toastTimer = null;

function showToast(msg, durationMs = 3500) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), durationMs);
}

// ---------------------------------------------------------------------------
// Sheet gesture handling (swipe up/down on handle)
// ---------------------------------------------------------------------------

function setupSheetGestures() {
  const handle = document.getElementById("sheet-handle");
  if (!handle) return;

  let startY = 0;

  handle.addEventListener("touchstart", (e) => {
    startY = e.touches[0].clientY;
  }, { passive: true });

  handle.addEventListener("touchend", (e) => {
    const deltaY = e.changedTouches[0].clientY - startY;
    const isTap  = Math.abs(deltaY) < 10;

    if (isTap || deltaY < -30) {
      // Tap or swipe up — advance level
      if (sheetLvl === 0 && appState === S.HAS_CENTER) setSheet(1);
    } else if (deltaY > 30) {
      // Swipe down — go back one level
      if      (sheetLvl === 2) setSheet(1);
      else if (sheetLvl === 1) setSheet(0);
    }
  }, { passive: true });
}

// ---------------------------------------------------------------------------
// Crowd report (gracefully offline-safe)
// ---------------------------------------------------------------------------

async function ensureToken() {
  let t = sessionStorage.getItem("parkd_token");
  if (!t) {
    try {
      const res = await fetch(`${API_BASE}/auth/anonymous`, { method: "POST" });
      if (res.ok) {
        t = (await res.json()).access_token;
        sessionStorage.setItem("parkd_token", t);
      }
    } catch (_) { /* API offline — silent */ }
  }
  return t;
}

async function reportEvent(eventType) {
  const token = await ensureToken();
  if (!token) return;

  const center = map.getCenter();
  const res = await fetch(`${API_BASE}/parking/event`, {
    method: "POST",
    headers: {
      "Content-Type":  "application/json",
      Authorization:   `Bearer ${token}`,
    },
    body: JSON.stringify({
      lat:        center.lat,
      lon:        center.lng,
      event_type: eventType,
    }),
  });

  navigator.vibrate?.(20);
  const statusEl = document.getElementById("report-status");
  statusEl.textContent = res.ok
    ? "Thanks! Predictions update in ~5 min."
    : "Error \u2014 please try again.";
  setTimeout(() => { statusEl.textContent = ""; }, 3000);
}

// ---------------------------------------------------------------------------
// Road overlay toggle
// ---------------------------------------------------------------------------

let overlayVisible = false;

function toggleOverlay() {
  overlayVisible = !overlayVisible;
  const vis = overlayVisible ? "visible" : "none";
  map.setLayoutProperty("parking-lines-glow", "visibility", vis);
  map.setLayoutProperty("parking-lines",      "visibility", vis);
  // Sync the toggle switch aria-checked (global state — persists across Level 1 visits)
  const btn = document.getElementById("btn-overlay");
  if (btn) btn.setAttribute("aria-checked", String(overlayVisible));
}

// ---------------------------------------------------------------------------
// Map initialisation
// ---------------------------------------------------------------------------

function setupMapLayers() {
  map.addSource("parking-segments", {
    type:    "vector",
    tiles:   [TILES_URL],
    minzoom: 14,
    maxzoom: 14,
  });

  const colorExpr = ["concat", "#", ["get", "color"]];

  map.addLayer({
    id:             "parking-lines-glow",
    type:           "line",
    source:         "parking-segments",
    "source-layer": "parking",
    minzoom:        12, maxzoom: 20,
    layout:  { "line-join": "round", "line-cap": "round", visibility: "none" },
    paint:   {
      "line-width":   ["interpolate", ["linear"], ["zoom"], 12, 4, 16, 12],
      "line-blur":    4,
      "line-opacity": 0.35,
      "line-color":   colorExpr,
    },
  });

  map.addLayer({
    id:             "parking-lines",
    type:           "line",
    source:         "parking-segments",
    "source-layer": "parking",
    minzoom:        12, maxzoom: 20,
    layout:  { "line-join": "round", "line-cap": "round", visibility: "none" },
    paint:   {
      "line-width": ["interpolate", ["linear"], ["zoom"], 12, 2, 16, 6],
      "line-color": colorExpr,
    },
  });
}

function initMap() {
  map = new mapboxgl.Map({
    container: "map",
    style:     "mapbox://styles/mapbox/dark-v11",
    center:    [-0.142, 51.536],
    zoom:      14,
    pitch:     0,
    bearing:   0,
    antialias: true,
  });

  const geolocate = new mapboxgl.GeolocateControl({
    positionOptions:   { enableHighAccuracy: true },
    trackUserLocation: false,
    showUserHeading:   false,
  });
  map.addControl(geolocate, "top-right");
  // NavigationControl (zoom + compass) removed — pinch handles zoom on mobile

  // Manual GPS button — restart continuous tracking from this fix
  geolocate.on("geolocate", (e) => {
    stopGpsTracking();
    setSearchCenter({ lat: e.coords.latitude, lng: e.coords.longitude });
    startGpsTracking();
  });

  // Tap map — stops GPS so pin stays at tapped point
  map.on("click", (e) => {
    if (!e.originalEvent.target.closest(".parking-marker, .destination-pin")) {
      stopGpsTracking();
      setSearchCenter(e.lngLat);
    }
  });

  map.on("load", () => {
    setupMapLayers();
    // Start GPS only after segments have loaded (avoids race where GPS fix
    // arrives before data and returns empty results).
    loadSegments().then(() => {
      // If the user manually tapped or pressed GPS while segments were loading,
      // a searchCenter may already be set — refresh now with real data.
      if (searchCenter) refreshRecommendations();
      startGpsTracking();
    });
  });
}

// ---------------------------------------------------------------------------
// DOM wiring
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  // Show loading state immediately so the sheet is visible before GPS fires
  setAppState(S.LOADING);

  initMap();
  ensureToken().catch(() => {});
  setupSheetGestures();

  // ── Time window pill ──────────────────────────────────────────────────────
  document.querySelectorAll(".time-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".time-btn")
        .forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
      windowMin = parseInt(btn.dataset.mins, 10);
      if (searchCenter) refreshRecommendations();
    });
  });

  // ── Glance card tap ───────────────────────────────────────────────────────
  // HAS_CENTER  → open detail for top result (1 tap to detail, hands-free)
  // NO_CENTER / OUT_OF_BOUNDS → trigger demo
  document.getElementById("glance-row")?.addEventListener("click", () => {
    if (appState === S.HAS_CENTER) {
      const recs = bestNearby(windowMin);
      if (recs.length > 0) {
        navigator.vibrate?.(10);
        openDetail(recs[0]);
      }
    } else if (appState === S.NO_CENTER || appState === S.OUT_OF_BOUNDS) {
      demoMode(51.5391, -0.1426, "Demo: showing parking near Camden Town");
    }
  });

  // ── Sheet navigation ──────────────────────────────────────────────────────
  document.getElementById("sheet-back")?.addEventListener("click", () => {
    setSheet(searchCenter ? 1 : 0);
  });

  // Close from Level 2 — go back to Level 1 (keeps results, matches user expectation)
  document.getElementById("sheet-close")?.addEventListener("click", () => {
    setSheet(searchCenter ? 1 : 0);
  });

  // Close from Level 1 — reset everything
  document.getElementById("rec-close")?.addEventListener("click", resetSearch);

  // ── Report expander ───────────────────────────────────────────────────────
  document.getElementById("report-toggle")?.addEventListener("click", () => {
    const toggle = document.getElementById("report-toggle");
    const body   = document.getElementById("report-body");
    const isOpen = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!isOpen));
    body.hidden = isOpen;
  });

  // ── Report buttons ────────────────────────────────────────────────────────
  document.getElementById("btn-parked")?.addEventListener("click", () =>
    reportEvent("PARKED")
  );
  document.getElementById("btn-left")?.addEventListener("click", () =>
    reportEvent("LEFT")
  );
  document.getElementById("btn-searching")?.addEventListener("click", () =>
    reportEvent("SEARCHING")
  );

  // ── Road overlay ──────────────────────────────────────────────────────────
  document.getElementById("btn-overlay")?.addEventListener("click", toggleOverlay);

  // ── Demo ──────────────────────────────────────────────────────────────────
  document.getElementById("btn-demo")?.addEventListener("click", () =>
    demoMode(51.5391, -0.1426, "Demo: showing parking near Camden Town")
  );
});

// ---------------------------------------------------------------------------
// Demo mode
// ---------------------------------------------------------------------------

function demoMode(lat, lng, label) {
  stopGpsTracking();
  map.flyTo({ center: [lng, lat], zoom: 15, speed: 1.2 });

  function activate() {
    console.log(`[Parkd-In] demoMode: allSegments.length = ${allSegments.length}`);
    setSearchCenter({ lat, lng });
    const recs = bestNearby(windowMin);
    console.log(`[Parkd-In] demoMode: bestNearby found ${recs.length} results`);

    showToast(
      recs.length > 0
        ? `${label} \u2014 ${recs.length} spaces found`
        : `${label} \u2014 no segments loaded (check console)`
    );
  }

  loadSegments().then(activate);
}
