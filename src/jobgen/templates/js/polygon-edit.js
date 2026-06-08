// ── Polygon editing ───────────────────────────────────────────────────────────
// Enter edit mode on dblclick on the polygon.
// Save edits on dblclick outside the polygon (or on the map background).
// We COPY the survey polygon into editLayers on demand — never share the same
// Leaflet layer object between two FeatureGroups, which causes silent drop.

var _editCHandler = null;        // container-level contextmenu capture (edit mode)
var _editKHandler = null;        // container-level click capture (bridge picking)
var _editVHandler = null;        // draw:editvertex → re-patch midpoint icons
var _editAllPolysDeleted = false; // set when user deletes the last polygon in edit mode

function toggleEdit() {
  if (!previewData || !lrs.survey || editMode) return;
  editMode = true;
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

function saveEdit() {
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
  } else if (_editAllPolysDeleted) {
    // All polygons were intentionally removed during editing — clear everything
    // and leave the map empty so right-click can create a scratch polygon,
    // exactly like a blank new job.
    _editAllPolysDeleted = false;
    _clearEditedPoly();
    if (lrs.survey) { map.removeLayer(lrs.survey); lrs.survey = null; }
    if (lrs.vertices) { map.removeLayer(lrs.vertices); lrs.vertices = null; }
    previewData = null;
    markDirty();
    exitBridgeMode();
    _detachEditListeners();
    if (_editVHandler) { map.off('draw:editvertex', _editVHandler); _editVHandler = null; }
    return;  // skip startPreview — map stays empty, right-click works
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

// Dblclick on map background saves the edit
map.on('dblclick', function(e) {
  if (_mvMode) return;
  if (editMode) saveEdit();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (_bridgeMode) exitBridgeMode();
    else if (editMode) saveEdit();
    else if (_activeJob && _activeJobFolder && !_mvMode) confirmIfDirty(function(){ openMapView(_activeJobFolder); });
  }
});

// ── Right-click scratch square ────────────────────────────────────────────────
// Right-click on an empty map creates a 300×300 m square centred on the cursor.
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
function _attachEditListeners() {
  _detachEditListeners();

  _editCHandler = function(e) {
    // Right-click in edit mode: enter bridge (snapping to nearest vertex)
    // or cancel if already in bridge mode.
    // Special case: right-clicking a vertex on a 3-vertex polygon removes the
    // whole polygon (Leaflet.draw won't allow deletion below 3, so we handle it).
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
    if (!best) return;
    // If the polygon this vertex belongs to is already at the minimum (3 verts),
    // right-click removes the whole polygon rather than entering bridge mode.
    if (editMode) {
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
    // Left-click in bridge mode: pick a vertex.
    if (!_bridgeMode || e.button !== 0) return;
    // In split-ready state, only the hint bar buttons are allowed through.
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

// ── Remove polygons that have dropped below 3 vertices ───────────────────────
function _removeDegenPolys() {
  var toRemove = [];
  editLayers.eachLayer(function(l) {
    var lls = l.getLatLngs();
    var ring = Array.isArray(lls[0]) ? lls[0] : lls;
    // Leaflet.draw stops at 3 vertices (triangle). Below that the layer is
    // degenerate and can't be a valid polygon — remove it.
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

// ── Leaflet.draw midpoint diamond styling ─────────────────────────────────────
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

// ── Survey display update ─────────────────────────────────────────────────────
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

// ── Reset polygon ─────────────────────────────────────────────────────────────
function resetPoly() {
  saveEdit();
  _clearEditedPoly();
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  document.getElementById('offset').value = 0;
  setSimpManual(0, true);
  if (isRunning) { _pendingPreview = true; } else { startPreview(); }
}
