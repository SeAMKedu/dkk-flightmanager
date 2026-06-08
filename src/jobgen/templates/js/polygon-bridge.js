// ── Bridge / Split mode ────────────────────────────────────────────────────────
// Right-click a vertex to enter bridge/split mode.
// 2 picks on same polygon → split line preview + "Split job" button in hint bar.
// 4 picks across 2 polygons → bridge (quad union).

var _bridgePts = [];        // [{coord:[lng,lat], polyIdx}]
var _bridgeVerts = [];      // all vertices of current survey geometry
var _bridgeGroup = null;
var _bridgeStyledEls = [];  // Leaflet.draw handle elements coloured during picking
var _splitReady = false;    // true when 2 pts on same polygon are picked → show Split button

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

// Build an interactive vertex layer for geom (used by map-layers.js and polygon-edit.js).
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
  enterBridgeMode();
  _bridgePts.push(v);
  _highlightBridgeVertex(v);
  _checkAndCommit();
}

// After each pick: auto-commit bridge (4 pts) or enter split-ready (2 pts same poly).
function _checkAndCommit() {
  var unique = _bridgePts.map(function(p){return p.polyIdx;})
                         .filter(function(v,i,a){return a.indexOf(v)===i;});
  if (_bridgePts.length === 2 && unique.length === 1) {
    _splitReady = true;
  } else {
    _splitReady = false;
    if (_bridgePts.length === 4) { _updateBridgePreview(); _commitBridge(); return; }
  }
  _updateBridgePreview();
}

function enterBridgeMode() {
  if (!previewData) return;
  _bridgeMode = true;
  _bridgePts = [];
  _bridgeVerts = editMode ? _collectVertsFromEditLayers() : _collectVerts(_currentSurveyGeom());
  if (_bridgeGroup) map.removeLayer(_bridgeGroup);
  _bridgeGroup = L.layerGroup().addTo(map);
  map.boxZoom.disable();
  map.getContainer().style.cursor = 'crosshair';
  _updateBridgePreview();
}

// Find the nearest Leaflet.draw vertex handle element to a map container point.
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
  _splitReady = false;
  _bridgePts = [];
  _bridgeVerts = [];
  if (_bridgeGroup) { map.removeLayer(_bridgeGroup); _bridgeGroup = null; }
  _restoreBridgeVertices();
  map.boxZoom.enable();
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'none';
  hint.style.background = '#1e293b';
  hint.style.color = '';
  hint.classList.remove('split-ready');
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
    if (willClose) lls.push(lls[0]);
    L.polyline(lls, {color:'#f97316', weight:2, dashArray:'5 4', interactive:false}).addTo(_bridgeGroup);
  }
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'block';
  if (_splitReady) {
    hint.classList.add('split-ready');
    hint.innerHTML = 'Split here?&nbsp; <button class="bridge-split-btn" onclick="commitSplit()">Split job</button>&nbsp;<span class="bridge-cancel-x" onclick="exitBridgeMode()">&#x2715;</span>';
    return;
  }
  hint.classList.remove('split-ready');
  var n = _bridgePts.length;
  var u = _bridgePts.map(function(p){return p.polyIdx;}).filter(function(v,i,a){return a.indexOf(v)===i;});
  var allSame = u.length <= 1;
  var hintText = n === 0 ? 'Right-click a vertex — Esc to cancel'
    : n === 1 ? 'Pick 2nd vertex on same polygon to split, or cross to bridge'
    : n === 2 && !allSame ? 'Vertex 2/4 — pick 2 more to bridge'
    : n === 3 && !allSame ? 'Vertex 3/4 — pick 1 more to bridge'
    : 'Bridging…';
  hint.textContent = hintText;
}

function _showBridgeError(msg) {
  _splitReady = false;
  var hint = document.getElementById('bridge-hint');
  hint.classList.remove('split-ready');
  hint.style.display = 'block';
  hint.style.background = '#dc2626';
  hint.textContent = '✕ ' + msg;
  setTimeout(function(){ hint.style.display = 'none'; hint.style.background = '#1e293b'; }, 3500);
}

