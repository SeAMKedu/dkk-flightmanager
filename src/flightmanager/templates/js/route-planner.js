// ── Route planner ─────────────────────────────────────────────────────────────

import { st } from './state.js';
import { map, lrs } from './map-init.js';
import { apiPost } from './api.js';
import { markDirty } from './dirty-tracking.js';
import { getTakeoffPt, getTakeoffAuto } from './takeoff.js';
import { notifyCesiumRouteReady } from './cesium-view.js';
import { getTplSettings } from './tpl-modal.js';

var _routeDebounceTimer = null;
var _routeLayer = null;
var _arrowLayer = null;
var _coverageLayer = null;
var _routeAngleAdjusting = false;
var _lastRouteStats = null;
var _lastFpAcross = null;
var _lastFpAlong = null;

var _ARROW_MIN_ZOOM = 17;
function _arrowsVisible() {
  var btn = document.getElementById('leg-route');
  var routeOn = !btn || !btn.classList.contains('off');
  return routeOn && map.getZoom() >= _ARROW_MIN_ZOOM;
}
map.on('zoomend', function() {
  if (!_arrowLayer) return;
  if (_arrowsVisible()) { if (!map.hasLayer(_arrowLayer)) _arrowLayer.addTo(map); }
  else                  { if (map.hasLayer(_arrowLayer))  map.removeLayer(_arrowLayer); }
});
// Keep arrows in sync when the route legend eye-toggle is clicked.
document.addEventListener('DOMContentLoaded', function() {
  var legBtn = document.getElementById('leg-route');
  if (legBtn) legBtn.addEventListener('click', function() {
    if (!_arrowLayer) return;
    // After the legend handler runs, classList already reflects new state.
    setTimeout(function() {
      if (_arrowsVisible()) { if (!map.hasLayer(_arrowLayer)) _arrowLayer.addTo(map); }
      else                  { if (map.hasLayer(_arrowLayer))  map.removeLayer(_arrowLayer); }
    }, 0);
  });
});

export function _getLastRouteStats() { return _lastRouteStats; }

function _effectiveRouteAngle() {
  return st._routeAngleDeg !== null ? st._routeAngleDeg
       : (st._routeAngleAuto !== null ? st._routeAngleAuto : 0);
}

function _getRouteParams() {
  var d = st.drones.find(function(x) { return x.name === document.getElementById('dsel').value; });
  var H = parseFloat(document.getElementById('hgt').value) || 60;
  if (!d) return null;
  var p_m = d.pixel_pitch_um * 1e-6, f_m = d.focal_length_mm * 1e-3;
  var fpAcross = H * d.image_width_px  * p_m / f_m;
  var fpAlong  = H * d.image_height_px * p_m / f_m;
  var tpl = getTplSettings();
  var ovf = tpl ? tpl.overlap_front_pct : st._cfgOverlapFront;
  var ovs = tpl ? tpl.overlap_side_pct  : st._cfgOverlapSide;
  return {
    angle:  _effectiveRouteAngle(),
    stripM: fpAcross * (1 - ovs / 100),
    photoM: fpAlong  * (1 - ovf / 100),
    fpAcross: fpAcross,
    fpAlong:  fpAlong,
  };
}

