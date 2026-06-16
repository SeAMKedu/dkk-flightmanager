// ── Satellite overpass + weather forecast bar (map-view, top-centre) ──────────
//
// Mirrors the battery/flight-time timeline pinned at the bottom of the map. Shows
// one slot per forecast day: date, weather icon + temp/wind, and badges for the
// Earth-observation satellites passing the job grid square that day.
//
// Data comes from GET /api/forecast (satellites + weather merged into day-slots).
// The bar is keyed on the folder — the MGRS tile is stable within a folder, so
// changing the job selection does not re-fetch. Render is skipped when the folder
// is unchanged and already loaded.

import { st } from './state.js';
import { escHtml } from './utils.js';

var _fcContainer = null;
var _fcLoadedKey = null;   // folder key currently rendered (undefined = not loaded)
var _fcReqSeq = 0;         // guards against out-of-order async responses
var _fcCollapsed = (function(){ try { return localStorage.getItem('fcCollapsed') === '1'; } catch(e){ return false; } })();

// Short codes + family colours for the satellite badges.
var _SAT_CODE = {
  'Sentinel-2A': 'S2A', 'Sentinel-2B': 'S2B', 'Sentinel-2C': 'S2C',
  'Landsat 8': 'L8', 'Landsat 9': 'L9',
};

function _satCode(name) {
  if (_SAT_CODE[name]) return _SAT_CODE[name];
  var letters = (name || '').replace(/[^A-Za-z0-9]/g, '');
  return letters.slice(0, 3).toUpperCase() || '?';
}

function _satColor(name) {
  if (/sentinel/i.test(name)) return '#16a34a';
  if (/landsat/i.test(name)) return '#ea580c';
  return '#0891b2';
}

export function showForecastBar(folder) {
  if (!st._mvMode) return;
  var key = folder || null;
  _fcEnsureContainer();
  // Already showing this folder — just make sure it's visible.
  if (_fcLoadedKey === key && _fcContainer.dataset.loaded === '1') {
    _fcContainer.style.display = 'block';
    return;
  }
  _fcLoadedKey = key;
  _fcFetchAndRender(key);
}

export function hideForecastBar() {
  if (_fcContainer) _fcContainer.style.display = 'none';
}

export function destroyForecastBar() {
  if (_fcContainer) { _fcContainer.remove(); _fcContainer = null; }
  _fcLoadedKey = null;
}

// Nudge the bar down when the selection-action pill occupies the top centre.
export function setForecastBarShifted(shifted) {
  if (_fcContainer) _fcContainer.classList.toggle('with-actions', !!shifted);
}

function _fcEnsureContainer() {
  if (!_fcContainer) {
    _fcContainer = document.createElement('div');
    _fcContainer.id = 'forecast-bar';
    document.getElementById('map').appendChild(_fcContainer);
    // Delegated: collapse toggle survives innerHTML rebuilds.
    _fcContainer.addEventListener('click', function(e) {
      if (e.target.closest('.fc-collapse')) _fcToggleCollapse();
    });
  }
  _fcContainer.classList.toggle('collapsed', _fcCollapsed);
  _fcContainer.style.display = 'block';
}

function _fcToggleCollapse() {
  _fcCollapsed = !_fcCollapsed;
  try { localStorage.setItem('fcCollapsed', _fcCollapsed ? '1' : '0'); } catch (e) {}
  _fcContainer.classList.toggle('collapsed', _fcCollapsed);
}

async function _fcFetchAndRender(folder) {
  var seq = ++_fcReqSeq;
  _fcContainer.dataset.loaded = '0';
  _fcContainer.innerHTML = '<div class="fc-loading">Loading overpass forecast…</div>';
  try {
    var url = '/api/forecast' + (folder ? '?folder=' + encodeURIComponent(folder) : '');
    var r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var data = await r.json();
    if (seq !== _fcReqSeq || !st._mvMode) return;  // superseded or left map view
    _fcRender(data);
  } catch (e) {
    if (seq !== _fcReqSeq) return;
    console.error('[forecast]', e);
    _fcContainer.innerHTML = '<div class="fc-loading">Forecast unavailable</div>';
  }
}

function _fcCollapseBtn() {
  return '<button class="fc-collapse" title="Collapse / expand forecast" aria-label="Collapse forecast">'
    + '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" '
    + 'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg></button>';
}

