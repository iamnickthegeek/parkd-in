/**
 * Parkd-In | Map Implementation
 *
 * Local-first deploy: no Supabase / Redis / R2 required.
 *
 * Data sources:
 *   /segments.json — road centroids + base probabilities, committed to git
 *                    and served by Vercel CDN (generated daily by GitHub Action)
 *   /tiles/{z}/{x}/{y}.pbf — optional road-overlay tiles (same source)
 *   /api/v1/parking/event — crowd reports (gracefully skipped if API offline)
 *
 * UX: destination-first, driver-safe.
 *   1. Tap the GPS button or tap anywhere on the map → sets search centre.
 *   2. Top 5 parking zones within 500 m appear as numbered "P" markers.
 *   3. Colours and predictions are adjusted for the current time of day.
 *   4. Tapping a marker shows a bottom sheet: 5/10/15-min predictions,
 *      bay type context, and a "Navigate" deep-link to Google Maps.
 *   5. Auto-refreshes colour every 60 s (time factor updates as clock ticks).
 *   6. "Show all roads" toggle reveals the full road-overlay layer.
 */

mapboxgl.accessToken =
  "pk.eyJ1Ijoibmlja3RoZWdlZWsiLCJhIjoiY21uOWd3dmx2MDd2MDJzcXl0Nno5czdzbSJ9.736RofO3J5RUSzyLXT69PQ";

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
// 5 min: less time for spaces to open up; 15 min: more turnover expected.
const WINDOW_MULT = { 5: 0.75, 10: 1.0, 15: 1.20 };

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/** @type {mapboxgl.Map} */
let map;

/** Full segments array loaded from segments.json */
let allSegments = [];

/** @type {{ lat: number, lng: number } | null} */
let searchCenter = null;

/** @type {mapboxgl.Marker[]} current P-markers on the map */
let parkingMarkers = [];

/** @type {mapboxgl.Marker | null} destination pin */
let destinationMarker = null;

/** @type {number | null} setInterval handle */
let refreshTimer = null;

/** Currently open bottom-sheet segment */
let activeRec = null;

// ---------------------------------------------------------------------------
// Camden time-of-day factor
//
// Applied client-side to the base probability from segments.json.
// Reflects CPZ enforcement hours (Mon–Sat 08:30–18:30 in most Camden zones),
// school-run peaks, and evening turnover.
// ---------------------------------------------------------------------------

function camdenTimeFactor() {
  const now = new Date();
  const h   = now.getHours() + now.getMinutes() / 60; // fractional hour
  const dow = now.getDay(); // 0=Sun, 6=Sat

  if (dow === 0) {
    // Sunday: CPZ mostly suspended, easy all day
    return 1.0;
  }

  if (dow === 6) {
    // Saturday: CPZ active 08:30–18:30, shopping traffic midday
    if (h < 9)            return 1.0;
    if (h < 10)           return 0.80;
    if (h < 13)           return 0.70;  // busy shopping period
    if (h < 16)           return 0.75;
    if (h < 18.5)         return 0.70;
    return 0.95;
  }

  // Weekday (Mon–Fri)
  if (h < 7)              return 1.0;   // overnight — easy
  if (h < 8.5)            return 0.70;  // early morning build-up
  if (h < 9.5)            return 0.45;  // peak morning rush + school run
  if (h < 10.5)           return 0.60;  // rush tail
  if (h < 17.5)           return 0.70;  // CPZ in effect, moderate
  if (h < 19)             return 0.45;  // peak evening rush
  if (h < 19.5)           return 0.80;  // CPZ lifting, spaces opening up
  return 0.95;                          // evening — generally free
}

// ---------------------------------------------------------------------------
// Probability helpers
// ---------------------------------------------------------------------------

/**
 * Adjust base probability for time of day and a prediction window.
 * @param {number} baseProb  Base probability in [0, 1].
 * @param {number} windowMin 5, 10, or 15.
 * @returns {number} Adjusted probability in [0, 1].
 */
function adjustedProb(baseProb, windowMin = 10) {
  const tf   = camdenTimeFactor();
  const wm   = WINDOW_MULT[windowMin] ?? 1.0;
  return Math.min(1.0, baseProb * tf * wm);
}

/** Map probability [0,1] → { bg colour, label }. */
function probStyle(p) {
  if (p >= 0.65) return { bg: "#00AA00", label: "Likely available" };
  if (p >= 0.30) return { bg: "#FF8800", label: "Limited"          };
  return               { bg: "#CC0000", label: "Busy"              };
}

/** Approximate walking minutes at 80 m/min. */
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