// Simple scanline clip — just parallel lines at the given angle, no ordering or transits.
// Used for the instant rough preview; Python supplies the accurate route after 500 ms.
function _computeRoughLines(polygon4326, angleDeg, stripM, fpAcross) {
  var ring = polygon4326.type === 'Polygon' ? polygon4326.coordinates[0]
           : (polygon4326.coordinates[0] ? polygon4326.coordinates[0][0] : null);
  if (!ring || ring.length < 3) return [];

  var lat0 = 0, lon0 = 0;
  ring.forEach(function(c) { lon0 += c[0]; lat0 += c[1]; });
  lon0 /= ring.length; lat0 /= ring.length;
  var mLat = 111132.0, mLon = mLat * Math.cos(lat0 * Math.PI / 180);

  var pts = ring.map(function(c) {
    return [(c[0] - lon0) * mLon, (c[1] - lat0) * mLat];
  });

  var rotRad = (angleDeg - 90) * Math.PI / 180;
  var cosR = Math.cos(rotRad), sinR = Math.sin(rotRad);
  function rotPt(p) { return [p[0]*cosR - p[1]*sinR, p[0]*sinR + p[1]*cosR]; }
  var backCos = Math.cos(-rotRad), backSin = Math.sin(-rotRad);
  function unrot(px, py) { return [px*backCos - py*backSin, px*backSin + py*backCos]; }

  var rp = pts.map(rotPt);
  var minY = Infinity, maxY = -Infinity;
  rp.forEach(function(p) { if (p[1] < minY) minY = p[1]; if (p[1] > maxY) maxY = p[1]; });

  var firstOffset = (fpAcross != null ? fpAcross : stripM) / 2;
  var n = rp.length, lines = [];
  for (var y = minY + firstOffset; y <= maxY + 1e-6; y += stripM) {
    var xs = [];
    for (var i = 0; i < n - 1; i++) {
      var ay = rp[i][1], by = rp[i+1][1];
      if ((ay <= y && by > y) || (by <= y && ay > y)) {
        var t = (y - ay) / (by - ay);
        xs.push(rp[i][0] + t * (rp[i+1][0] - rp[i][0]));
      }
    }
    xs.sort(function(a, b) { return a - b; });
    for (var j = 0; j + 1 < xs.length; j += 2) {
      var x0 = xs[j], x1 = xs[j+1];
      if (x1 <= x0 + 0.1) continue;
      var pa = unrot(x0, y), pb = unrot(x1, y);
      lines.push([
        [lat0 + pa[1]/mLat, lon0 + pa[0]/mLon],
        [lat0 + pb[1]/mLat, lon0 + pb[0]/mLon],
      ]);
    }
  }
  return lines;
}

function _drawRoughPreview(polygon4326, angleDeg, stripM, fpAcross) {
  if (_routeLayer)    { map.removeLayer(_routeLayer);    _routeLayer    = null; }
  if (_arrowLayer)    { map.removeLayer(_arrowLayer);    _arrowLayer    = null; }
  if (_coverageLayer) { map.removeLayer(_coverageLayer); _coverageLayer = null; }
  lrs.route = null; lrs.coverage = null;

  var lines = _computeRoughLines(polygon4326, angleDeg, stripM, fpAcross);
  if (!lines.length) return;

  var g = L.layerGroup();
  lines.forEach(function(line) {
    L.polyline(line, {color: '#f59e0b', weight: 1.5, opacity: 0.45,
                      dashArray: '5 5', interactive: false}).addTo(g);
  });
  _routeLayer = g; lrs.route = g;
  var btn = document.getElementById('leg-route');
  if (btn && !btn.classList.contains('off')) g.addTo(map);
  var row = document.getElementById('leg-route-row');
  if (row) row.style.display = '';
  notifyCesiumRouteReady(null, null);
}

export function _clearRouteLayer() {
  if (_routeLayer)    { map.removeLayer(_routeLayer);    _routeLayer    = null; }
  if (_arrowLayer)    { map.removeLayer(_arrowLayer);    _arrowLayer    = null; }
  if (_coverageLayer) { map.removeLayer(_coverageLayer); _coverageLayer = null; }
  lrs.route = null; lrs.coverage = null;
  var row = document.getElementById('leg-route-row');
  if (row) row.style.display = 'none';
  var crow = document.getElementById('leg-coverage-row');
  if (crow) crow.style.display = 'none';
  var altRow = document.getElementById('leg-alt-row');
  if (altRow) altRow.style.display = 'none';
  st._altProfileMin = null;
  st._altProfileMax = null;
  notifyCesiumRouteReady(null, null);
}

function _stripCoverageRect(ll1, ll2, fpAcross, fpAlong, lat0, lon0, mLat, mLon) {
  var x1 = (ll1[1] - lon0) * mLon, y1 = (ll1[0] - lat0) * mLat;
  var x2 = (ll2[1] - lon0) * mLon, y2 = (ll2[0] - lat0) * mLat;
  var dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy);
  if (len < 0.1) return null;
  var ux = dx / len, uy = dy / len;          // unit along-strip vector
  var px = -uy,      py = ux;                // unit cross-strip vector
  var hw = fpAcross / 2;
  var hf = (fpAlong  || 0) / 2;             // half along-track footprint (vertical FOV overhang)
  // Extend each endpoint outward by hf to cover the full camera footprint
  var ex1 = x1 - ux * hf, ey1 = y1 - uy * hf;
  var ex2 = x2 + ux * hf, ey2 = y2 + uy * hf;
  return [
    [lat0 + (ey1 + py*hw)/mLat, lon0 + (ex1 + px*hw)/mLon],
    [lat0 + (ey2 + py*hw)/mLat, lon0 + (ex2 + px*hw)/mLon],
    [lat0 + (ey2 - py*hw)/mLat, lon0 + (ex2 - px*hw)/mLon],
    [lat0 + (ey1 - py*hw)/mLat, lon0 + (ex1 - px*hw)/mLon],
  ];
}