function _fcRender(data) {
  var days = (data && data.days) || [];
  var tiles = (data && (data.tile_ids || []).join(', ')) || '';
  var headLabel = tiles || (data && data.grid_msg) || 'Forecast';

  var out = ['<div class="fc-head">' + _fcCollapseBtn()
    + '<span class="fc-head-label">' + escHtml(headLabel) + '</span></div>'];

  if (!days.length) {
    out.push('<div class="fc-body"><div class="fc-loading">'
      + escHtml((data && data.grid_msg) || 'No forecast available.') + '</div></div>');
    _fcContainer.innerHTML = out.join('');
    _fcContainer.dataset.loaded = '1';
    return;
  }

  var today = new Date().toLocaleDateString('en-CA');  // local YYYY-MM-DD
  var n = days.length;

  out.push('<div class="fc-body">');
  if (data.weather_warning) {
    out.push('<div class="fc-warn">⚠ ' + escHtml(data.weather_warning) + '</div>');
  }
  // Row-labelled grid: a left header column names each row; units live in the
  // labels so cell values stay bare. Cells flow row-major into the column track.
  out.push('<div class="fc-grid" style="grid-template-columns:'
    + 'auto repeat(' + n + ',minmax(38px,1fr))">');
  out.push(_fcRow('Date', 'fc-r-date', days, today, _cellDate));
  out.push(_fcRow('Weather', 'fc-r-wx', days, today, _cellWx));
  out.push(_fcRow('Temp °C', 'fc-r-temp', days, today, _cellTemp));
  out.push(_fcRow('Wind m/s', 'fc-r-wind', days, today, _cellWind));
  out.push(_fcRow('Cloud %', 'fc-r-cloud', days, today, _cellCloud));
  out.push(_fcRow('Satellites', 'fc-r-sats', days, today, _cellSats));
  out.push('</div>');

  var attr = (data.attribution && data.attribution.weather) || '';
  var dw = data.daytime_window || [6, 18];
  var note = 'Daytime ' + dw[0] + '–' + dw[1] + ' only · ☀ clear-sky · ★ golden (flyable + clear)'
    + (attr ? ' · ' + attr : '');
  out.push('<div class="fc-attr" title="' + escHtml(
    note + ' ' + ((data.attribution && data.attribution.satellites) || '')
  ) + '">' + escHtml(note) + '</div>');
  out.push('</div>');

  _fcContainer.innerHTML = out.join('');
  _fcContainer.dataset.loaded = '1';
}

// Emit one grid row: a label cell followed by one cell per day.
function _fcRow(label, rowCls, days, today, cellFn) {
  var out = ['<div class="fc-rlabel ' + rowCls + '">' + escHtml(label) + '</div>'];
  days.forEach(function (slot) {
    var cls = rowCls;
    if (slot.date === today) cls += ' fc-col-today';
    if (slot.golden) cls += ' fc-col-golden';
    out.push('<div class="fc-cell ' + cls + '" title="'
      + escHtml(_fcTooltip(slot)) + '">' + cellFn(slot) + '</div>');
  });
  return out.join('');
}

function _cellDate(slot) {
  var dt = new Date(slot.date + 'T00:00:00Z');
  var wd = dt.toLocaleDateString(undefined, { weekday: 'short', timeZone: 'UTC' });
  var dom = dt.toLocaleDateString(undefined, { day: 'numeric', timeZone: 'UTC' });
  var star = slot.golden ? '<span class="fc-golden-star">★</span>' : '';
  return '<span class="fc-wd">' + escHtml(wd) + '</span> ' + escHtml(dom) + star;
}

function _cellWx(slot) {
  if (!slot.weather) return '<svg class="fc-wx fc-wx-empty"></svg>';
  return '<svg class="fc-wx" aria-hidden="true"><use href="#ic-wx-'
    + escHtml(slot.weather.icon || 'unknown') + '"/></svg>';
}

function _cellTemp(slot) {
  var w = slot.weather;
  return (w && w.t_avg_c != null) ? Math.round(w.t_avg_c) + '°' : '–';
}

function _cellWind(slot) {
  var w = slot.weather;
  return (w && w.wind_avg_ms != null) ? String(Math.round(w.wind_avg_ms)) : '–';
}

function _cellCloud(slot) {
  var w = slot.weather;
  if (!w || w.cloud_pct == null) return '–';
  var cl = Math.round(w.cloud_pct);
  var cls = cl <= 30 ? 'fc-cloudv-clear' : (cl > 70 ? 'fc-cloudv-over' : '');
  return '<span class="' + cls + '">' + cl + '</span>';
}

function _cellSats(slot) {
  var sats = slot.satellites || [];
  var dayPasses = sats.filter(function (s) { return s.daytime; });
  var nightCount = sats.length - dayPasses.length;
  var out = [];
  dayPasses.forEach(function (s) {
    var clear = s.clear_window ? ' fc-sat-clear' : '';
    out.push('<span class="fc-sat' + clear + '" style="background:' + _satColor(s.name)
      + '">' + escHtml(_satCode(s.name)) + '</span>');
  });
  if (nightCount > 0) {
    out.push('<span class="fc-sat fc-sat-night">+' + nightCount + '☾</span>');
  }
  return '<div class="fc-sats">' + out.join('') + '</div>';
}

function _satLine(s) {
  var t = new Date(s.peak_local || s.peak_utc).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  var cloud = s.cloud_at_pass != null ? '  ' + s.cloud_at_pass + '% cloud' : '';
  return s.name + '  ' + t + '  ' + Math.round(s.max_elev_deg) + '°' + cloud;
}

function _fcTooltip(slot) {
  var lines = [slot.date + '  (daytime avg)'];
  if (slot.golden) lines.push('★ Golden: flyable weather + clear-sky satellite pass');
  var w = slot.weather;
  if (w) {
    lines.push(w.label || '');
    if (w.t_avg_c != null) lines.push('Temp ' + Math.round(w.t_avg_c) + ' °C');
    if (w.wind_avg_ms != null) lines.push('Wind ' + w.wind_avg_ms + ' m/s  (flight)');
    if (w.cloud_pct != null) lines.push('Cloud ' + Math.round(w.cloud_pct) + ' %  (imaging)');
    if (w.precip_mm != null) lines.push('Precip ' + w.precip_mm + ' mm');
  }
  (slot.satellites || []).forEach(function (s) {
    var mark = s.daytime ? (s.clear_window ? ' ☀ clear window' : '') : ' (low light)';
    lines.push(_satLine(s) + '  ' + s.tile_id + mark);
  });
  return lines.filter(Boolean).join('\n');
}