async function _commitBridge() {
  _splitReady = false;
  var geom = editMode ? _geomFromEditLayers() : _currentSurveyGeom();
  if (!geom) { exitBridgeMode(); return; }

  var indices = _bridgePts.map(function(p){ return p.polyIdx; });
  var unique = indices.filter(function(v,i,a){ return a.indexOf(v)===i; });
  var op = unique.length === 1 ? 'subtract' : 'bridge';

  _updateBridgePreview();

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

// ── Polygon split ─────────────────────────────────────────────────────────────

// Split polygon geom at two boundary vertices, returning [halfA, halfB].
// halfA keeps any holes and, for MultiPolygon, all other parts.
// Returns null if the split is degenerate (< 2 vertices on either side).
function _computeSplitPolygons(geom, coordA, coordB, polyIdx) {
  var partCoords, otherParts = [];
  if (geom.type === 'Polygon') {
    partCoords = geom.coordinates;
  } else {
    partCoords = geom.coordinates[polyIdx];
    for (var i = 0; i < geom.coordinates.length; i++) {
      if (i !== polyIdx) otherParts.push(geom.coordinates[i]);
    }
  }
  var ring = partCoords[0];
  var N = ring.length - 1; // unique vertex count (ring is closed: last === first)
  var iA = -1, iB = -1;
  for (var i = 0; i < N; i++) {
    if (ring[i][0] === coordA[0] && ring[i][1] === coordA[1]) iA = i;
    if (ring[i][0] === coordB[0] && ring[i][1] === coordB[1]) iB = i;
  }
  if (iA === -1 || iB === -1 || iA === iB) return null;
  if (iA > iB) { var t = iA; iA = iB; iB = t; }
  // Require at least 2 vertices on each side (3-point ring minimum per half)
  if (iB - iA < 2 || N - (iB - iA) < 2) return null;

  // Half A: ring[iA] → ring[iB] (forward)
  var r1 = [];
  for (var i = iA; i <= iB; i++) r1.push(ring[i]);
  r1.push(ring[iA]);

  // Half B: ring[iB] → ring[N-1] → ring[0] → ring[iA] (wrapping)
  var r2 = [];
  for (var i = iB; i < N; i++) r2.push(ring[i]);
  for (var i = 0; i <= iA; i++) r2.push(ring[i]);
  r2.push(ring[iB]);

  var holesA = partCoords.slice(1); // interior rings → stay with existing job
  var coordsA = [r1].concat(holesA);
  var coordsB = [r2];

  var halfA, halfB;
  if (geom.type === 'Polygon') {
    halfA = {type: 'Polygon', coordinates: coordsA};
    halfB = {type: 'Polygon', coordinates: coordsB};
  } else {
    var allPartsA = otherParts.concat([coordsA]);
    halfA = allPartsA.length === 1
      ? {type: 'Polygon', coordinates: allPartsA[0]}
      : {type: 'MultiPolygon', coordinates: allPartsA};
    halfB = {type: 'Polygon', coordinates: coordsB};
  }
  return [halfA, halfB];
}

async function commitSplit() {
  if (!_activeJob) { _showBridgeError('Save the job first before splitting'); return; }
  if (!_splitReady || _bridgePts.length !== 2) return;
  var geom = editMode ? _geomFromEditLayers() : _currentSurveyGeom();
  if (!geom) { exitBridgeMode(); return; }
  var halves = _computeSplitPolygons(geom, _bridgePts[0].coord, _bridgePts[1].coord, _bridgePts[0].polyIdx);
  if (!halves) {
    _showBridgeError('Select points that leave at least 2 vertices on each side');
    return;
  }
  exitBridgeMode();
  if (editMode) {
    editMode = false;
    map.doubleClickZoom.enable();
    editLayers.clearLayers();
    _detachEditListeners();
  }
  try {
    _ownSavedJob = _activeJob; // suppress ext-modified notice from our own write
    var r = await fetch(jobApiUrl(_activeJob, '/split'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({polygon_a: halves[0], polygon_b: halves[1]})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){ return {detail:'Server error'}; });
      _showBridgeError(err.detail || 'Split failed');
      return;
    }
    _dirty = false;
    await loadJobsList();
    openJob(_activeJob);
  } catch(e) {
    _showBridgeError('Network error: ' + e.message);
  }
}
