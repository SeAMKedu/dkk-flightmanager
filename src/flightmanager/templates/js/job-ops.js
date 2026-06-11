// ── Job open / restore / delete / rename / stale / color ─────────────────────

import { st } from './state.js';
import { map, lrs, editLayers, resetLrs, resetMapToUserLocation } from './map-init.js';
import { escHtml, jobApiUrl } from './utils.js';
import { markDirty, confirmIfDirty, xbUpdate } from './dirty-tracking.js';
import { showError, clearError, updateFolderHint, updateGsd, setRadiusLinked,
         setSub, setSimpAuto, setSimpManual, setAutoTimer,
         getAutoTimer, setFitBoundsFlag, setLastPreviewedIds, _setEditedPoly, _clearEditedPoly,
         _setSec } from './form-controls.js';
import { _legendUserVis, redrawRings, resetLegend } from './legend.js';
import { loadJobsList } from './jobs-panel.js';
import { renderStatus } from './status-panel.js';
import { renderMap } from './map-layers.js';
import { updateRouteOverlay, updateRouteStats, setRouteAngleSilent as _setRouteAngleSilentRP,
         setSpeedSilent, _renderAngleControl } from './route-planner.js';
import { _cpSetFromHex, _syncPaletteActive } from './color-picker.js';
import { hideExtModifiedNotice } from './event-stream.js';
// Circular — only called at runtime:
import { startPreview } from './preview-runner.js';
import { closeMapView, getMvMode, setMvFromEditor, openMapView } from './map-view.js';
import { _detachEditListeners } from './polygon-edit.js';
import { _clearTakeoff, getTakeoffAuto, getTakeoffUserMoved, _renderTakeoffMarker,
         setTakeoffAuto, setTakeoffUserMoved } from './takeoff.js';

export function openJob(path) {
  if (st.isRunning) return;
  if (getMvMode()) closeMapView();
  confirmIfDirty(function() { setMvFromEditor(true); _doOpenJob(path); });
}

