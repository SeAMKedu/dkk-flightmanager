// ── State ─────────────────────────────────────────────────────────────────────
var drones = [];
var outputDir = '';
var previewData = null;
var editedPoly = null;
var polyModified = false;
var _polySetWithIds = false; // was the polygon established while ID fields were populated?
var isRunning = false;
var _pendingPreview = false;  // startPreview() deferred because isRunning was true
var currentSSE = null;
var editMode = false;
var _bridgeMode = false;
// Jobs panel state
var _dirty = false;
var _activeJob = null;       // full path (folder/name or name)
var _activeJobFolder = null; // folder part, null for root
var _jpOpen = localStorage.getItem('jp-open') !== 'false';
var _jobsCache = [];         // flat list of all job cards (for filter search)
var _jobsGroups = [];        // grouped structure from API
var _bridgePts = [];        // [{coord:[lng,lat], polyIdx}]
var _bridgeVerts = [];      // all vertices of current survey geometry
var _bridgeGroup = null;
var _bridgeStyledEls = [];  // Leaflet.draw handle elements coloured during picking
var _editCHandler = null;  // container-level contextmenu capture (edit mode)
var _editKHandler = null;  // container-level click capture (bridge picking)
var _editVHandler = null;  // draw:editvertex → re-patch midpoint icons

// ── Measurement tool state ────────────────────────────────────────────────────
var _measItems   = [];      // [{startLL, endLL, shift}] committed measurements
var _measTemp    = null;    // {startLL, endLL, shift} during current drag
var _measSvg     = null;    // <svg> overlay element inside #map
var _measActive  = false;   // right-drag in progress
var _measShift   = false;   // shift key held at drag start
var _measStartPx = null;    // {x,y} client coords at right mousedown
var _measDragged = false;   // crossed 5 px threshold → treat as measurement drag

// ── Takeoff position ──────────────────────────────────────────────────────────
var _takeoffAuto = null;        // [lng, lat] suggested by server
var _takeoffPt   = null;        // [lng, lat] current (auto or user-dragged)
var _takeoffUserMoved = false;  // true once user drags the marker
var _takeoffMarker = null;      // Leaflet draggable marker
var _vlosRange   = 300;         // metres, set from /api/config
var _vlosOuter   = null;        // L.circle — full VLOS range ring
var _vlosInner   = null;        // L.circle — half VLOS range ring
var _vlosVisible = false;       // toggled by click on marker

// ── Map ───────────────────────────────────────────────────────────────────────
var map = L.map('map', {preferCanvas:true}).setView([64.5, 26.0], 5);
var _baseOSM = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap', maxZoom:19});
var _baseOrto = null;
var _baseLayerCtrl = null;
_baseOSM.addTo(map);

function _initBaseLayers(mmlKey) {
  if (!mmlKey) return;
  var url = 'https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts'
    + '?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0'
    + '&LAYER=ortokuva&STYLE=default&TILEMATRIXSET=WGS84_Pseudo-Mercator'
    + '&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg'
    + '&api-key=' + mmlKey;
  _baseOrto = L.tileLayer(url, {attribution:'&copy; <a href="https://maanmittauslaitos.fi">MML</a>', maxZoom:21});
  if (_baseLayerCtrl) map.removeControl(_baseLayerCtrl);
  _baseLayerCtrl = L.control.layers({'Map': _baseOSM, 'Ortho': _baseOrto}, null, {position:'topleft', collapsed:true}).addTo(map);
}

function resetMapToUserLocation() {
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(function(pos) {
      map.setView([pos.coords.latitude, pos.coords.longitude], 15);
    });
  } else {
    map.setView([64.5, 26.0], 5);
  }
}
resetMapToUserLocation();

// DSM pane sits below overlayPane (400) so vectors always render on top
map.createPane('dsmPane');
map.getPane('dsmPane').style.zIndex = 350;
map.getPane('dsmPane').style.pointerEvents = 'none';

var editLayers = new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({draw:false, edit:{featureGroup:editLayers, remove:false}}));

map.on(L.Draw.Event.EDITED, function(e) {
  e.layers.eachLayer(function(l) {
    _setEditedPoly(layerGeom(l)); markDirty();
  });
  editMode = false;
  map.doubleClickZoom.enable();
  if (lrs.survey) lrs.survey.addTo(map);
});

var lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
var _altCap = null;         // minimum AGL ceiling (metres) from current zone hits; null if none
var _dataAttribution = '';  // attribution string currently added to the map control

function layerGeom(layer) {
  var lls = layer.getLatLngs();
  var ring = (Array.isArray(lls[0]) ? lls[0] : lls).map(function(ll){return [ll.lng,ll.lat];});
  ring.push(ring[0]);
  return {type:'Polygon', coordinates:[ring]};
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  // Default job name = timestamp
  document.getElementById('jname').value = defaultJobName();

  try {
    var r = await fetch('/api/drones');
    if (!r.ok) throw new Error('drones ' + r.status);
    drones = await r.json();
    var sel = document.getElementById('dsel');
    drones.forEach(function(d) {
      var o = document.createElement('option');
      o.value = d.name; o.textContent = d.label;
      sel.appendChild(o);
    });

    var cr = await fetch('/api/config');
    if (!cr.ok) throw new Error('config ' + cr.status);
    var cfg = await cr.json();

    outputDir = cfg.output_dir || '';
    updatePathHint();

    if (cfg.default_drone) sel.value = cfg.default_drone;
    if (cfg.subcategory) setSub(cfg.subcategory, true);
    if (cfg.offset_m !== undefined) document.getElementById('offset').value = cfg.offset_m;
    if (cfg.height_m) {
      var h0 = Math.round(cfg.height_m);
      document.getElementById('hgt').value = h0;
      document.getElementById('warn-radius').value = 3 * h0;
    }
    if (cfg.simplify && cfg.simplify !== 'auto') {
      setSimpManual(parseFloat(cfg.simplify) || 0, true);
    }
    document.getElementById('kochk').checked = cfg.keepout !== false;
    if (cfg.vlos_range_m) _vlosRange = cfg.vlos_range_m;
    updateGsd();
    _mmlApiKey = cfg.mml_api_key || '';
    if (_mmlApiKey) _initBaseLayers(_mmlApiKey);
    console.log('[init] config loaded, outputDir='+outputDir+', drone='+cfg.default_drone);
  } catch(e) {
    console.error('[init] failed:', e);
  }
  renderStatus(null);
  focusArea();
  // Jobs panel
  setJpOpen(_jpOpen);
  loadJobsList();
}

function defaultJobName() {
  var n = new Date();
  return 'job-' + n.getFullYear()
    + String(n.getMonth()+1).padStart(2,'0')
    + String(n.getDate()).padStart(2,'0')
    + '-'
    + String(n.getHours()).padStart(2,'0')
    + String(n.getMinutes()).padStart(2,'0');
}

function updatePathHint() {
  var jn = document.getElementById('jname').value.trim() || '(name)';
  var rel = _activeJobFolder ? _activeJobFolder + '/' + jn : jn;
  document.getElementById('pathint').textContent = 'Output: ' + outputDir + '/' + rel;
}
document.getElementById('jname').addEventListener('input', updatePathHint);

// ── GSD ───────────────────────────────────────────────────────────────────────
function updateGsd() {
  var h = parseFloat(document.getElementById('hgt').value);
  var d = drones.find(function(x){return x.name === document.getElementById('dsel').value;});
  var el = document.getElementById('gsdv');
  if (!d || isNaN(h)) { el.textContent = '—'; return; }
  el.textContent = (h * d.pixel_pitch_um / (d.focal_length_mm * 10)).toFixed(2);
}
var _radiusLinked = true;

function setRadiusLinked(linked) {
  _radiusLinked = linked;
  var hint = document.getElementById('warn-radius-hint');
  hint.style.textDecoration = linked ? '' : 'line-through';
  hint.style.cursor = linked ? 'default' : 'pointer';
  hint.title = linked ? '3× flight height' : 'Double-click to restore 3:1 link';
  if (linked) {
    var h = parseFloat(document.getElementById('hgt').value);
    if (!isNaN(h) && h > 0) document.getElementById('warn-radius').value = Math.round(3 * h);
  }
}

document.getElementById('warn-radius-hint').addEventListener('dblclick', function() {
  if (!_radiusLinked) { setRadiusLinked(true); redrawRings(); }
});

document.getElementById('hgt').addEventListener('input', function() {
  updateGsd();
  if (_radiusLinked) {
    var h = parseFloat(this.value);
    if (!isNaN(h) && h > 0) document.getElementById('warn-radius').value = Math.round(3 * h);
  }
  if (_altCap !== null && previewData) renderStatus(previewData.stats);
});
document.getElementById('dsel').addEventListener('change', updateGsd);
document.getElementById('warn-radius').addEventListener('input', function() {
  markDirty();
  setRadiusLinked(false);
  redrawRings();
});
document.getElementById('warn-radius').addEventListener('blur', function() {
  if (this.value === '') { setRadiusLinked(true); redrawRings(); }
});

