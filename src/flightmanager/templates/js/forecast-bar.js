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
  }
  _fcContainer.style.display = 'block';
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

function _fcRender(data) {
  var days = (data && data.days) || [];
  if (!days.length) {
    var msg = (data && data.grid_msg) || 'No forecast available.';
    _fcContainer.innerHTML = '<div class="fc-loading">' + escHtml(msg) + '</div>';
    _fcContainer.dataset.loaded = '1';
    return;
  }

  var today = new Date().toISOString().slice(0, 10);
  var tiles = (data.tile_ids || []).join(', ');
  var out = [];
  out.push('<div class="fc-head">' + escHtml(tiles || 'No MGRS tile') + '</div>');
  out.push('<div class="fc-slots">');
  days.forEach(function (slot) { out.push(_fcSlot(slot, today)); });
  out.push('</div>');

  var attr = (data.attribution && data.attribution.weather) || '';
  if (attr) out.push('<div class="fc-attr" title="' + escHtml(
    (data.attribution.weather || '') + ' ' + (data.attribution.satellites || '')
  ) + '">' + escHtml(attr) + '</div>');

  _fcContainer.innerHTML = out.join('');
  _fcContainer.dataset.loaded = '1';
}

function _fcSlot(slot, today) {
  var dt = new Date(slot.date + 'T00:00:00Z');
  var wd = dt.toLocaleDateString(undefined, { weekday: 'short', timeZone: 'UTC' });
  var dom = dt.toLocaleDateString(undefined, { day: 'numeric', timeZone: 'UTC' });
  var isToday = slot.date === today;
  var w = slot.weather;

  var parts = ['<div class="fc-slot' + (isToday ? ' fc-today' : '') + '" title="'
    + escHtml(_fcTooltip(slot)) + '">'];
  parts.push('<div class="fc-date">' + escHtml(wd) + ' ' + escHtml(dom) + '</div>');

  if (w) {
    parts.push('<svg class="fc-wx" aria-hidden="true"><use href="#ic-wx-'
      + escHtml(w.icon || 'unknown') + '"/></svg>');
    var tmax = w.t_max_c == null ? '–' : Math.round(w.t_max_c) + '°';
    var tmin = w.t_min_c == null ? '' : '<span class="fc-tmin">' + Math.round(w.t_min_c) + '°</span>';
    parts.push('<div class="fc-temp">' + tmax + tmin + '</div>');
    var wind = w.wind_max_ms == null ? '' : Math.round(w.wind_max_ms) + '<span class="fc-u">m/s</span>';
    parts.push('<div class="fc-wind">' + wind + '</div>');
  } else {
    parts.push('<div class="fc-wx fc-wx-empty"></div>');
    parts.push('<div class="fc-temp">–</div>');
    parts.push('<div class="fc-wind"></div>');
  }

  parts.push('<div class="fc-sats">');
  (slot.satellites || []).forEach(function (s) {
    parts.push('<span class="fc-sat" style="background:' + _satColor(s.name) + '">'
      + escHtml(_satCode(s.name)) + '</span>');
  });
  parts.push('</div>');

  parts.push('</div>');
  return parts.join('');
}

function _fcTooltip(slot) {
  var lines = [slot.date];
  var w = slot.weather;
  if (w) {
    lines.push(w.label || '');
    if (w.t_max_c != null) lines.push('Temp ' + Math.round(w.t_min_c) + '…' + Math.round(w.t_max_c) + ' °C');
    if (w.wind_max_ms != null) lines.push('Wind max ' + w.wind_max_ms + ' m/s');
    if (w.precip_mm != null) lines.push('Precip ' + w.precip_mm + ' mm');
    if (w.cloud_pct != null) lines.push('Cloud ' + Math.round(w.cloud_pct) + ' %');
  }
  (slot.satellites || []).forEach(function (s) {
    var t = new Date(s.peak_utc).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    lines.push(s.name + '  ' + t + '  ' + Math.round(s.max_elev_deg) + '°  ' + s.tile_id);
  });
  return lines.filter(Boolean).join('\n');
}