// Viridis-inspired 5-stop palette for altitude coloring (low→high)
var _ALT_STOPS = [
  [68,  1,  84], // deep purple  (lowest)
  [59, 82, 139], // blue-purple
  [33, 145, 140], // teal
  [94, 201, 98],  // green
  [253, 231, 37], // yellow      (highest)
];
function _altColor(t) {
  // t in [0,1]; returns CSS hex
  t = Math.max(0, Math.min(1, t));
  var seg = t * (_ALT_STOPS.length - 1);
  var i = Math.min(Math.floor(seg), _ALT_STOPS.length - 2);
  var f = seg - i;
  var a = _ALT_STOPS[i], b = _ALT_STOPS[i + 1];
  var r = Math.round(a[0] + f * (b[0] - a[0]));
  var g = Math.round(a[1] + f * (b[1] - a[1]));
  var bv = Math.round(a[2] + f * (b[2] - a[2]));
  return '#' + [r,g,bv].map(function(x){return x.toString(16).padStart(2,'0');}).join('');
}

function _drawRouteLines(lines, transits, fpAcross, altitudes, wptAltsList) {
  if (_routeLayer)    { map.removeLayer(_routeLayer);    _routeLayer    = null; }
  if (_arrowLayer)    { map.removeLayer(_arrowLayer);    _arrowLayer    = null; }
  if (_coverageLayer) { map.removeLayer(_coverageLayer); _coverageLayer = null; }
  if (!lines || !lines.length) { lrs.route = null; lrs.coverage = null; return; }

  var lat0 = lines[0][0][0], lon0 = lines[0][0][1];
  var mLat = 111132.0, mLon = mLat * Math.cos(lat0 * Math.PI / 180);

  var hasAlt     = altitudes    && altitudes.length    === lines.length;
  var hasWptAlts = wptAltsList  && wptAltsList.length  === lines.length;

  // Global altitude range across all waypoints for consistent colour mapping
  var allAlts = [];
  if (hasWptAlts) {
    wptAltsList.forEach(function(wa) { if (wa) wa.forEach(function(a) { allAlts.push(a); }); });
  }
  if (!allAlts.length && hasAlt) allAlts = altitudes;
  var altMin   = allAlts.length ? Math.min.apply(null, allAlts) : 0;
  var altMax   = allAlts.length ? Math.max.apply(null, allAlts) : 1;
  var altRange = (altMax - altMin) || 1;

  var g = L.layerGroup();
  var ag = L.layerGroup();
  var defaultColor = '#f59e0b';
  lines.forEach(function(line, idx) {
    var wptAlts = hasWptAlts ? wptAltsList[idx] : null;
    if (wptAlts && wptAlts.length === line.length && wptAlts.length > 2) {
      // Gradient: one Leaflet polyline segment per waypoint pair, coloured by mid-altitude
      for (var k = 0; k < line.length - 1; k++) {
        var midAlt = (wptAlts[k] + wptAlts[k + 1]) / 2;
        var segColor = _altColor((midAlt - altMin) / altRange);
        L.polyline([line[k], line[k + 1]], {color: segColor, weight: 2, opacity: 0.9, interactive: false}).addTo(g);
      }
    } else {
      var color = hasAlt ? _altColor((altitudes[idx] - altMin) / altRange) : defaultColor;
      L.polyline(line, {color: color, weight: 2, opacity: 0.9, interactive: false}).addTo(g);
    }
    // Direction arrow at strip midpoint (use start→end for bearing regardless of inner coords)
    var ll1 = line[0], ll2 = line[line.length - 1];
    var mid = [(ll1[0]+ll2[0])/2, (ll1[1]+ll2[1])/2];
    var stripAlt = hasAlt ? altitudes[idx] : null;
    var arrowColor = stripAlt != null ? _altColor((stripAlt - altMin) / altRange) : '#b45309';
    var dx = (ll2[1]-ll1[1])*mLon, dy = (ll2[0]-ll1[0])*mLat;
    var deg = Math.atan2(-dy, dx) * 180 / Math.PI;
    var arrowHtml =
      '<svg width="14" height="14" viewBox="-7 -7 14 14" xmlns="http://www.w3.org/2000/svg" ' +
      'style="transform:rotate('+deg.toFixed(1)+'deg);display:block">' +
      '<polyline points="-3.5,-5 4,0 -3.5,5" fill="none" stroke="'+arrowColor+'" ' +
      'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    L.marker(mid, {
      icon: L.divIcon({html:arrowHtml, className:'', iconSize:[14,14], iconAnchor:[7,7]}),
      interactive:false, keyboard:false,
    }).addTo(ag);
  });
  (transits || []).forEach(function(seg) {
    L.polyline(seg, {color: defaultColor, weight: 2, opacity: 0.85, interactive: false}).addTo(g);
  });

  _routeLayer = g; lrs.route = g;
  _arrowLayer = ag;
  var btn = document.getElementById('leg-route');
  if (btn && !btn.classList.contains('off')) {
    g.addTo(map);
    if (_arrowsVisible()) ag.addTo(map);
  }
  var row = document.getElementById('leg-route-row');
  if (row) row.style.display = '';

  // Altitude legend — show gradient bar with min/max when altitudes vary
  var altRow = document.getElementById('leg-alt-row');
  if (altRow) {
    if (allAlts.length) {
      var altMin2 = Math.round(altMin);
      var altMax2 = Math.round(altMax);
      var minEl = document.getElementById('leg-alt-min');
      var maxEl = document.getElementById('leg-alt-max');
      if (minEl) minEl.textContent = altMin2 + ' m';
      if (maxEl) maxEl.textContent = altMax2 + ' m';
      altRow.style.display = '';
      st._altProfileMin = altMin2;
      st._altProfileMax = altMax2;
    } else {
      altRow.style.display = 'none';
      st._altProfileMin = null;
      st._altProfileMax = null;
    }
  }

  if (fpAcross && fpAcross > 0) {
    var cg = L.layerGroup();
    lines.forEach(function(line, idx) {
      var color = hasAlt ? _altColor((altitudes[idx] - altMin) / altRange) : '#f59e0b';
      var covStyle = {color: color, weight: 0.8, opacity: 0.5,
                      fillColor: color, fillOpacity: 0.15, interactive: false};
      // Coverage rect uses only the endpoints, not intermediate waypoints
      var corners = _stripCoverageRect(line[0], line[line.length - 1], fpAcross, _lastFpAlong, lat0, lon0, mLat, mLon);
      if (corners) L.polygon(corners, covStyle).addTo(cg);
    });
    _coverageLayer = cg; lrs.coverage = cg;
    var cbtn = document.getElementById('leg-coverage');
    if (cbtn && !cbtn.classList.contains('off')) cg.addTo(map);
    var crow = document.getElementById('leg-coverage-row');
    if (crow) crow.style.display = '';
  }
}

