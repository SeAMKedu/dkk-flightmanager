// ── Cesium 3D flight path view ─────────────────────────────────────────────
// Visualises the current job's flat-altitude lawnmower route in 3D.
// Activated via the 2D/3D toggle button (custom Leaflet control, topleft).
// Data source: last accurate route estimate (strips_geojson + transits_geojson)
// stored here via notifyCesiumRouteReady(); no separate API call on activation.

import { st } from './state.js';
import { map } from './map-init.js';

// ── Module state ──────────────────────────────────────────────────────────────
var _viewer = null;
var _cesiumLoaded = false;
var _cesiumActive = false;
var _toggle3dBtn = null;

var _lastStripsGj = null;
var _lastTransitsGj = null;

var _currentEntities = [];
var _dsmLayer = null;

// Per-layer entity groups for visibility toggling
var _entityGroups = {area: [], path: [], curtain: [], drone: []};

// Layer visibility state — persists across re-renders
var _layerVis = {dsm: true, area: true, path: true, curtain: true, drone: true};

// Playback
var _isPlaying = false;
var _playbackTime = 0;
var _totalDuration = 0;
var _lastTickTime = null;
var _dronePositionProperty = null;
var _waypoints = [];

// ── Public API ────────────────────────────────────────────────────────────────

export function isCesiumActive() { return _cesiumActive; }

/** Called from route-planner after accurate estimate arrives (or clears). */
export function notifyCesiumRouteReady(stripsGj, transitsGj) {
  _lastStripsGj = stripsGj;
  _lastTransitsGj = transitsGj;
  var hasData = !!(stripsGj && stripsGj.features && stripsGj.features.length);
  if (_toggle3dBtn) _toggle3dBtn.disabled = !hasData;
  if (!hasData && _cesiumActive) hideCesiumView();
}

/** Add the 2D/3D Leaflet control; call once from main.js init() after basemap controls. */
export function initCesiumView() {
  var Toggle3d = L.Control.extend({
    options: { position: 'topleft' },
    onAdd: function() {
      var btn = L.DomUtil.create('button', 'toggle-3d-ctrl');
      btn.textContent = '3D';
      btn.title = 'Switch to 3D view';
      btn.disabled = true;
      _toggle3dBtn = btn;
      L.DomEvent.on(btn, 'click', function(e) {
        L.DomEvent.stopPropagation(e);
        if (st._mvMode) return;
        toggle3dView();
      });
      return btn;
    }
  });
  map.addControl(new Toggle3d());
}

export function toggle3dView() {
  if (_cesiumActive) hideCesiumView();
  else showCesiumView();
}

export function showCesiumView() {
  if (!_lastStripsGj || st._mvMode) return;
  _cesiumActive = true;
  _setToggleActive(true);
  document.getElementById('cesium-container').classList.add('active');
  document.getElementById('map').style.visibility = 'hidden';
  document.getElementById('legend').style.visibility = 'hidden';
  var measSvg = document.getElementById('meas-svg');
  if (measSvg) measSvg.style.visibility = 'hidden';
  _positionOverlayBtn(true);

  if (!_cesiumLoaded) {
    _showLoadingMsg(true);
    _loadCesium().then(function() {
      _createViewer();
      _showLoadingMsg(false);
      _renderScene();
      _showPlayback(true);
    }).catch(function(err) {
      console.error('[cesium] load failed', err);
      hideCesiumView();
    });
  } else {
    if (!_viewer) _createViewer();
    _renderScene();
    _showPlayback(true);
  }
}

export function hideCesiumView() {
  if (!_cesiumActive) return;
  _cesiumActive = false;
  _stopPlayback();
  _setToggleActive(false);
  document.getElementById('cesium-container').classList.remove('active');
  document.getElementById('map').style.visibility = '';
  document.getElementById('legend').style.visibility = '';
  var measSvg = document.getElementById('meas-svg');
  if (measSvg) measSvg.style.visibility = '';
  _showPlayback(false);
  _positionOverlayBtn(false);
  var leg = document.getElementById('cesium-legend');
  if (leg) leg.style.display = 'none';
}

// ── Cesium bootstrap ──────────────────────────────────────────────────────────

