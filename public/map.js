/**
 * Parkd-In | Map Implementation
 * Deployed version — tiles served as static files, API via relative URLs.
 */

mapboxgl.accessToken = "pk.eyJ1Ijoibmlja3RoZWdlZWsiLCJhIjoiY21uOWd3dmx2MDd2MDJzcXl0Nno5czdzbSJ9.736RofO3J5RUSzyLXT69PQ";

/** @type {mapboxgl.Map} */
let map;

/** @type {string | null} */
let currentPopupSegmentId = null;

/** @type {mapboxgl.LngLat | null} */
let currentPopupLngLat = null;

// Tiles are committed as static files — served directly by Vercel CDN.
// Mapbox GL JS workers need an absolute URL, so derive it from window.location.
const TILE_BASE = window.location.origin;
const TILES_URL = `${TILE_BASE}/tiles/{z}/{x}/{y}.pbf`;

// API calls use relative URLs — backend and frontend are on the same domain.
const API_BASE = "/api/v1";

async function ensureToken() {
  let token = sessionStorage.getItem("parkd_token");
  if (!token) {
    try {
      const res = await fetch(`${API_BASE}/auth/anonymous`, { method: "POST" });
      if (res.ok) {
        token = (await res.json()).access_token;
        sessionStorage.setItem("parkd_token", token);
      }
    } catch (_) {
      // API unavailable in static-only deploy — continue without token
    }
  }
  return token;
}

async function reportEvent(eventType) {
  const token = await ensureToken();
  if (!token) return;
  const center = map.getCenter();
  const res = await fetch(`${API_BASE}/parking/event`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      lat: center.lat,
      lon: center.lng,
      event_type: eventType,
    }),
  });
  const status = document.getElementById("report-status");
  status.style.display = "block";
  status.textContent = res.ok
    ? "Reported! Map updates in ~5 min."
    : "Error — please try again.";
  setTimeout(() => { status.style.display = "none"; }, 3000);
}

async function fetchAndShowPopup(segmentId, windowMin, lngLat) {
  try {
    const res = await fetch(`${API_BASE}/parking/segment/${segmentId}`);
    if (!res.ok) return;
    const data = await res.json();
    const probKey = `probability_${windowMin}min`;
    const prob = ((data[probKey] ?? 0) * 100).toFixed(0);
    const html = [
      `<b>${data.street_name || "Unknown street"}</b>`,
      `${prob}% available (${windowMin} min)`,
      `${data.bay_count} bays · ${data.restriction_active ? "⚠ Restricted" : "Unrestricted"}`,
      data.last_crowd_event
        ? `Last report: ${data.last_crowd_event} (${data.last_crowd_event_age_min} min ago)`
        : "No recent reports",
    ].join("<br>");
    new mapboxgl.Popup().setLngLat(lngLat).setHTML(html).addTo(map);
  } catch (_) {}
}

function initMap() {
  map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/dark-v11",
    center: [-0.142, 51.536],
    zoom: 14,
    pitch: 0,
    bearing: 0,
    antialias: true,
  });

  map.addControl(new mapboxgl.NavigationControl(), "top-right");

  map.on("load", () => {
    console.log("[Parkd-In] Map loaded. Attaching vector tile source...");
    setupMapLayers();
  });
}

function setupMapLayers() {
  map.addSource("parking-segments", {
    type: "vector",
    tiles: [TILES_URL],
    minzoom: 14,
    maxzoom: 14,
  });

  // Local tiles use a hex string "color" property (e.g. "00AA00").
  // Use ["concat","#",["get","color"]] to build a CSS colour expression.
  const colorExpr = ["concat", "#", ["get", "color"]];

  map.addLayer({
    id: "parking-lines-glow",
    type: "line",
    source: "parking-segments",
    "source-layer": "parking",
    minzoom: 12,
    maxzoom: 20,
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 12, 4, 16, 12],
      "line-blur": 4,
      "line-opacity": 0.4,
      "line-color": colorExpr,
    },
  });

  map.addLayer({
    id: "parking-lines",
    type: "line",
    source: "parking-segments",
    "source-layer": "parking",
    minzoom: 12,
    maxzoom: 20,
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 12, 2, 16, 6],
      "line-color": colorExpr,
    },
  });

  map.on("click", "parking-lines", (e) => {
    currentPopupSegmentId = e.features[0].properties.segment_id;
    currentPopupLngLat = e.lngLat;
    const windowMin = parseInt(
      document.getElementById("window-select")?.value || "10", 10
    );
    fetchAndShowPopup(currentPopupSegmentId, windowMin, e.lngLat);
  });

  map.on("mouseenter", "parking-lines", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "parking-lines", () => {
    map.getCanvas().style.cursor = "";
  });

  ensureToken().catch(() => {});
}

document.addEventListener("DOMContentLoaded", () => {
  const btnParked = document.getElementById("btn-parked");
  const btnLeft = document.getElementById("btn-left");
  const btnSearching = document.getElementById("btn-searching");
  if (btnParked) btnParked.addEventListener("click", () => reportEvent("PARKED"));
  if (btnLeft) btnLeft.addEventListener("click", () => reportEvent("LEFT"));
  if (btnSearching) btnSearching.addEventListener("click", () => reportEvent("SEARCHING"));

  const sel = document.getElementById("window-select");
  if (sel) {
    sel.addEventListener("change", function () {
      const windowMin = parseInt(this.value, 10);
      if (currentPopupSegmentId && currentPopupLngLat) {
        fetchAndShowPopup(currentPopupSegmentId, windowMin, currentPopupLngLat);
      }
    });
  }

  initMap();
});
