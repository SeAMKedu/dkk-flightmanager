// ── Polygon editing ───────────────────────────────────────────────────────────

import { st } from '../core/state.js';
import { map, lrs, editLayers, layerGeom } from '../map/map-init.js';
import { markDirty } from '../core/dirty-tracking.js';
import { confirmIfDirty } from '../core/dirty-tracking.js';
import { _setEditedPoly, _clearEditedPoly, setSimpManual } from './form-controls.js';
import { _buildVertexLayer, exitBridgeMode, _enterBridgeModeWithVertex,
         _collectVertsFromEditLayers, _collectVerts, _pickBridgeClick } from './polygon-bridge.js';
import { geomToPolys } from '../map/map-layers.js';
// Circular — only called at runtime:
import { startPreview } from './preview-runner.js';

var _editCHandler = null;
var _editKHandler = null;
var _editVHandler = null;
var _editAllPolysDeleted = false;

export function toggleEdit() {
  if (!st.previewData || !lrs.survey || st.editMode) return;
  st.editMode = true;
  _editAllPolysDeleted = false;
  map.doubleClickZoom.disable();
  editLayers.clearLayers();
  if (lrs.survey) map.removeLayer(lrs.survey);
  var style = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  lrs.survey.eachLayer(function(dp) {
    var clone = L.polygon(dp.getLatLngs(), style);
    editLayers.addLayer(clone);
    if (clone.editing) clone.editing.enable();
  });
  setTimeout(_patchMidpointIcons, 0);
  _editVHandler = function() {
    setTimeout(function() {
      _patchMidpointIcons();
      _removeDegenPolys();
    }, 0);
  };
  map.on('draw:editvertex', _editVHandler);
  _attachEditListeners();
}

export function saveEdit() {
  if (!st.editMode) return;
  st.editMode = false;
  map.doubleClickZoom.enable();
  var liveGeom = _geomFromEditLayers();
  editLayers.eachLayer(function(l) {
    if (l.editing && l.editing.enabled()) l.editing.disable();
  });
  editLayers.clearLayers();
  if (liveGeom) {
    document.getElementById('offset').value = 0;
    _setEditedPoly(liveGeom); markDirty();
    _updateSurveyDisplay(liveGeom);
  } else if (_editAllPolysDeleted) {
    _editAllPolysDeleted = false;
    _clearEditedPoly();
    if (lrs.survey) { map.removeLayer(lrs.survey); lrs.survey = null; }
    if (lrs.vertices) { map.removeLayer(lrs.vertices); lrs.vertices = null; }
    st.previewData = null;
    markDirty();
    exitBridgeMode();
    _detachEditListeners();
    if (_editVHandler) { map.off('draw:editvertex', _editVHandler); _editVHandler = null; }
    return;
  } else {
    var _fallback = (st.previewData && st.previewData.survey) || null;
    if (_fallback) _updateSurveyDisplay(_fallback);
    else if (lrs.survey) lrs.survey.addTo(map);
  }
  exitBridgeMode();
  _detachEditListeners();
  if (_editVHandler) { map.off('draw:editvertex', _editVHandler); _editVHandler = null; }
  setTimeout(startPreview, 0);
}

map.on('dblclick', function(_e) {
  if (st._mvMode) return;
  if (st.editMode) saveEdit();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (st._bridgeMode) exitBridgeMode();
    else if (st.editMode) saveEdit();
    else if (!st._mvMode) {
      if (document.activeElement && document.activeElement.tagName !== 'BODY') {
        document.activeElement.blur();
      }
      confirmIfDirty(function(){
        import('../map/map-view.js').then(function(m){ m.openMapView(st._activeJobFolder || null); });
      });
    }
  }
});

map.on('contextmenu', function(e) {
  if (st._mvMode) {
    import('../map/map-view.js').then(function(m){ if (st.mv.selected.size > 0) m.mvClearSel(); });
    return;
  }
  if (st.editMode || st._bridgeMode || _currentSurveyGeom()) return;
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
  st.previewData = {survey: geom};
  _updateSurveyDisplay(geom);
  map.fitBounds(lrs.survey.getBounds(), {padding: [60, 60]});
  document.getElementById('xb').disabled = false;
  document.getElementById('rstbtn').disabled = false;
  toggleEdit();
});