function _loadCesium() {
  return new Promise(function(resolve, reject) {
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://cesium.com/downloads/cesiumjs/releases/1.116/Build/Cesium/Widgets/widgets.css';
    document.head.appendChild(link);
    var script = document.createElement('script');
    script.src = 'https://cesium.com/downloads/cesiumjs/releases/1.116/Build/Cesium/Cesium.js';
    script.onload = function() { _cesiumLoaded = true; resolve(); };
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

function _createViewer() {
  /* eslint-disable no-undef */
  Cesium.Ion.defaultAccessToken = '';
  _viewer = new Cesium.Viewer('cesium-container', {
    baseLayerPicker:       false,
    geocoder:              false,
    homeButton:            false,
    infoBox:               false,
    navigationHelpButton:  false,
    sceneModePicker:       false,
    animation:             false,
    timeline:              false,
    fullscreenButton:      false,
    terrainProvider:       new Cesium.EllipsoidTerrainProvider(),
    creditContainer:       document.createElement('div'),
  });
  _viewer.imageryLayers.removeAll();
  _viewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
    url:        'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    subdomains: ['a', 'b', 'c'],
  }));
  _viewer.scene.globe.depthTestAgainstTerrain = false;
  _viewer.scene.highDynamicRange = false;
  _viewer.clock.onTick.addEventListener(_onTick);
  /* eslint-enable no-undef */
}

// ── Scene rendering ───────────────────────────────────────────────────────────

function _clearScene() {
  _currentEntities.forEach(function(e) { _viewer.entities.remove(e); });
  _currentEntities = [];
  Object.keys(_entityGroups).forEach(function(k) { _entityGroups[k] = []; });
  if (_dsmLayer) { _viewer.imageryLayers.remove(_dsmLayer); _dsmLayer = null; }
  _dronePositionProperty = null;
  _waypoints = [];
  _playbackTime = 0;
  _totalDuration = 0;
  _isPlaying = false;
}

/** Add entity to a named group and to the flat list; applies current layer visibility. */
function _addEntity(group, entityDef) {
  /* eslint-disable no-undef */
  var e = _viewer.entities.add(entityDef);
  /* eslint-enable no-undef */
  e.show = _layerVis[group];
  _entityGroups[group].push(e);
  _currentEntities.push(e);
  return e;
}

