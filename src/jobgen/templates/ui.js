// ── State ─────────────────────────────────────────────────────────────────────
var drones = [];
var outputDir = '';
var previewData = null;
var editedPoly = null;
var polyModified = false;
var isRunning = false;
var _pendingPreview = false;  // startPreview() deferred because isRunning was true
var currentSSE = null;
var editMode = false;
var _bridgeMode = false;
// Jobs panel state
var _dirty = false;
var _activeJob = null;
var _jpOpen = localStorage.getItem('jp-open') !== 'false';
var _jobsCache = [];
var _bridgePts = [];        // [{coord:[lng,lat], polyIdx}]
var _bridgeVerts = [];      // all vertices of current survey geometry
var _bridgeGroup = null;
var _bridgeStyledEls = [];  // Leaflet.draw handle elements coloured during picking
var _editCHandler = null;  // container-level contextmenu capture (edit mode)
var _editKHandler = null;  // container-level click capture (bridge picking)
var _editVHandler = null;  // draw:editvertex → re-patch midpoint icons

// ── Map ───────────────────────────────────────────────────────────────────────
var map = L.map('map', {preferCanvas:true}).setView([64.5, 26.0], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap', maxZoom:19}).addTo(map);

// DSM pane sits below overlayPane (400) so vectors always render on top
map.createPane('dsmPane');
map.getPane('dsmPane').style.zIndex = 350;
map.getPane('dsmPane').style.pointerEvents = 'none';

var editLayers = new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({draw:false, edit:{featureGroup:editLayers, remove:false}}));

map.on(L.Draw.Event.EDITED, function(e) {
  e.layers.eachLayer(function(l) {
    editedPoly = layerGeom(l);
    polyModified = true; markDirty();
    document.getElementById('modbadge').style.display = 'block';
  });
  editMode = false;
  map.doubleClickZoom.enable();
  if (lrs.survey) lrs.survey.addTo(map);
});

var lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};

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
    updateGsd();
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
  document.getElementById('pathint').textContent = 'Output: ' + outputDir + '/' + jn;
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

// Clear polygon edit when geometry params change
['offset','kochk','dsel'].forEach(function(id){
  document.getElementById(id).addEventListener('change', clearPolyEdit);
});
document.getElementById('pids').addEventListener('input', clearPolyEdit);
document.getElementById('kids').addEventListener('input', clearPolyEdit);
function clearPolyEdit() {
  editedPoly = null; polyModified = false;
  document.getElementById('modbadge').style.display = 'none';
}

// Auto-update on flight / polygon param changes (only when a preview exists)
var _autoTimer = null;
var _lastPreviewedIds = '';

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
    scheduleAutoUpdate(true);
  }, 150);
}
document.getElementById('pids').addEventListener('blur', onIdBlur);
document.getElementById('kids').addEventListener('blur', onIdBlur);

