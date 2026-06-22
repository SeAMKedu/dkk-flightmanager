// ── Job open / restore / delete / rename / stale / color ─────────────────────

import { st } from '../core/state.js';
import { map, lrs, resetLrs, resetMapToUserLocation } from '../map/map-init.js';
import { escHtml, jobApiUrl } from '../core/utils.js';
import { apiGet, apiPost, apiPatch, apiDelete } from '../core/api.js';
import { confirmIfDirty, xbUpdate } from '../core/dirty-tracking.js';
import { showError, clearError, updateFolderHint, updateGsd, setRadiusLinked,
         setSub, setSimpAuto, setSimpManual, _setEditedPoly,
         _setSec } from '../editor/form-controls.js';
import { redrawRings } from '../map/legend.js';
import { loadJobsList } from './jobs-panel.js';
import { renderStatus } from '../panels/status-panel.js';
import { renderMap } from '../map/map-layers.js';
import { setRouteAngleSilent as _setRouteAngleSilentRP,
         setSpeedSilent } from '../editor/route-planner.js';
import { _cpSetFromHex } from '../panels/color-picker.js';
import { restoreTplSettings } from '../panels/tpl-modal.js';
import { hideExtModifiedNotice } from '../core/event-stream.js';
// Circular — only called at runtime:
import { startPreview } from '../editor/preview-runner.js';
import { closeMapView, getMvMode, openMapView } from '../map/map-view.js';
import { cancelEdit } from '../editor/polygon-edit.js';
import { _clearTakeoff, _renderTakeoffMarker } from '../editor/takeoff.js';

export function openJob(path) {
  if (st.isRunning) return;
  if (getMvMode()) closeMapView();
  confirmIfDirty(function() { st.mv.fromEditor = true; _doOpenJob(path); });
}

export async function _doOpenJob(path) {
  try {
    var data;
    try { data = await apiGet(jobApiUrl(path)); }
    catch (e) { showError('Could not load job: ' + (e.detail || e.message)); return; }
    var p = data.params;
    var name = path.includes('/') ? path.split('/').pop() : path;
    if (st.editor.autoTimer) { clearTimeout(st.editor.autoTimer); st.editor.autoTimer = null; }
    Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
    resetLrs();
    // Fully tear down any in-progress polygon edit/bridge mode (single seam in
    // polygon-edit) so its machinery doesn't linger into the job we're opening.
    cancelEdit();
    _clearTakeoff();
    if (p && p.takeoff_point_4326) {
      st.takeoff.auto = p.takeoff_point_4326;
      st.takeoff.userMoved = true;
      _renderTakeoffMarker(p.takeoff_point_4326);
    }
    st._waypointMode = !!(p && p.template_settings && p.template_settings.advanced_mode);
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
    st.editor.fitBounds = true;
    // Instant first-paint from the stored survey outline (map view + open). The
    // strips/transits/status are filled by the live startPreview() below, which
    // runs on every open to refresh buildings + UAS zones for the current area.
    var _outline = p && (p.survey_outline || p.custom_polygon_4326
      || (p.last_preview_geojson || {}).survey);
    if (_outline) {
      st.previewData = {survey: _outline};
      st.editor.lastPreviewedIds =
        ((p.inputs && p.inputs.parcel_ids)||[]).join(',')
        + '||' + ((p.inputs && p.inputs.property_ids)||[]).join(',');
      try {
        renderMap(st.previewData);
        redrawRings();
        document.getElementById('rstbtn').disabled = false;
      } catch(ex) { console.error('[openJob] render error', ex); }
    } else {
      st.previewData = null;
      renderStatus(null);
      document.getElementById('legend').classList.add('inactive');
      if (st.editedPoly) {
        st.previewData = {survey: st.editedPoly};
        import('../editor/polygon-edit.js').then(function(m){ m._updateSurveyDisplay(st.editedPoly); });
        // fitBounds after display
        setTimeout(function(){
          if (lrs.survey) map.fitBounds(lrs.survey.getBounds(), {padding: [40, 40]});
        }, 50);
        document.getElementById('rstbtn').disabled = false;
      } else {
        document.getElementById('rstbtn').disabled = true;
        resetMapToUserLocation();
        import('../editor/form-controls.js').then(function(m){ m.focusArea(); });
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
  restoreTplSettings(p.template_settings || {});
  var hasIds = !!(p.inputs && ((p.inputs.parcel_ids||[]).length || (p.inputs.property_ids||[]).length));
  _setSec('area', hasIds);
}

export function goBackToMap() {
  confirmIfDirty(function() { openMapView(st._activeJobFolder || null); });
}

export async function revealJob(path) {
  try { await apiPost(jobApiUrl(path, '/reveal')); }
  catch(e) { showError(e.detail || ('Could not open folder: ' + e.message)); }
}

export async function cloneJob(path) {
  if (st.isRunning) return;
  try {
    var data = await apiPost(jobApiUrl(path, '/clone'));
    await loadJobsList();
    openJob(data.path);
  } catch(e) { showError(e.detail || ('Clone failed: ' + e.message)); }
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
    await apiDelete(jobApiUrl(j.path));
    if (st._activeJob === j.path) {
      st._activeJob = null; st._activeJobFolder = null; st._dirty = false;
      import('../editor/form-controls.js').then(function(m){ m._doNewJob(); });
    }
    await loadJobsList();
  } catch(e) { showError(e.detail || ('Delete failed: ' + e.message)); }
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
    var data = await apiPatch(jobApiUrl(j.path), {new_name: newName});
    if (st._activeJob === j.path) {
      st._activeJob = data.path;
      document.getElementById('jname').value = newName;
      updateFolderHint();
    }
    await loadJobsList();
  } catch(e) { showError(e.detail || ('Rename failed: ' + e.message)); await loadJobsList(); }
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
    await apiPatch(jobApiUrl(st._activeJob), {color: this.value});
  } catch(e) { console.warn('[color patch]', e); }
});