function _haversineM(lat1, lon1, lat2, lon2) {
  var R = 6371e3;
  var φ1 = lat1 * Math.PI / 180, φ2 = lat2 * Math.PI / 180;
  var dφ = (lat2 - lat1) * Math.PI / 180;
  var dλ = (lon2 - lon1) * Math.PI / 180;
  var a = Math.sin(dφ/2) * Math.sin(dφ/2) +
          Math.cos(φ1) * Math.cos(φ2) * Math.sin(dλ/2) * Math.sin(dλ/2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function _effectiveSpeedMs() {
  if (st._speedMsOverride !== null && st._speedMsOverride !== undefined) return st._speedMsOverride;
  var h = parseFloat(document.getElementById('hgt').value);
  var d = st.drones.find(function(x) { return x.name === document.getElementById('dsel').value; });
  if (!d || isNaN(h) || h <= 0) return 5.0;
  var sensorH = d.image_height_px * d.pixel_pitch_um * 1e-6;
  var fp = h * sensorH / (d.focal_length_mm * 1e-3);
  return ((1 - st._cfgOverlapFront / 100) * fp) / d.min_capture_interval_s;
}

/**
 * Reconstruct an ordered flat array of {lon, lat, height, time} waypoints
 * from the strips + transits GeoJSON returned by route_estimate.
 *
 * Ordering (from route.py):
 *   With home (transits.len === strips.len + 1):
 *     transit[0], strip[0], transit[1], strip[1], ..., strip[N-1], transit[N]
 *   Without home (transits.len === strips.len - 1):
 *     strip[0], transit[0], strip[1], ..., strip[N-1]
 */
// Viridis-inspired palette for altitude coloring in Cesium
var _C3D_STOPS = [
  [68,  1,  84],
  [59, 82, 139],
  [33, 145, 140],
  [94, 201, 98],
  [253, 231, 37],
];
function _viridisCesiumColor(t) {
  t = Math.max(0, Math.min(1, t));
  var seg = t * (_C3D_STOPS.length - 1);
  var i = Math.min(Math.floor(seg), _C3D_STOPS.length - 2);
  var f = seg - i;
  var a = _C3D_STOPS[i], b = _C3D_STOPS[i + 1];
  return new Cesium.Color( // eslint-disable-line no-undef
    (a[0] + f * (b[0] - a[0])) / 255,
    (a[1] + f * (b[1] - a[1])) / 255,
    (a[2] + f * (b[2] - a[2])) / 255,
    1.0
  );
}

function _buildWaypoints(altM) {
  var strips   = _lastStripsGj.features;
  var transits = _lastTransitsGj ? _lastTransitsGj.features : [];
  var N        = strips.length;
  var hasHome  = transits.length === N + 1;
  var speed    = Math.max(0.5, _effectiveSpeedMs());

  // Per-strip altitudes and speeds from GeoJSON properties (fallback to global)
  var stripAlts = strips.map(function(f) {
    return (f.properties && f.properties.altitude_m != null) ? f.properties.altitude_m : altM;
  });
  var stripSpeeds = strips.map(function(f) {
    return (f.properties && f.properties.speed_ms != null)
      ? Math.max(0.5, f.properties.speed_ms) : speed;
  });

  var pts = [];

  function addCoords(coords, h, spd) {
    coords.forEach(function(c) {
      var last = pts.length > 0 ? pts[pts.length - 1] : null;
      if (last && Math.abs(last.lon - c[0]) < 1e-9 && Math.abs(last.lat - c[1]) < 1e-9) {
        // Duplicate lat/lon at a boundary: update height/speed instead of creating a
        // zero-length horizontal segment (which causes NaN in Cesium polylineVolume).
        last.height = h;
        last.speed = spd;
      } else {
        pts.push({lon: c[0], lat: c[1], height: h, speed: spd});
      }
    });
  }

  if (hasHome) {
    addCoords(transits[0].geometry.coordinates, stripAlts[0], stripSpeeds[0]);
    strips.forEach(function(strip, i) {
      addCoords(strip.geometry.coordinates, stripAlts[i], stripSpeeds[i]);
      if (i < N - 1) {
        var tAlt = (stripAlts[i] + stripAlts[i + 1]) / 2;
        var tSpd = (stripSpeeds[i] + stripSpeeds[i + 1]) / 2;
        addCoords(transits[i + 1].geometry.coordinates, tAlt, tSpd);
      }
    });
    addCoords(transits[N].geometry.coordinates, stripAlts[N - 1], stripSpeeds[N - 1]);
  } else {
    strips.forEach(function(strip, i) {
      addCoords(strip.geometry.coordinates, stripAlts[i], stripSpeeds[i]);
      if (i < N - 1) {
        var tAlt = (stripAlts[i] + stripAlts[i + 1]) / 2;
        var tSpd = (stripSpeeds[i] + stripSpeeds[i + 1]) / 2;
        addCoords(transits[i].geometry.coordinates, tAlt, tSpd);
      }
    });
  }

  // Assign cumulative times using per-waypoint speed
  var t = 0;
  return pts.map(function(p, i) {
    if (i > 0) t += _haversineM(pts[i - 1].lat, pts[i - 1].lon, p.lat, p.lon) / pts[i - 1].speed;
    return {lon: p.lon, lat: p.lat, height: p.height, speed: p.speed, time: t};
  });
}

function _renderScene() {
  if (!_viewer || !_lastStripsGj) return;
  _clearScene();

  var altM      = parseFloat(document.getElementById('hgt').value) || 60;
  _waypoints = _buildWaypoints(altM);
  var waypoints = _waypoints;
  if (!waypoints.length) return;

  _totalDuration = waypoints[waypoints.length - 1].time;

  /* eslint-disable no-undef */
  var positions = waypoints.map(function(wp) {
    return Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, wp.height);
  });

  var jobColorHex = (document.getElementById('job-color').value || '#3b82f6');
  var pathColor   = Cesium.Color.fromCssColorString(jobColorHex);

  // Detect variable-altitude (advanced) mode: altitudes differ by > 1 m
  var _altMin = waypoints[0].height, _altMax = waypoints[0].height;
  waypoints.forEach(function(wp) {
    if (wp.height < _altMin) _altMin = wp.height;
    if (wp.height > _altMax) _altMax = wp.height;
  });
  var _altRange = _altMax - _altMin;
  var _useAltColor = _altRange > 1.0;

  function _segColor(h) {
    if (!_useAltColor) return pathColor;
    return _viridisCesiumColor((_altRange > 0) ? (h - _altMin) / _altRange : 0.5);
  }

  // ── 0. Survey boundary polygon ──────────────────────────────────────────────
  var hasArea = !!(st.previewData && st.previewData.survey);
  if (hasArea) {
    var geom  = st.previewData.survey;
    var rings = geom.type === 'Polygon'       ? [geom.coordinates[0]]
              : geom.type === 'MultiPolygon'  ? geom.coordinates.map(function(p){ return p[0]; })
              : [];
    rings.forEach(function(ring) {
      var rPos = ring.map(function(c) { return Cesium.Cartesian3.fromDegrees(c[0], c[1], 0.5); });
      _addEntity('area', {
        polygon: {
          hierarchy:    new Cesium.PolygonHierarchy(rPos),
          material:     pathColor.withAlpha(0.12),
          outline:      true,
          outlineColor: pathColor.withAlpha(0.85),
          outlineWidth: 3,
          height:       0.5,
        },
      });
      // Clamped polyline for a crisp ground outline
      _addEntity('area', {
        polyline: {
          positions:     [...rPos, rPos[0]],
          width:         3,
          material:      pathColor.withAlpha(0.85),
          clampToGround: true,
        },
      });
    });
  }

  // ── 1. Flight path tubes ────────────────────────────────────────────────────
  var tubeShape = [];
  for (var ti = 0; ti < 360; ti += 45) {
    var rad = Cesium.Math.toRadians(ti);
    tubeShape.push(new Cesium.Cartesian2(0.8 * Math.cos(rad), 0.8 * Math.sin(rad)));
  }

  for (var si = 0; si < positions.length - 1; si++) {
    var midH = (waypoints[si].height + waypoints[si + 1].height) / 2;
    var segColor = _segColor(midH);
    _addEntity('path', {
      polylineVolume: {
        positions: [positions[si], positions[si + 1]],
        shape:     tubeShape,
        material:  segColor,
      },
    });
    _addEntity('path', {
      position: positions[si],
      ellipsoid: {
        radii:    new Cesium.Cartesian3(0.8, 0.8, 0.8),
        material: _segColor(waypoints[si].height),
      },
    });
  }
  if (positions.length > 0) {
    var lastWp = waypoints[waypoints.length - 1];
    _addEntity('path', {
      position: positions[positions.length - 1],
      ellipsoid: {
        radii:    new Cesium.Cartesian3(0.8, 0.8, 0.8),
        material: _segColor(lastWp.height),
      },
    });
  }

  // ── 2. Curtain (translucent wall below path) ────────────────────────────────
  var curtainRef = _addEntity('curtain', {
    wall: {
      positions:    positions,
      material:     pathColor.withAlpha(0.08),
      outline:      true,
      outlineColor: pathColor.withAlpha(0.22),
    },
  });

  // ── 3. DSM imagery overlay ──────────────────────────────────────────────────
  var hasDsm = !!(st.previewData && st.previewData.dsm_b64 && st.previewData.dsm_bounds);
  if (hasDsm) {
    var b = st.previewData.dsm_bounds; // [west, south, east, north]
    _dsmLayer = _viewer.imageryLayers.addImageryProvider(
      new Cesium.SingleTileImageryProvider({
        url:       'data:image/png;base64,' + st.previewData.dsm_b64,
        rectangle: Cesium.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]),
      })
    );
    _dsmLayer.alpha = 0.65;
    _dsmLayer.show  = _layerVis.dsm;
  }

  // ── 4. Drone marker for playback ────────────────────────────────────────────
  var epoch = Cesium.JulianDate.fromIso8601('2020-01-01T00:00:00Z');
  _dronePositionProperty = new Cesium.SampledPositionProperty();
  waypoints.forEach(function(wp) {
    var jd = Cesium.JulianDate.addSeconds(epoch, wp.time, new Cesium.JulianDate());
    _dronePositionProperty.addSample(jd, Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, wp.height));
  });

  _addEntity('drone', {
    position: new Cesium.CallbackProperty(function() {
      var jd = Cesium.JulianDate.addSeconds(epoch, _playbackTime, new Cesium.JulianDate());
      return _dronePositionProperty.getValue(jd);
    }, false),
    point: {
      pixelSize:    15,
      color:        Cesium.Color.WHITE,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
    },
  });

  /* eslint-enable no-undef */

  // Fly to scene
  _viewer.zoomTo(curtainRef);

  // Reset playback UI
  _playbackTime = 0;
  var slider = document.getElementById('cesium-slider');
  if (slider) { slider.value = 0; slider.max = _totalDuration; }
  _updateTimeDisplay();
  document.getElementById('cesium-play-btn').textContent = '▶';

  // Build layer legend (after entities are placed so rows match reality)
  _buildLegend(hasArea, hasDsm, jobColorHex, _useAltColor ? {min: _altMin, max: _altMax} : null);
}

