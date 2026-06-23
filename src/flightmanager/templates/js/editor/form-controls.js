// ── Form controls: GSD, radius, subcategory, simplify, poly state ─────────────

import { st } from '../core/state.js';
import { getTplSettings } from '../panels/tpl-modal.js';
import { map, clearAllLayers, resetMapToUserLocation } from '../map/map-init.js';
import { markDirty, confirmIfDirty } from '../core/dirty-tracking.js';
import { redrawRings } from '../map/legend.js';
import { _clearTakeoff } from './takeoff.js';
import { renderStatus } from '../panels/status-panel.js';
// Circular imports — safe, only called at runtime:
import { startPreview } from './preview-runner.js';
import { closeMapView, getMvMode } from '../map/map-view.js';
import { hideExtModifiedNotice } from '../core/event-stream.js';
import { setSpeedSilent, setRouteAngleSilent, _clearRouteLayer, updateRouteStats } from './route-planner.js';
import { hideStaleNotice, _setColorPicker, clearActiveJob } from '../jobs/job-ops.js';
import { cancelEdit } from './polygon-edit.js';

export function defaultJobName() {
  var n = new Date();
  return 'job-' + n.getFullYear()
    + String(n.getMonth()+1).padStart(2,'0')
    + String(n.getDate()).padStart(2,'0')
    + '-'
    + String(n.getHours()).padStart(2,'0')
    + String(n.getMinutes()).padStart(2,'0');
}

export function updateFolderHint() {
  var el = document.getElementById('job-folder-hint');
  if (st._activeJobFolder) {
    el.textContent = st._activeJobFolder;
    el.classList.add('has-folder');
  } else {
    el.classList.remove('has-folder');
  }
}

// ── GSD ───────────────────────────────────────────────────────────────────────
export function updateGsd() {
  var h = parseFloat(document.getElementById('hgt').value);
  var d = st.drones.find(function(x){return x.name === document.getElementById('dsel').value;});
  var el = document.getElementById('gsdv');
  if (!d || isNaN(h)) { el.textContent = '—'; return; }
  el.textContent = (h * d.pixel_pitch_um / (d.focal_length_mm * 10)).toFixed(2);
  if (st._speedMsOverride === null) _renderSpeedControl();
}

// Module-local state with getter/setter exports for cross-module access
var _radiusLinked = true;