// New Job — reset editor to a blank slate
function newJob() {
  if (isRunning) return;
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
  previewData = null; editedPoly = null; polyModified = false; _lastPreviewedIds = '';
  _activeJob = null; _dirty = false;
  clearPolyEdit();
  clearError();
  // Reset polygon controls to a clean neutral state
  document.getElementById('offset').value = 0;
  setSimpAuto(true);  // silent — no scheduleAutoUpdate, no clearPolyEdit
  hideStaleNotice();
  document.getElementById('xb').disabled = true;
  document.getElementById('rstbtn').disabled = true;
  document.getElementById('bridge-btn').disabled = true;
  renderStatus(null);
  setRadiusLinked(true);
  document.getElementById('legend').classList.add('inactive');
  // Deselect panel card
  document.querySelectorAll('.jcard').forEach(function(c){ c.classList.remove('active'); });
  focusArea();
  map.setView([64.5, 26.0], 5);
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
  if (isRunning) return;
  clearError();
  var p = getParams();
  if (!p.parcel_ids.length && !p.property_ids.length) {
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
  var p = Object.assign(getParams(), {
    job_name: jn,
    custom_polygon: polyModified ? editedPoly : null
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
  // bridge-btn stays disabled until the user enters edit mode
  try {
    renderMap(payload);
    redrawRings();
    resetLegend(savedVis);  // null on first render → applies startOff defaults
    renderStatus(payload.stats);
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
  var zf = (data.zone_hits||[]).filter(function(z){return z.geojson;}).map(function(z){
    return {type:'Feature', geometry:z.geojson, properties:{name:z.name, r:z.restriction}};
  });
  if (zf.length) {
    lrs.zones = L.geoJSON({type:'FeatureCollection', features:zf}, {
      style:{color:'#ea580c',weight:2,fillColor:'#f97316',fillOpacity:.14},
      onEachFeature:function(f,l){
        l.bindPopup('<b>'+f.properties.name+'</b><br>'+f.properties.r);
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
    map.fitBounds(lrs.survey.getBounds(), {padding:[40,40]});
  } else {
    console.warn('[renderMap] no survey polygons rendered, survey type:', data.survey && data.survey.type);
  }

  // Vertex dots (on top of survey polygon)
  if (data.survey) lrs.vertices = _buildVertexLayer(data.survey).addTo(map);
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
  document.getElementById('bridge-btn').disabled = false;
}

function saveEdit() {
  // Called by dblclick outside polygon — save and exit edit mode
  if (!editMode) return;
  editMode = false;
  map.doubleClickZoom.enable();
  editedPoly = null;
  editLayers.eachLayer(function(l) {
    if (l.editing && l.editing.enabled()) {
      l.editing.disable();
      if (!editedPoly) {
        editedPoly = layerGeom(l);
        polyModified = true; markDirty();
        document.getElementById('modbadge').style.display = 'block';
      }
    }
  });
  editLayers.clearLayers();
  if (lrs.survey) lrs.survey.addTo(map);
  exitBridgeMode();
  _detachEditListeners();
  if (_editVHandler) { map.off('draw:editvertex', _editVHandler); _editVHandler = null; }
  document.getElementById('bridge-btn').disabled = true;
}

// Dblclick on map background (not on polygon) saves the edit
map.on('dblclick', function(e) {
  if (editMode) saveEdit();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (_bridgeMode) exitBridgeMode();
    else if (editMode) saveEdit();
  }
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
    var verts = _collectVerts(_currentSurveyGeom());
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

function toggleBridgeMode() {
  if (_bridgeMode) exitBridgeMode();
  else enterBridgeMode();
}

function enterBridgeMode() {
  if (!previewData) return;
  _bridgeMode = true;
  _bridgePts = [];
  _bridgeVerts = _collectVerts(_currentSurveyGeom());
  if (_bridgeGroup) map.removeLayer(_bridgeGroup);
  _bridgeGroup = L.layerGroup().addTo(map);
  map.boxZoom.disable();  // prevent Shift+drag box-zoom during picking
  var btn = document.getElementById('bridge-btn');
  btn.textContent = '✕ Cancel bridge/cut';
  btn.classList.add('active');
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
  var btn = document.getElementById('bridge-btn');
  btn.textContent = '⬡ Bridge / Cut';
  btn.classList.remove('active');
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
  var geom = _currentSurveyGeom();
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
      document.getElementById('bridge-btn').disabled = true;
    }
    _detachEditListeners();
    editedPoly = data.geometry;
    polyModified = true; markDirty();
    document.getElementById('modbadge').style.display = 'block';
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
  if (!previewData) return;
  saveEdit();  // exit edit mode cleanly if active
  clearPolyEdit();
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
  _activeJob = payload.job_name || null;
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
  renderJobsList(_jobsCache);
});

async function loadJobsList() {
  try {
    var r = await fetch('/api/jobs');
    if (!r.ok) return;
    var data = await r.json();
    _jobsCache = data.jobs || [];
    // Auto-open panel on first ever load if jobs exist
    if (_jobsCache.length > 0 && localStorage.getItem('jp-open') === null) {
      setJpOpen(true);
    }
    renderJobsList(_jobsCache);
    // Highlight active job card
    if (_activeJob) {
      document.querySelectorAll('.jcard').forEach(function(c){
        c.classList.toggle('active', c.dataset.name === _activeJob);
      });
    }
  } catch(e) { console.error('[loadJobsList]', e); }
}

function renderJobsList(jobs) {
  var list = document.getElementById('jp-list');
  var filter = (document.getElementById('jp-filter').value || '').toLowerCase();
  list.innerHTML = '';
  var filtered = jobs.filter(function(j){ return !filter || j.name.toLowerCase().includes(filter); });
  if (!filtered.length) {
    list.innerHTML = '<div style="padding:16px 8px;color:#475569;font-size:11px;text-align:center">'
      + (filter ? 'No matches' : 'No saved jobs yet') + '</div>';
    return;
  }
  filtered.forEach(function(j) { list.appendChild(buildJobCard(j)); });
}

function buildJobCard(j) {
  var card = document.createElement('div');
  card.className = 'jcard' + (j.name === _activeJob ? ' active' : '') + (j.status === 'failed' ? ' failed' : '');
  card.dataset.name = j.name;
  var date = j.saved_at || j.run_at || '';
  var dateStr = date ? new Date(date).toLocaleString('fi-FI',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
  var meta = [dateStr, j.area_ha != null ? j.area_ha.toFixed(1)+' ha' : '', j.drone||''].filter(Boolean).join(' · ');
  var badge = j.status === 'failed' ? '<span class="jbadge fail">!</span>'
    : j.flight_ready === true  ? '<span class="jbadge ok">&#10003;</span>'
    : j.needs_review === true  ? '<span class="jbadge wrn">!</span>'
    : '';
  var thumb = j.thumbnail_svg || '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#1e293b"/><text x="32" y="40" text-anchor="middle" font-size="28" fill="#334155">?</text></svg>';
  card.innerHTML =
    '<div class="jcard-thumb">' + thumb + '</div>'
    + '<div class="jcard-body">'
    +   '<div class="jcard-name">' + escHtml(j.name) + '</div>'
    +   '<div class="jcard-meta">' + escHtml(meta) + '</div>'
    + '</div>'
    + '<div class="jcard-right">' + badge
    +   '<button class="jcard-menu-btn" title="Actions" onclick="toggleCardMenu(event,\'' + escHtml(j.name) + '\',\'' + j.status + '\')">&#8942;</button>'
    + '</div>';
  if (j.status !== 'failed') {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.jcard-menu-btn') || e.target.closest('.jmenu')) return;
      openJob(j.name);
    });
  }
  return card;
}

// ── Card menu ─────────────────────────────────────────────────────────────────
var _openMenu = null;
function toggleCardMenu(e, name, status) {
  e.stopPropagation();
  closeCardMenu();
  var btn = e.currentTarget;
  var menu = document.createElement('div');
  menu.className = 'jmenu';
  var items = status === 'failed'
    ? [['Delete', function(){ confirmDeleteJob(name); }]]
    : [
        ['Open',            function(){ openJob(name); }],
        ['Show folder',     function(){ revealJob(name); }],
        ['Clone',           function(){ cloneJob(name); }],
        ['Rename',          function(){ startRename(name); }],
        ['Delete',          function(){ confirmDeleteJob(name); }],
      ];
  items.forEach(function(it) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item' + (it[0] === 'Delete' ? ' danger' : '');
    mi.textContent = it[0];
    mi.addEventListener('click', function(ev) { ev.stopPropagation(); closeCardMenu(); it[1](); });
    menu.appendChild(mi);
  });
  // Position relative to the card's right area
  btn.closest('.jcard-right').appendChild(menu);
  _openMenu = menu;
  setTimeout(function() { document.addEventListener('click', closeCardMenu, {once:true}); }, 0);
}
function closeCardMenu() {
  if (_openMenu) { _openMenu.remove(); _openMenu = null; }
}

// ── Open job ──────────────────────────────────────────────────────────────────
function openJob(name) {
  if (isRunning) return;
  confirmIfDirty(function() { _doOpenJob(name); });
}
async function _doOpenJob(name) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name));
    if (!r.ok) { showError('Could not load job: HTTP ' + r.status); return; }
    var data = await r.json();
    var p = data.params;
    // Cancel any pending timer
    if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
    // Clear map first
    Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
    lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
    editLayers.clearLayers();
    editMode = false; _detachEditListeners();
    // Restore form
    _restoreFormFromParams(p);
    document.getElementById('jname').value = name;
    updatePathHint();
    _activeJob = name;
    _dirty = false;
    clearError();
    // Highlight card
    document.querySelectorAll('.jcard').forEach(function(c){ c.classList.toggle('active', c.dataset.name === name); });
    // Restore map from stored preview (instant)
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
        document.getElementById('bridge-btn').disabled = true;
      } catch(ex) { console.error('[openJob] render error', ex); }
    } else {
      previewData = null;
      document.getElementById('xb').disabled = true;
      document.getElementById('rstbtn').disabled = true;
      document.getElementById('bridge-btn').disabled = true;
      renderStatus(null);
      document.getElementById('legend').classList.add('inactive');
      map.setView([64.5, 26.0], 5);
      focusArea();
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
    editedPoly = p.custom_polygon_4326; polyModified = true;
    document.getElementById('modbadge').style.display = 'block';
  } else {
    editedPoly = null; polyModified = false;
    document.getElementById('modbadge').style.display = 'none';
  }
}

