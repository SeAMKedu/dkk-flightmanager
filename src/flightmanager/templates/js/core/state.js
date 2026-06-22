// ── Shared application state ──────────────────────────────────────────────────
// Single mutable object shared by all modules via import { st } from './state.js'.
// _altCap and _dataAttribution moved here from map-init.js.
//
// `st` is wrapped in a tiny reactive store (store.js): reads/writes behave like
// a plain object, but writes notify subscribers registered via st.subscribe(key,
// cb). This lets derived UI react to a state change instead of every caller
// remembering to refresh it (see dirty-tracking.js for the Save-button example).

import { createStore } from './store.js';

export const st = createStore({
  // App-wide state
  // Per-tab session id — sent with preview/export/route_estimate so the server
  // keeps each client's last-preview obstacle data separate (no cross-clobber).
  sessionId: (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
             : 'sess-' + Date.now() + '-' + Math.random().toString(36).slice(2),
  drones: [],
  outputDir: '',
  previewData: null,
  editedPoly: null,
  polyModified: false,
  _polySetWithIds: false, // was the polygon established while ID fields were populated?
  isRunning: false,
  _pendingPreview: false,  // startPreview() deferred because isRunning was true
  _ownSavedJob: null,      // path of job we just saved ourselves (suppress ext-modified notice)
  currentSSE: null,
  editMode: false,
  _bridgeMode: false,
  _dirty: false,
  _activeJob: null,       // full path (folder/name or name)
  _activeJobFolder: null, // folder part, null for root

  // Takeoff / landing spot (owned by takeoff.js; Leaflet handles stay local there)
  takeoff: { auto: null, pt: null, userMoved: false, vlosRange: 300 },

  // Drag-reorder of job cards (owned by jobs-panel.js)
  drag: { path: null, folder: null },

  // Map-view stat panel mode (owned by stat-view.js; persisted to localStorage)
  stat: { mode: localStorage.getItem('mv-stat-mode') || 'normal' },

  // Editor auto-preview timer + fit-bounds + last-previewed ids (owned by form-controls.js)
  editor: { autoTimer: null, fitBounds: false, lastPreviewedIds: '' },

  // Map view working state (owned by map-view.js; other _mv* handles stay local there)
  mv: { fromEditor: false, currentFolder: null, selected: new Set(), layers: [] },

  // Route planner state
  _routeAngleDeg: null,    // null = auto, number = user override
  _routeAngleAuto: null,   // computed by Python on preview
  _speedMsOverride: null,  // null = auto, number = user override
  _cfgOverlapFront: 80,    // set from /api/config
  _cfgOverlapSide: 70,
  _cfgDefaultSpeedMs: 8.9,
  _cfgMaxAreaLossPct: 30,  // set from /api/config

  // Map state (moved from map-init.js)
  _altCap: null,         // minimum AGL ceiling (metres) from current zone hits; null if none
  _dataAttribution: '',  // attribution string currently added to the map control

  // Map-view mode flag — used by polygon-edit, measurement, etc. to gate interactions
  _mvMode: false,

  // Whether the current job's KMZ uses explicit per-waypoint altitudes (advanced mode)
  _waypointMode: false,

  // Altitude range from last variable-altitude route estimate (null when uniform)
  _altProfileMin: null,
  _altProfileMax: null,
});
