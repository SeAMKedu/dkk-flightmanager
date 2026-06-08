// ── Job open / restore ────────────────────────────────────────────────────────

function openJob(path) {
  if (isRunning) return;
  if (_mvMode) closeMapView();
  confirmIfDirty(function() { _mvFromEditor = true; _doOpenJob(path); });
}

async function _doOpenJob(path) {
  try {
    var r = await fetch(jobApiUrl(path));
    if (!r.ok) { showError('Could not load job: HTTP ' + r.status); return; }
    var data = await r.json();
    var p = data.params;
    var name = path.includes('/') ? path.split('/').pop() : path;
    if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
    Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
    lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
    editLayers.clearLayers();
    editMode = false; _detachEditListeners();
    _clearTakeoff();
    if (p && p.takeoff_point_4326) {
      _takeoffAuto = p.takeoff_point_4326;
      _takeoffUserMoved = true;
      _renderTakeoffMarker(p.takeoff_point_4326);
    }
    _restoreFormFromParams(p);
    document.getElementById('jname').value = name;
    updatePathHint();
    _activeJob = path;
    _activeJobFolder = data.folder || null;
    _setColorPicker(p && p.color);
    _dirty = false;
    clearError();
    hideExtModifiedNotice();
    document.querySelectorAll('.jcard').forEach(function(c){ c.classList.toggle('active', c.dataset.path === path); });
    _fitBoundsOnNextRender = true;
    if (p && p.last_preview_geojson) {
      previewData = p.last_preview_geojson;
      _lastPreviewedIds = ((p.inputs && p.inputs.parcel_ids)||[]).join(',')
        + '||' + ((p.inputs && p.inputs.property_ids)||[]).join(',');
      try {
        renderMap(previewData);
        redrawRings();
        if (previewData.stats && previewData.stats.route_angle_deg_auto != null) {
          _routeAngleAuto = previewData.stats.route_angle_deg_auto;
          _renderAngleControl();
        }
        updateRouteOverlay();
        resetLegend(_legendUserVis);
        renderStatus(previewData.stats);
        if (previewData.stats) {
          updateRouteStats({
            strip_count:     previewData.stats.route_strip_count,
            photo_count:     previewData.stats.route_photo_count,
            flight_time_min: previewData.stats.route_flight_time_min,
          });
        }
        document.getElementById('xb').disabled = false;
        document.getElementById('rstbtn').disabled = false;
      } catch(ex) { console.error('[openJob] render error', ex); }
    } else {
      previewData = null;
      renderStatus(null);
      document.getElementById('legend').classList.add('inactive');
      if (editedPoly) {
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
    if (data.cache_stale && data.cache_stale.length) showStaleNotice(data.cache_stale);
    else hideStaleNotice();
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
    setRouteAngleSilent(p.flight.route_angle_deg != null ? p.flight.route_angle_deg : null);
    setSpeedSilent(p.flight.speed_ms != null ? p.flight.speed_ms : null);
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