export function setRadiusLinked(linked) {
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
export function getRadiusLinked() { return _radiusLinked; }

document.getElementById('warn-radius-hint').addEventListener('dblclick', function() {
  if (!_radiusLinked) { setRadiusLinked(true); redrawRings(); }
});

document.getElementById('hgt').addEventListener('input', function() {
  updateGsd();
  if (_radiusLinked) {
    var h = parseFloat(this.value);
    if (!isNaN(h) && h > 0) document.getElementById('warn-radius').value = Math.round(3 * h);
  }
  if (st._altCap !== null && st.previewData) renderStatus(st.previewData.stats);
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
export function setSub(v, silent) {
  _subVal = v;
  document.getElementById('sub-a3').classList.toggle('active', v === 'A3');
  document.getElementById('sub-a2').classList.toggle('active', v === 'A2');
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
export function getSub() { return _subVal; }

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
export function setSimpAuto(silent) {
  _simpAuto = true; _simpRender();
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
export function setSimpManual(v, silent) {
  _simpAuto = false;
  // snap to nearest step
  var best = 0;
  for (var i = 0; i < _simpSteps.length; i++) {
    if (Math.abs(_simpSteps[i] - v) < Math.abs(_simpSteps[best] - v)) best = i;
  }
  _simpIdx = best; _simpRender();
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
export function simpStep(dir) {
  _simpAuto = false;
  _simpIdx = Math.max(0, Math.min(_simpSteps.length - 1, _simpIdx + dir));
  _simpRender(); clearPolyEdit(); scheduleAutoUpdate();
}
export function getSimplify() {
  return _simpAuto ? 'auto' : String(_simpSteps[_simpIdx]);
}
_simpRender();

// Speed control render — called from updateGsd
function _renderSpeedControl() {
  // Delegate to route-planner when auto mode; no-op until route-planner is loaded.
  // route-planner exports setSpeedSilent; _renderSpeedControl is internal to route-planner.
  // We only need to trigger the display update from here in GSD context.
  // The route-planner handles its own render on speed changes.
}

// ── Polygon state helpers ─────────────────────────────────────────────────────
// Clear the custom polygon only when the polygon was established WITH IDs present.
// If the polygon was drawn/loaded while IDs were empty, changing IDs afterwards
// should keep the polygon as the survey area.  Only the explicit Reset button
// truly discards a custom polygon.
['kochk','dsel'].forEach(function(id){
  document.getElementById(id).addEventListener('change', clearPolyEdit);
});
document.getElementById('pids').addEventListener('input', clearPolyEdit);
document.getElementById('kids').addEventListener('input', clearPolyEdit);

export function clearPolyEdit() {
  var hasPids = !!(document.getElementById('pids').value.trim() || document.getElementById('kids').value.trim());
  if (!hasPids) return;
  if (!st._polySetWithIds) return;
  _clearEditedPoly();
}

export function _setEditedPoly(geom) {
  st.editedPoly = geom;
  st.polyModified = true;
  st._polySetWithIds = !!(
    document.getElementById('pids').value.trim() ||
    document.getElementById('kids').value.trim()
  );
  document.getElementById('modbadge').style.display = 'block';
}

export function _clearEditedPoly() {
  st.editedPoly = null; st.polyModified = false; st._polySetWithIds = false;
  document.getElementById('modbadge').style.display = 'none';
}

// ── Auto-update scheduling ────────────────────────────────────────────────────

export function idsKey() {
  return document.getElementById('pids').value.trim() + '||' + document.getElementById('kids').value.trim();
}

export function scheduleAutoUpdate(force) {
  markDirty();
  if (!force && !st.previewData) return;
  if (st.editor.autoTimer) clearTimeout(st.editor.autoTimer);
  st.editor.autoTimer = setTimeout(function() { st.editor.autoTimer = null; startPreview(); }, 400);
}
['dsel','kochk'].forEach(function(id){
  document.getElementById(id).addEventListener('change', scheduleAutoUpdate);
});
document.getElementById('hgt').addEventListener('change', scheduleAutoUpdate);
document.getElementById('offset').addEventListener('change', scheduleAutoUpdate);

export function onIdBlur() {
  var key = idsKey();
  if (key.replace('||', '').trim() && key !== st.editor.lastPreviewedIds) markDirty();
  setTimeout(function() {
    var active = document.activeElement;
    if (active === document.getElementById('pids') || active === document.getElementById('kids')) return;
    var key = idsKey();
    if (!key.replace('||','').trim()) return;
    if (key === st.editor.lastPreviewedIds) return;
    st.editor.fitBounds = true;
    scheduleAutoUpdate(true);
    _setSec('area', true);
  }, 150);
}
document.getElementById('pids').addEventListener('blur', onIdBlur);
document.getElementById('kids').addEventListener('blur', onIdBlur);

// ── New Job ───────────────────────────────────────────────────────────────────
export function newJob() {
  if (st.isRunning) return;
  if (getMvMode()) closeMapView();
  confirmIfDirty(_doNewJob);
}
export function _doNewJob() {
  if (st.editor.autoTimer) { clearTimeout(st.editor.autoTimer); st.editor.autoTimer = null; }
  document.getElementById('jname').value = defaultJobName();
  document.getElementById('pids').value = '';
  document.getElementById('kids').value = '';
  updateFolderHint();
  clearAllLayers();
  cancelEdit();   // single edit-mode teardown (also re-enables zoom + drops the vertex listener)
  st.previewData = null; _clearEditedPoly(); st.editor.lastPreviewedIds = '';
  clearActiveJob();
  _clearTakeoff();
  _setColorPicker(null);
  if (st._dataAttribution) { map.attributionControl.removeAttribution(st._dataAttribution); st._dataAttribution = ''; }
  clearPolyEdit();
  clearError();
  hideExtModifiedNotice();
  document.getElementById('offset').value = 0;
  setSpeedSilent(null);
  setRouteAngleSilent(null);
  st._routeAngleAuto = null;
  _clearRouteLayer();
  updateRouteStats(null);
  setSimpAuto(true);
  hideStaleNotice();
  document.getElementById('xb').disabled = true;
  document.getElementById('rstbtn').disabled = true;
  st._waypointMode = false;
  renderStatus(null);
  setRadiusLinked(true);
  document.getElementById('legend').classList.add('inactive');
  document.querySelectorAll('.jcard').forEach(function(c){ c.classList.remove('active'); });
  focusArea();
  resetMapToUserLocation();
}

// ── Section collapse ──────────────────────────────────────────────────────────
export function toggleSec(id) {
  var sec = document.getElementById(id + '-sec');
  if (!sec) return;
  var collapsed = sec.classList.toggle('collapsed');
  if (id !== 'area') localStorage.setItem('sec-' + id + '-collapsed', collapsed ? '1' : '0');
}

export function _setSec(id, collapsed) {
  var sec = document.getElementById(id + '-sec');
  if (sec) sec.classList.toggle('collapsed', collapsed);
}

function _initSecState() {
  ['flight', 'poly'].forEach(function(id) {
    if (localStorage.getItem('sec-' + id + '-collapsed') === '1') {
      var sec = document.getElementById(id + '-sec');
      var body = sec && sec.querySelector('.sec-body');
      if (body) body.style.transition = 'none';
      if (sec) sec.classList.add('collapsed');
      if (body) requestAnimationFrame(function() { body.style.transition = ''; });
    }
  });
}
_initSecState();

// ── Area section focus hint ───────────────────────────────────────────────────
export function focusArea() {
  _setSec('area', false);
  var el = document.getElementById('area-sec');
  el.classList.remove('area-focus');
  void el.offsetWidth;
  el.classList.add('area-focus');
}
export function clearAreaFocus() {
  document.getElementById('area-sec').classList.remove('area-focus');
}
document.getElementById('pids').addEventListener('input', clearAreaFocus);
document.getElementById('kids').addEventListener('input', clearAreaFocus);

// ── Form params ───────────────────────────────────────────────────────────────
export function parseIds(txt) {
  return txt.split(/[,\s]+/).map(function(s){return s.trim();}).filter(Boolean);
}

export function getParams() {
  return {
    parcel_ids: parseIds(document.getElementById('pids').value),
    property_ids: parseIds(document.getElementById('kids').value),
    drone: document.getElementById('dsel').value || null,
    height_m: parseFloat(document.getElementById('hgt').value) || null,
    subcategory: getSub(),
    offset_m: parseFloat(document.getElementById('offset').value) || 0,
    simplify: getSimplify(),
    keepout: document.getElementById('kochk').checked,
    preview_radius_m: parseFloat(document.getElementById('warn-radius').value) || null,
    route_angle_deg: st._routeAngleDeg,
    speed_ms: st._speedMsOverride,
    template_settings: getTplSettings(),
  };
}

export function showError(msg) {
  var el = document.getElementById('errdiv');
  el.textContent = 'Error: ' + msg;
  el.style.display = 'block';
}
export function clearError() {
  document.getElementById('errdiv').style.display = 'none';
  document.getElementById('errdiv').textContent = '';
}
