// ── Form controls: GSD, radius, subcategory, simplify, poly state ─────────────

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

function clearPolyEdit() {
  var hasPids = !!(document.getElementById('pids').value.trim() || document.getElementById('kids').value.trim());
  if (!hasPids) return;
  if (!_polySetWithIds) return;
  _clearEditedPoly();
}

function _setEditedPoly(geom) {
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

// ── Auto-update scheduling ────────────────────────────────────────────────────
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

function onIdBlur() {
  setTimeout(function() {
    var active = document.activeElement;
    if (active === document.getElementById('pids') || active === document.getElementById('kids')) return;
    var key = idsKey();
    if (!key.replace('||','').trim()) return;
    if (key === _lastPreviewedIds) return;
    _fitBoundsOnNextRender = true;
    scheduleAutoUpdate(true);
  }, 150);
}
document.getElementById('pids').addEventListener('blur', onIdBlur);
document.getElementById('kids').addEventListener('blur', onIdBlur);

// ── New Job ───────────────────────────────────────────────────────────────────
function newJob() {
  if (isRunning) return;
  if (_mvMode) closeMapView();
  confirmIfDirty(_doNewJob);
}
function _doNewJob() {
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  document.getElementById('jname').value = defaultJobName();
  document.getElementById('pids').value = '';
  document.getElementById('kids').value = '';
  updatePathHint();
  Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
  editLayers.clearLayers();
  editMode = false;
  _detachEditListeners();
  previewData = null; _clearEditedPoly(); _lastPreviewedIds = '';
  _activeJob = null; _activeJobFolder = null; _dirty = false; _altCap = null;
  _clearTakeoff();
  _setColorPicker(null);
  if (_dataAttribution) { map.attributionControl.removeAttribution(_dataAttribution); _dataAttribution = ''; }
  clearPolyEdit();
  clearError();
  hideExtModifiedNotice();
  document.getElementById('offset').value = 0;
  setSimpAuto(true);
  hideStaleNotice();
  document.getElementById('xb').disabled = true;
  document.getElementById('rstbtn').disabled = true;
  renderStatus(null);
  setRadiusLinked(true);
  document.getElementById('legend').classList.add('inactive');
  document.querySelectorAll('.jcard').forEach(function(c){ c.classList.remove('active'); });
  focusArea();
  resetMapToUserLocation();
}

// ── Area section focus hint ───────────────────────────────────────────────────
function focusArea() {
  var el = document.getElementById('area-sec');
  el.classList.remove('area-focus');
  void el.offsetWidth;
  el.classList.add('area-focus');
}
function clearAreaFocus() {
  document.getElementById('area-sec').classList.remove('area-focus');
}
document.getElementById('pids').addEventListener('input', clearAreaFocus);
document.getElementById('kids').addEventListener('input', clearAreaFocus);

// ── Form params ───────────────────────────────────────────────────────────────
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