function _drawRouteGeoJSON(stripsGeojson, transitsGeojson) {
  if (!stripsGeojson || !stripsGeojson.features || !stripsGeojson.features.length) return;
  var features = stripsGeojson.features;
  var lines = features.map(function(f) {
    return f.geometry.coordinates.map(function(c) { return [c[1], c[0]]; });
  });
  var altitudes = features.map(function(f) {
    return f.properties && f.properties.altitude_m != null ? f.properties.altitude_m : null;
  });
  var wptAltsList = features.map(function(f) {
    return f.properties && f.properties.wpt_alts ? f.properties.wpt_alts : null;
  });
  var _validAlts = altitudes.filter(function(a) { return a !== null; });
  var _altVaries = _validAlts.length > 1 &&
    (Math.max.apply(null, _validAlts) - Math.min.apply(null, _validAlts)) > 1.0;
  if (!_altVaries) { altitudes = null; wptAltsList = null; }
  var transits = (transitsGeojson && transitsGeojson.features || []).map(function(f) {
    return f.geometry.coordinates.map(function(c) { return [c[1], c[0]]; });
  });
  _drawRouteLines(lines, transits, _lastFpAcross, altitudes, wptAltsList);
  notifyCesiumRouteReady(stripsGeojson, transitsGeojson);
}