// ── Subcategory pills ─────────────────────────────────────────────────────────
var _subVal = 'A3';
function setSub(v, silent) {
  _subVal = v;
  document.getElementById('sub-a3').classList.toggle('active', v === 'A3');
  document.getElementById('sub-a2').classList.toggle('active', v === 'A2');
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
function getSub() { return _subVal; }

// ── Simplify control ──────────────────────────────────────────────────────────
var _simpSteps = [0, 1, 2, 3, 5, 8, 10, 15, 20];
var _simpIdx = 0;   // index into _simpSteps when in manual mode
var _simpAuto = true;

function _simpRender() {
  document.getElementById('simp-auto').classList.toggle('active', _simpAuto);
  document.getElementById('simp-minus').disabled = !_simpAuto && _simpIdx === 0;
  document.getElementById('simp-plus').disabled  = !_simpAuto && _simpIdx === _simpSteps.length - 1;
  document.getElementById('simp-val').textContent = _simpAuto ? '—' : (_simpSteps[_simpIdx] === 0 ? 'off' : _simpSteps[_simpIdx] + ' m');
}
function setSimpAuto(silent) {
  _simpAuto = true; _simpRender();
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
function setSimpManual(v, silent) {
  _simpAuto = false;
  // snap to nearest step
  var best = 0;
  for (var i = 0; i < _simpSteps.length; i++) {
    if (Math.abs(_simpSteps[i] - v) < Math.abs(_simpSteps[best] - v)) best = i;
  }
  _simpIdx = best; _simpRender();
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
function simpStep(dir) {
  _simpAuto = false;
  _simpIdx = Math.max(0, Math.min(_simpSteps.length - 1, _simpIdx + dir));
  _simpRender(); clearPolyEdit(); scheduleAutoUpdate();
}
function getSimplify() {
  return _simpAuto ? 'auto' : String(_simpSteps[_simpIdx]);
}
_simpRender();

// Clear polygon edit when geometry source params change
['kochk','dsel'].forEach(function(id){
  document.getElementById(id).addEventListener('change', clearPolyEdit);
});
document.getElementById('pids').addEventListener('input', clearPolyEdit);
document.getElementById('kids').addEventListener('input', clearPolyEdit);
function clearPolyEdit() {
  // Clear the custom polygon only when the polygon was established WITH IDs present.
  // If the polygon was drawn/loaded while IDs were empty, changing IDs afterwards
  // should keep the polygon as the survey area and just update the parcel-outline
  // reference layer.  Only the explicit Reset button truly discards a custom polygon.
  var hasPids = !!(document.getElementById('pids').value.trim() || document.getElementById('kids').value.trim());
  if (!hasPids) return;              // no IDs → always keep (existing rule)
  if (!_polySetWithIds) return;      // polygon pre-dates the IDs → keep it
  _clearEditedPoly();
}

function _setEditedPoly(geom) {
  /**
   * Central assignment for editedPoly.  Records whether ID fields were already
   * populated when the polygon was established — used by clearPolyEdit() to decide
   * whether a later ID-field change should discard the polygon or keep it.
   */
  editedPoly = geom;
  polyModified = true;
  _polySetWithIds = !!(
    document.getElementById('pids').value.trim() ||
    document.getElementById('kids').value.trim()
  );
  document.getElementById('modbadge').style.display = 'block';
}

function _clearEditedPoly() {
  editedPoly = null; polyModified = false; _polySetWithIds = false;
  document.getElementById('modbadge').style.display = 'none';
}

// Auto-update on flight / polygon param changes (only when a preview exists)
var _autoTimer = null;
var _lastPreviewedIds = '';
var _fitBoundsOnNextRender = false;

function idsKey() {
  return document.getElementById('pids').value.trim() + '||' + document.getElementById('kids').value.trim();
}

function scheduleAutoUpdate(force) {
  markDirty();
  if (!force && !previewData) return;
  if (_autoTimer) clearTimeout(_autoTimer);
  _autoTimer = setTimeout(function() { _autoTimer = null; startPreview(); }, 400);
}
['dsel','kochk'].forEach(function(id){
  document.getElementById(id).addEventListener('change', scheduleAutoUpdate);
});
document.getElementById('hgt').addEventListener('change', scheduleAutoUpdate);
document.getElementById('offset').addEventListener('change', scheduleAutoUpdate);

// Auto-update when area IDs are committed (both fields lose focus)
function onIdBlur() {
  setTimeout(function() {
    var active = document.activeElement;
    if (active === document.getElementById('pids') || active === document.getElementById('kids')) return;
    var key = idsKey();
    if (!key.replace('||','').trim()) return; // no IDs entered
    if (key === _lastPreviewedIds) return;     // unchanged since last fetch
    _fitBoundsOnNextRender = true;
    scheduleAutoUpdate(true);
  }, 150);
}
document.getElementById('pids').addEventListener('blur', onIdBlur);
document.getElementById('kids').addEventListener('blur', onIdBlur);

// New Job — reset editor to a blank slate
function newJob() {
  if (isRunning) return;
  if (_mvMode) closeMapView();
  confirmIfDirty(_doNewJob);
}
function _doNewJob() {
  // Cancel any pending auto-update
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  // Reset IDs and job name
  document.getElementById('jname').value = defaultJobName();
  document.getElementById('pids').value = '';
  document.getElementById('kids').value = '';
  updatePathHint();
  // Clear map
  Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
  editLayers.clearLayers();
  editMode = false;
  _detachEditListeners();
  // Reset state
  previewData = null; _clearEditedPoly(); _lastPreviewedIds = '';
  _activeJob = null; _activeJobFolder = null; _dirty = false; _altCap = null;
  _clearTakeoff();
  _setColorPicker(null);
  if (_dataAttribution) { map.attributionControl.removeAttribution(_dataAttribution); _dataAttribution = ''; }
  clearPolyEdit();
  clearError();
  // Reset polygon controls to a clean neutral state
  document.getElementById('offset').value = 0;
  setSimpAuto(true);  // silent — no scheduleAutoUpdate, no clearPolyEdit
  hideStaleNotice();
  document.getElementById('xb').disabled = true;
  document.getElementById('rstbtn').disabled = true;
  renderStatus(null);
  setRadiusLinked(true);
  document.getElementById('legend').classList.add('inactive');
  // Deselect panel card
  document.querySelectorAll('.jcard').forEach(function(c){ c.classList.remove('active'); });
  focusArea();
  resetMapToUserLocation();
}

// ── Area section focus hint ───────────────────────────────────────────────────
function focusArea() {
  var el = document.getElementById('area-sec');
  // Re-trigger animation by removing and re-adding the class
  el.classList.remove('area-focus');
  void el.offsetWidth; // force reflow
  el.classList.add('area-focus');
}
function clearAreaFocus() {
  document.getElementById('area-sec').classList.remove('area-focus');
}
// Clear highlight as soon as the user types in either ID field
document.getElementById('pids').addEventListener('input', clearAreaFocus);
document.getElementById('kids').addEventListener('input', clearAreaFocus);

// ── Warning rings ─────────────────────────────────────────────────────────────
function redrawRings() {
  if (lrs.rings) { map.removeLayer(lrs.rings); lrs.rings = null; }
  var warnR = parseFloat(document.getElementById('warn-radius').value) || 0;
  var row = document.getElementById('leg-rings-row');
  var lbl = document.getElementById('leg-rings-label');
  if (!previewData || !previewData.buildings || !warnR) {
    if (row) row.style.display = 'none';
    return;
  }
  var wg = L.layerGroup();
  var count = 0;
  previewData.buildings.forEach(function(b) {
    if (!b.is_keepout) return;
    var pt = centroid(b.geojson);
    if (!pt) return;
    L.circle(pt, {
      radius: warnR, color: '#ca8a04', weight: 1.5,
      fillColor: '#fef08a', fillOpacity: 0.25, dashArray: '4 4', interactive: false
    }).addTo(wg);
    count++;
  });
  if (!count) { if (row) row.style.display = 'none'; return; }
  lrs.rings = wg;
  if (lbl) lbl.textContent = warnR + ' m radius';
  var btn = document.getElementById('leg-rings');
  if (!btn || !btn.classList.contains('off')) lrs.rings.addTo(map);
  if (row) row.style.display = '';
}

// ── Legend ────────────────────────────────────────────────────────────────────
(function initLegend() {
  var rows = [
    {btnId:'leg-dsm',      lrKey:'dsm',      rowId:'leg-dsm-row',   startOff:true},
    {btnId:'leg-areas',    lrKey:'areas',    rowId:null},
    {btnId:'leg-survey',   lrKey:'survey',   rowId:null},
    {btnId:'leg-vertices', lrKey:'vertices', rowId:null},
    {btnId:'leg-rings',    lrKey:'rings',    rowId:'leg-rings-row'},
    {btnId:'leg-ko',       lrKey:'ko',       rowId:'leg-ko-row'},
    {btnId:'leg-bldgs',    lrKey:'bldgs',    rowId:'leg-bldgs-row'},
    {btnId:'leg-zones',    lrKey:'zones',    rowId:'leg-zones-row'},
  ];
  rows.forEach(function(r) {
    document.getElementById(r.btnId).addEventListener('click', function() {
      var layer = lrs[r.lrKey];
      if (!layer) return;
      if (this.classList.toggle('off')) { map.removeLayer(layer); }
      else { layer.addTo(map); }
    });
  });
  document.getElementById('legend').classList.add('inactive');
  window._legendRows = rows;
})();

// savedVis: optional {lrKey: bool} map of user-chosen visibility to restore.
// When omitted (e.g. first render, open-job), defaults are applied (startOff for DSM).
function resetLegend(savedVis) {
  window._legendRows.forEach(function(r) {
    var btn = document.getElementById(r.btnId);
    var hasLayer = !!lrs[r.lrKey];
    if (r.rowId) {
      document.getElementById(r.rowId).style.display = hasLayer ? '' : 'none';
    }
    if (!hasLayer) { btn.classList.add('off'); return; }
    // Restore user's toggle choice if available; otherwise apply the startup default
    var visible = (savedVis && r.lrKey in savedVis) ? savedVis[r.lrKey] : !r.startOff;
    btn.classList.toggle('off', !visible);
    // renderMap already added all new layers to the map; remove the ones that should be hidden
    if (!visible) map.removeLayer(lrs[r.lrKey]);
  });
  document.getElementById('legend').classList.remove('inactive');
}

// ── Form ──────────────────────────────────────────────────────────────────────
function parseIds(txt) {
  return txt.split(/[,\s]+/).map(function(s){return s.trim();}).filter(Boolean);
}

function getParams() {
  return {
    parcel_ids: parseIds(document.getElementById('pids').value),
    property_ids: parseIds(document.getElementById('kids').value),
    drone: document.getElementById('dsel').value || null,
    height_m: parseFloat(document.getElementById('hgt').value) || null,
    subcategory: getSub(),
    offset_m: parseFloat(document.getElementById('offset').value) || 0,
    simplify: getSimplify(),
    keepout: document.getElementById('kochk').checked,
    preview_radius_m: parseFloat(document.getElementById('warn-radius').value) || null
  };
}

function showError(msg) {
  var el = document.getElementById('errdiv');
  el.textContent = 'Error: ' + msg;
  el.style.display = 'block';
}
function clearError() {
  document.getElementById('errdiv').style.display = 'none';
  document.getElementById('errdiv').textContent = '';
}

// ── Preview ───────────────────────────────────────────────────────────────────
async function startPreview() {
  if (isRunning || editMode) return;  // don't preview while editing — renderMap clears editLayers
  clearError();
  var p = getParams();
  if (polyModified) p.custom_polygon = editedPoly;
  if (!p.parcel_ids.length && !p.property_ids.length && !p.custom_polygon) {
    showError('Enter at least one parcel ID or property ID.'); return;
  }
  await runJob('/api/preview', p, 'Preview', onPreviewDone);
}

// ── Save (formerly Export) ────────────────────────────────────────────────────
async function startExport() {
  if (isRunning) return;
  clearError();
  var jn = document.getElementById('jname').value.trim();
  if (!jn) { showError('Enter a job name.'); return; }
  if (editMode) saveEdit();  // commit any pending vertex edits before saving
  var colorEl = document.getElementById('job-color');
  var p = Object.assign(getParams(), {
    job_name: jn,
    folder: _activeJobFolder || null,
    color: colorEl.value !== _DEFAULT_JOB_COLOR ? colorEl.value : null,
    custom_polygon: polyModified ? editedPoly : null,
    takeoff_point_4326: _takeoffPt || null
  });
  await runJob('/api/export', p, 'Saving…', onSaveDone);
}

// ── Job runner ────────────────────────────────────────────────────────────────
async function runJob(endpoint, params, label, onDone) {
  isRunning = true;
  document.getElementById('xb').disabled = true;
  showToast(label + '…', 0, 'Starting…');
  showPg(true, 0, 'Starting…');

  var res;
  try {
    res = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(params)
    });
  } catch(e) { onErr('Network error: ' + e.message); return; }

  if (!res.ok) {
    var e2 = await res.json().catch(function(){return {detail:'HTTP ' + res.status};});
    onErr((e2.detail || 'Server error') + ' (HTTP ' + res.status + ')'); return;
  }

  var data = await res.json();
  var jid = data.job_id;
  console.log('[' + label + '] job_id=' + jid);

  if (currentSSE) currentSSE.close();
  currentSSE = new EventSource('/api/progress/' + jid);

  currentSSE.onmessage = function(e) {
    var d;
    try { d = JSON.parse(e.data); } catch(ex) { console.error('SSE parse error', e.data); return; }
    console.log('[sse]', d.stage, d.pct + '%', d.msg || '');
    if (d.stage === 'keepalive') return;
    if (d.stage === 'error') {
      currentSSE.close(); onErr(d.msg);
    } else if (d.stage === 'done') {
      currentSSE.close(); finishRun(); onDone(d.payload);
    } else {
      showPg(true, d.pct, d.msg);
      showToast(null, d.pct, d.msg);
    }
  };

  currentSSE.onerror = function(ev) {
    console.error('[sse] onerror', ev, 'readyState='+currentSSE.readyState);
    // readyState 2 = CLOSED — means we already called .close() → ignore
    if (currentSSE.readyState === EventSource.CLOSED) return;
    currentSSE.close();
    onErr('SSE connection lost (check server terminal for details).');
  };
}

function showPg(on, pct, msg) {
  var wrap = document.getElementById('pgwrap');
  wrap.style.opacity = on ? '1' : '0';
  wrap.style.pointerEvents = on ? '' : 'none';
  document.getElementById('pgfill').style.width = (pct||0) + '%';
  document.getElementById('pgmsg').textContent = on ? (msg || '') : '';
}
function showToast(title, pct, msg) {
  var t = document.getElementById('toast');
  t.style.display = 'block';
  if (title) document.getElementById('ttitle').textContent = title;
  document.getElementById('tfill').style.width = (pct||0) + '%';
  document.getElementById('tmsg').textContent = msg || '';
}
function finishRun() {
  isRunning = false;
  // xb state is owned by each completion callback (onPreviewDone/onSaveDone/onErr)
  // — do NOT touch it here to avoid a stale-previewData flicker.
  document.getElementById('toast').style.display = 'none';
  showPg(false, 0, '');
  if (_pendingPreview) { _pendingPreview = false; startPreview(); }
}
function onErr(msg) {
  console.error('[err]', msg);
  finishRun();
  // Restore xb to whatever is correct given the current state
  document.getElementById('xb').disabled = !previewData;
  document.getElementById('toast').style.display = 'none';
  showError(msg);
}

// ── Map ───────────────────────────────────────────────────────────────────────
function onPreviewDone(payload) {
  console.log('[preview done]', payload.stats);
  // Capture user's eye-toggle choices before renderMap replaces the layer objects
  var savedVis = null;
  if (!document.getElementById('legend').classList.contains('inactive')) {
    savedVis = {};
    window._legendRows.forEach(function(r) {
      savedVis[r.lrKey] = !document.getElementById(r.btnId).classList.contains('off');
    });
  }
  previewData = payload;
  _lastPreviewedIds = idsKey();
  clearAreaFocus();
  document.getElementById('xb').disabled = false;
  document.getElementById('rstbtn').disabled = false;
  // Compute the lowest zone floor (lower_limit) across all zone hits.
  // lower_limit is the binding altitude: fly below it to exit the zone without authorisation.
  // Zones with lower_limit=0/null apply from the ground — no safe altitude below them.
  _altCap = null;
  (payload.zone_hits||[]).forEach(function(z) {
    if (z.lower_ref === 'AGL' && z.lower_limit != null && z.lower_limit > 0) {
      var m = z.lower_uom === 'FT' ? z.lower_limit * 0.3048 : parseFloat(z.lower_limit);
      if (!isNaN(m) && (_altCap === null || m < _altCap)) _altCap = m;
    }
  });
  if (_altCap !== null) {
    var suggested = Math.floor(_altCap * 0.75);
    document.getElementById('hgt').value = suggested;
    updateGsd();
    if (_radiusLinked) setRadiusLinked(true);  // re-sync radius to new height
  }
  try {
    // If zones are appearing for the first time (layer was absent before), force them
    // visible regardless of the previously-saved eye state.
    if (savedVis && (payload.zone_hits||[]).length && !lrs.zones) savedVis.zones = true;
    // Force original-areas layer visible when IDs are added to a custom-polygon job
    // (transition from no areas → has areas).
    if (savedVis && (payload.original_areas||[]).length && !lrs.areas) savedVis.areas = true;
    renderMap(payload);
    redrawRings();
    resetLegend(savedVis);  // null on first render → applies startOff defaults
    renderStatus(payload.stats);
    // Takeoff marker: update auto position; only move marker if user hasn't dragged it
    if (payload.takeoff_point_4326) {
      _takeoffAuto = payload.takeoff_point_4326;
      if (!_takeoffUserMoved) _renderTakeoffMarker(_takeoffAuto);
    }
  } catch(e) {
    console.error('[onPreviewDone]', e);
    showError('Render error: ' + e.message);
  }
}

// Convert a GeoJSON geometry (Polygon or MultiPolygon) to an array of
// L.polygon layers. Does NOT add them to the map or editLayers.
function geomToPolys(geom, style) {
  var out = [];
  if (!geom) return out;
  if (geom.type === 'Polygon') {
    var lls = geom.coordinates[0].map(function(c){return [c[1],c[0]];});
    out.push(L.polygon(lls, style));
  } else if (geom.type === 'MultiPolygon') {
    geom.coordinates.forEach(function(pc) {
      var lls = pc[0].map(function(c){return [c[1],c[0]];});
      out.push(L.polygon(lls, style));
    });
  }
  return out;
}

function renderMap(data) {
  exitBridgeMode();
  // Remove all layers from map and reset lrs (do NOT touch editLayers)
  Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
  editLayers.clearLayers();
  // Clear previous data-source attributions
  if (_dataAttribution) { map.attributionControl.removeAttribution(_dataAttribution); _dataAttribution = ''; }

  // DSM grayscale overlay — rendered in dsmPane (z 350) below all vectors
  if (data.dsm_b64 && data.dsm_bounds) {
    var b = data.dsm_bounds; // [west, south, east, north]
    var dg = L.layerGroup();
    L.imageOverlay(
      'data:image/png;base64,' + data.dsm_b64,
      [[b[1], b[0]], [b[3], b[2]]],
      {opacity: 0.65, interactive: false, pane: 'dsmPane'}
    ).addTo(dg);
    lrs.dsm = dg;  // start hidden — user enables via legend
  }

  // Parcel/property outlines (green dashed)
  if (data.original_areas && data.original_areas.length) {
    var fc = {type:'FeatureCollection', features:data.original_areas.map(function(g){
      return {type:'Feature', geometry:g, properties:{}};
    })};
    lrs.areas = L.geoJSON(fc, {
      style:{color:'#16a34a',weight:2,dashArray:'6 3',fillOpacity:.04}
    }).addTo(map);
  }

  // Keep-out circles — one true circle per keepout building
  var koBuf = data.stats && data.stats.home_buffer_m;
  if (koBuf && data.buildings && data.buildings.length) {
    var kg = L.layerGroup();
    data.buildings.forEach(function(b) {
      if (!b.is_keepout) return;
      var pt = centroid(b.geojson);
      if (!pt) return;
      L.circle(pt, {
        radius: koBuf, color: '#dc2626', weight: 1,
        fillColor: '#fca5a5', fillOpacity: 0.20, dashArray: '4 4'
      }).addTo(kg);
    });
    if (kg.getLayers().length) lrs.ko = kg.addTo(map);
  }

  // UAS restriction zones (orange)
  // Sort largest→smallest so outer zones render first (bottom); inner zones stay on top and receive clicks.
  var zf = (data.zone_hits||[]).filter(function(z){return z.geojson;}).map(function(z){
    return {type:'Feature', geometry:z.geojson, properties:{
      name:z.name, r:z.restriction,
      upper_limit:z.upper_limit, upper_uom:z.upper_uom, upper_ref:z.upper_ref,
      lower_limit:z.lower_limit, lower_uom:z.lower_uom, lower_ref:z.lower_ref,
      contained_by:z.contained_by||[],
      context_only:!!z.context_only
    }};
  });
  zf.sort(function(a, b) {
    function bboxArea(f) {
      var c = f.geometry.type === 'Polygon' ? f.geometry.coordinates[0]
            : f.geometry.coordinates[0][0];
      var lons = c.map(function(p){return p[0];}), lats = c.map(function(p){return p[1];});
      return (Math.max.apply(null,lons)-Math.min.apply(null,lons)) *
             (Math.max.apply(null,lats)-Math.min.apply(null,lats));
    }
    return bboxArea(b) - bboxArea(a);
  });
  if (zf.length) {
    lrs.zones = L.geoJSON({type:'FeatureCollection', features:zf}, {
      style: function(f) {
        var ctx = f.properties.context_only;
        return {color:'#ea580c', weight:ctx?1.5:2, dashArray:ctx?'5,4':null,
                fillColor:'#f97316', fillOpacity:ctx?.08:.14};
      },
      onEachFeature:function(f,l){
        l.on('click', function(e) {
          L.DomEvent.stopPropagation(e);
          var pt = map.latLngToLayerPoint(e.latlng);
          var hits = [];
          lrs.zones.eachLayer(function(zl) {
            if (zl._containsPoint && zl._containsPoint(pt)) {
              hits.push(zl.feature.properties);
            }
          });
          if (hits.length) {
            var content = hits.map(function(p){
              var altLine = '';
              if (p.lower_ref === 'AGL' && p.lower_limit != null && p.lower_limit > 0) {
                var lo = p.lower_uom === 'FT' ? Math.round(p.lower_limit * 0.3048) : p.lower_limit;
                var hi = (p.upper_ref === 'AGL' && p.upper_limit != null)
                  ? (p.upper_uom === 'FT' ? Math.round(p.upper_limit * 0.3048) : p.upper_limit)
                  : null;
                altLine = '<br><small>Altitude: '+lo+(hi?' – '+hi:'+')+' m AGL — fly below '+lo+' m to exit</small>';
              } else if (p.upper_ref === 'AGL' && p.upper_limit != null) {
                var hi = p.upper_uom === 'FT' ? Math.round(p.upper_limit * 0.3048) : p.upper_limit;
                altLine = '<br><small>Ground to '+hi+' m AGL</small>';
              }
              var nestLine = p.contained_by && p.contained_by.length
                ? '<br><small style="color:#94a3b8">Within: '+p.contained_by.map(function(c){return c.name;}).join(', ')+'</small>'
                : '';
              var ctxNote = p.context_only ? ' <small style="color:#94a3b8">(nearby)</small>' : '';
              return '<b>'+p.name+'</b>'+ctxNote+'<br>'+p.r+altLine+nestLine;
            }).join('<hr style="margin:4px 0">');
            L.popup().setLatLng(e.latlng).setContent(content).openOn(map);
          }
        });
      }
    }).addTo(map);
  }

  // Buildings (red = keepout, yellow = info)
  if (data.buildings && data.buildings.length) {
    var bg = L.layerGroup();
    data.buildings.forEach(function(b) {
      var c = b.is_keepout ? '#dc2626' : '#FFBB00';
      var pt = centroid(b.geojson);
      if (pt) L.circleMarker(pt, {radius:5,color:c,fillColor:c,fillOpacity:.85,weight:1.5}).addTo(bg);
    });
    lrs.bldgs = bg.addTo(map);
  }

  // Survey polygon — display only; NOT added to editLayers to avoid Leaflet
  // double-ownership conflicts. Editing copies the polygon on demand.
  var surveyStyle = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  var surveyPolys = geomToPolys(data.survey, surveyStyle);
  if (surveyPolys.length) {
    lrs.survey = L.featureGroup(surveyPolys).addTo(map);
    lrs.survey.eachLayer(function(l) {
      l.on('dblclick', function(e) { L.DomEvent.stop(e); if (!editMode && !_bridgeMode) toggleEdit(); });
    });
    console.log('[renderMap] survey bounds', lrs.survey.getBounds());
    if (_fitBoundsOnNextRender) { _fitBoundsOnNextRender = false; map.fitBounds(lrs.survey.getBounds(), {padding:[40,40]}); }
  } else {
    console.warn('[renderMap] no survey polygons rendered, survey type:', data.survey && data.survey.type);
  }

  // Vertex dots (on top of survey polygon)
  if (data.survey) lrs.vertices = _buildVertexLayer(data.survey).addTo(map);

  // Data-source attribution — appears in the map control only when data is loaded.
  var attrs = [];
  var s = data.stats || {};
  if (s.has_parcels) attrs.push('Parcels &copy; <a href="https://ruokavirasto.fi" target="_blank">Ruokavirasto</a>');
  if (s.has_properties) attrs.push('Properties &copy; <a href="https://maanmittauslaitos.fi" target="_blank">MML</a>');
  if (data.buildings && data.buildings.length) attrs.push('Topographic DB &amp; DEM &copy; <a href="https://maanmittauslaitos.fi" target="_blank">MML</a>');
  if (data.zone_hits) attrs.push('UAS zones &copy; <a href="https://traficom.fi" target="_blank">Traficom</a>');
  if (attrs.length) { _dataAttribution = attrs.join(' | '); map.attributionControl.addAttribution(_dataAttribution); }
}

function centroid(geom) {
  try {
    if (geom.type==='Point') return [geom.coordinates[1], geom.coordinates[0]];
    if (geom.type==='Polygon') {
      var cs = geom.coordinates[0];
      return [cs.reduce(function(s,c){return s+c[1];},0)/cs.length,
              cs.reduce(function(s,c){return s+c[0];},0)/cs.length];
    }
  } catch(e){}
  return null;
}

function _vlosCircleOpts(full) {
  return full
    ? {radius: _vlosRange,       color:'#ffffff', weight:2,   dashArray:'8 6', fillOpacity:0.08, fillColor:'#ffffff', interactive:false}
    : {radius: _vlosRange / 2,   color:'#ffffff', weight:1.5, dashArray:'4 5', fillOpacity:0.05, fillColor:'#ffffff', interactive:false};
}

function _showVlos(ll) {
  _hideVlos();
  _vlosOuter = L.circle(ll, _vlosCircleOpts(true)).addTo(map);
  _vlosInner = L.circle(ll, _vlosCircleOpts(false)).addTo(map);
}

function _hideVlos() {
  if (_vlosOuter) { map.removeLayer(_vlosOuter); _vlosOuter = null; }
  if (_vlosInner) { map.removeLayer(_vlosInner); _vlosInner = null; }
  _vlosVisible = false;
}

function _moveVlos(ll) {
  if (_vlosOuter) _vlosOuter.setLatLng(ll);
  if (_vlosInner) _vlosInner.setLatLng(ll);
}

function _renderTakeoffMarker(lngLat) {
  if (_takeoffMarker) { map.removeLayer(_takeoffMarker); _takeoffMarker = null; }
  _hideVlos();
  var row = document.getElementById('leg-takeoff-row');
  if (!lngLat) { if (row) row.style.display = 'none'; return; }
  _takeoffPt = lngLat;
  var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
    + '<line x1="4" y1="4" x2="20" y2="20" stroke="#0f172a" stroke-width="5" stroke-linecap="round"/>'
    + '<line x1="20" y1="4" x2="4" y2="20" stroke="#0f172a" stroke-width="5" stroke-linecap="round"/>'
    + '<line x1="4" y1="4" x2="20" y2="20" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>'
    + '<line x1="20" y1="4" x2="4" y2="20" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>'
    + '</svg>';
  _takeoffMarker = L.marker([lngLat[1], lngLat[0]], {
    icon: L.divIcon({className:'takeoff-icon', html:svg, iconSize:[24,24], iconAnchor:[12,12], tooltipAnchor:[0,-14]}),
    draggable: true,
    zIndexOffset: 1000,
  }).addTo(map);
  _takeoffMarker.bindTooltip('Takeoff / Landing', {permanent:false, direction:'top', className:'takeoff-tooltip'});
  _takeoffMarker.on('click', function() {
    if (_vlosVisible) { _hideVlos(); } else { _showVlos(this.getLatLng()); _vlosVisible = true; }
  });
  _takeoffMarker.on('dragstart', function() {
    _takeoffUserMoved = true;
    _showVlos(this.getLatLng()); _vlosVisible = true;
  });
  _takeoffMarker.on('drag', function() { _moveVlos(this.getLatLng()); });
  _takeoffMarker.on('dragend', function() {
    var ll = _takeoffMarker.getLatLng();
    _takeoffPt = [ll.lng, ll.lat];
    _hideVlos();
  });
  if (row) row.style.display = '';
  document.getElementById('takeoff-recalc-btn').disabled = false;
  var btn = document.getElementById('leg-takeoff');
  if (btn && btn.classList.contains('off')) map.removeLayer(_takeoffMarker);
}

function recalcTakeoff() {
  if (!_takeoffAuto) return;
  _takeoffUserMoved = false;
  _renderTakeoffMarker(_takeoffAuto);
}

function _clearTakeoff() {
  _takeoffAuto = null; _takeoffPt = null; _takeoffUserMoved = false;
  _renderTakeoffMarker(null);
  document.getElementById('takeoff-recalc-btn').disabled = true;
}

// Eye-toggle for takeoff marker (not in lrs, handled separately)
document.getElementById('leg-takeoff').addEventListener('click', function() {
  if (!_takeoffMarker) return;
  if (this.classList.toggle('off')) { map.removeLayer(_takeoffMarker); _hideVlos(); }
  else _takeoffMarker.addTo(map);
});

// ── Status panel ──────────────────────────────────────────────────────────────
var _dash = '<span style="color:#cbd5e1">—</span>';
function renderStatus(s) {
  var sh = !s ? ''
    : s.flight_ready ? '<div class="sh"><span class="sok">&#10003; FLIGHT READY</span></div>'
    : s.needs_review  ? '<div class="sh"><span class="swrn">&#9888; NEEDS REVIEW</span></div>'
                      : '<div class="sh"><span class="serr">&#10007; NOT FLIGHT READY</span></div>';
  var zh = !s ? _dash
    : !s.zones_checked ? '<span class="swrn">not checked</span>'
    : s.zones_clear    ? '<span class="sok">clear</span>'
                       : '<span class="serr">'+s.zone_count+' zone(s)</span>';
  function fmt1(v) { return s && v != null ? v.toFixed(1) : _dash; }
  function fmt0(v) { return s && v != null ? v.toFixed(0) : _dash; }
  function fmt2(v) { return s && v != null ? v.toFixed(2) : _dash; }
  function fmti(v) { return s && v != null ? String(v)    : _dash; }
  var rh = s ? (s.review_reasons||[]).map(function(r){
    return '<div class="ritem">&#9888; '+r+'</div>';
  }).join('') : '';
  // Client-side altitude cap warning: shown when user's current height exceeds the zone ceiling.
  var curH = parseFloat(document.getElementById('hgt').value);
  if (_altCap !== null && !isNaN(curH) && curH >= _altCap) {
    rh += '<div class="ritem" style="color:#f97316">&#9888; Height '+curH.toFixed(0)+' m is at or above zone floor '+Math.round(_altCap)+' m AGL — fly below '+Math.round(_altCap)+' m or obtain authorisation</div>';
  }
  document.getElementById('spcontent').innerHTML =
    sh
   +'<div class="sgrid">'
   +'<div class="sbox"><div class="slbl">Area</div><div class="sval">'+fmt1(s&&s.final_area_ha)+' '+(s?'ha':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Height</div><div class="sval">'+fmt0(s&&s.flight_height_m)+' '+(s?'m':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">GSD</div><div class="sval">'+fmt2(s&&s.target_gsd_cm)+' '+(s?'cm':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Vertices</div><div class="sval">'+fmti(s&&s.survey_vertex_count)+'</div></div>'
   +'<div class="sbox"><div class="slbl">Lost</div><div class="sval">'+fmt1(s&&s.area_lost_pct)+' '+(s?'%':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Zones</div><div class="sval">'+zh+'</div></div>'
   +'</div>'
   +(rh ? '<div class="rlist">'+rh+'</div>' : '');
}

// ── Polygon editing ───────────────────────────────────────────────────────────
// Enter edit mode on dblclick on the polygon.
// Save edits on dblclick outside the polygon (or on the map background).
// We COPY the survey polygon into editLayers on demand — never share the same
// Leaflet layer object between two FeatureGroups, which causes silent drop.

function toggleEdit() {
  // Called by dblclick on polygon — enter edit mode only
  if (!previewData || !lrs.survey || editMode) return;
  editMode = true;
  map.doubleClickZoom.disable();  // prevent zoom while editing
  editLayers.clearLayers();
  if (lrs.survey) map.removeLayer(lrs.survey);
  var style = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  lrs.survey.eachLayer(function(dp) {
    var clone = L.polygon(dp.getLatLngs(), style);
    editLayers.addLayer(clone);
    if (clone.editing) clone.editing.enable();
  });
  // Midpoint markers are in the DOM now; tag them for diamond CSS
  setTimeout(_patchMidpointIcons, 0);
  // Re-patch after every vertex edit (midpoint promoted, new midpoints added)
  _editVHandler = function() { setTimeout(_patchMidpointIcons, 0); };
  map.on('draw:editvertex', _editVHandler);
  _attachEditListeners();
}

function saveEdit() {
  // Called by dblclick outside polygon — save and exit edit mode
  if (!editMode) return;
  editMode = false;
  map.doubleClickZoom.enable();
  // Read geometry BEFORE disabling — some Leaflet.draw builds revert _latlngs on disable().
  var liveGeom = _geomFromEditLayers();
  editLayers.eachLayer(function(l) {
    if (l.editing && l.editing.enabled()) l.editing.disable();
  });
  editLayers.clearLayers();
  if (liveGeom) {
    // Bake in the current visual state: the edited shape already incorporates any
    // offset that was active when the user entered edit mode, so clear the offset
    // to avoid double-applying it on the next preview.
    document.getElementById('offset').value = 0;
    _setEditedPoly(liveGeom); markDirty();
    _updateSurveyDisplay(liveGeom);
  } else {
    var _fallback = (previewData && previewData.survey) || null;
    if (_fallback) _updateSurveyDisplay(_fallback);
    else if (lrs.survey) lrs.survey.addTo(map);
  }
  exitBridgeMode();
  _detachEditListeners();
  if (_editVHandler) { map.off('draw:editvertex', _editVHandler); _editVHandler = null; }
  // Auto-fetch buildings + zones for the new polygon shape.
  // Deferred so startExport()'s runJob() can set isRunning=true first if this
  // saveEdit() was called from startExport(), preventing a double-run.
  setTimeout(startPreview, 0);
}

// Dblclick on map background (not on polygon) saves the edit
map.on('dblclick', function(e) {
  if (_mvMode) return;
  if (editMode) saveEdit();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (_bridgeMode) exitBridgeMode();
    else if (editMode) saveEdit();
  }
});

// ── Right-click scratch square ────────────────────────────────────────────────
// Right-click on an empty map creates a 300×300 m square centred on the cursor.
// Blocked when a polygon already exists or when edit/bridge mode is active.
map.on('contextmenu', function(e) {
  if (_mvMode) { if (_mvSelected.size > 0) mvClearSel(); return; }
  if (editMode || _bridgeMode || _currentSurveyGeom()) return;
  var lat = e.latlng.lat, lng = e.latlng.lng;
  var dLat = 150 / 111320;
  var dLng = 150 / (111320 * Math.cos(lat * Math.PI / 180));
  var geom = {
    type: 'Polygon',
    coordinates: [[
      [lng - dLng, lat - dLat],
      [lng + dLng, lat - dLat],
      [lng + dLng, lat + dLat],
      [lng - dLng, lat + dLat],
      [lng - dLng, lat - dLat]
    ]]
  };
  _setEditedPoly(geom);
  markDirty();
  previewData = {survey: geom};
  _updateSurveyDisplay(geom);
  map.fitBounds(lrs.survey.getBounds(), {padding: [60, 60]});
  document.getElementById('xb').disabled = false;
  document.getElementById('rstbtn').disabled = false;
  toggleEdit();
});

// ── Edit-mode container listeners (capture phase, bypass Leaflet.draw) ────────
// Registered on toggleEdit, removed on saveEdit / _detachEditListeners.
// Capture phase fires before Leaflet.draw can intercept, letting us intercept
// right-click for bridge entry and left-click for vertex picking in bridge mode.

function _attachEditListeners() {
  _detachEditListeners();

  _editCHandler = function(e) {
    // Right-click in edit mode: enter bridge (snapping to nearest vertex)
    // or cancel if already in bridge mode.
    e.preventDefault(); e.stopPropagation();
    if (_bridgeMode) { exitBridgeMode(); return; }
    var latlng = map.mouseEventToLatLng(e);
    var verts = editMode ? _collectVertsFromEditLayers() : _collectVerts(_currentSurveyGeom());
    var mp = map.latLngToContainerPoint(latlng);
    var best = null, bestD = 28;
    verts.forEach(function(v) {
      var vp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
      var d = Math.sqrt(Math.pow(vp.x-mp.x,2) + Math.pow(vp.y-mp.y,2));
      if (d < bestD) { bestD = d; best = v; }
    });
    if (best) _enterBridgeModeWithVertex(best);
  };

  _editKHandler = function(e) {
    // Left-click in bridge mode: pick a vertex.
    // When NOT in bridge mode, do nothing so normal Leaflet.draw drag works.
    if (!_bridgeMode || e.button !== 0) return;
    e.stopPropagation();
    var latlng = map.mouseEventToLatLng(e);
    var v = _nearestVertex(latlng, 28);
    if (!v) {
      var h = document.getElementById('bridge-hint');
      h.style.color = '#fca5a5';
      setTimeout(function(){ h.style.color = ''; }, 400);
      return;
    }
    var dup = _bridgePts.some(function(p){ return p.coord[0]===v.coord[0]&&p.coord[1]===v.coord[1]; });
    if (dup) return;
    _bridgePts.push(v);
    _highlightBridgeVertex(v);
    _checkAndCommit();
  };

  var c = map.getContainer();
  c.addEventListener('contextmenu', _editCHandler, true);
  c.addEventListener('click',       _editKHandler, true);
}

function _detachEditListeners() {
  var c = map.getContainer();
  if (_editCHandler) { c.removeEventListener('contextmenu', _editCHandler, true); _editCHandler = null; }
  if (_editKHandler) { c.removeEventListener('click',       _editKHandler, true); _editKHandler = null; }
}

// ── Bridge / Cut mode ─────────────────────────────────────────────────────────

// Build an interactive vertex layer for geom. Each dot accepts right-click to
// enter bridge mode (with that vertex pre-selected) or cancel if already active.
// Always non-interactive — bridge/cut interaction is handled via container-level
// capture listeners attached in edit mode (_attachEditListeners).
function _buildVertexLayer(geom) {
  var vg = L.layerGroup();
  var verts = _collectVerts(geom);
  var seen = {};
  verts.forEach(function(v) {
    var key = v.coord[0].toFixed(7)+','+v.coord[1].toFixed(7);
    if (seen[key]) return; seen[key] = true;
    L.circleMarker([v.coord[1], v.coord[0]], {
      radius: 3, color: '#1d4ed8', weight: 1,
      fillColor: '#93c5fd', fillOpacity: 0.9, interactive: false
    }).addTo(vg);
  });
  return vg;
}

function _enterBridgeModeWithVertex(v) {
  enterBridgeMode();   // sets up state, disables box-zoom, refreshes _bridgeVerts
  _bridgePts.push(v);
  _highlightBridgeVertex(v);
  _checkAndCommit();
}

// After each pick: auto-commit when the selection is complete.
// 3 picks all on same polygon → triangle cut.
// 4 picks spanning 2 polygons → quad bridge.
function _checkAndCommit() {
  _updateBridgePreview();
  var unique = _bridgePts.map(function(p){return p.polyIdx;})
                         .filter(function(v,i,a){return a.indexOf(v)===i;});
  if (_bridgePts.length === 3 && unique.length === 1) _commitBridge();
  else if (_bridgePts.length === 4) _commitBridge();
}

function _currentSurveyGeom() {
  return editedPoly || (previewData && previewData.survey) || null;
}

function _geomFromEditLayers() {
  var polys = [];
  editLayers.eachLayer(function(l) { polys.push(layerGeom(l)); });
  if (!polys.length) return null;
  if (polys.length === 1) return polys[0];
  return {type:'MultiPolygon', coordinates: polys.map(function(p){return p.coordinates;})};
}

function _collectVertsFromEditLayers() {
  var verts = [];
  var pi = 0;
  editLayers.eachLayer(function(l) {
    var lls = l.getLatLngs();
    var ring = Array.isArray(lls[0]) ? lls[0] : lls;
    for (var i = 0; i < ring.length; i++) {
      verts.push({coord: [ring[i].lng, ring[i].lat], polyIdx: pi});
    }
    pi++;
  });
  return verts;
}

function _collectVerts(geom) {
  var verts = [];
  if (!geom) return verts;
  if (geom.type === 'Polygon') {
    var ring = geom.coordinates[0];
    for (var i = 0; i < ring.length - 1; i++) verts.push({coord: ring[i], polyIdx: 0});
  } else if (geom.type === 'MultiPolygon') {
    geom.coordinates.forEach(function(pc, pi) {
      var ring = pc[0];
      for (var i = 0; i < ring.length - 1; i++) verts.push({coord: ring[i], polyIdx: pi});
    });
  }
  return verts;
}

function _nearestVertex(latlng, snapPx) {
  var mp = map.latLngToContainerPoint(latlng);
  var best = null, bestD = snapPx;
  _bridgeVerts.forEach(function(v) {
    var vp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
    var d = Math.sqrt(Math.pow(vp.x - mp.x, 2) + Math.pow(vp.y - mp.y, 2));
    if (d < bestD) { bestD = d; best = v; }
  });
  return best;
}

function enterBridgeMode() {
  if (!previewData) return;
  _bridgeMode = true;
  _bridgePts = [];
  _bridgeVerts = editMode ? _collectVertsFromEditLayers() : _collectVerts(_currentSurveyGeom());
  if (_bridgeGroup) map.removeLayer(_bridgeGroup);
  _bridgeGroup = L.layerGroup().addTo(map);
  map.boxZoom.disable();  // prevent Shift+drag box-zoom during picking
  map.getContainer().style.cursor = 'crosshair';
  _updateBridgePreview();
}

// Find the nearest Leaflet.draw vertex handle element to a map container point.
// Excludes midpoint elements (.ld-mid) — only vertex squares.
function _findEditIconAt(cp) {
  var mr = map.getContainer().getBoundingClientRect();
  var best = null, bestD = 30;
  document.querySelectorAll('.leaflet-editing-icon:not(.ld-mid)').forEach(function(el) {
    var r = el.getBoundingClientRect();
    var cx = r.left + r.width / 2 - mr.left;
    var cy = r.top  + r.height / 2 - mr.top;
    var d  = Math.sqrt(Math.pow(cx - cp.x, 2) + Math.pow(cy - cp.y, 2));
    if (d < bestD) { bestD = d; best = el; }
  });
  return best;
}

// Colour the vertex handle nearest to vertex v in the bridge-selection orange.
function _highlightBridgeVertex(v) {
  var cp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
  var el = _findEditIconAt(cp);
  if (el && _bridgeStyledEls.indexOf(el) === -1) {
    el.style.background  = '#f97316';
    el.style.borderColor = '#c2410c';
    el.style.boxShadow   = '0 0 0 2px #fed7aa';
    _bridgeStyledEls.push(el);
  }
}

// Restore all coloured vertex handles to their default appearance.
function _restoreBridgeVertices() {
  _bridgeStyledEls.forEach(function(el) {
    el.style.background  = '';
    el.style.borderColor = '';
    el.style.boxShadow   = '';
  });
  _bridgeStyledEls = [];
}

function exitBridgeMode() {
  if (!_bridgeMode) return;
  _bridgeMode = false;
  _bridgePts = [];
  _bridgeVerts = [];
  if (_bridgeGroup) { map.removeLayer(_bridgeGroup); _bridgeGroup = null; }
  _restoreBridgeVertices();
  map.boxZoom.enable();
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'none';
  hint.style.background = '#1e293b';
  hint.style.color = '';
  map.getContainer().style.cursor = '';
}

function _updateBridgePreview() {
  if (!_bridgeGroup) return;
  _bridgeGroup.clearLayers();
  _bridgePts.forEach(function(p) {
    L.circleMarker([p.coord[1], p.coord[0]], {
      radius: 6, color: '#f97316', weight: 2.5,
      fillColor: '#fb923c', fillOpacity: 1, interactive: false
    }).addTo(_bridgeGroup);
  });
  if (_bridgePts.length >= 2) {
    var lls = _bridgePts.map(function(p){ return [p.coord[1], p.coord[0]]; });
    var unique = _bridgePts.map(function(p){return p.polyIdx;})
                           .filter(function(v,i,a){return a.indexOf(v)===i;});
    var willClose = (_bridgePts.length === 3 && unique.length === 1) || _bridgePts.length >= 4;
    if (willClose) lls.push(lls[0]);  // close the preview shape
    L.polyline(lls, {color:'#f97316', weight:2, dashArray:'5 4', interactive:false}).addTo(_bridgeGroup);
  }
  var n = _bridgePts.length;
  var u = _bridgePts.map(function(p){return p.polyIdx;}).filter(function(v,i,a){return a.indexOf(v)===i;});
  var allSame = u.length <= 1;
  var hintText = n === 0 ? 'Right-click a vertex to start — Esc to cancel'
    : n === 1 ? 'Vertex 1 — pick 2 more to cut triangle, or cross to bridge'
    : n === 2 && allSame  ? 'Vertex 2/3 — pick 1 more to cut triangle, or cross to bridge'
    : n === 2 && !allSame ? 'Vertex 2/4 — pick 2 more to bridge'
    : n === 3 && allSame  ? 'Cutting triangle…'
    : n === 3 && !allSame ? 'Vertex 3/4 — pick 1 more to bridge'
    : 'Bridging…';
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'block';
  hint.textContent = hintText;
}

function _showBridgeError(msg) {
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'block';
  hint.style.background = '#dc2626';
  hint.textContent = '✕ ' + msg;
  setTimeout(function(){ hint.style.display = 'none'; hint.style.background = '#1e293b'; }, 3500);
}

async function _commitBridge() {
  var geom = editMode ? _geomFromEditLayers() : _currentSurveyGeom();
  if (!geom) { exitBridgeMode(); return; }

  var indices = _bridgePts.map(function(p){ return p.polyIdx; });
  var unique = indices.filter(function(v,i,a){ return a.indexOf(v)===i; });
  var op = unique.length === 1 ? 'subtract' : 'bridge';

  _updateBridgePreview();  // show "Processing…"

  try {
    var res = await fetch('/api/polygon_op', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        operation: op,
        polygon: geom,
        points: _bridgePts.map(function(p){ return p.coord; })
      })
    });
    if (!res.ok) {
      var err = await res.json().catch(function(){ return {detail:'Server error'}; });
      exitBridgeMode();
      _showBridgeError(err.detail || 'Operation failed');
      return;
    }
    var data = await res.json();
    exitBridgeMode();
    // Exit edit mode — editLayers has stale geometry, result replaces it
    if (editMode) {
      editMode = false;
      map.doubleClickZoom.enable();
      editLayers.clearLayers();
    }
    _detachEditListeners();
    _setEditedPoly(data.geometry); markDirty();
    document.getElementById('rstbtn').disabled = false;
    _updateSurveyDisplay(data.geometry);
  } catch(e) {
    exitBridgeMode();
    _showBridgeError('Network error: ' + e.message);
  }
}

