// ── Route planner ─────────────────────────────────────────────────────────────
// Two-tier estimation: JS computes strips immediately on angle/param change for
// live visualization; a debounced POST /api/route_estimate provides accurate
// numbers (EPSG:3067 intersections, proper home transit) after input settles.

var _routeDebounceTimer = null;
var _routeLayer = null;
var _routeAngleAdjusting = false; // true while +/- is held; suppresses home transit legs
var _lastRouteStats = null;       // last non-null stats; re-applied after renderStatus rebuilds DOM

// ── Parameter helpers ─────────────────────────────────────────────────────────

function _effectiveRouteAngle() {
  return _routeAngleDeg !== null ? _routeAngleDeg
       : (_routeAngleAuto !== null ? _routeAngleAuto : 0);
}

function _getRouteParams() {
  var d = drones.find(function(x) { return x.name === document.getElementById('dsel').value; });
  var H = parseFloat(document.getElementById('hgt').value) || 60;
  if (!d) return null;
  var p_m = d.pixel_pitch_um * 1e-6, f_m = d.focal_length_mm * 1e-3;
  var fpAcross = H * d.image_width_px  * p_m / f_m;
  var fpAlong  = H * d.image_height_px * p_m / f_m;
  return {
    angle:  _effectiveRouteAngle(),
    stripM: fpAcross * (1 - _cfgOverlapSide  / 100),
    photoM: fpAlong  * (1 - _cfgOverlapFront / 100),
  };
}

// ── JS strip computation (immediate, approximate) ─────────────────────────────
// Scanline intersection in a local metric frame rotated by (angle-90)°.
// Handles exterior ring only; holes from keep-out are excluded (DJI handles them).

function _computeStripsJS(polygon4326, angleDeg, stripM, photoM) {
  var ring = polygon4326.type === 'Polygon' ? polygon4326.coordinates[0]
           : (polygon4326.coordinates[0] ? polygon4326.coordinates[0][0] : null);
  if (!ring || ring.length < 3) return { lines: [], photoCount: 0 };

  var lat0 = 0, lon0 = 0;
  ring.forEach(function(c) { lon0 += c[0]; lat0 += c[1]; });
  lon0 /= ring.length; lat0 /= ring.length;
  var mLat = 111132.0, mLon = mLat * Math.cos(lat0 * Math.PI / 180);

  var pts = ring.map(function(c) {
    return [(c[0] - lon0) * mLon, (c[1] - lat0) * mLat];
  });

  var rotRad = (angleDeg - 90) * Math.PI / 180;
  var cosR = Math.cos(rotRad), sinR = Math.sin(rotRad);
  function rotPt(p)  { return [p[0]*cosR - p[1]*sinR, p[0]*sinR + p[1]*cosR]; }

  var rp = pts.map(rotPt);
  var minY = Infinity, maxY = -Infinity, sumX = 0;
  rp.forEach(function(p) {
    if (p[1] < minY) minY = p[1]; if (p[1] > maxY) maxY = p[1]; sumX += p[0];
  });
  var midX = sumX / rp.length;

  // Home in rotated frame
  var home = _takeoffPt || _takeoffAuto;
  var homeRotY = (minY + maxY) / 2, homeRotX = midX;
  if (home) {
    var hx = (home[0] - lon0) * mLon, hy = (home[1] - lat0) * mLat;
    homeRotX = hx * cosR - hy * sinR;
    homeRotY = hx * sinR + hy * cosR;
  }

  var stripsY = [];
  for (var y = minY + stripM / 2; y <= maxY + 1e-6; y += stripM) stripsY.push(y);
  if (Math.abs(homeRotY - maxY) < Math.abs(homeRotY - minY)) stripsY.reverse();

  var firstFromLeft = homeRotX <= midX;
  var backCos = Math.cos(-rotRad), backSin = Math.sin(-rotRad);
  function unrot(px, py) { return [px*backCos - py*backSin, px*backSin + py*backCos]; }

  var n = rp.length, lines = [], photoCount = 0;
  stripsY.forEach(function(yS, idx) {
    var xs = [];
    for (var i = 0; i < n - 1; i++) {
      var ay = rp[i][1], by = rp[i+1][1];
      if ((ay <= yS && by > yS) || (by <= yS && ay > yS)) {
        var t = (yS - ay) / (by - ay);
        xs.push(rp[i][0] + t * (rp[i+1][0] - rp[i][0]));
      }
    }
    xs.sort(function(a, b) { return a - b; });
    for (var j = 0; j + 1 < xs.length; j += 2) {
      var x0 = xs[j], x1 = xs[j+1];
      if (x1 <= x0 + 0.1) continue;
      var fml = (idx % 2 === 0) ? firstFromLeft : !firstFromLeft;
      var pa = unrot(fml ? x0 : x1, yS), pb = unrot(fml ? x1 : x0, yS);
      lines.push([
        [lat0 + pa[1]/mLat, lon0 + pa[0]/mLon],
        [lat0 + pb[1]/mLat, lon0 + pb[0]/mLon],
      ]);
      photoCount += Math.ceil((x1 - x0) / photoM) + 1;
    }
  });

  // Build transit legs: inter-strip hops always; home legs only when not adjusting angle
  var transits = [];
  var homePt = _routeAngleAdjusting ? null : (_takeoffPt || _takeoffAuto);
  if (lines.length) {
    if (homePt) transits.push([homePt, lines[0][0]]);
    for (var ti = 0; ti < lines.length - 1; ti++) {
      transits.push([lines[ti][1], lines[ti+1][0]]);
    }
    if (homePt) transits.push([lines[lines.length-1][1], homePt]);
  }

  return { lines: lines, transits: transits, photoCount: photoCount };
}