function _attachEditListeners() {
  _detachEditListeners();

  _editCHandler = function(e) {
    e.preventDefault(); e.stopPropagation();
    if (st._bridgeMode) { exitBridgeMode(); return; }
    var latlng = map.mouseEventToLatLng(e);
    var verts = st.editMode ? _collectVertsFromEditLayers() : _collectVerts(_currentSurveyGeom());
    var mp = map.latLngToContainerPoint(latlng);
    var best = null, bestD = 28;
    verts.forEach(function(v) {
      var vp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
      var d = Math.sqrt(Math.pow(vp.x-mp.x,2) + Math.pow(vp.y-mp.y,2));
      if (d < bestD) { bestD = d; best = v; }
    });
    if (!best) return;
    if (st.editMode) {
      var editLayerList = [];
      editLayers.eachLayer(function(l) { editLayerList.push(l); });
      var targetLayer = editLayerList[best.polyIdx];
      if (targetLayer) {
        var lls = targetLayer.getLatLngs();
        var ring = Array.isArray(lls[0]) ? lls[0] : lls;
        if (ring.length <= 3) {
          if (targetLayer.editing && targetLayer.editing.enabled()) targetLayer.editing.disable();
          editLayers.removeLayer(targetLayer);
          if (editLayers.getLayers().length === 0) _editAllPolysDeleted = true;
          return;
        }
      }
    }
    _enterBridgeModeWithVertex(best);
  };

  _editKHandler = function(e) {
    _pickBridgeClick(e);
  };

  var c = map.getContainer();
  c.addEventListener('contextmenu', _editCHandler, true);
  c.addEventListener('click',       _editKHandler, true);
}

export function _detachEditListeners() {
  var c = map.getContainer();
  if (_editCHandler) { c.removeEventListener('contextmenu', _editCHandler, true); _editCHandler = null; }
  if (_editKHandler) { c.removeEventListener('click',       _editKHandler, true); _editKHandler = null; }
}

function _removeDegenPolys() {
  var toRemove = [];
  editLayers.eachLayer(function(l) {
    var lls = l.getLatLngs();
    var ring = Array.isArray(lls[0]) ? lls[0] : lls;
    if (ring.length < 3) toRemove.push(l);
  });
  toRemove.forEach(function(l) {
    if (l.editing && l.editing.enabled()) l.editing.disable();
    editLayers.removeLayer(l);
  });
  if (toRemove.length > 0 && editLayers.getLayers().length === 0) {
    _editAllPolysDeleted = true;
  }
}

function _patchMidpointIcons() {
  var all = document.querySelectorAll('.leaflet-editing-icon');
  all.forEach(function(el) { el.classList.remove('ld-mid'); });
  var found = 0;
  all.forEach(function(el) {
    var op = parseFloat(el.style.opacity);
    if (!isNaN(op) && op < 1) { el.classList.add('ld-mid'); found++; }
  });
  if (!found) {
    all.forEach(function(el) {
      var op = el.parentElement && parseFloat(el.parentElement.style.opacity);
      if (op && op < 1) { el.classList.add('ld-mid'); }
    });
  }
}

export function _updateSurveyDisplay(geom) {
  if (lrs.survey) { map.removeLayer(lrs.survey); lrs.survey = null; }
  if (lrs.vertices) { map.removeLayer(lrs.vertices); lrs.vertices = null; }

  var surveyStyle = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  var polys = geomToPolys(geom, surveyStyle);
  if (polys.length) {
    lrs.survey = L.featureGroup(polys).addTo(map);
    lrs.survey.eachLayer(function(l) {
      l.on('dblclick', function(e) { L.DomEvent.stop(e); if (!st.editMode && !st._bridgeMode) toggleEdit(); });
    });
  }
  lrs.vertices = _buildVertexLayer(geom).addTo(map);
}

function _geomFromEditLayers() {
  var polys = [];
  editLayers.eachLayer(function(l) { polys.push(layerGeom(l)); });
  if (!polys.length) return null;
  if (polys.length === 1) return polys[0];
  return {type:'MultiPolygon', coordinates: polys.map(function(p){return p.coordinates;})};
}

function _currentSurveyGeom() {
  return st.editedPoly || (st.previewData && st.previewData.survey) || null;
}

export function resetPoly() {
  saveEdit();
  _clearEditedPoly();
  if (st.editor.autoTimer) { clearTimeout(st.editor.autoTimer); st.editor.autoTimer = null; }
  document.getElementById('offset').value = 0;
  setSimpManual(0, true);
  if (st.isRunning) { st._pendingPreview = true; } else { startPreview(); }
}