// ── Layer legend ──────────────────────────────────────────────────────────────

var _EYE_OPEN = '<svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
var _EYE_SLASH = '<svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

function _legRow(layer, iconHTML, label) {
  var row = document.createElement('div');
  row.className = 'leg-row';
  var btn = document.createElement('button');
  btn.className = 'leg-eye' + (_layerVis[layer] ? '' : ' off');
  btn.id = 'c3d-eye-' + layer;
  btn.title = label;
  btn.innerHTML = _EYE_OPEN + _EYE_SLASH;
  btn.addEventListener('click', function() { _toggle3dLayer(layer); });
  var iconDiv = document.createElement('div');
  iconDiv.className = 'leg-icon';
  iconDiv.innerHTML = iconHTML;
  var span = document.createElement('span');
  span.textContent = label;
  row.appendChild(btn);
  row.appendChild(iconDiv);
  row.appendChild(span);
  return row;
}

function _buildLegend(hasArea, hasDsm, colorHex, altRange) {
  var leg = document.getElementById('cesium-legend');
  if (!leg) return;
  leg.innerHTML = '<h4>Layers</h4>';

  if (hasDsm) {
    leg.appendChild(_legRow('dsm',
      '<div class="l-swatch" style="background:linear-gradient(to right,#440154,#31688e,#35b779,#fde725);border:1px solid #9ca3af;"></div>',
      'DSM elevation'));
  }

  if (hasArea) {
    leg.appendChild(_legRow('area',
      '<div class="l-swatch" style="background:' + colorHex + '20;border:1.5px solid ' + colorHex + ';"></div>',
      'Area'));
  }

  if (altRange) {
    // Variable-altitude mode: show viridis gradient with min/max labels
    var pathRow = _legRow('path',
      '<div class="l-swatch" style="background:linear-gradient(to right,#440154,#3b528b,#21918c,#5ec962,#fde725);border:1px solid #9ca3af;"></div>',
      'Flight path');
    var altLabel = document.createElement('span');
    altLabel.className = 'leg-alt-range';
    altLabel.textContent = Math.round(altRange.min) + '–' + Math.round(altRange.max) + ' m';
    altLabel.style.cssText = 'font-size:10px;color:#9ca3af;margin-left:4px;';
    pathRow.appendChild(altLabel);
    leg.appendChild(pathRow);
  } else {
    leg.appendChild(_legRow('path',
      '<svg width="22" height="10"><line x1="0" y1="5" x2="22" y2="5" stroke="' + colorHex + '" stroke-width="3" stroke-linecap="round"/></svg>',
      'Flight path'));
  }

  leg.appendChild(_legRow('curtain',
    '<div class="l-swatch" style="background:' + colorHex + '14;border:1px solid ' + colorHex + '44;"></div>',
    'Curtain'));

  leg.appendChild(_legRow('drone',
    '<div class="l-dot" style="background:#fff;border:1.5px solid #374151;"></div>',
    'Drone'));

  leg.style.display = 'block';
}