function _updateSurveyDisplay(geom) {
  if (lrs.survey) { map.removeLayer(lrs.survey); lrs.survey = null; }
  if (lrs.vertices) { map.removeLayer(lrs.vertices); lrs.vertices = null; }

  var surveyStyle = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  var polys = geomToPolys(geom, surveyStyle);
  if (polys.length) {
    lrs.survey = L.featureGroup(polys).addTo(map);
    lrs.survey.eachLayer(function(l) {
      l.on('dblclick', function(e) { L.DomEvent.stop(e); if (!editMode && !_bridgeMode) toggleEdit(); });
    });
  }

  lrs.vertices = _buildVertexLayer(geom).addTo(map);
}

function resetPoly() {
  saveEdit();  // exit edit mode cleanly if active
  _clearEditedPoly();  // unconditional — this is the explicit "start fresh from IDs" action
  // Cancel any pending auto-update so the stale simplify value doesn't fire after reset
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  // Reset polygon controls to neutral: no offset, no simplification
  document.getElementById('offset').value = 0;
  setSimpManual(0, true);  // silent=true so it doesn't trigger scheduleAutoUpdate
  // If a job is already running, defer the preview until it finishes
  if (isRunning) { _pendingPreview = true; } else { startPreview(); }
}

// ── Save result ───────────────────────────────────────────────────────────────
function onSaveDone(payload) {
  console.log('[save done]', payload);
  document.getElementById('xb').disabled = false;
  _activeJob = payload.job_name ? (payload.folder ? payload.folder + '/' + payload.job_name : payload.job_name) : null;
  _activeJobFolder = payload.folder || null;
  _dirty = false;
  if (payload.stats) renderStatus(payload.stats);
  // Open the panel (first save reveals it) then refresh the job cards
  setJpOpen(true);
  loadJobsList();
}