// ── Reveal in file manager ────────────────────────────────────────────────────
async function revealJob(name) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name) + '/reveal', {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Could not open folder');
    }
  } catch(e) { showError('Could not open folder: ' + e.message); }
}

// ── Clone ─────────────────────────────────────────────────────────────────────
async function cloneJob(name) {
  if (isRunning) return;
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name) + '/clone', {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Clone failed'); return;
    }
    var data = await r.json();
    await loadJobsList();
    openJob(data.name);
  } catch(e) { showError('Clone failed: ' + e.message); }
}

// ── Delete ────────────────────────────────────────────────────────────────────
function confirmDeleteJob(name) {
  var card = document.querySelector('.jcard[data-name="' + CSS.escape(name) + '"]');
  if (!card) return;
  card.innerHTML =
    '<div style="padding:6px 10px;font-size:11px;color:#fca5a5;flex:1">Delete <b>' + escHtml(name) + '</b>?</div>'
    + '<div style="display:flex;gap:4px;padding:6px 8px;flex-shrink:0">'
    + '<button class="jcard-del-yes" onclick="deleteJob(\'' + escHtml(name).replace(/'/g,"\\'") + '\')">Delete</button>'
    + '<button class="jcard-del-no" onclick="loadJobsList()">Cancel</button>'
    + '</div>';
  card.style.alignItems = 'center';
}
async function deleteJob(name) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name), {method:'DELETE'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Delete failed'); return;
    }
    if (_activeJob === name) { _activeJob = null; _dirty = false; _doNewJob(); }
    await loadJobsList();
  } catch(e) { showError('Delete failed: ' + e.message); }
}

// ── Rename ────────────────────────────────────────────────────────────────────
function startRename(name) {
  var card = document.querySelector('.jcard[data-name="' + CSS.escape(name) + '"]');
  if (!card) return;
  var nameEl = card.querySelector('.jcard-name');
  if (!nameEl) return;
  var input = document.createElement('input');
  input.className = 'jcard-rename-input';
  input.value = name;
  nameEl.replaceWith(input);
  input.focus(); input.select();
  var committed = false;
  function commit() {
    if (committed) return; committed = true;
    var newName = input.value.trim();
    if (!newName || newName === name) { loadJobsList(); return; }
    doRename(name, newName);
  }
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { committed = true; loadJobsList(); }
  });
}
async function doRename(oldName, newName) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(oldName), {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({new_name: newName})
    });
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Rename failed'); await loadJobsList(); return;
    }
    if (_activeJob === oldName) {
      _activeJob = newName;
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

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

init();