export async function _doOpenJob(path) {
  try {
    var r = await fetch(jobApiUrl(path));
    if (!r.ok) { showError('Could not load job: HTTP ' + r.status); return; }
    var data = await r.json();
    var p = data.params;
    var name = path.includes('/') ? path.split('/').pop() : path;
    var autoTimer = getAutoTimer();
    if (autoTimer) { clearTimeout(autoTimer); setAutoTimer(null); }
    Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
    resetLrs();
    editLayers.clearLayers();
    st.editMode = false; _detachEditListeners();
    _clearTakeoff();
    if (p && p.takeoff_point_4326) {
      setTakeoffAuto(p.takeoff_point_4326);
      setTakeoffUserMoved(true);
      _renderTakeoffMarker(p.takeoff_point_4326);
    }
    _restoreFormFromParams(p);
    document.getElementById('jname').value = name;
    st._activeJob = path;
    st._activeJobFolder = data.folder || null;
    updateFolderHint();
    _setColorPicker(p && p.color);
    st._dirty = false; xbUpdate();
    clearError();
    hideExtModifiedNotice();
    document.querySelectorAll('.jcard').forEach(function(c){ c.classList.toggle('active', c.dataset.path === path); });
    setFitBoundsFlag(true);
    if (p && p.last_preview_geojson) {
      st.previewData = p.last_preview_geojson;
      setLastPreviewedIds(
        ((p.inputs && p.inputs.parcel_ids)||[]).join(',')
        + '||' + ((p.inputs && p.inputs.property_ids)||[]).join(',')
      );
      try {
        renderMap(st.previewData);
        redrawRings();
        if (st.previewData.stats && st.previewData.stats.route_angle_deg_auto != null) {
          st._routeAngleAuto = st.previewData.stats.route_angle_deg_auto;
          _renderAngleControl();
        }
        updateRouteOverlay();
        resetLegend(_legendUserVis);
        renderStatus(st.previewData.stats);
        if (st.previewData.stats) {
          updateRouteStats({
            strip_count:     st.previewData.stats.route_strip_count,
            photo_count:     st.previewData.stats.route_photo_count,
            flight_time_min: st.previewData.stats.route_flight_time_min,
          });
        }
        document.getElementById('rstbtn').disabled = false;
      } catch(ex) { console.error('[openJob] render error', ex); }
    } else {
      st.previewData = null;
      renderStatus(null);
      document.getElementById('legend').classList.add('inactive');
      if (st.editedPoly) {
        st.previewData = {survey: st.editedPoly};
        import('./polygon-edit.js').then(function(m){ m._updateSurveyDisplay(st.editedPoly); });
        // fitBounds after display
        setTimeout(function(){
          if (lrs.survey) map.fitBounds(lrs.survey.getBounds(), {padding: [40, 40]});
        }, 50);
        document.getElementById('rstbtn').disabled = false;
      } else {
        document.getElementById('rstbtn').disabled = true;
        resetMapToUserLocation();
        import('./form-controls.js').then(function(m){ m.focusArea(); });
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
    _setRouteAngleSilentRP(p.flight.route_angle_deg != null ? p.flight.route_angle_deg : null);
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
    st.editedPoly = null; st.polyModified = false;
    document.getElementById('modbadge').style.display = 'none';
  }
  var hasIds = !!(p.inputs && ((p.inputs.parcel_ids||[]).length || (p.inputs.property_ids||[]).length));
  _setSec('area', hasIds);
}

export function goBackToMap() {
  confirmIfDirty(function() { openMapView(st._activeJobFolder || null); });
}

export async function revealJob(path) {
  try {
    var r = await fetch(jobApiUrl(path, '/reveal'), {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Could not open folder');
    }
  } catch(e) { showError('Could not open folder: ' + e.message); }
}

export async function cloneJob(path) {
  if (st.isRunning) return;
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

export function confirmDeleteJob(j) {
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

export async function deleteJob(j) {
  try {
    var r = await fetch(jobApiUrl(j.path), {method:'DELETE'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Delete failed'); return;
    }
    if (st._activeJob === j.path) {
      st._activeJob = null; st._activeJobFolder = null; st._dirty = false;
      import('./form-controls.js').then(function(m){ m._doNewJob(); });
    }
    await loadJobsList();
  } catch(e) { showError('Delete failed: ' + e.message); }
}

export function startRename(j) {
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

export async function doRename(j, newName) {
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
    if (st._activeJob === j.path) {
      st._activeJob = data.path;
      document.getElementById('jname').value = newName;
      updateFolderHint();
    }
    await loadJobsList();
  } catch(e) { showError('Rename failed: ' + e.message); await loadJobsList(); }
}

export function showStaleNotice(stale) {
  var el = document.getElementById('stale-notice');
  el.textContent = 'Cached tiles may be stale (' + stale.length + ' missing) — preview will re-fetch.';
  el.style.display = 'block';
}
export function hideStaleNotice() {
  var el = document.getElementById('stale-notice');
  el.style.display = 'none'; el.textContent = '';
}

var _DEFAULT_JOB_COLOR = '#3b82f6';

export function _setColorPicker(color) {
  var hex = color || _DEFAULT_JOB_COLOR;
  document.getElementById('job-color').value = hex;
  document.getElementById('color-btn').disabled = !st._activeJob;
  _cpSetFromHex(hex);
}

function _closeColorPopup() {
  document.getElementById('color-popup').classList.remove('open');
}

export function toggleColorPopup(e) {
  e.stopPropagation();
  var popup = document.getElementById('color-popup');
  if (popup.classList.toggle('open')) {
    _cpSetFromHex(document.getElementById('job-color').value || _DEFAULT_JOB_COLOR);
    setTimeout(function() {
      document.addEventListener('click', function handler(ev) {
        if (!popup.contains(ev.target)) { _closeColorPopup(); }
        else { document.addEventListener('click', handler, {once: true}); }
      }, {once: true});
    }, 0);
  }
}

export function _applyColor(hex) {
  _cpSetFromHex(hex);
  document.getElementById('job-color').value = hex;
  document.getElementById('job-color').dispatchEvent(new Event('change'));
  _closeColorPopup();
}

export function initColorPalette(colors) {
  var palette = document.getElementById('color-palette');
  if (!palette || !colors || !colors.length) return;
  palette.innerHTML = '';
  colors.forEach(function(hex) {
    var s = document.createElement('div');
    s.className = 'color-swatch';
    s.dataset.color = hex.toLowerCase();
    s.style.background = hex;
    s.title = hex;
    s.addEventListener('click', function(e) { e.stopPropagation(); _applyColor(hex); });
    palette.appendChild(s);
  });
}

document.getElementById('job-color').addEventListener('change', async function() {
  if (!st._activeJob) return;
  try {
    st._ownSavedJob = st._activeJob;
    await fetch(jobApiUrl(st._activeJob), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({color: this.value})
    });
  } catch(e) { console.warn('[color patch]', e); }
});