// ── Leaflet.draw midpoint diamond styling ─────────────────────────────────────
// Vertex and midpoint handles share the same CSS class.  Leaflet.draw sets
// opacity 0.6 on midpoint marker elements and leaves vertex elements at the
// default (no inline opacity style).  After editing.enable() we query the DOM
// and add .ld-mid to midpoint elements so CSS can target them distinctly.
function _patchMidpointIcons() {
  // Called on edit-mode enter and after every draw:editvertex event so that
  // promoted midpoints lose the class and newly created midpoints gain it.
  var all = document.querySelectorAll('.leaflet-editing-icon');
  // Clear first — handles the case where a midpoint was just promoted to vertex
  all.forEach(function(el) { el.classList.remove('ld-mid'); });
  // Re-tag midpoints: Leaflet.draw sets opacity:0.6 inline on midpoint elements;
  // vertex handles have no inline opacity (or opacity:1).
  var found = 0;
  all.forEach(function(el) {
    var op = parseFloat(el.style.opacity);
    if (!isNaN(op) && op < 1) { el.classList.add('ld-mid'); found++; }
  });
  // Fallback: check parent element opacity
  if (!found) {
    all.forEach(function(el) {
      var op = el.parentElement && parseFloat(el.parentElement.style.opacity);
      if (op && op < 1) { el.classList.add('ld-mid'); }
    });
  }
}

// ── Dirty tracking ────────────────────────────────────────────────────────────
function markDirty() { _dirty = true; }

function confirmIfDirty(onConfirm) {
  if (!_dirty) { onConfirm(); return; }
  document.getElementById('confirm-msg').textContent =
    'You have unsaved changes. Discard them and continue?';
  document.getElementById('confirm-modal').style.display = 'flex';
  document.getElementById('confirm-discard').onclick = function() {
    hideConfirmModal(); _dirty = false; onConfirm();
  };
}
function hideConfirmModal() {
  document.getElementById('confirm-modal').style.display = 'none';
}
window.addEventListener('beforeunload', function(e) {
  if (_dirty) { e.preventDefault(); e.returnValue = ''; }
});

// ── Jobs panel ────────────────────────────────────────────────────────────────
function setJpOpen(open) {
  _jpOpen = open;
  localStorage.setItem('jp-open', open ? 'true' : 'false');
  document.getElementById('jp').classList.toggle('closed', !open);
  document.getElementById('jp-tog').innerHTML = open ? '&#9664;' : '&#9654;';
  document.getElementById('jp-tog').title = open ? 'Hide jobs panel' : 'Show jobs panel';
}
function toggleJp() { setJpOpen(!_jpOpen); }

document.getElementById('jp-filter').addEventListener('input', function() {
  renderJobsList(_jobsGroups);
});

async function loadJobsList() {
  try {
    var r = await fetch('/api/jobs');
    if (!r.ok) return;
    var data = await r.json();
    _jobsGroups = data.groups || [];
    // Flatten to cache for filter searching
    _jobsCache = [];
    _jobsGroups.forEach(function(g){ _jobsCache = _jobsCache.concat(g.jobs || []); });
    // Drop selections for jobs that no longer exist
    var validPaths = new Set(_jobsCache.map(function(j){return j.path;}));
    _selectedJobs.forEach(function(p){ if (!validPaths.has(p)) { _selectedJobs.delete(p); _selectedMeta.delete(p); } });
    _updateSelBar();
    // Auto-open panel on first ever load if jobs exist
    if (_jobsCache.length > 0 && localStorage.getItem('jp-open') === null) {
      setJpOpen(true);
    }
    renderJobsList(_jobsGroups);
  } catch(e) { console.error('[loadJobsList]', e); }
}

function renderJobsList(groups) {
  var list = document.getElementById('jp-list');
  var filter = (document.getElementById('jp-filter').value || '').toLowerCase();
  list.innerHTML = '';

  // If filtering, flatten and show matching cards without folder headers
  if (filter) {
    var matched = _jobsCache.filter(function(j){ return j.name.toLowerCase().includes(filter); });
    if (!matched.length) {
      list.innerHTML = '<div style="padding:16px 8px;color:#475569;font-size:11px;text-align:center">No matches</div>';
      return;
    }
    matched.forEach(function(j){ list.appendChild(buildJobCard(j)); });
    return;
  }

  if (!_jobsCache.length) {
    list.innerHTML = '<div style="padding:16px 8px;color:#475569;font-size:11px;text-align:center">No saved jobs yet</div>';
    return;
  }

  groups.forEach(function(g) { list.appendChild(buildFolderSection(g)); });
}