export function updateRouteStats(data) {
  if (data) _lastRouteStats = data;
  else _lastRouteStats = null;
  var se = document.getElementById('rstat-strips');
  var pe = document.getElementById('rstat-photos');
  var te = document.getElementById('rstat-time');
  if (!se) return;
  se.textContent = (data && data.strip_count     != null) ? data.strip_count                              : '—';
  pe.textContent = (data && data.photo_count     != null) ? '~' + data.photo_count                        : '—';
  te.textContent = (data && data.flight_time_min != null) ? '~' + Math.round(data.flight_time_min) + ' min' : '—';

  var minEl = document.getElementById('rstat-spd-min');
  var avgEl = document.getElementById('rstat-spd-avg');
  var maxEl = document.getElementById('rstat-spd-max');
  if (!minEl) return;
  var speeds = data && data.strips_geojson && data.strips_geojson.features
    ? data.strips_geojson.features.map(function(f) { return f.properties && f.properties.speed_ms; }).filter(function(v) { return v != null; })
    : [];
  if (speeds.length) {
    var mn = speeds.reduce(function(a, b) { return Math.min(a, b); }, Infinity);
    var mx = speeds.reduce(function(a, b) { return Math.max(a, b); }, -Infinity);
    var av = speeds.reduce(function(a, b) { return a + b; }, 0) / speeds.length;
    minEl.textContent = mn.toFixed(1) + ' m/s';
    avgEl.textContent = av.toFixed(1) + ' m/s';
    maxEl.textContent = mx.toFixed(1) + ' m/s';
  } else {
    minEl.textContent = '—';
    avgEl.textContent = '—';
    maxEl.textContent = '—';
  }
}

export function updateRouteOverlay(cachedStrips, cachedTransits) {
  if (!st.previewData || !st.previewData.survey) { _clearRouteLayer(); updateRouteStats(null); return; }
  var p = _getRouteParams();
  if (!p) { _clearRouteLayer(); updateRouteStats(null); return; }
  _lastFpAcross = p.fpAcross;
  _lastFpAlong  = p.fpAlong;
  if (cachedStrips) {
    _drawRouteGeoJSON(cachedStrips, cachedTransits);
    return;
  }
  _drawRoughPreview(st.previewData.survey, p.angle, p.stripM, p.fpAcross);
  updateRouteStats(null);
  _scheduleAccurateEstimate();
}

function _scheduleAccurateEstimate() {
  if (_routeDebounceTimer) clearTimeout(_routeDebounceTimer);
  _routeDebounceTimer = setTimeout(_fetchAccurateEstimate, 500);
}

async function _fetchAccurateEstimate() {
  if (!st.previewData || !st.previewData.survey) return;
  var home = getTakeoffPt() || getTakeoffAuto();
  var tpl = getTplSettings();
  var body = {
    polygon_4326:              st.previewData.survey,
    angle_deg:                 st._routeAngleDeg,
    height_m:                  parseFloat(document.getElementById('hgt').value) || null,
    drone:                     document.getElementById('dsel').value || null,
    speed_ms:                  st._speedMsOverride,
    takeoff_point_4326:        home || null,
    overlap_front_pct:         tpl ? tpl.overlap_front_pct : null,
    overlap_side_pct:          tpl ? tpl.overlap_side_pct  : null,
    advanced_mode:             tpl ? !!tpl.advanced_mode : false,
    adv_min_height_m:          tpl ? tpl.adv_min_height_m          : null,
    adv_max_height_m:          tpl ? tpl.adv_max_height_m           : null,
    adv_powerline_clearance_m: tpl ? tpl.adv_powerline_clearance_m  : null,
    adv_slope_f:               tpl ? tpl.adv_slope_f                : null,
    adv_min_dip_m:             tpl ? tpl.adv_min_dip_m              : null,
    session_id:                st.sessionId,
  };
  try {
    var data = await apiPost('/api/route_estimate', body);
    updateRouteStats(data);
    if (data.strips_geojson) _drawRouteGeoJSON(data.strips_geojson, data.transits_geojson);
    if (st._routeAngleDeg === null && data.angle_deg_used != null) {
      st._routeAngleAuto = data.angle_deg_used;
      _renderAngleControl();
    }
  } catch(e) { console.warn('[route] estimate failed', e); }
}

export function _renderAngleControl() {
  var isAuto = (st._routeAngleDeg === null);
  document.getElementById('route-auto-btn').classList.toggle('active', isAuto);
  var displayed = isAuto
    ? (st._routeAngleAuto != null ? Math.round(st._routeAngleAuto) : null)
    : Math.round(st._routeAngleDeg);
  document.getElementById('route-angle-val').textContent =
    displayed != null ? displayed + '°' : '—';
}