// ── Map layer ─────────────────────────────────────────────────────────────────

function _clearRouteLayer() {
  if (_routeLayer) { map.removeLayer(_routeLayer); _routeLayer = null; }
  lrs.route = null;
  var row = document.getElementById('leg-route-row');
  if (row) row.style.display = 'none';
}

function _drawRouteLines(lines, transits) {
  if (_routeLayer) { map.removeLayer(_routeLayer); _routeLayer = null; }
  if (!lines || !lines.length) { lrs.route = null; return; }
  var g = L.layerGroup();
  var style = {color:'#f59e0b', weight:2, opacity:0.85, interactive:false};
  lines.forEach(function(line) { L.polyline(line, style).addTo(g); });
  (transits || []).forEach(function(seg) { L.polyline(seg, style).addTo(g); });
  _routeLayer = g; lrs.route = g;
  var btn = document.getElementById('leg-route');
  if (btn && !btn.classList.contains('off')) g.addTo(map);
  var row = document.getElementById('leg-route-row');
  if (row) row.style.display = '';
}

function _drawRouteGeoJSON(stripsGeojson, transitsGeojson) {
  if (!stripsGeojson || !stripsGeojson.features || !stripsGeojson.features.length) return;
  var lines = stripsGeojson.features.map(function(f) {
    return f.geometry.coordinates.map(function(c) { return [c[1], c[0]]; });
  });
  var transits = (transitsGeojson && transitsGeojson.features || []).map(function(f) {
    return f.geometry.coordinates.map(function(c) { return [c[1], c[0]]; });
  });
  _drawRouteLines(lines, transits);
}

// ── Route stats display ───────────────────────────────────────────────────────

function updateRouteStats(data) {
  if (data) _lastRouteStats = data;
  else _lastRouteStats = null;
  var se = document.getElementById('rstat-strips');
  var pe = document.getElementById('rstat-photos');
  var te = document.getElementById('rstat-time');
  if (!se) return;
  se.textContent = (data && data.strip_count     != null) ? data.strip_count                              : '—';
  pe.textContent = (data && data.photo_count     != null) ? '~' + data.photo_count                        : '—';
  te.textContent = (data && data.flight_time_min != null) ? '~' + Math.round(data.flight_time_min) + ' min' : '—';
}

// ── Main update (called after every polygon/angle/param change) ───────────────

function updateRouteOverlay() {
  if (!previewData || !previewData.survey) { _clearRouteLayer(); updateRouteStats(null); return; }
  var p = _getRouteParams();
  if (!p) { _clearRouteLayer(); updateRouteStats(null); return; }
  var r = _computeStripsJS(previewData.survey, p.angle, p.stripM, p.photoM);
  _drawRouteLines(r.lines, r.transits);
  updateRouteStats({ strip_count: r.lines.length, photo_count: r.photoCount, flight_time_min: null });
  _scheduleAccurateEstimate();
}

function _scheduleAccurateEstimate() {
  if (_routeDebounceTimer) clearTimeout(_routeDebounceTimer);
  _routeDebounceTimer = setTimeout(_fetchAccurateEstimate, 800);
}

async function _fetchAccurateEstimate() {
  if (!previewData || !previewData.survey) return;
  var home = _takeoffPt || _takeoffAuto;
  var body = {
    polygon_4326:       previewData.survey,
    angle_deg:          _routeAngleDeg,
    height_m:           parseFloat(document.getElementById('hgt').value) || null,
    drone:              document.getElementById('dsel').value || null,
    speed_ms:           parseFloat(document.getElementById('speed-ms').value) || null,
    takeoff_point_4326: home || null,
  };
  try {
    var res = await fetch('/api/route_estimate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) return;
    var data = await res.json();
    updateRouteStats(data);
    if (data.strips_geojson) _drawRouteGeoJSON(data.strips_geojson, data.transits_geojson);
    // Show computed auto-angle in the control when user hasn't overridden
    if (_routeAngleDeg === null && data.angle_deg_used != null) {
      _routeAngleAuto = data.angle_deg_used;
      _renderAngleControl();
    }
  } catch(e) { console.warn('[route] estimate failed', e); }
}

// ── Angle control ─────────────────────────────────────────────────────────────

function _renderAngleControl() {
  var isAuto = (_routeAngleDeg === null);
  document.getElementById('route-auto-btn').classList.toggle('active', isAuto);
  var displayed = isAuto
    ? (_routeAngleAuto != null ? Math.round(_routeAngleAuto) : null)
    : Math.round(_routeAngleDeg);
  document.getElementById('route-angle-val').textContent =
    displayed != null ? displayed + '°' : '—';
}

function routeAngleAuto() {
  _routeAngleDeg = null;
  _renderAngleControl();
  markDirty();
  updateRouteOverlay();
}

function routeAngleStep(dir) {
  var cur = _routeAngleDeg !== null ? _routeAngleDeg : (_routeAngleAuto || 0);
  _routeAngleDeg = ((Math.round(cur) + dir) % 180 + 180) % 180;
  _renderAngleControl();
  markDirty();
  updateRouteOverlay();
}

// Called by job-ops.js when restoring a saved job
function setRouteAngleSilent(v) {
  _routeAngleDeg = (v != null) ? v : null;
  _renderAngleControl();
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