function buildFolderSection(group) {
  var frag = document.createDocumentFragment();
  var isRoot = group.name === null || group.name === undefined;
  var folderKey = isRoot ? null : group.name;
  var storageKey = 'jf-open-' + (isRoot ? '__root__' : group.name);
  var isOpen = localStorage.getItem(storageKey) !== 'false';

  // Both root and named folders get a header — root uses the output dir basename
  var displayName = isRoot ? (outputDir.split('/').pop() || 'output') : group.name;
  var dataFolder = isRoot ? '' : escHtml(group.name);

  var hdr = document.createElement('div');
  hdr.className = 'jfolder-hdr';
  hdr.innerHTML = '<span class="jfolder-caret' + (isOpen ? ' open' : '') + '">&#9658;</span>'
    + '<span class="jfolder-name" title="' + escHtml(displayName) + '">' + escHtml(displayName) + '</span>'
    + '<span class="jfolder-count">' + group.jobs.length + '</span>'
    + '<button class="jfolder-sel-all-btn" title="Select all in folder">&#10003;</button>'
    + '<button class="jfolder-map-btn" data-folder="' + dataFolder + '" title="Show jobs on map"'
    + ' onclick="showFolderOnMap(event,' + (isRoot ? 'null' : '\'' + escHtml(group.name) + '\'') + ')">Map</button>';

  var container = document.createElement('div');
  container.className = 'jfolder';
  var jobs = document.createElement('div');
  jobs.className = 'jfolder-jobs' + (isOpen ? '' : ' hidden');

  hdr.querySelector('.jfolder-sel-all-btn').addEventListener('click', function(e) {
    e.stopPropagation();
    var folderJobs = group.jobs || [];
    var allSelected = folderJobs.length > 0 && folderJobs.every(function(j){ return _selectedJobs.has(j.path); });
    folderJobs.forEach(function(j){ toggleJobSelection(j, !allSelected); });
    var chks = jobs.querySelectorAll('.jcard-chk');
    chks.forEach(function(chk){ chk.checked = !allSelected; });
  });

  hdr.addEventListener('click', function(e) {
    if (e.target.closest('.jfolder-map-btn')) return;
    if (e.target.closest('.jfolder-sel-all-btn')) return;
    isOpen = !isOpen;
    localStorage.setItem(storageKey, isOpen ? 'true' : 'false');
    jobs.classList.toggle('hidden', !isOpen);
    hdr.querySelector('.jfolder-caret').classList.toggle('open', isOpen);
  });

  (group.jobs || []).forEach(function(j){ jobs.appendChild(buildJobCard(j)); });
  container.appendChild(hdr);
  container.appendChild(jobs);
  frag.appendChild(container);
  return frag;
}

function buildJobCard(j) {
  var card = document.createElement('div');
  var isActive = j.path === _activeJob;
  var isSelected = _selectedJobs.has(j.path);
  card.className = 'jcard'
    + (isActive ? ' active' : '')
    + (j.status === 'failed' ? ' failed' : '')
    + (isSelected ? ' selected' : '');
  card.dataset.path = j.path;
  var date = j.saved_at || j.run_at || '';
  var dateStr = date ? new Date(date).toLocaleString('fi-FI',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
  var meta = [dateStr, j.area_ha != null ? j.area_ha.toFixed(1)+' ha' : '', j.drone||''].filter(Boolean).join(' · ');
  var badge = j.status === 'failed' ? '<span class="jbadge fail">!</span>'
    : j.untouched              ? '<span class="jbadge untouched">new</span>'
    : j.flight_ready === true  ? '<span class="jbadge ok">&#10003;</span>'
    : j.needs_review === true  ? '<span class="jbadge wrn">!</span>'
    : '';
  var thumb = j.thumbnail_svg || '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#1e293b"/><text x="32" y="40" text-anchor="middle" font-size="28" fill="#334155">?</text></svg>';
  card.innerHTML =
    '<label class="jcard-sel" title="Select"><input type="checkbox" class="jcard-chk"' + (isSelected ? ' checked' : '') + '></label>'
    + '<div class="jcard-thumb">' + thumb + '</div>'
    + '<div class="jcard-body">'
    +   '<div class="jcard-name">' + escHtml(j.name) + '</div>'
    +   '<div class="jcard-meta">' + escHtml(meta) + '</div>'
    + '</div>'
    + '<div class="jcard-right">' + badge
    +   '<button class="jcard-menu-btn" title="Actions">&#8942;</button>'
    + '</div>';

  card.querySelector('.jcard-menu-btn').addEventListener('click', function(e) {
    toggleCardMenu(e, j);
  });

  // Checkbox toggles selection
  var chk = card.querySelector('.jcard-chk');
  chk.addEventListener('change', function(e) {
    e.stopPropagation();
    toggleJobSelection(j, chk.checked);
  });

  if (j.status !== 'failed') {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.jcard-menu-btn') || e.target.closest('.jmenu') || e.target.closest('.jcard-sel')) return;
      openJob(j.path);
    });
  }
  return card;
}

// ── Card menu ─────────────────────────────────────────────────────────────────
var _openMenu = null;
function toggleCardMenu(e, j) {
  e.stopPropagation();
  closeCardMenu();
  var btn = e.currentTarget;
  var menu = document.createElement('div');
  menu.className = 'jmenu';
  var items = j.status === 'failed'
    ? [['Delete', function(){ confirmDeleteJob(j); }]]
    : [
        ['Open',            function(){ openJob(j.path); }],
        ['Show folder',     function(){ revealJob(j.path); }],
        ['Move to Folder',  function(){ showMoveMenu(btn, j); }],
        ['Clone',           function(){ cloneJob(j.path); }],
        ['Rename',          function(){ startRename(j); }],
        ['Delete',          function(){ confirmDeleteJob(j); }],
      ];
  items.forEach(function(it) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item' + (it[0] === 'Delete' ? ' danger' : '');
    mi.textContent = it[0];
    mi.addEventListener('click', function(ev) { ev.stopPropagation(); closeCardMenu(); it[1](); });
    menu.appendChild(mi);
  });
  btn.closest('.jcard-right').appendChild(menu);
  _openMenu = menu;
  setTimeout(function() { document.addEventListener('click', closeCardMenu, {once:true}); }, 0);
}
function closeCardMenu() {
  if (_openMenu) { _openMenu.remove(); _openMenu = null; }
}

// ── Move to folder ─────────────────────────────────────────────────────────────
function showMoveMenu(btn, j) {
  closeCardMenu();
  // Collect all folder names from current groups
  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el){
    var n = el.textContent.trim();
    if (n) folderNames.push(n);
  });

  var sub = document.createElement('div');
  sub.className = 'jmenu jmenu-sub';

  var makeItem = function(label, fn) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item';
    mi.textContent = label;
    mi.addEventListener('click', function(ev){ ev.stopPropagation(); sub.remove(); fn(); });
    sub.appendChild(mi);
  };

  if (j.folder) {
    makeItem('Move to root', function(){ doMoveJob(j, null); });
  }
  folderNames.forEach(function(name) {
    if (name !== j.folder) {
      makeItem('→ ' + name, function(){ doMoveJob(j, name); });
    }
  });
  makeItem('+ New folder…', function(){ promptNewFolderForJob(j); });

  btn.closest('.jcard-right').appendChild(sub);
  setTimeout(function(){ document.addEventListener('click', function(){ sub.remove(); }, {once:true}); }, 0);
}

async function doMoveJob(j, toFolder) {
  try {
    var r = await fetch(jobApiUrl(j.path, '/move'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({folder: toFolder})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(err.detail || 'Move failed'); return;
    }
    var data = await r.json();
    if (_activeJob === j.path) {
      _activeJob = data.path;
      _activeJobFolder = data.folder || null;
    }
    await loadJobsList();
  } catch(e) { showError('Move failed: ' + e.message); }
}

function createFolder() {
  document.getElementById('folder-name-input').value = '';
  document.getElementById('folder-modal').classList.add('open');
  setTimeout(function(){ document.getElementById('folder-name-input').focus(); }, 50);
}

function closeFolderDialog() {
  document.getElementById('folder-modal').classList.remove('open');
}

async function submitFolder() {
  var name = document.getElementById('folder-name-input').value.trim();
  if (!name) return;
  var btn = document.getElementById('folder-submit');
  btn.disabled = true;
  try {
    var r = await fetch('/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(err.detail || 'Could not create folder');
      return;
    }
    closeFolderDialog();
    await loadJobsList();
  } catch(e) { showError('Failed: ' + e.message); }
  finally { btn.disabled = false; }
}

async function promptNewFolderForJob(j) {
  var name = window.prompt('New folder name:');
  if (!name || !name.trim()) return;
  name = name.trim();
  try {
    var r = await fetch('/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      // 409 = already exists, still try the move
      if (r.status !== 409) { showError(err.detail || 'Could not create folder'); return; }
    }
    await doMoveJob(j, name);
  } catch(e) { showError('Failed: ' + e.message); }
}

// ── Map view (in-place — reuses existing #map, hides #sb) ─────────────────────
var _mvMode = false;
var _mvJobGroup = null;      // L.LayerGroup on the main map
var _mvLayers = [];          // [{path, layer, feature}]
var _mvSelected = new Set();
var _mvAllFeatures = [];
var _mvCurrentFolder = null;
var _DEFAULT_COLOR = '#3b82f6';
var _mmlApiKey = '';           // set from /api/config

function showFolderOnMap(e, folderName) {
  e.stopPropagation();
  var f = folderName || null;
  if (_mvMode && _mvCurrentFolder === f) { closeMapView(); return; }
  openMapView(f);
}

function openMapView(folderFilter) {
  _mvMode = true;
  _mvCurrentFolder = folderFilter || null;
  if (editMode) saveEdit();

  // Remove any live preview layers (zones, keepout circles, etc.) left on the map
  Object.values(lrs).forEach(function(l){ if (l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
  editLayers.clearLayers();
  if (_takeoffMarker) map.removeLayer(_takeoffMarker);
  _hideVlos();

  // Hide editor sidebar, swap legend
  document.getElementById('sb').classList.add('mv-hidden');
  document.getElementById('legend').classList.add('mv-hidden');
  document.getElementById('sp').classList.add('mv-hidden');
  document.getElementById('mv-status-legend').classList.add('visible');
  // Highlight the active folder's Map button
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.folder === (folderFilter || ''));
  });

  _mvSelected.clear();
  _mvUpdateSelBar();

  if (!_mvJobGroup) { _mvJobGroup = L.layerGroup().addTo(map); }
  _mvLoad(folderFilter);
}

function closeMapView() {
  if (!_mvMode) return;
  _mvMode = false;
  _mvCurrentFolder = null;
  _mvClearLayers();
  _mvSelected.forEach(function(path) {
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) card.classList.remove('selected');
  });
  _mvSelected.clear();
  _mvUpdateSelBar();

  document.getElementById('sb').classList.remove('mv-hidden');
  document.getElementById('legend').classList.remove('mv-hidden');
  document.getElementById('sp').classList.remove('mv-hidden');
  document.getElementById('mv-status-legend').classList.remove('visible');
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) { btn.classList.remove('active'); });
  map.closePopup();
}

async function _mvLoad(folderFilter) {
  try {
    var r = await fetch('/api/jobs/geojson');
    if (!r.ok) return;
    var fc = await r.json();
    _mvAllFeatures = fc.features || [];
    _mvApplyFilter(folderFilter);
  } catch(e) { console.error('[mapview]', e); }
}

function _mvApplyFilter(folderFilter) {
  _mvClearLayers();
  var bounds = [];
  var features = folderFilter
    ? _mvAllFeatures.filter(function(f){ return f.properties.folder === folderFilter; })
    : _mvAllFeatures;
  features.forEach(function(f) {
    if (!f.geometry) return;
    var layer = _mvMakeLayer(f);
    if (layer) {
      _mvJobGroup.addLayer(layer);
      _mvLayers.push({path: f.properties.path, layer: layer, feature: f});
      try { bounds.push(layer.getBounds()); } catch(e) {}
    }
  });
  if (bounds.length) {
    var combined = bounds[0];
    bounds.forEach(function(b){ combined = combined.extend(b); });
    map.fitBounds(combined, {padding: [40, 40]});
  }
}

function _mvClearLayers() {
  _mvLayers.forEach(function(item){ if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); });
  _mvLayers = [];
}

function _mvMakeLayer(feature) {
  var p = feature.properties;
  var color = p.color || _DEFAULT_COLOR;
  var dashArray = _mvDash(p);
  var geom = feature.geometry;
  try {
    var coords;
    if (geom.type === 'Polygon') {
      coords = geom.coordinates[0].map(function(c){ return [c[1], c[0]]; });
    } else if (geom.type === 'MultiPolygon') {
      // Leaflet polygon accepts array of rings
      coords = geom.coordinates.map(function(poly){
        return poly[0].map(function(c){ return [c[1], c[0]]; });
      });
    } else { return null; }

    var layer = L.polygon(coords, {
      color: color,
      weight: 2.5,
      fillColor: color,
      fillOpacity: 0.18,
      dashArray: dashArray,
    });

    var statusLabel = p.flight_ready === true ? '✓ Ready'
      : p.needs_review === true ? '⚠ Review'
      : p.untouched ? 'New' : '—';
    var areaStr = p.area_ha != null ? ' · ' + p.area_ha.toFixed(1) + ' ha' : '';
    var ttLabel = escHtml(p.name) + (p.folder ? ' (' + escHtml(p.folder) + ')' : '')
      + '<br><span style="font-size:10px">' + statusLabel + areaStr + '</span>';
    layer.bindTooltip(ttLabel, {direction: 'top', opacity: 0.92, sticky: false, className: 'mv-tooltip'});

    layer.on('click', function(e) {
      L.DomEvent.stopPropagation(e);
      _mvToggleSel(p.path);
    });
    layer.on('dblclick', function(e) {
      L.DomEvent.stopPropagation(e);
      mvOpenJob(p.path);
    });
    return layer;
  } catch(e) { return null; }
}

function _mvDash(p) {
  if (p.status === 'failed') return '2, 6';
  if (p.flight_ready === true) return null;
  if (p.needs_review === true) return '10, 5';
  if (p.untouched) return '4, 4';
  return null;
}

function _mvShowPopup(latlng, feature, layer) {
  var p = feature.properties;
  var statusChip = p.flight_ready === true ? '<span style="color:#4ade80">✓ Ready</span>'
    : p.needs_review === true ? '<span style="color:#fb923c">⚠ Review</span>'
    : p.untouched ? '<span style="color:#64748b">New</span>'
    : '<span style="color:#94a3b8">—</span>';
  var area = p.area_ha != null ? p.area_ha.toFixed(1) + ' ha' : '';
  var html = '<div style="font-size:12px;line-height:1.7;min-width:160px">'
    + '<b style="font-size:13px">' + escHtml(p.name) + '</b><br>'
    + (p.folder ? '<span style="font-size:10px;color:#64748b">' + escHtml(p.folder) + '</span><br>' : '')
    + statusChip + (area ? ' &nbsp;' + area : '')
    + '<div style="display:flex;gap:5px;margin-top:8px">'
    + '<button onclick="mvOpenJob(\'' + escHtml(p.path) + '\')" style="flex:1;padding:4px;font-size:11px;background:#3b82f6;color:#fff;border:none;border-radius:3px;cursor:pointer">Open</button>'
    + '<button onclick="mvDeleteJob(\'' + escHtml(p.path) + '\',\'' + escHtml(p.name) + '\')" style="flex:1;padding:4px;font-size:11px;background:#dc2626;color:#fff;border:none;border-radius:3px;cursor:pointer">Delete</button>'
    + '</div></div>';

  L.popup({closeButton: true, minWidth: 170}).setLatLng(latlng).setContent(html).openOn(map);
}

