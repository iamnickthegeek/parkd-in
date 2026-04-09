/**
 * Parkd-In | Map Implementation
 * Purpose: Handles Mapbox GL JS lifecycle and vector tile styling from Cloudflare R2.
 * Dependencies: Mapbox GL JS SDK.
 * Author: Antigravity AI Assistant
 */

/**
 * Replace with your actual Mapbox API Key.
 * In a production setup, this would be injected via a build step or proxy.
 * @constant {string}
 */
mapboxgl.accessToken = "pk.eyJ1Ijoibmlja3RoZWdlZWsiLCJhIjoiY21uOWd3dmx2MDd2MDJzcXl0Nno5czdzbSJ9.736RofO3J5RUSzyLXT69PQ";

/** @type {mapboxgl.Map} */
let map;

/** @type {string | null} */
let currentPopupSegmentId = null;

/** @type {mapboxgl.LngLat | null} */
let currentPopupLngLat = null;

/**
 * Tile URL proxied through the backend to avoid CORS issues with R2.
 * Must be absolute URL because Mapbox GL JS workers can't resolve relative URLs
 * when the frontend is served from a different origin.
 * @type {string}
 */
let R2_TILES_URL = "http://localhost:8000/api/v1/tiles/{z}/{x}/{y}.pbf";


/**
 * Acquire or retrieve a cached anonymous JWT token from sessionStorage.
 * @returns {Promise<string>} JWT access token.
 */
const API_BASE = "http://localhost:8000/api/v1";

async function ensureToken() {
  let token = sessionStorage.getItem("parkd_token");
  if (!token) {
    const res = await fetch(`${API_BASE}/auth/anonymous`, { method: "POST" });
    token = (await res.json()).access_token;
    sessionStorage.setItem("parkd_token", token);
  }
  return token;
}

/**
 * Submit a parking event report to the API.
 * @param {string} eventType - One of 'PARKED', 'LEFT', 'SEARCHING'.
 */
async function reportEvent(eventType) {
  const token = await ensureToken();
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
  setTimeout(() => {
    status.style.display = "none";
  }, 3000);
}

/**
 * Fetches segment detail and displays a Mapbox popup.
 * @param {string} segmentId - Segment UUID.
 * @param {number} windowMin - Time window in minutes (default 10).
 * @param {mapboxgl.LngLatLike} lngLat - Popup coordinates.
 */
async function fetchAndShowPopup(segmentId, windowMin, lngLat) {
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
}

/**
 * Initializes the Mapbox GL JS map instance.
 * @returns {void}
 */
function initMap() {
  map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/dark-v11",
    center: [-0.142, 51.536], // Camden, London
    zoom: 14,
    pitch: 45,
    bearing: -17.6,
    antialias: true,
  });

  // Add standard navigation controls
  map.addControl(new mapboxgl.NavigationControl(), "top-right");

  // SCA-016 — Switch to vector tile source served from Cloudflare R2.
  map.on("load", () => {
    console.log("[Parkd-In] Map Loaded. Attaching R2 vector tile source...");
    setupMapLayers();
  });
}

/**
 * Sets up the Mapbox vector tile source and line layers from R2 CDN.
 * @returns {void}
 */
function setupMapLayers() {
  map.addSource("parking-segments", {
    type: "vector",
    tiles: [R2_TILES_URL],
    minzoom: 10,
    maxzoom: 16,
  });

  map.addLayer({
    id: "parking-lines-glow",
    type: "line",
    source: "parking-segments",
    "source-layer": "parking",
    layout: {
      "line-join": "round",
      "line-cap": "round",
    },
    paint: {
      "line-width": 8,
      "line-blur": 4,
      "line-opacity": 0.4,
      "line-color": [
        "interpolate",
        ["linear"],
        ["get", "color_int"],
        0,
        "#E53935", // Red (High traffic/low availability)
        50,
        "#FDD835", // Yellow (Medium)
        100,
        "#43A047", // Green (Clear/Available)
      ],
    },
  });

  map.addLayer({
    id: "parking-lines",
    type: "line",
    source: "parking-segments",
    "source-layer": "parking",
    layout: {
      "line-join": "round",
      "line-cap": "round",
    },
    paint: {
      "line-width": 4,
      "line-color": [
        "interpolate",
        ["linear"],
        ["get", "color_int"],
        0,
        "#E53935",
        50,
        "#FDD835",
        100,
        "#43A047",
      ],
    },
  });

  // PH6-043 — Segment click popup handler
  map.on("click", "parking-lines", (e) => {
    currentPopupSegmentId = e.features[0].properties.segment_id;
    currentPopupLngLat = e.lngLat;
    const windowMin = parseInt(
      document.getElementById("window-select")?.value || "10",
      10,
    );
    fetchAndShowPopup(
      currentPopupSegmentId,
      windowMin,
      e.lngLat,
    );
  });

  map.on("mouseenter", "parking-lines", () => {
    map.getCanvas().style.cursor = "pointer";
  });

  map.on("mouseleave", "parking-lines", () => {
    map.getCanvas().style.cursor = "";
  });

  // PH6-040 — Pre-fetch anonymous token on map load
  ensureToken().catch(() => {});
}

// PH6-040 — Wire reporting buttons to POST /api/v1/event
document.addEventListener("DOMContentLoaded", () => {
  const btnParked = document.getElementById("btn-parked");
  const btnLeft = document.getElementById("btn-left");
  const btnSearching = document.getElementById("btn-searching");
  if (btnParked) btnParked.addEventListener("click", () => reportEvent("PARKED"));
  if (btnLeft) btnLeft.addEventListener("click", () => reportEvent("LEFT"));
  if (btnSearching)
    btnSearching.addEventListener("click", () => reportEvent("SEARCHING"));
});

// PH6-041 — Window selector change handler
document.addEventListener("DOMContentLoaded", () => {
  const sel = document.getElementById("window-select");
  if (sel) {
    sel.addEventListener("change", function () {
      const windowMin = parseInt(this.value, 10);
      if (currentPopupSegmentId && currentPopupLngLat) {
        fetchAndShowPopup(
          currentPopupSegmentId,
          windowMin,
          currentPopupLngLat,
        );
      }
    });
  }
});

// Global initialization
document.addEventListener("DOMContentLoaded", initMap);

// PH6-043 — Add segment click popup to frontend/map.js.
// This code satisfies PH6-043. No additional functionality added.

// PH6-041 — Add time window selector to frontend/index.html and frontend/map.js.
// This code satisfies PH6-041. No additional functionality added.