function _toggle3dLayer(layer) {
  _layerVis[layer] = !_layerVis[layer];
  var visible = _layerVis[layer];
  var btn = document.getElementById('c3d-eye-' + layer);
  if (btn) btn.classList.toggle('off', !visible);
  if (layer === 'dsm') {
    if (_dsmLayer) _dsmLayer.show = visible;
  } else {
    _entityGroups[layer].forEach(function(e) { e.show = visible; });
  }
}

// ── Playback ──────────────────────────────────────────────────────────────────

function _onTick() {
  if (!_isPlaying) return;
  var now = performance.now();
  if (_lastTickTime !== null) {
    var dt    = (now - _lastTickTime) / 1000;
    var speed = parseFloat((document.getElementById('cesium-speed') || {}).value) || 1;
    _playbackTime += dt * speed;
    if (_playbackTime >= _totalDuration) {
      _playbackTime = _totalDuration;
      _isPlaying    = false;
      _lastTickTime = null;
      document.getElementById('cesium-play-btn').textContent = '▶';
    }
    var slider = document.getElementById('cesium-slider');
    if (slider) slider.value = _playbackTime;
    _updateTimeDisplay();
  }
  _lastTickTime = now;
}

function _stopPlayback() {
  _isPlaying    = false;
  _lastTickTime = null;
  var btn = document.getElementById('cesium-play-btn');
  if (btn) btn.textContent = '▶';
}