async function loadSegments() {
  try {
    const res = await fetch(SEGMENTS_URL);
    if (!res.ok) return;
    const data = await res.json();
    allSegments = data.segments || [];
    console.log(`[Parkd-In] Loaded ${allSegments.length} segments`);
  } catch (e) {
    console.warn("[Parkd-In] Could not load segments.json:", e);
  }
}

// ---------------------------------------------------------------------------
// Best nearby — pure client-side (no API)
// ---------------------------------------------------------------------------

/**
 * Return top MAX_RECS segments near searchCenter, scored by probability
 * (70%) and proximity (30%), with client-side time + window adjustment.
 */
function bestNearby(windowMin = 10) {
  if (!searchCenter || allSegments.length === 0) return [];

  const tf = camdenTimeFactor();

  const scored = [];
  const seenNames = new Map(); // name → best dist (dedup near-duplicates)

  for (const seg of allSegments) {
    const dist = haversineM(searchCenter.lat, searchCenter.lng, seg.lat, seg.lon);
    if (dist > SEARCH_RADIUS_M) continue;

    // Deduplicate: if the same street name appears closer already, skip.
    const name = seg.n || "";
    if (name && seenNames.has(name) && seenNames.get(name) <= dist) continue;
    if (name) seenNames.set(name, dist);

    const baseProb  = seg.p / 100;
    const adjProb   = Math.min(1.0, baseProb * tf * (WINDOW_MULT[windowMin] ?? 1.0));
    const proximity = 1.0 - dist / SEARCH_RADIUS_M;
    const score     = adjProb * 0.70 + proximity * 0.30;

    scored.push({ score, adjProb, dist, seg });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, MAX_RECS).map((item, i) => ({
    rank:       i + 1,
    name:       item.seg.n || "Unnamed street",
    lat:        item.seg.lat,
    lon:        item.seg.lon,
    baseProb:   item.seg.p / 100,
    prob:       item.adjProb,
    walkM:      item.dist,
    roadClass:  item.seg.c,
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

  document.getElementById("recommendations-panel").classList.add("visible");
  document.getElementById("search-hint").classList.add("hidden");
  refreshRecommendations();

  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshRecommendations, REFRESH_MS);
}

// ---------------------------------------------------------------------------
// Refresh recommendations
// ---------------------------------------------------------------------------

function refreshRecommendations() {
  const windowMin = parseInt(
    document.getElementById("window-select")?.value || "10",
    10
  );
  const recs = bestNearby(windowMin);
  renderMarkers(recs);
  renderList(recs);
}

// ---------------------------------------------------------------------------
// P-marker rendering
// ---------------------------------------------------------------------------

function renderMarkers(recs) {
  parkingMarkers.forEach((m) => m.remove());
  parkingMarkers = [];

  recs.forEach((rec) => {
    const { bg } = probStyle(rec.prob);
    const wm     = walkMin(rec.walkM);

    const el = document.createElement("div");
    el.className = "parking-marker";
    el.innerHTML = `
      <div class="marker-pin" style="background:${bg}">
        <span class="marker-rank">${rec.rank}</span>
      </div>
      <div class="marker-walk">${wm} min</div>
    `;
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      openSheet(rec);
    });

    const marker = new mapboxgl.Marker({ element: el, anchor: "bottom" })
      .setLngLat([rec.lon, rec.lat])
      .addTo(map);
    parkingMarkers.push(marker);
  });
}

// ---------------------------------------------------------------------------
// Recommendation list
// ---------------------------------------------------------------------------

function renderList(recs) {
  const list = document.getElementById("rec-list");
  if (!list) return;
  list.innerHTML = "";

  if (recs.length === 0) {
    list.innerHTML =
      '<li class="rec-empty">No parking found within 500 m — try tapping a different spot</li>';
    return;
  }

  recs.forEach((rec) => {
    const { bg }  = probStyle(rec.prob);
    const wm      = walkMin(rec.walkM);
    const pct     = Math.round(rec.prob * 100);

    const li = document.createElement("li");
    li.className = "rec-item";
    li.innerHTML = `
      <span class="rec-badge" style="background:${bg}">${rec.rank}</span>
      <span class="rec-name">${rec.name}</span>
      <span class="rec-prob" style="color:${bg}">${pct}%</span>
      <span class="rec-walk">${wm} min walk</span>
    `;
    li.addEventListener("click", () => openSheet(rec));
    list.appendChild(li);
  });
}

// ---------------------------------------------------------------------------
// Bottom sheet — segment detail (no API, all client-side)
// ---------------------------------------------------------------------------