function mvOpenJob(path) {
  map.closePopup();
  closeMapView();
  openJob(path);
}

async function mvDeleteJob(path, name) {
  if (!window.confirm('Delete job "' + name + '"?')) return;
  try {
    var r = await fetch(jobApiUrl(path), {method: 'DELETE'});
    if (!r.ok) { showError('Delete failed'); return; }
    map.closePopup();
    _mvLayers = _mvLayers.filter(function(item) {
      if (item.path === path) { _mvJobGroup.removeLayer(item.layer); return false; }
      return true;
    });
    _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== path; });
    if (_activeJob === path) { _activeJob = null; _activeJobFolder = null; }
    loadJobsList();
  } catch(e) { showError('Delete failed: ' + e.message); }
}

// Map view multi-select
function _mvToggleSel(path) {
  var item = _mvLayers.find(function(i){ return i.path === path; });
  if (!item) return;
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
  if (_mvSelected.has(path)) {
    _mvSelected.delete(path);
    var origColor = item.feature.properties.color || _DEFAULT_COLOR;
    item.layer.setStyle({weight: 2.5, opacity: 1, color: origColor, fillColor: origColor});
    if (card) card.classList.remove('selected');
  } else {
    _mvSelected.add(path);
    item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    if (card) card.classList.add('selected');
  }
  _mvUpdateSelBar();
}

function mvClearSel() {
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (item) { var c = item.feature.properties.color || _DEFAULT_COLOR; item.layer.setStyle({weight: 2.5, opacity: 1, color: c, fillColor: c}); }
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) card.classList.remove('selected');
  });
  _mvSelected.clear();
  _mvUpdateSelBar();
}

function _mvUpdateSelBar() {
  var n = _mvSelected.size;
  document.getElementById('mv-actions').classList.toggle('visible', n > 0);
  document.getElementById('mv-sel-count').textContent = n + ' selected';
  document.getElementById('mv-merge-btn').disabled = n < 2;
  var openBtn = document.getElementById('mv-open-btn');
  if (openBtn) {
    openBtn.style.display = n === 1 ? '' : 'none';
    if (n === 1) openBtn.dataset.path = Array.from(_mvSelected)[0];
  }
}

function mvMerge() {
  // Populate list-view _selectedJobs from map selection and open merge modal
  _selectedJobs.clear(); _selectedMeta.clear();
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (item) {
      var p = item.feature.properties;
      _selectedJobs.add(path);
      _selectedMeta.set(path, {path: path, name: p.name, folder: p.folder, untouched: p.untouched});
    }
  });
  _updateSelBar();
  closeMapView();
  openMergeModal();
}

async function mvBulkMove() {
  var paths = Array.from(_mvSelected);
  var metas = paths.map(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    return item ? {path: path, name: item.feature.properties.name, folder: item.feature.properties.folder} : null;
  }).filter(Boolean);
  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el){
    var n = el.textContent.trim(); if (n) folderNames.push(n);
  });
  var dest = window.prompt('Move to folder (blank = root, or folder name):\n\nAvailable: ' + (folderNames.join(', ') || '(none)'));
  if (dest === null) return;
  dest = dest.trim() || null;
  for (var i = 0; i < metas.length; i++) {
    try {
      await fetch(jobApiUrl(metas[i].path, '/move'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({folder: dest})
      });
    } catch(e) { showError('Move failed: ' + e.message); }
  }
  mvClearSel();
  await loadJobsList();
  openMapView(dest);
}

async function mvBulkDelete() {
  var n = _mvSelected.size;
  if (!window.confirm('Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '?')) return;
  var paths = Array.from(_mvSelected);
  for (var i = 0; i < paths.length; i++) {
    try {
      await fetch(jobApiUrl(paths[i]), {method: 'DELETE'});
      _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== paths[i]; });
      _mvLayers = _mvLayers.filter(function(item) {
        if (item.path === paths[i]) { if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); return false; }
        return true;
      });
    } catch(e) { showError('Delete failed: ' + e.message); }
  }
  mvClearSel();
  loadJobsList();
}

// ── URL helper ────────────────────────────────────────────────────────────────
function jobApiUrl(path, suffix) {
  var encoded = path.split('/').map(encodeURIComponent).join('/');
  return '/api/jobs/' + encoded + (suffix || '');
}

// ── Open job ──────────────────────────────────────────────────────────────────
function openJob(path) {
  if (isRunning) return;
  if (_mvMode) closeMapView();
  confirmIfDirty(function() { _doOpenJob(path); });
}
async function _doOpenJob(path) {
  try {
    var r = await fetch(jobApiUrl(path));
    if (!r.ok) { showError('Could not load job: HTTP ' + r.status); return; }
    var data = await r.json();
    var p = data.params;
    var name = path.includes('/') ? path.split('/').pop() : path;
    // Cancel any pending timer
    if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
    // Clear map first
    Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
    lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
    editLayers.clearLayers();
    editMode = false; _detachEditListeners();
    _clearTakeoff();
    if (p && p.takeoff_point_4326) {
      _takeoffAuto = p.takeoff_point_4326;
      _takeoffUserMoved = true;   // preserve saved position; ↺ resets to auto
      _renderTakeoffMarker(p.takeoff_point_4326);
    }
    // Restore form
    _restoreFormFromParams(p);
    document.getElementById('jname').value = name;
    updatePathHint();
    _activeJob = path;
    _activeJobFolder = data.folder || null;
    _setColorPicker(p && p.color);
    _dirty = false;
    clearError();
    // Highlight card
    document.querySelectorAll('.jcard').forEach(function(c){ c.classList.toggle('active', c.dataset.path === path); });
    // Restore map from stored preview (instant); fit bounds once for this job load
    _fitBoundsOnNextRender = true;
    if (p && p.last_preview_geojson) {
      previewData = p.last_preview_geojson;
      _lastPreviewedIds = ((p.inputs && p.inputs.parcel_ids)||[]).join(',')
        + '||' + ((p.inputs && p.inputs.property_ids)||[]).join(',');
      try {
        renderMap(previewData);
        redrawRings();
        resetLegend();
        renderStatus(previewData.stats);
        document.getElementById('xb').disabled = false;
        document.getElementById('rstbtn').disabled = false;
      } catch(ex) { console.error('[openJob] render error', ex); }
    } else {
      previewData = null;
      renderStatus(null);
      document.getElementById('legend').classList.add('inactive');
      if (editedPoly) {
        // Custom-polygon-only job: show the polygon immediately; startPreview() below will fill in DSM etc.
        previewData = {survey: editedPoly};
        _updateSurveyDisplay(editedPoly);
        map.fitBounds(lrs.survey.getBounds(), {padding: [40, 40]});
        document.getElementById('xb').disabled = false;
        document.getElementById('rstbtn').disabled = false;
      } else {
        document.getElementById('xb').disabled = true;
        document.getElementById('rstbtn').disabled = true;
        resetMapToUserLocation();
        focusArea();
      }
    }
    // Cache staleness notice
    if (data.cache_stale && data.cache_stale.length) showStaleNotice(data.cache_stale);
    else hideStaleNotice();
    // Auto-run preview for fresh data (and DSM overlay)
    startPreview();
  } catch(ex) { showError('Failed to open job: ' + ex.message); }
}

function _restoreFormFromParams(p) {
  if (!p) return;
  if (p.inputs) {
    document.getElementById('pids').value = (p.inputs.parcel_ids||[]).join('\n');
    document.getElementById('kids').value = (p.inputs.property_ids||[]).join('\n');
  }
  if (p.flight) {
    if (p.flight.drone) document.getElementById('dsel').value = p.flight.drone;
    if (p.flight.height_m != null) {
      document.getElementById('hgt').value = p.flight.height_m;
      updateGsd();
    }
    if (p.flight.subcategory) setSub(p.flight.subcategory, true);
  }
  if (p.polygon) {
    if (p.polygon.offset_m != null) document.getElementById('offset').value = p.polygon.offset_m;
    if (p.polygon.simplify === 'auto') setSimpAuto(true);
    else if (p.polygon.simplify != null) setSimpManual(parseFloat(p.polygon.simplify)||0, true);
    if (p.polygon.keepout != null) document.getElementById('kochk').checked = p.polygon.keepout;
  }
  if (p.safety && p.safety.preview_radius_m != null) {
    document.getElementById('warn-radius').value = p.safety.preview_radius_m;
    setRadiusLinked(false);
  } else {
    setRadiusLinked(true);
  }
  if (p.custom_polygon_4326) {
    _setEditedPoly(p.custom_polygon_4326);
  } else {
    editedPoly = null; polyModified = false;
    document.getElementById('modbadge').style.display = 'none';
  }
}

// ── Reveal in file manager ────────────────────────────────────────────────────
async function revealJob(path) {
  try {
    var r = await fetch(jobApiUrl(path, '/reveal'), {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Could not open folder');
    }
  } catch(e) { showError('Could not open folder: ' + e.message); }
}

// ── Clone ─────────────────────────────────────────────────────────────────────
async function cloneJob(path) {
  if (isRunning) return;
  try {
    var r = await fetch(jobApiUrl(path, '/clone'), {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Clone failed'); return;
    }
    var data = await r.json();
    await loadJobsList();
    openJob(data.path);
  } catch(e) { showError('Clone failed: ' + e.message); }
}

// ── Delete ────────────────────────────────────────────────────────────────────
function confirmDeleteJob(j) {
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(j.path) + '"]');
  if (!card) return;
  card.innerHTML =
    '<div style="padding:6px 10px;font-size:11px;color:#fca5a5;flex:1">Delete <b>' + escHtml(j.name) + '</b>?</div>'
    + '<div style="display:flex;gap:4px;padding:6px 8px;flex-shrink:0">'
    + '<button class="jcard-del-yes" onclick="deleteJob(' + escHtml(JSON.stringify(j)) + ')">Delete</button>'
    + '<button class="jcard-del-no" onclick="loadJobsList()">Cancel</button>'
    + '</div>';
  card.style.alignItems = 'center';
}
async function deleteJob(j) {
  try {
    var r = await fetch(jobApiUrl(j.path), {method:'DELETE'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Delete failed'); return;
    }
    if (_activeJob === j.path) { _activeJob = null; _activeJobFolder = null; _dirty = false; _doNewJob(); }
    await loadJobsList();
  } catch(e) { showError('Delete failed: ' + e.message); }
}

// ── Rename ────────────────────────────────────────────────────────────────────
function startRename(j) {
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(j.path) + '"]');
  if (!card) return;
  var nameEl = card.querySelector('.jcard-name');
  if (!nameEl) return;
  var input = document.createElement('input');
  input.className = 'jcard-rename-input';
  input.value = j.name;
  nameEl.replaceWith(input);
  input.focus(); input.select();
  var committed = false;
  function commit() {
    if (committed) return; committed = true;
    var newName = input.value.trim();
    if (!newName || newName === j.name) { loadJobsList(); return; }
    doRename(j, newName);
  }
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { committed = true; loadJobsList(); }
  });
}
async function doRename(j, newName) {
  try {
    var r = await fetch(jobApiUrl(j.path), {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({new_name: newName})
    });
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Rename failed'); await loadJobsList(); return;
    }
    var data = await r.json();
    if (_activeJob === j.path) {
      _activeJob = data.path;
      document.getElementById('jname').value = newName;
      updatePathHint();
    }
    await loadJobsList();
  } catch(e) { showError('Rename failed: ' + e.message); await loadJobsList(); }
}

// ── Staleness notice ──────────────────────────────────────────────────────────
function showStaleNotice(stale) {
  var el = document.getElementById('stale-notice');
  el.textContent = 'Cached tiles may be stale (' + stale.length + ' missing) — preview will re-fetch.';
  el.style.display = 'block';
}
function hideStaleNotice() {
  var el = document.getElementById('stale-notice');
  el.style.display = 'none'; el.textContent = '';
}

// ── Job color picker ──────────────────────────────────────────────────────────
var _DEFAULT_JOB_COLOR = '#3b82f6';

function _setColorPicker(color) {
  var el = document.getElementById('job-color');
  el.value = color || _DEFAULT_JOB_COLOR;
  el.disabled = !_activeJob;
}

document.getElementById('job-color').addEventListener('change', async function() {
  if (!_activeJob) return;
  var color = this.value;
  try {
    await fetch(jobApiUrl(_activeJob), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({color: color})
    });
  } catch(e) { console.warn('[color patch]', e); }
});

// ── Multi-select & bulk operations ────────────────────────────────────────────
var _selectedJobs = new Set();   // Set of job path strings
var _selectedMeta = new Map();   // path → job card object (for merge/move)

function toggleJobSelection(j, selected) {
  if (selected) {
    _selectedJobs.add(j.path);
    _selectedMeta.set(j.path, j);
  } else {
    _selectedJobs.delete(j.path);
    _selectedMeta.delete(j.path);
  }
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(j.path) + '"]');
  if (card) card.classList.toggle('selected', selected);
  // Sync to map view when active
  if (_mvMode) {
    if (selected) {
      _mvSelected.add(j.path);
      var item = _mvLayers.find(function(i){ return i.path === j.path; });
      if (item) item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    } else {
      _mvSelected.delete(j.path);
      var item = _mvLayers.find(function(i){ return i.path === j.path; });
      if (item) { var c = item.feature.properties.color || _DEFAULT_COLOR; item.layer.setStyle({weight: 2.5, opacity: 1, color: c, fillColor: c}); }
    }
    _mvUpdateSelBar();
  }
  _updateSelBar();
}

function clearSelection() {
  _selectedJobs.clear();
  _selectedMeta.clear();
  document.querySelectorAll('.jcard.selected').forEach(function(c) {
    c.classList.remove('selected');
    var chk = c.querySelector('.jcard-chk');
    if (chk) chk.checked = false;
  });
  if (_mvMode) {
    _mvSelected.forEach(function(path) {
      var item = _mvLayers.find(function(i){ return i.path === path; });
      if (item) { var c = item.feature.properties.color || _DEFAULT_COLOR; item.layer.setStyle({weight: 2.5, opacity: 1, color: c, fillColor: c}); }
    });
    _mvSelected.clear();
    _mvUpdateSelBar();
  }
  _updateSelBar();
}

function _updateSelBar() {
  var n = _selectedJobs.size;
  var bar = document.getElementById('jp-sel-bar');
  bar.classList.toggle('visible', n > 0);
  document.getElementById('jp-sel-count').textContent = n + ' selected';
  // Merge requires ≥2
  document.getElementById('sel-merge-btn').disabled = n < 2;
}

// ── Merge modal ───────────────────────────────────────────────────────────────
function openMergeModal() {
  if (_selectedJobs.size < 2) return;
  var jobs = Array.from(_selectedMeta.values());
  var names = jobs.map(function(j){ return j.name; });
  // Detect strategy client-side: untouched = batch_created + no KMZ
  var allUntouched = jobs.every(function(j){ return j.untouched; });
  var strategyNote = allUntouched
    ? '(IDs will be combined — geometry re-fetched on preview)'
    : '(polygons will be unioned)';
  document.getElementById('merge-sources').innerHTML =
    'Merging: <b>' + names.map(escHtml).join(', ') + '</b><br>'
    + '<span style="font-size:9px;color:#64748b">' + strategyNote + '</span>';
  document.getElementById('merge-name').value = names[0] + '-merged';
  document.getElementById('merge-folder').value = '';
  document.getElementById('merge-del-src').checked = false;
  document.getElementById('merge-modal').classList.add('open');
  setTimeout(function(){ document.getElementById('merge-name').focus(); document.getElementById('merge-name').select(); }, 50);
}

function closeMergeModal() {
  document.getElementById('merge-modal').classList.remove('open');
}

async function submitMerge() {
  var newName = document.getElementById('merge-name').value.trim();
  if (!newName) { document.getElementById('merge-name').focus(); return; }
  var folder = document.getElementById('merge-folder').value.trim() || null;
  var delSrc  = document.getElementById('merge-del-src').checked;
  closeMergeModal();

  try {
    var r = await fetch('/api/merge', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        job_paths: Array.from(_selectedJobs),
        new_name: newName,
        folder: folder,
        delete_sources: delSrc
      })
    });
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Merge failed'); return;
    }
    var merged = await r.json().catch(function(){return null;});
    clearSelection();
    await loadJobsList();
    if (merged && merged.path) openJob(merged.path);
  } catch(e) { showError('Merge failed: ' + e.message); }
}

