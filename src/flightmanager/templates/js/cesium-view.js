// ── Cesium 3D flight path view ─────────────────────────────────────────────
// Activated from the job editor via the 2D/3D toggle when viewing a saved job.
// Replaces the Leaflet view for exported jobs; implements playback/simulation
// of the 3D route with per-waypoint altitude (1:1 horizontal-distance rule).
//
// Prerequisites:
//   - Per-waypoint altitude computed by altitude.py (backend)
//   - Full waylines.wpml generation replacing the current stub (wpml.py)
//   - Phase 4 Proxy reactivity in state.js for clean state subscription

import { st } from './state.js'; // eslint-disable-line no-unused-vars

var _cesiumActive = false;

export function isCesiumActive() { return _cesiumActive; }

export function initCesiumView() {
  // TODO: lazy-load Cesium.js, create viewer, wire OSM tile URL
}

export function showCesiumView(jobPath) { // eslint-disable-line no-unused-vars
  // TODO: fetch job KMZ, parse waylines.wpml, render 3D route with play/reset
  _cesiumActive = true;
}

export function hideCesiumView() {
  // TODO: destroy Cesium viewer, restore Leaflet map
  _cesiumActive = false;
}