// Human-readable hint per road class about typical restrictions in Camden
const CLASS_HINTS = {
  street:         "Residential street. Usually free evenings & Sundays.",
  street_limited: "Limited parking. Check signs for hours.",
  service:        "Service road or car park. Check for access restrictions.",
  tertiary:       "Quieter road. May have single yellow lines.",
  secondary:      "Busy road. Single or double yellow lines likely.",
  primary:        "Main road. Likely double yellow — check before stopping.",
  primary_link:   "Main road junction. Likely double yellow.",
  trunk:          "Trunk road. Stopping almost certainly restricted.",
};

function openSheet(rec) {
  activeRec = rec;
  const sheet = document.getElementById("detail-sheet");
  sheet.classList.add("visible");

  document.getElementById("sheet-street").textContent = rec.name;
  document.getElementById("sheet-walk").textContent =
    `${walkMin(rec.walkM)} min walk · ${Math.round(rec.walkM)} m`;

  const hint = CLASS_HINTS[rec.roadClass] || "";
  document.getElementById("sheet-hint-text").textContent = hint;

  // 5 / 10 / 15 min predictions — calculated client-side
  [5, 10, 15].forEach((w) => {
    const p   = adjustedProb(rec.baseProb, w);
    const pct = Math.round(p * 100);
    const el  = document.getElementById(`sheet-${w}min`);
    if (el) {
      const { bg } = probStyle(p);
      el.textContent  = `${pct}%`;
      el.style.color  = bg;
    }
  });

  // Navigate deep-link → Google Maps driving directions to the bay
  document.getElementById("sheet-navigate").href =
    `https://www.google.com/maps/dir/?api=1` +
    `&destination=${rec.lat},${rec.lon}&travelmode=driving`;
}

function closeSheet() {
  document.getElementById("detail-sheet").classList.remove("visible");
  activeRec = null;
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
    } catch (_) {
      // API offline — report buttons will silently no-op
    }
  }
  return t;
}

async function reportEvent(eventType) {
  const token = await ensureToken();
  if (!token) return;   // API offline — fail silently

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

  const statusEl = document.getElementById("report-status");
  statusEl.style.display = "block";
  statusEl.textContent   = res.ok
    ? "Thanks! Predictions update in ~5 min."
    : "Error — please try again.";
  setTimeout(() => { statusEl.style.display = "none"; }, 3000);
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
  const btn = document.getElementById("btn-overlay");
  if (btn) btn.textContent = overlayVisible ? "Hide road overlay" : "Show road overlay";
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
    id:           "parking-lines-glow",
    type:         "line",
    source:       "parking-segments",
    "source-layer": "parking",
    minzoom:      12,
    maxzoom:      20,
    layout:       { "line-join": "round", "line-cap": "round", visibility: "none" },
    paint: {
      "line-width":   ["interpolate", ["linear"], ["zoom"], 12, 4, 16, 12],
      "line-blur":    4,
      "line-opacity": 0.35,
      "line-color":   colorExpr,
    },
  });

  map.addLayer({
    id:           "parking-lines",
    type:         "line",
    source:       "parking-segments",
    "source-layer": "parking",
    minzoom:      12,
    maxzoom:      20,
    layout:       { "line-join": "round", "line-cap": "round", visibility: "none" },
    paint: {
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
    positionOptions:  { enableHighAccuracy: true },
    trackUserLocation: false,
    showUserHeading:  false,
  });
  map.addControl(geolocate, "top-right");
  map.addControl(new mapboxgl.NavigationControl(), "top-right");

  geolocate.on("geolocate", (e) => {
    setSearchCenter({ lat: e.coords.latitude, lng: e.coords.longitude });
  });

  // Tap map to set search centre (ignore taps on markers / sheet)
  map.on("click", (e) => {
    if (!e.originalEvent.target.closest(".parking-marker, .destination-pin")) {
      setSearchCenter(e.lngLat);
    }
  });

  map.on("load", () => {
    setupMapLayers();
    loadSegments();   // pre-load so first tap is instant
  });
}

// ---------------------------------------------------------------------------
// DOM wiring
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  initMap();
  ensureToken().catch(() => {});

  document.getElementById("window-select")?.addEventListener("change", () => {
    if (searchCenter) refreshRecommendations();
  });

  document.getElementById("sheet-close")?.addEventListener("click", closeSheet);

  document.getElementById("btn-parked")?.addEventListener("click", () =>
    reportEvent("PARKED")
  );
  document.getElementById("btn-left")?.addEventListener("click", () =>
    reportEvent("LEFT")
  );
  document.getElementById("btn-searching")?.addEventListener("click", () =>
    reportEvent("SEARCHING")
  );

  document.getElementById("btn-overlay")?.addEventListener("click", toggleOverlay);
});
