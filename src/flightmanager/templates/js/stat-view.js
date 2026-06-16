// ── Map view: stat panel ────────────────────────────────────────────────────

import { escHtml } from './utils.js';

var _mvStatMode = localStorage.getItem('mv-stat-mode') || 'normal';
var _statBinMap = {};

var _ALT_PAL  = ['#2563eb','#60a5fa','#93c5fd','#dbeafe'];
var _AREA_PAL = ['#fef9c3','#fde047','#fb923c','#f97316','#ef4444'];
var _LOST_PAL = ['#4ade80','#a3e635','#fde047','#fb923c','#ef4444'];
var _TIME_PAL = ['#f0fdf4','#86efac','#4ade80','#22c55e','#16a34a'];
var _SUB_PAL  = {A1:'#10b981', A2:'#f59e0b', A3:'#3b82f6'};
var _ND_COL   = '#94a3b8';
// Distinct hues for MGRS tiles (cycled). Job tiles vs neighbours differ by style.
var _MGRS_PAL = ['#06b6d4','#8b5cf6','#ec4899','#f59e0b','#10b981','#ef4444',
                 '#3b82f6','#84cc16','#f97316','#a855f7','#14b8a6','#eab308'];

var _mgrsLayer = null;                       // Leaflet layer group for tile outlines
var _mgrsCache = { folder: undefined, data: null };  // folder-stable tile data

export function getMvStatMode() { return _mvStatMode; }
export function getStatBinMap() { return _statBinMap; }

// Modes that recolor the job polygons (and dim the basemap). 'normal' and 'mgrs'
// leave jobs in their own colours so they read against the tile/basemap.
export function statModeColorsJobs() {
  return _mvStatMode !== 'normal' && _mvStatMode !== 'mgrs';
}

export function getMvStatColor(props) {
  return statModeColorsJobs() ? (_statBinMap[props.path] || _ND_COL) : (props.color || '#3b82f6');
}

export function onStatModeChange() {
  _mvStatMode = document.getElementById('mv-stat-mode').value;
  localStorage.setItem('mv-stat-mode', _mvStatMode);
  // map-view.js will call renderStatPanel and update layers
  import('./map-view.js').then(function(m){ m._onStatModeChangeInternal(_mvStatMode); });
}

export function renderStatPanel(allFeatures, mvSelected) {
  _statBinMap = {};
  var body = document.getElementById('mv-stat-body');
  if (!body) return;

  // MGRS mode draws a tile overlay + legend (async, folder-keyed); other modes clear it.
  if (_mvStatMode === 'mgrs') { _stMgrs(body); return; }
  clearMgrsLayer();

  var sel = mvSelected && mvSelected.size > 0 ? mvSelected : null;
  var active = sel ? allFeatures.filter(function(f) { return sel.has(f.properties.path); }) : allFeatures;

  switch (_mvStatMode) {
    case 'normal':      body.innerHTML = _stNormal(active); break;
    case 'subcategory': body.innerHTML = _stSubcat(allFeatures, active); break;
    case 'altitude':    body.innerHTML = _stBinned(allFeatures, active, _getAlt,  _ALT_PAL,  'm',   0, 'Lowest altitude', null, true); break;
    case 'area':        body.innerHTML = _stBinned(allFeatures, active, _getArea, _AREA_PAL, 'ha',  1, 'Largest', 'Smallest'); break;
    case 'lost_pct':    body.innerHTML = _stLost(allFeatures, active, false); break;
    case 'lost_ha':     body.innerHTML = _stLost(allFeatures, active, true); break;
    case 'flight_time': body.innerHTML = _stBinned(allFeatures, active, _getFT,   _TIME_PAL, 'min', 0, 'Longest', 'Shortest'); break;
  }
}

function _getAlt(p)  { return p.height_m; }
function _getArea(p) { return p.area_ha; }
function _getFT(p)   { return p.flight_time_min; }
function _getLostHa(p) {
  if (p.original_area_ha == null) return null;
  if (p.area_lost_pct != null) return p.original_area_ha * p.area_lost_pct / 100;
  if (p.area_ha == null) return null;
  return Math.max(0, p.original_area_ha - p.area_ha);
}

