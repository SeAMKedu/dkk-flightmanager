// ── Shared application state ──────────────────────────────────────────────────
// Single mutable object shared by all modules via import { st } from './state.js'.
// _altCap and _dataAttribution moved here from map-init.js.

export const st = {
  // App-wide state
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
};
