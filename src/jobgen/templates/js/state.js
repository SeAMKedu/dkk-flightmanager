// ── Shared application state ──────────────────────────────────────────────────
// Variables read/written by multiple modules. Declared here so every included
// file can reference them without worrying about declaration order.

var drones = [];
var outputDir = '';
var previewData = null;
var editedPoly = null;
var polyModified = false;
var _polySetWithIds = false; // was the polygon established while ID fields were populated?
var isRunning = false;
var _pendingPreview = false;  // startPreview() deferred because isRunning was true
var _ownSavedJob = null;      // path of job we just saved ourselves (suppress ext-modified notice)
var currentSSE = null;
var editMode = false;
var _bridgeMode = false;
var _dirty = false;
var _activeJob = null;       // full path (folder/name or name)
var _activeJobFolder = null; // folder part, null for root

// Route planner state
var _routeAngleDeg = null;    // null = auto, number = user override
var _routeAngleAuto = null;   // computed by Python on preview
var _speedMsOverride = null;  // null = auto, number = user override
var _cfgOverlapFront = 80;    // set from /api/config
var _cfgOverlapSide  = 70;
var _cfgDefaultSpeedMs = 8.9;
var _cfgMaxAreaLossPct = 30;  // set from /api/config