// ── Bulk move ─────────────────────────────────────────────────────────────────
function bulkMove() {
  if (!_selectedJobs.size) return;
  // Reuse the move submenu logic but for all selected jobs
  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el){
    var n = el.textContent.trim(); if (n) folderNames.push(n);
  });

  // Build a small inline picker anchored to the Move button
  var btn = document.getElementById('jp-sel-bar').querySelector('.sel-action:nth-child(3)');
  closeCardMenu();
  var sub = document.createElement('div');
  sub.className = 'jmenu';
  sub.style.cssText = 'position:fixed;z-index:9999';
  var rect = btn.getBoundingClientRect();
  sub.style.top = (rect.bottom + 4) + 'px';
  sub.style.left = rect.left + 'px';

  var makeItem = function(label, fn) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item'; mi.textContent = label;
    mi.addEventListener('click', function(ev){ ev.stopPropagation(); sub.remove(); fn(); });
    sub.appendChild(mi);
  };

  makeItem('Move to root', function(){ _bulkMoveToFolder(null); });
  folderNames.forEach(function(name){ makeItem('→ ' + name, function(){ _bulkMoveToFolder(name); }); });
  makeItem('+ New folder…', function(){
    var name = window.prompt('New folder name:');
    if (!name || !name.trim()) return;
    name = name.trim();
    fetch('/api/folders', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})})
      .then(function(){ _bulkMoveToFolder(name); })
      .catch(function(e){ showError('Failed: ' + e.message); });
  });

  document.body.appendChild(sub);
  _openMenu = sub;
  setTimeout(function(){ document.addEventListener('click', closeCardMenu, {once:true}); }, 0);
}

async function _bulkMoveToFolder(toFolder) {
  var paths = Array.from(_selectedJobs);
  var metas = Array.from(_selectedMeta.values());
  for (var i = 0; i < metas.length; i++) {
    var j = metas[i];
    try {
      var r = await fetch(jobApiUrl(j.path, '/move'), {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({folder: toFolder})
      });
      if (!r.ok) {
        var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
        showError('Move failed for ' + j.name + ': ' + (e.detail||''));
      } else {
        var data = await r.json();
        if (_activeJob === j.path) { _activeJob = data.path; _activeJobFolder = data.folder || null; }
      }
    } catch(err) { showError('Move failed: ' + err.message); }
  }
  clearSelection();
  await loadJobsList();
}

// ── Google Maps export ────────────────────────────────────────────────────────
function _hexToKmlColor(hex, alpha) {
  // CSS #RRGGBB → KML AABBGGRR
  var h = (hex || '#3b82f6').replace('#', '');
  if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  return alpha + h.slice(4,6) + h.slice(2,4) + h.slice(0,2);
}

function _escapeXml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&apos;');
}

async function _loadSelectedJobs() {
  var paths = _mvMode ? Array.from(_mvSelected) : Array.from(_selectedJobs);
  if (!paths.length) return null;
  var jobs = [];
  for (var i = 0; i < paths.length; i++) {
    try {
      var r = await fetch(jobApiUrl(paths[i]));
      if (!r.ok) continue;
      var data = await r.json();
      jobs.push({path: paths[i], params: data.params});
    } catch (e) { /* skip */ }
  }
  return jobs.length ? {paths, jobs} : null;
}