function _updateTimeDisplay() {
  var m  = Math.floor(_playbackTime / 60);
  var s  = Math.floor(_playbackTime % 60);
  var el = document.getElementById('cesium-time-display');
  if (el) el.textContent = m + ':' + String(s).padStart(2, '0');
  _updateTelemetryDisplay();
}

function _updateTelemetryDisplay() {
  var altEl = document.getElementById('cesium-tel-alt');
  var spdEl = document.getElementById('cesium-tel-spd');
  if (!altEl || !spdEl || !_waypoints.length) return;
  var wps = _waypoints;
  var t   = _playbackTime;

  // Find surrounding waypoints
  var i = 0;
  while (i < wps.length - 1 && wps[i + 1].time <= t) i++;

  var wp0 = wps[i];
  var wp1 = wps[Math.min(i + 1, wps.length - 1)];
  var dt  = wp1.time - wp0.time;
  var f   = dt > 0 ? Math.min(1, (t - wp0.time) / dt) : 0;

  var alt = wp0.height + f * (wp1.height - wp0.height);
  var spd = wp0.speed  + f * (wp1.speed  - wp0.speed);

  altEl.textContent = alt.toFixed(1);
  spdEl.textContent = spd.toFixed(1);
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function _setToggleActive(active) {
  if (!_toggle3dBtn) return;
  _toggle3dBtn.textContent = active ? '2D' : '3D';
  _toggle3dBtn.title       = active ? 'Switch to 2D view' : 'Switch to 3D view';
  _toggle3dBtn.classList.toggle('active', active);
}

function _showLoadingMsg(show) {
  var container = document.getElementById('cesium-container');
  var existing  = document.getElementById('cesium-loading');
  if (show && !existing) {
    var div = document.createElement('div');
    div.id = 'cesium-loading';
    div.textContent = 'Loading 3D engine…';
    container.appendChild(div);
  } else if (!show && existing) {
    existing.remove();
  }
}

function _showPlayback(show) {
  var panel = document.getElementById('cesium-playback');
  if (panel) panel.classList.toggle('active', show);
  var tel = document.getElementById('cesium-telemetry');
  if (tel) tel.classList.toggle('active', show);
  if (!show) {
    var altEl = document.getElementById('cesium-tel-alt');
    var spdEl = document.getElementById('cesium-tel-spd');
    if (altEl) altEl.textContent = '—';
    if (spdEl) spdEl.textContent = '—';
  }
}

function _positionOverlayBtn(show) {
  var btn = document.getElementById('cesium-2d-btn');
  if (!btn) return;
  if (!show) { btn.style.display = 'none'; return; }
  if (_toggle3dBtn) {
    var tRect  = _toggle3dBtn.getBoundingClientRect();
    var mcRect = document.getElementById('mc').getBoundingClientRect();
    btn.style.top  = (tRect.top  - mcRect.top)  + 'px';
    btn.style.left = (tRect.left - mcRect.left) + 'px';
  }
  btn.style.display = 'block';
}

// ── Playback button wiring (done once on DOMContentLoaded) ────────────────────
document.addEventListener('DOMContentLoaded', function() {
  var playBtn  = document.getElementById('cesium-play-btn');
  var resetBtn = document.getElementById('cesium-reset-btn');
  var slider   = document.getElementById('cesium-slider');

  if (playBtn) {
    playBtn.addEventListener('click', function() {
      if (_isPlaying) {
        _isPlaying    = false;
        _lastTickTime = null;
        playBtn.textContent = '▶';
      } else {
        if (_playbackTime >= _totalDuration) _playbackTime = 0;
        _isPlaying    = true;
        _lastTickTime = performance.now();
        playBtn.textContent = '⏸';
      }
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', function() {
      _isPlaying    = false;
      _lastTickTime = null;
      _playbackTime = 0;
      if (slider) slider.value = 0;
      _updateTimeDisplay();
      if (playBtn) playBtn.textContent = '▶';
    });
  }

  if (slider) {
    slider.addEventListener('input', function(e) {
      _playbackTime = parseFloat(e.target.value);
      _updateTimeDisplay();
    });
  }
});