function _makeBins(vals, palette) {
  if (!vals.length) return [];
  var sorted = vals.slice().sort(function(a, b) { return a - b; });
  var min = sorted[0], max = sorted[sorted.length - 1];
  var n = Math.min(palette.length, sorted.length);
  var bins = [];
  for (var i = 0; i < n; i++) {
    bins.push({
      lo: min + (max - min) * i / n,
      hi: i === n - 1 ? max : min + (max - min) * (i + 1) / n,
      color: palette[i],
    });
  }
  return bins;
}

function _binIdx(v, bins) {
  if (v == null || !bins.length) return null;
  var min = bins[0].lo, max = bins[bins.length - 1].hi;
  if (min === max) return 0;
  return Math.min(bins.length - 1, Math.floor((v - min) / (max - min) * bins.length));
}

function _binRow(color, label, count) {
  return '<div class="mv-st-brow">'
    + '<span class="mv-st-sw" style="background:' + color + '"></span>'
    + '<span class="mv-st-bl">' + label + '</span>'
    + '<span class="mv-st-bc">' + count + '</span></div>';
}

function _divRow(label) {
  return '<div class="mv-st-div">' + label + '</div>';
}

function _jobRow(p, valStr) {
  var c = _statBinMap[p.path] || _ND_COL;
  var path = p.path.replace(/'/g, "\\'");
  return '<div class="mv-st-job" onclick="_mvStatJobClick(\'' + path + '\')" title="' + escHtml(p.name) + '">'
    + '<span class="mv-st-jdot" style="background:' + c + '"></span>'
    + '<span class="mv-st-jname">' + escHtml(p.name) + '</span>'
    + '<span class="mv-st-jval">' + valStr + '</span></div>';
}

function _fmtMin(m) {
  if (m < 60) return Math.round(m) + ' min';
  var h = Math.floor(m / 60), mm = Math.round(m % 60);
  return h + ' h' + (mm ? ' ' + mm + ' min' : '');
}

export function _mvStatJobClick(path) {
  import('./map-view.js').then(function(m){ m._mvStatJobClickInternal(path); });
}

// ── MGRS tiles mode ───────────────────────────────────────────────────────────

export function clearMgrsLayer() {
  if (_mgrsLayer) { _mgrsLayer.remove(); _mgrsLayer = null; }
}

function _tileColor(i) { return _MGRS_PAL[i % _MGRS_PAL.length]; }

async function _stMgrs(body) {
  var mv = await import('./map-view.js');
  var folder = mv.getMvCurrentFolder();
  if (_mgrsCache.folder === folder && _mgrsCache.data) {
    body.innerHTML = _mgrsLegend(_mgrsCache.data);
    if (_mgrsCache.data.grid_ok) _drawMgrsTiles(_mgrsCache.data.tiles);
    return;
  }
  body.innerHTML = '<div class="mv-st-nodata">Loading tiles…</div>';
  try {
    var url = '/api/mgrs_tiles' + (folder ? '?folder=' + encodeURIComponent(folder) : '');
    var data = await (await fetch(url)).json();
    if (_mvStatMode !== 'mgrs') return;  // mode changed while loading
    _mgrsCache = { folder: folder, data: data };
    body.innerHTML = _mgrsLegend(data);
    if (data.grid_ok) _drawMgrsTiles(data.tiles);
  } catch (e) {
    console.error('[mgrs]', e);
    body.innerHTML = '<div class="mv-st-nodata">MGRS tiles unavailable</div>';
  }
}

function _mgrsLegend(data) {
  if (!data.grid_ok || !data.tiles.length) {
    return '<div class="mv-st-nodata">' + escHtml(data.grid_msg || 'MGRS grid not available') + '</div>';
  }
  var r = '<div class="mv-st-div">Job tiles + neighbours</div>';
  data.tiles.forEach(function (t, i) {
    var col = _tileColor(i);
    var note = t.is_job ? (t.job_count + (t.job_count === 1 ? ' job' : ' jobs')) : 'neighbour';
    var swCls = t.is_job ? 'mv-st-sw' : 'mv-st-sw mv-st-sw-nb';
    r += '<div class="mv-st-brow"><span class="' + swCls + '" style="background:' + col
      + '"></span><span class="mv-st-bl">' + escHtml(t.id) + '</span>'
      + '<span class="mv-st-bc">' + note + '</span></div>';
  });
  return r;
}

function _drawMgrsTiles(tiles) {
  import('./map-init.js').then(function (m) {
    clearMgrsLayer();
    var grp = L.layerGroup();
    tiles.forEach(function (t, i) {
      if (!t.geometry) return;
      var col = _tileColor(i);
      L.geoJSON(t.geometry, { style: {
        color: col, weight: t.is_job ? 2.5 : 1.5, opacity: 0.95,
        dashArray: t.is_job ? null : '5,5',
        fill: true, fillColor: col, fillOpacity: t.is_job ? 0.14 : 0.05,
      } }).addTo(grp);
      if (t.center) {
        L.marker([t.center[0], t.center[1]], {
          interactive: false,
          icon: L.divIcon({
            className: 'mgrs-tile-label' + (t.is_job ? ' mgrs-job' : ''),
            html: t.id, iconSize: [0, 0],
          }),
        }).addTo(grp);
      }
    });
    grp.addTo(m.map);
    _mgrsLayer = grp;
  });
}

function _stNormal(active) {
  var count = active.length, area = 0, hasA = false, time = 0, hasT = false, bats = 0, hasB = false;
  active.forEach(function(f) {
    var p = f.properties;
    if (p.area_ha != null)        { area += p.area_ha;        hasA = true; }
    if (p.flight_time_min != null){ time += p.flight_time_min; hasT = true; }
    if (p.battery_count != null)  { bats += p.battery_count;  hasB = true; }
  });
  var r = '<div class="mv-st-row"><span class="mv-st-lbl">Jobs</span><span>' + count + '</span></div>';
  if (hasA) r += '<div class="mv-st-row"><span class="mv-st-lbl">Total area</span><span>' + area.toFixed(1) + ' ha</span></div>';
  if (hasT) r += '<div class="mv-st-row"><span class="mv-st-lbl">Flight time</span><span>' + _fmtMin(time) + '</span></div>';
  if (hasB) r += '<div class="mv-st-row"><span class="mv-st-lbl">Batteries</span><span>' + bats + '</span></div>';
  return r;
}

function _stSubcat(all, active) {
  all.forEach(function(f) {
    var s = f.properties.subcategory;
    _statBinMap[f.properties.path] = (s && _SUB_PAL[s]) ? _SUB_PAL[s] : _ND_COL;
  });
  var counts = {}, nd = 0;
  active.forEach(function(f) {
    var s = f.properties.subcategory;
    if (!s) { nd++; return; }
    counts[s] = (counts[s] || 0) + 1;
  });
  var r = '';
  ['A1', 'A2', 'A3'].forEach(function(s) {
    if (counts[s]) r += _binRow(_SUB_PAL[s], s, counts[s] + (counts[s] === 1 ? ' job' : ' jobs'));
  });
  if (nd) r += _binRow(_ND_COL, 'No data', nd + (nd === 1 ? ' job' : ' jobs'));
  return r || '<div class="mv-st-nodata">No data</div>';
}

function _stBinned(all, active, getVal, palette, unit, dec, topLabel, botLabel, topAsc) {
  var allVals = all.map(function(f) { return getVal(f.properties); }).filter(function(v) { return v != null; });
  var bins = _makeBins(allVals, palette);

  all.forEach(function(f) {
    var v = getVal(f.properties);
    if (v == null) { _statBinMap[f.properties.path] = _ND_COL; return; }
    var idx = _binIdx(v, bins);
    _statBinMap[f.properties.path] = idx != null ? bins[idx].color : _ND_COL;
  });

  if (!bins.length) return '<div class="mv-st-nodata">No data</div>';

  var activeBinCounts = new Array(bins.length).fill(0), nd = 0;
  active.forEach(function(f) {
    var v = getVal(f.properties);
    if (v == null) { nd++; return; }
    var idx = _binIdx(v, bins);
    if (idx != null) activeBinCounts[idx]++;
  });

  var r = '';
  for (var i = bins.length - 1; i >= 0; i--) {
    if (!activeBinCounts[i]) continue;
    var lbl = bins.length === 1
      ? bins[i].lo.toFixed(dec) + ' ' + unit
      : bins[i].lo.toFixed(dec) + '–' + bins[i].hi.toFixed(dec) + ' ' + unit;
    r += _binRow(bins[i].color, lbl, activeBinCounts[i]);
  }
  if (nd) r += _binRow(_ND_COL, 'No data', nd);

  var sorted = active.filter(function(f) { return getVal(f.properties) != null; })
    .sort(function(a, b) { return getVal(b.properties) - getVal(a.properties); });

  function fmt(v) { return unit === 'min' ? _fmtMin(v) : v.toFixed(dec) + ' ' + unit; }

  if (topLabel && sorted.length) {
    r += _divRow(topLabel);
    var topList = topAsc ? sorted.slice(-5).reverse() : sorted.slice(0, 5);
    topList.forEach(function(f) { r += _jobRow(f.properties, fmt(getVal(f.properties))); });
  }
  if (botLabel && sorted.length > 5) {
    r += _divRow(botLabel);
    sorted.slice(-5).reverse().forEach(function(f) { r += _jobRow(f.properties, fmt(getVal(f.properties))); });
  }
  return r || '<div class="mv-st-nodata">No data</div>';
}

function _stLost(all, active, isHa) {
  function lv(p) { return isHa ? _getLostHa(p) : p.area_lost_pct; }
  var unitStr = isHa ? ' ha' : '%', dec = isHa ? 2 : 1;

  var allNonzero = all.map(function(f) { return lv(f.properties); }).filter(function(v) { return v != null && v > 0; });
  var bins4 = _makeBins(allNonzero, _LOST_PAL.slice(1));

  all.forEach(function(f) {
    var v = lv(f.properties);
    if (v == null) { _statBinMap[f.properties.path] = _ND_COL; return; }
    if (v <= 0)    { _statBinMap[f.properties.path] = _LOST_PAL[0]; return; }
    if (!bins4.length) { _statBinMap[f.properties.path] = _LOST_PAL[1]; return; }
    var idx = _binIdx(v, bins4);
    _statBinMap[f.properties.path] = idx != null ? bins4[idx].color : _LOST_PAL[1];
  });

  var zero = 0, bc4 = new Array(bins4.length).fill(0), nd = 0;
  active.forEach(function(f) {
    var v = lv(f.properties);
    if (v == null) { nd++; return; }
    if (v <= 0) { zero++; return; }
    var idx = _binIdx(v, bins4);
    if (idx != null) bc4[idx]++;
  });

  var r = '';
  for (var i = bins4.length - 1; i >= 0; i--) {
    if (!bc4[i]) continue;
    var lbl = bins4[i].lo.toFixed(dec) + '–' + bins4[i].hi.toFixed(dec) + unitStr;
    r += _binRow(bins4[i].color, lbl, bc4[i]);
  }
  if (zero) r += _binRow(_LOST_PAL[0], '0' + unitStr, zero);
  if (nd)   r += _binRow(_ND_COL, 'No data', nd);

  var sorted = active.filter(function(f) { return lv(f.properties) != null && lv(f.properties) > 0; })
    .sort(function(a, b) { return lv(b.properties) - lv(a.properties); });

  if (sorted.length) {
    r += _divRow('Most lost');
    sorted.slice(0, 10).forEach(function(f) {
      r += _jobRow(f.properties, lv(f.properties).toFixed(dec) + unitStr);
    });
  }
  return r || '<div class="mv-st-nodata">No data</div>';
}