async function exportKml() {
  var result = await _loadSelectedJobs();
  if (!result) return;
  var {paths, jobs} = result;

  var kml = ['<?xml version="1.0" encoding="UTF-8"?>',
    '<kml xmlns="http://www.opengis.net/kml/2.2">',
    '<Document><name>DKK Jobs</name>'];

  jobs.forEach(function(job) {
    var p = job.params;
    var name = p.job_name || job.path;
    var lineColor = _hexToKmlColor(p.color, 'ff');
    var fillColor = _hexToKmlColor(p.color, '55');

    kml.push('<Folder><name>' + _escapeXml(name) + '</name>');

    var poly = p.custom_polygon_4326;
    if (poly && poly.coordinates && poly.coordinates[0]) {
      var ring = poly.coordinates[0];
      var coords = ring.map(function(c){ return c[0]+','+c[1]+',0'; }).join(' ');
      kml.push('<Placemark>');
      kml.push('<name>' + _escapeXml(name) + '</name>');
      kml.push('<Style>');
      kml.push('<LineStyle><color>' + lineColor + '</color><width>2</width></LineStyle>');
      kml.push('<PolyStyle><color>' + fillColor + '</color></PolyStyle>');
      kml.push('</Style>');
      kml.push('<Polygon><outerBoundaryIs><LinearRing>');
      kml.push('<coordinates>' + coords + '</coordinates>');
      kml.push('</LinearRing></outerBoundaryIs></Polygon>');
      kml.push('</Placemark>');
    }

    var tp = p.takeoff_point_4326;
    if (tp) {
      kml.push('<Placemark>');
      kml.push('<name>' + _escapeXml(name) + '</name>');
      kml.push('<Style><IconStyle><color>' + lineColor + '</color></IconStyle></Style>');
      kml.push('<Point><coordinates>' + tp[0] + ',' + tp[1] + ',0</coordinates></Point>');
      kml.push('</Placemark>');
    }

    kml.push('</Folder>');
  });

  kml.push('</Document></kml>');

  var folders = new Set(paths.map(function(p){ var s = p.indexOf('/'); return s >= 0 ? p.slice(0, s) : null; }));
  var fileName = (folders.size === 1 && Array.from(folders)[0] !== null)
    ? 'dkk-' + Array.from(folders)[0] + '.kml'
    : 'dkk-jobs.kml';

  var blob = new Blob([kml.join('\n')], {type: 'application/vnd.google-earth.kml+xml'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = fileName; a.click();
  URL.revokeObjectURL(url);
}

async function openGoogleMaps() {
  var result = await _loadSelectedJobs();
  if (!result) return;
  var {jobs} = result;

  var navPoints = [];
  jobs.forEach(function(job) {
    var tp = job.params.takeoff_point_4326;
    if (tp) navPoints.push(tp[1] + ',' + tp[0]);
  });

  if (navPoints.length === 1) {
    window.open('https://www.google.com/maps/search/?api=1&query=' + navPoints[0], '_blank');
  } else if (navPoints.length >= 2) {
    var pts = navPoints.slice(0, 10);
    window.open('https://www.google.com/maps/dir/' + pts.join('/'), '_blank');
  }
}

// ── Bulk delete ───────────────────────────────────────────────────────────────
async function bulkDelete() {
  var n = _selectedJobs.size;
  if (!n) return;
  if (!window.confirm('Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '? This cannot be undone.')) return;
  var metas = Array.from(_selectedMeta.values());
  for (var i = 0; i < metas.length; i++) {
    var j = metas[i];
    try {
      var r = await fetch(jobApiUrl(j.path), {method:'DELETE'});
      if (r.ok && _activeJob === j.path) { _activeJob = null; _activeJobFolder = null; _dirty = false; _doNewJob(); }
    } catch(err) { showError('Delete failed: ' + err.message); }
  }
  clearSelection();
  await loadJobsList();
}

// ── Batch dialog ──────────────────────────────────────────────────────────────
var _batchType = 'parcels';

function openBatchDialog() {
  // Pre-fill folder with today's date
  var today = new Date();
  var iso = today.getFullYear() + '-'
    + String(today.getMonth()+1).padStart(2,'0') + '-'
    + String(today.getDate()).padStart(2,'0');
  var folderEl = document.getElementById('batch-folder');
  if (!folderEl.value) folderEl.value = 'batch-' + iso;

  // Populate drone select if empty
  var bdr = document.getElementById('batch-drone');
  if (!bdr.options.length) {
    var defOpt = document.createElement('option');
    defOpt.value = ''; defOpt.textContent = '(default)';
    bdr.appendChild(defOpt);
    drones.forEach(function(d) {
      var o = document.createElement('option');
      o.value = d.name; o.textContent = d.name;
      bdr.appendChild(o);
    });
  }

  document.getElementById('batch-form').style.display = 'flex';
  document.getElementById('batch-progress').style.display = 'none';
  document.getElementById('batch-modal').classList.add('open');
  _updateBatchCount();
}

function closeBatchDialog() {
  document.getElementById('batch-modal').classList.remove('open');
}

var _batchPlaceholders = {
  parcels:    'One parcel ID per line\n5241087453\n5241087454\n\nOr paste comma-separated',
  properties: 'One property ID per line\n214-407-3-22\n214-407-3-23\n\nOr paste comma-separated'
};

function setBatchType(type) {
  _batchType = type;
  document.getElementById('btype-parcels').classList.toggle('active', type === 'parcels');
  document.getElementById('btype-props').classList.toggle('active', type === 'properties');
  document.getElementById('batch-ids').placeholder = _batchPlaceholders[type];
}

function _parseBatchIds() {
  var raw = document.getElementById('batch-ids').value;
  var ids = [];
  raw.split('\n').forEach(function(line) {
    line = line.trim();
    if (!line || line.startsWith('#')) return;
    line.split(',').forEach(function(part) {
      var id = part.trim();
      if (id) ids.push(id);
    });
  });
  return ids;
}

function _updateBatchCount() {
  var n = _parseBatchIds().length;
  document.getElementById('batch-count').textContent = n;
  document.getElementById('batch-n').textContent = n;
  document.getElementById('batch-submit').disabled = n === 0;
}

document.getElementById('batch-ids').addEventListener('input', _updateBatchCount);

// File upload
document.getElementById('batch-file-input').addEventListener('change', function(e) {
  var file = e.target.files[0];
  if (!file) return;
  document.getElementById('batch-file-name').textContent = file.name;
  var reader = new FileReader();
  reader.onload = function(ev) {
    var existing = document.getElementById('batch-ids').value.trim();
    var added = ev.target.result;
    document.getElementById('batch-ids').value = existing ? existing + '\n' + added : added;
    _updateBatchCount();
  };
  reader.readAsText(file);
  // Reset so same file can be loaded again
  e.target.value = '';
});

async function submitBatch() {
  var ids = _parseBatchIds();
  if (!ids.length) return;

  var folder = document.getElementById('batch-folder').value.trim() || null;
  var drone  = document.getElementById('batch-drone').value || null;
  var height = parseFloat(document.getElementById('batch-height').value) || null;
  var sub    = document.getElementById('batch-sub').value;

  var params = {
    drone: drone,
    height_m: height,
    subcategory: sub,
    offset_m: 0,
    simplify: 'auto',
    keepout: true,
    preview_radius_m: null,
  };

  // Switch to progress view
  document.getElementById('batch-form').style.display = 'none';
  document.getElementById('batch-progress').style.display = 'flex';
  document.getElementById('batch-prog-title').textContent = 'Creating ' + ids.length + ' jobs…';
  document.getElementById('bpgfill').style.width = '0%';
  document.getElementById('bpgmsg').textContent = 'Starting…';
  document.getElementById('batch-results').innerHTML = '';
  document.getElementById('batch-prog-close').disabled = true;

  var res;
  try {
    res = await fetch('/api/batch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: ids, id_type: _batchType, folder: folder, params: params})
    });
  } catch(e) {
    _batchError('Network error: ' + e.message); return;
  }
  if (!res.ok) {
    var e2 = await res.json().catch(function(){return{detail:'HTTP '+res.status};});
    _batchError(e2.detail || 'Batch failed'); return;
  }

  var jobId = (await res.json()).job_id;
  var sse = new EventSource('/api/progress/' + jobId);

  sse.onmessage = function(ev) {
    var data = JSON.parse(ev.data);
    if (data.stage === 'keepalive') return;
    if (data.stage === 'batch') {
      document.getElementById('bpgfill').style.width = data.pct + '%';
      document.getElementById('bpgmsg').textContent = data.msg || '';
    } else if (data.stage === 'done') {
      sse.close();
      _batchDone(data.payload);
    } else if (data.stage === 'error') {
      sse.close();
      _batchError(data.msg || 'Unknown error');
    }
  };
  sse.onerror = function() {
    sse.close();
    _batchError('Connection lost');
  };
}

function _batchDone(payload) {
  var results = payload.results || [];
  document.getElementById('bpgfill').style.width = '100%';
  document.getElementById('batch-prog-title').textContent =
    'Done — ' + payload.created + ' created, ' + payload.skipped + ' skipped, ' + payload.failed + ' failed';
  document.getElementById('bpgmsg').textContent = '';
  document.getElementById('batch-prog-close').disabled = false;

  var container = document.getElementById('batch-results');
  results.forEach(function(r) {
    var row = document.createElement('div');
    row.className = 'bres-row ' + r.status;
    var icon = r.status === 'ok' ? '✓' : r.status === 'skipped' ? '–' : '✗';
    row.innerHTML = '<span class="bres-icon">' + icon + '</span>'
      + '<span class="bres-id">' + escHtml(r.id) + '</span>'
      + (r.reason ? '<span class="bres-reason" title="' + escHtml(r.reason) + '">' + escHtml(r.reason) + '</span>' : '');
    container.appendChild(row);
  });

  // Refresh job list and open the new folder if one was created
  loadJobsList();
}

function _batchError(msg) {
  document.getElementById('batch-prog-title').textContent = 'Error';
  document.getElementById('bpgmsg').textContent = msg;
  document.getElementById('batch-prog-close').disabled = false;
}

// Close on backdrop click
document.getElementById('batch-modal').addEventListener('click', function(e) {
  if (e.target === this) closeBatchDialog();
});

document.getElementById('folder-modal').addEventListener('click', function(e) {
  if (e.target === this) closeFolderDialog();
});

document.getElementById('folder-name-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') submitFolder();
  if (e.key === 'Escape') closeFolderDialog();
});

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Measurement tool ──────────────────────────────────────────────────────────
// Right-click + drag: draw a dimensioning line (perpendicular end caps, aligned label).
// Right-click + drag with Shift: draw a radius line + circle, label on the line.
// Measurements persist until cleared. Does not activate in edit / bridge mode.

function _initMeasSvg() {
  var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'meas-svg';
  svg.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:650;overflow:visible';
  document.getElementById('map').appendChild(svg);
  _measSvg = svg;
  map.on('move zoom viewreset resize', _redrawMeas);

  // Floating "Clear measurements" Leaflet control — visible in both editor and map-view
  var MeasClearControl = L.Control.extend({
    options: {position: 'topleft'},
    onAdd: function() {
      var btn = L.DomUtil.create('button', 'meas-clear-ctrl');
      btn.id = 'meas-clear-btn';
      btn.innerHTML = '&#10005;';
      btn.title = 'Clear measurements (Ctrl + right-click drag to measure)';
      L.DomEvent.on(btn, 'click', L.DomEvent.stopPropagation);
      L.DomEvent.on(btn, 'click', clearMeasurements);
      return btn;
    }
  });
  new MeasClearControl().addTo(map);
}

function _initMeasEvents() {
  var container = map.getContainer();

  container.addEventListener('mousedown', function(e) {
    if (e.button !== 2 || !e.ctrlKey) return;
    if (editMode || _bridgeMode) return;
    _measStartPx = {x: e.clientX, y: e.clientY};
    _measShift   = e.shiftKey;
    _measDragged = false;
    _measActive  = false;
  }, false);

  document.addEventListener('mousemove', function(e) {
    if (!_measStartPx) return;
    if (editMode || _bridgeMode) { _measStartPx = null; return; }
    var dx = e.clientX - _measStartPx.x;
    var dy = e.clientY - _measStartPx.y;
    if (!_measDragged && Math.sqrt(dx*dx + dy*dy) > 5) {
      _measDragged = true;
      _measActive  = true;
      var rect = container.getBoundingClientRect();
      var cp = L.point(_measStartPx.x - rect.left, _measStartPx.y - rect.top);
      _measTemp = {
        startLL: map.containerPointToLatLng(cp),
        endLL:   map.containerPointToLatLng(cp),
        shift:   _measShift
      };
    }
    if (_measActive && _measTemp) {
      var rect = container.getBoundingClientRect();
      var cp = L.point(e.clientX - rect.left, e.clientY - rect.top);
      _measTemp.endLL = map.containerPointToLatLng(cp);
      _redrawMeas();
    }
  }, false);

  document.addEventListener('mouseup', function(e) {
    if (e.button !== 2) return;
    var wasDragging = _measActive;
    if (_measActive && _measTemp) {
      var rect = container.getBoundingClientRect();
      var cp = L.point(e.clientX - rect.left, e.clientY - rect.top);
      _measTemp.endLL = map.containerPointToLatLng(cp);
      if (_measTemp.startLL.distanceTo(_measTemp.endLL) > 0.5) {
        _measItems.push(_measTemp);
      }
      _measTemp  = null;
      _measActive = false;
      _redrawMeas();
    }
    _measStartPx = null;
    if (!wasDragging) _measDragged = false;
    // when wasDragging, _measDragged stays true until contextmenu suppresses it
  }, false);

  // Capture-phase contextmenu: suppress the event when we just completed a drag
  // measurement so neither Leaflet's handler nor _editCHandler see it.
  container.addEventListener('contextmenu', function(e) {
    if (_measDragged) {
      e.stopPropagation();
      e.preventDefault();
      _measDragged = false;
    }
  }, true);
}

function clearMeasurements() {
  _measItems  = [];
  _measTemp   = null;
  _measActive = false;
  _redrawMeas();
}

function _redrawMeas() {
  if (!_measSvg) return;
  while (_measSvg.firstChild) _measSvg.removeChild(_measSvg.firstChild);
  var items = _measItems.slice();
  if (_measTemp) items.push(_measTemp);
  items.forEach(_drawMeasItem);
}

function _drawMeasItem(item) {
  var p1 = map.latLngToContainerPoint(item.startLL);
  var p2 = map.latLngToContainerPoint(item.endLL);
  var dist = item.startLL.distanceTo(item.endLL);
  if (dist < 0.5) return;

  var distLabel = dist < 1000
    ? Math.round(dist) + ' m'
    : (dist / 1000).toFixed(2) + ' km';

  var dx = p2.x - p1.x, dy = p2.y - p1.y;
  var len = Math.sqrt(dx*dx + dy*dy);
  if (len < 3) return;

  var ux = dx/len, uy = dy/len;  // unit along line
  var perpX = -uy, perpY = ux;   // unit perpendicular (left-hand side)

  var mx = (p1.x + p2.x) / 2;
  var my = (p1.y + p2.y) / 2;
  var angleDeg = Math.atan2(dy, dx) * 180 / Math.PI;
  if (angleDeg >  90) angleDeg -= 180;  // keep text readable (never upside-down)
  if (angleDeg < -90) angleDeg += 180;

  var g = _measSvgEl('g', {});

  if (item.shift) {
    // Circle mode: radius line + circle; no end caps
    g.appendChild(_measSvgEl('line', {
      x1:p1.x, y1:p1.y, x2:p2.x, y2:p2.y,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'butt'
    }));
    g.appendChild(_measSvgEl('circle', {
      cx:p1.x, cy:p1.y, r:len,
      stroke:'#111', 'stroke-width':1.5, fill:'none'
    }));
    _measSvgLabel(g, mx, my, distLabel, angleDeg);
  } else {
    // Dimensioning mode: line + perpendicular end caps
    var CAP = 7; // half-length of each end cap in pixels
    g.appendChild(_measSvgEl('line', {
      x1:p1.x, y1:p1.y, x2:p2.x, y2:p2.y,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'butt'
    }));
    g.appendChild(_measSvgEl('line', {
      x1:p1.x + perpX*CAP, y1:p1.y + perpY*CAP,
      x2:p1.x - perpX*CAP, y2:p1.y - perpY*CAP,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'square'
    }));
    g.appendChild(_measSvgEl('line', {
      x1:p2.x + perpX*CAP, y1:p2.y + perpY*CAP,
      x2:p2.x - perpX*CAP, y2:p2.y - perpY*CAP,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'square'
    }));
    _measSvgLabel(g, mx, my, distLabel, angleDeg);
  }
  _measSvg.appendChild(g);
}

function _measSvgEl(tag, attrs) {
  var el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  var keys = Object.keys(attrs);
  for (var i = 0; i < keys.length; i++) el.setAttribute(keys[i], attrs[keys[i]]);
  return el;
}

function _measSvgLabel(parent, x, y, text, angleDeg) {
  var g = _measSvgEl('g', {
    transform: 'translate(' + x + ',' + y + ') rotate(' + angleDeg + ')'
  });
  var commonAttrs = [
    ['text-anchor',       'middle'],
    ['dominant-baseline', 'middle'],
    ['dy',                '-6'],
    ['font-size',         '11'],
    ['font-family',       'system-ui,sans-serif'],
    ['font-weight',       '600']
  ];
  function applyAttrs(el, extra) {
    commonAttrs.forEach(function(kv) { el.setAttribute(kv[0], kv[1]); });
    extra.forEach(function(kv) { el.setAttribute(kv[0], kv[1]); });
    el.textContent = text;
  }
  var bg = _measSvgEl('text', {});
  applyAttrs(bg, [['fill','#fff'],['stroke','#fff'],['stroke-width','3'],['paint-order','stroke']]);
  var fg = _measSvgEl('text', {});
  applyAttrs(fg, [['fill','#111']]);
  g.appendChild(bg);
  g.appendChild(fg);
  parent.appendChild(g);
}

_initMeasSvg();
_initMeasEvents();
init();

// ── Settings panel ────────────────────────────────────────────────────────────

var _cfgSections   = [];   // [{id, label, fields:[{key,label,desc,type,unit,value,...}]}]
var _cfgValues     = {};   // key → current (possibly edited) value
var _cfgOrigValues = {};   // key → value as loaded from server
var _cfgActiveSid  = null; // active section id
var _cfgSearchQ    = '';   // current search query

async function openSettings() {
  var overlay = document.getElementById('cfg-overlay');
  overlay.style.display = 'flex';
  document.getElementById('cfg-search').value = '';
  _cfgSearchQ = '';
  try {
    var resp = await fetch('/api/settings');
    var data = await resp.json();
    _cfgSections   = data.sections;
    _cfgValues     = {};
    _cfgOrigValues = {};
    for (var s of _cfgSections) {
      for (var f of s.fields) {
        _cfgValues[f.key]     = f.value;
        _cfgOrigValues[f.key] = f.value;
      }
    }
    _cfgRenderNav();
    _cfgActivate(_cfgSections[0]?.id);
  } catch(e) {
    _cfgStatus('Failed to load settings: ' + e.message, 'err');
  }
}

function closeSettings() {
  if (_cfgIsDirty() && !confirm('Discard unsaved changes?')) return;
  _cfgClose();
}

function discardSettings() { closeSettings(); }

function _cfgClose() {
  document.getElementById('cfg-overlay').style.display = 'none';
  _cfgValues = {}; _cfgOrigValues = {}; _cfgSections = [];
}

function _cfgIsDirty() {
  for (var key of Object.keys(_cfgValues)) {
    if (!_cfgValEq(_cfgValues[key], _cfgOrigValues[key])) return true;
  }
  return false;
}

function _cfgValEq(a, b) {
  if (a === b) return true;
  if (a === null || b === null) return false;
  return String(a) === String(b);
}

function _cfgSectionDirty(section) {
  return section.fields.some(function(f) {
    return !_cfgValEq(_cfgValues[f.key], _cfgOrigValues[f.key]);
  });
}

// ── Nav ───────────────────────────────────────────────────────────────────────

function _cfgRenderNav() {
  var nav = document.getElementById('cfg-nav');
  nav.innerHTML = '';
  for (var s of _cfgSections) {
    (function(section) {
      var btn = document.createElement('button');
      btn.className = 'cfg-nav-item';
      btn.dataset.sid = section.id;
      var lbl = document.createElement('span');
      lbl.textContent = section.label;
      var dot = document.createElement('span');
      dot.className = 'cfg-nav-dot';
      btn.appendChild(lbl);
      btn.appendChild(dot);
      btn.onclick = function() { _cfgActivate(section.id); };
      nav.appendChild(btn);
    })(s);
  }
}

function _cfgUpdateNavDots() {
  for (var s of _cfgSections) {
    var btn = document.querySelector('.cfg-nav-item[data-sid="' + s.id + '"]');
    if (btn) btn.classList.toggle('dirty', _cfgSectionDirty(s));
  }
}

function _cfgActivate(sid) {
  _cfgActiveSid = sid;
  document.querySelectorAll('.cfg-nav-item').forEach(function(b) {
    b.classList.toggle('active', b.dataset.sid === sid);
  });
  var section = _cfgSections.find(function(s) { return s.id === sid; });
  if (!section) return;

  if (_cfgSearchQ) {
    _cfgRenderSearch(_cfgSearchQ);
    return;
  }

  document.getElementById('cfg-section-title').textContent = section.label;
  var container = document.getElementById('cfg-fields');
  container.innerHTML = '';
  var visible = section.fields.filter(function(f) { return !f._hidden; });
  for (var field of visible) {
    container.appendChild(_cfgFieldEl(field));
  }
}

// ── Search ────────────────────────────────────────────────────────────────────

function cfgSearch(q) {
  _cfgSearchQ = q.trim().toLowerCase();
  if (!_cfgSearchQ) {
    // Restore normal section view
    _cfgActivate(_cfgActiveSid);
    document.querySelectorAll('.cfg-nav-item').forEach(function(b) { b.style.display = ''; });
    return;
  }

  // Filter nav items by whether their section has any matching field
  var matchingSids = new Set();
  for (var s of _cfgSections) {
    if (s.fields.some(function(f) { return _cfgFieldMatches(f, _cfgSearchQ); })) {
      matchingSids.add(s.id);
    }
  }
  document.querySelectorAll('.cfg-nav-item').forEach(function(b) {
    b.style.display = matchingSids.has(b.dataset.sid) ? '' : 'none';
  });

  _cfgRenderSearch(_cfgSearchQ);
}

function _cfgFieldMatches(field, q) {
  return (field.label + ' ' + field.description + ' ' + field.key).toLowerCase().includes(q);
}

function _cfgRenderSearch(q) {
  document.getElementById('cfg-section-title').textContent = 'Search results';
  var container = document.getElementById('cfg-fields');
  container.innerHTML = '';
  var found = false;
  for (var s of _cfgSections) {
    var matches = s.fields.filter(function(f) { return _cfgFieldMatches(f, q); });
    if (!matches.length) continue;
    found = true;
    var hdr = document.createElement('div');
    hdr.className = 'cfg-search-section-hdr';
    hdr.textContent = s.label;
    container.appendChild(hdr);
    for (var field of matches) {
      container.appendChild(_cfgFieldEl(field));
    }
  }
  if (!found) {
    var msg = document.createElement('div');
    msg.className = 'cfg-no-results';
    msg.textContent = 'No settings match "' + q + '"';
    container.appendChild(msg);
  }
}

// ── Field rendering ───────────────────────────────────────────────────────────

function _cfgFieldEl(field) {
  var wrap = document.createElement('div');
  wrap.className = 'cfg-field';

  // Label + unit
  var labelRow = document.createElement('div');
  labelRow.className = 'cfg-field-label';
  var lbl = document.createElement('span');
  lbl.textContent = field.label;
  labelRow.appendChild(lbl);
  if (field.unit) {
    var unit = document.createElement('span');
    unit.className = 'cfg-field-unit';
    unit.textContent = field.unit;
    labelRow.appendChild(unit);
  }
  wrap.appendChild(labelRow);

  // Description
  if (field.description) {
    var desc = document.createElement('div');
    desc.className = 'cfg-field-desc';
    desc.textContent = field.description;
    wrap.appendChild(desc);
  }

  // Input row
  var row = document.createElement('div');
  row.className = 'cfg-field-row';

  var currentVal = _cfgValues[field.key];
  var input = _cfgMakeInput(field, currentVal);
  row.appendChild(input);

  // Nullable clear button
  if (field.nullable && currentVal !== null) {
    var clrBtn = document.createElement('button');
    clrBtn.className = 'cfg-nullable-clear';
    clrBtn.textContent = 'Use default';
    clrBtn.onclick = function() {
      _cfgValues[field.key] = null;
      input.value = '';
      input.placeholder = 'default';
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    };
    row.appendChild(clrBtn);
  }

  wrap.appendChild(row);
  return wrap;
}

function _cfgMakeInput(field, currentVal) {
  var input;
  if (field.type === 'boolean') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'cfg-input cfg-input-bool';
    input.checked = currentVal === true;
    input.addEventListener('change', function() {
      _cfgValues[field.key] = input.checked;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  } else if (field.type === 'enum') {
    input = document.createElement('select');
    input.className = 'cfg-input';
    for (var opt of (field.options || [])) {
      var o = document.createElement('option');
      o.value = opt;
      o.textContent = (field.option_labels && field.option_labels[opt]) ? field.option_labels[opt] + ' (' + opt + ')' : opt;
      if (opt === currentVal) o.selected = true;
      input.appendChild(o);
    }
    input.addEventListener('change', function() {
      _cfgValues[field.key] = input.value;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  } else if (field.type === 'number' || field.type === 'integer') {
    input = document.createElement('input');
    input.type = 'number';
    input.className = 'cfg-input';
    input.value = currentVal !== null && currentVal !== undefined ? currentVal : '';
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    input.step = field.step !== undefined ? field.step : 1;
    if (field.nullable) input.placeholder = 'default';
    input.addEventListener('input', function() {
      var v = input.value === '' && field.nullable ? null
        : field.type === 'integer' ? parseInt(input.value, 10)
        : parseFloat(input.value);
      _cfgValues[field.key] = v;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.className = 'cfg-input';
    input.value = currentVal !== null && currentVal !== undefined ? currentVal : '';
    if (field.nullable) input.placeholder = 'default';
    input.addEventListener('input', function() {
      _cfgValues[field.key] = input.value === '' && field.nullable ? null : input.value;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  }
  return input;
}

function _cfgMarkModified(input, key) {
  var isModified = !_cfgValEq(_cfgValues[key], _cfgOrigValues[key]);
  if (input.type === 'checkbox') return;  // checkbox color doesn't apply
  input.classList.toggle('cfg-modified', isModified);
}

// ── Save / Reset ──────────────────────────────────────────────────────────────

async function saveSettings() {
  var changes = {};
  for (var key of Object.keys(_cfgValues)) {
    if (!_cfgValEq(_cfgValues[key], _cfgOrigValues[key])) {
      changes[key] = _cfgValues[key];
    }
  }
  if (!Object.keys(changes).length) {
    _cfgStatus('No changes to save.', 'ok');
    return;
  }

  var btn = document.getElementById('cfg-save-btn');
  btn.disabled = true;
  try {
    var resp = await fetch('/api/settings', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(changes),
    });
    var data = await resp.json();
    if (!resp.ok) {
      _cfgStatus('Save failed: ' + (data.detail || resp.status), 'err');
      return;
    }
    // Commit originals and clear modified styles
    _cfgOrigValues = Object.assign({}, _cfgValues);
    document.querySelectorAll('.cfg-input.cfg-modified').forEach(function(el) {
      el.classList.remove('cfg-modified');
    });
    _cfgUpdateNavDots();
    _cfgStatus('Settings saved. Some changes (output dir, cache TTLs) take effect immediately; drone/flight defaults apply to new jobs.', 'ok');
  } catch(e) {
    _cfgStatus('Network error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function _cfgStatus(msg, kind) {
  var el = document.getElementById('cfg-status-msg');
  el.textContent = msg;
  el.className = kind || '';
  if (msg) setTimeout(function() { if (el.textContent === msg) el.textContent = ''; }, 5000);
}