export function routeAngleAuto() {
  st._routeAngleDeg = null;
  _renderAngleControl();
  markDirty();
  updateRouteOverlay();
}

export function routeAngleStep(dir) {
  var cur = st._routeAngleDeg !== null ? st._routeAngleDeg : (st._routeAngleAuto || 0);
  st._routeAngleDeg = ((Math.round(cur) + dir) % 180 + 180) % 180;
  _renderAngleControl();
  markDirty();
  updateRouteOverlay();
}

export function setRouteAngleSilent(v) {
  st._routeAngleDeg = (v != null) ? v : null;
  _renderAngleControl();
}

function _autoSpeedMs() {
  var h = parseFloat(document.getElementById('hgt').value);
  var d = st.drones.find(function(x) { return x.name === document.getElementById('dsel').value; });
  if (!d || isNaN(h) || h <= 0) return null;
  var sensor_h_m   = d.image_height_px * d.pixel_pitch_um * 1e-6;
  var footprint_m  = h * sensor_h_m / (d.focal_length_mm * 1e-3);
  var trigger_m    = (1 - st._cfgOverlapFront / 100) * footprint_m;
  return trigger_m / d.min_capture_interval_s;
}

function _renderSpeedControl() {
  var isAuto = (st._speedMsOverride === null);
  document.getElementById('speed-auto-btn').classList.toggle('active', isAuto);
  var val = isAuto ? _autoSpeedMs() : st._speedMsOverride;
  document.getElementById('speed-val').textContent = val != null ? val.toFixed(1) : '—';
}

export function speedAuto() {
  st._speedMsOverride = null;
  _renderSpeedControl();
  markDirty();
  _scheduleAccurateEstimate();
}

export function speedStep(dir) {
  var cur = st._speedMsOverride !== null ? st._speedMsOverride : (_autoSpeedMs() || 4.0);
  st._speedMsOverride = Math.max(0.1, Math.round((cur + dir * 0.1) * 10) / 10);
  _renderSpeedControl();
  markDirty();
  _scheduleAccurateEstimate();
}

export function setSpeedSilent(v) {
  st._speedMsOverride = (v != null) ? v : null;
  _renderSpeedControl();
}

// ── Init ──────────────────────────────────────────────────────────────────────

(function _initAngleButtons() {
  var _angleHoldTimer = null, _angleRepeatInterval = null;

  function _stopRepeat() {
    clearTimeout(_angleHoldTimer);
    clearInterval(_angleRepeatInterval);
    _angleHoldTimer = null; _angleRepeatInterval = null;
    _routeAngleAdjusting = false;
    updateRouteOverlay();
  }

  function _startRepeat(dir) {
    _routeAngleAdjusting = true;
    routeAngleStep(dir);
    _angleHoldTimer = setTimeout(function() {
      _angleRepeatInterval = setInterval(function() { routeAngleStep(dir); }, 80);
    }, 350);
  }

  ['route-minus', 'route-plus'].forEach(function(id) {
    var btn = document.getElementById(id);
    var dir = id === 'route-plus' ? 1 : -1;
    btn.addEventListener('mousedown', function(e) { e.preventDefault(); _startRepeat(dir); });
    btn.addEventListener('touchstart', function(e) { e.preventDefault(); _startRepeat(dir); }, {passive: false});
    ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach(function(ev) {
      btn.addEventListener(ev, _stopRepeat);
    });
  });

  _renderAngleControl();
}());

(function _initSpeedButtons() {
  var _speedHoldTimer = null, _speedRepeatInterval = null;

  function _stopRepeat() {
    clearTimeout(_speedHoldTimer);
    clearInterval(_speedRepeatInterval);
    _speedHoldTimer = null; _speedRepeatInterval = null;
  }

  function _startRepeat(dir) {
    speedStep(dir);
    _speedHoldTimer = setTimeout(function() {
      _speedRepeatInterval = setInterval(function() { speedStep(dir); }, 80);
    }, 350);
  }

  ['speed-minus', 'speed-plus'].forEach(function(id) {
    var btn = document.getElementById(id);
    var dir = id === 'speed-plus' ? 1 : -1;
    btn.addEventListener('mousedown', function(e) { e.preventDefault(); _startRepeat(dir); });
    btn.addEventListener('touchstart', function(e) { e.preventDefault(); _startRepeat(dir); }, {passive: false});
    ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach(function(ev) {
      btn.addEventListener(ev, _stopRepeat);
    });
  });

  _renderSpeedControl();
}());
