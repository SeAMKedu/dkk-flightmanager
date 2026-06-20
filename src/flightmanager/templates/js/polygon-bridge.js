// ── Bridge / Split mode ────────────────────────────────────────────────────────

import { st } from './state.js';
import { map, editLayers, layerGeom } from './map-init.js';
import { markDirty } from './dirty-tracking.js';
import { _setEditedPoly } from './form-controls.js';
// Circular — only called at runtime:
import { _detachEditListeners } from './polygon-edit.js';
import { jobApiUrl } from './utils.js';
import { apiPost } from './api.js';

var _bridgePts = [];
var _bridgeVerts = [];
var _bridgeGroup = null;
var _bridgeStyledEls = [];
var _splitReady = false;

function _currentSurveyGeom() {
  return st.editedPoly || (st.previewData && st.previewData.survey) || null;
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

export function _buildVertexLayer(geom) {
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
  if (!st.previewData) return;
  st._bridgeMode = true;
  _bridgePts = [];
  _bridgeVerts = st.editMode ? _collectVertsFromEditLayers() : _collectVerts(_currentSurveyGeom());
  if (_bridgeGroup) map.removeLayer(_bridgeGroup);
  _bridgeGroup = L.layerGroup().addTo(map);
  map.boxZoom.disable();
  map.getContainer().style.cursor = 'crosshair';
  _updateBridgePreview();
}

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

export function exitBridgeMode() {
  if (!st._bridgeMode) return;
  st._bridgeMode = false;
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
  var geom = st.editMode ? _geomFromEditLayers() : _currentSurveyGeom();
  if (!geom) { exitBridgeMode(); return; }

  var indices = _bridgePts.map(function(p){ return p.polyIdx; });
  var unique = indices.filter(function(v,i,a){ return a.indexOf(v)===i; });
  var op = unique.length === 1 ? 'subtract' : 'bridge';

  _updateBridgePreview();

  try {
    var data = await apiPost('/api/polygon_op', {
      operation: op,
      polygon: geom,
      points: _bridgePts.map(function(p){ return p.coord; })
    });
    exitBridgeMode();
    if (st.editMode) {
      st.editMode = false;
      map.doubleClickZoom.enable();
      editLayers.clearLayers();
    }
    _detachEditListeners();
    _setEditedPoly(data.geometry); markDirty();
    document.getElementById('rstbtn').disabled = false;
    // _updateSurveyDisplay is in polygon-edit.js — import at runtime
    import('./polygon-edit.js').then(function(m){ m._updateSurveyDisplay(data.geometry); });
  } catch(e) {
    exitBridgeMode();
    _showBridgeError(e.detail || ('Operation failed: ' + e.message));
  }
}

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
  var N = ring.length - 1;
  var iA = -1, iB = -1;
  for (i = 0; i < N; i++) {
    if (ring[i][0] === coordA[0] && ring[i][1] === coordA[1]) iA = i;
    if (ring[i][0] === coordB[0] && ring[i][1] === coordB[1]) iB = i;
  }
  if (iA === -1 || iB === -1 || iA === iB) return null;
  if (iA > iB) { var t = iA; iA = iB; iB = t; }
  if (iB - iA < 2 || N - (iB - iA) < 2) return null;

  var r1 = [];
  for (i = iA; i <= iB; i++) r1.push(ring[i]);
  r1.push(ring[iA]);

  var r2 = [];
  for (i = iB; i < N; i++) r2.push(ring[i]);
  for (i = 0; i <= iA; i++) r2.push(ring[i]);
  r2.push(ring[iB]);

  var holesA = partCoords.slice(1);
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

export async function commitSplit() {
  if (!st._activeJob) { _showBridgeError('Save the job first before splitting'); return; }
  if (!_splitReady || _bridgePts.length !== 2) return;
  var geom = st.editMode ? _geomFromEditLayers() : _currentSurveyGeom();
  if (!geom) { exitBridgeMode(); return; }
  var halves = _computeSplitPolygons(geom, _bridgePts[0].coord, _bridgePts[1].coord, _bridgePts[0].polyIdx);
  if (!halves) {
    _showBridgeError('Select points that leave at least 2 vertices on each side');
    return;
  }
  exitBridgeMode();
  if (st.editMode) {
    st.editMode = false;
    map.doubleClickZoom.enable();
    editLayers.clearLayers();
    _detachEditListeners();
  }
  try {
    st._ownSavedJob = st._activeJob;
    await apiPost(jobApiUrl(st._activeJob, '/split'), {polygon_a: halves[0], polygon_b: halves[1]});
    st._dirty = false;
    var { loadJobsList } = await import('./jobs-panel.js');
    var { openJob } = await import('./job-ops.js');
    await loadJobsList();
    openJob(st._activeJob);
  } catch(e) {
    _showBridgeError(e.detail || ('Split failed: ' + e.message));
  }
}

// Called from polygon-edit.js event handler for bridge pick clicks
export function _pickBridgeClick(e) {
  if (!st._bridgeMode || e.button !== 0) return;
  if (_splitReady) {
    if (!e.target.classList.contains('bridge-split-btn') && !e.target.classList.contains('bridge-cancel-x'))
      e.stopPropagation();
    return;
  }
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
}

export { enterBridgeMode, _enterBridgeModeWithVertex, _collectVertsFromEditLayers, _collectVerts };
