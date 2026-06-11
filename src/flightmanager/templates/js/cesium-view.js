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

// Playback
var _isPlaying = false;
var _playbackTime = 0;
var _totalDuration = 0;
var _lastTickTime = null;
var _dronePositionProperty = null;

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
  if (_dsmLayer) { _viewer.imageryLayers.remove(_dsmLayer); _dsmLayer = null; }
  _dronePositionProperty = null;
  _playbackTime = 0;
  _totalDuration = 0;
  _isPlaying = false;
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
function _buildWaypoints(altM) {
  var strips   = _lastStripsGj.features;
  var transits = _lastTransitsGj ? _lastTransitsGj.features : [];
  var N        = strips.length;
  var hasHome  = transits.length === N + 1;
  var speed    = Math.max(0.5, _effectiveSpeedMs());

  var pts = [];

  function addCoords(coords) {
    coords.forEach(function(c) { pts.push({lon: c[0], lat: c[1]}); });
  }

  if (hasHome) {
    addCoords(transits[0].geometry.coordinates);
    strips.forEach(function(strip, i) {
      addCoords(strip.geometry.coordinates);
      if (i < N - 1) addCoords(transits[i + 1].geometry.coordinates);
    });
    addCoords(transits[N].geometry.coordinates);
  } else {
    strips.forEach(function(strip, i) {
      addCoords(strip.geometry.coordinates);
      if (i < N - 1) addCoords(transits[i].geometry.coordinates);
    });
  }

  // Assign cumulative times based on distance / speed
  var t = 0;
  return pts.map(function(p, i) {
    if (i > 0) t += _haversineM(pts[i - 1].lat, pts[i - 1].lon, p.lat, p.lon) / speed;
    return {lon: p.lon, lat: p.lat, height: altM, time: t};
  });
}

function _renderScene() {
  if (!_viewer || !_lastStripsGj) return;
  _clearScene();

  var altM      = parseFloat(document.getElementById('hgt').value) || 60;
  var waypoints = _buildWaypoints(altM);
  if (!waypoints.length) return;

  _totalDuration = waypoints[waypoints.length - 1].time;

  /* eslint-disable no-undef */
  var positions = waypoints.map(function(wp) {
    return Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, wp.height);
  });

  var jobColorHex = (document.getElementById('job-color').value || '#3b82f6');
  var pathColor   = Cesium.Color.fromCssColorString(jobColorHex);

  // ── 0. Survey boundary polygon ──────────────────────────────────────────────
  if (st.previewData && st.previewData.survey) {
    var geom  = st.previewData.survey;
    var rings = geom.type === 'Polygon'       ? [geom.coordinates[0]]
              : geom.type === 'MultiPolygon'  ? geom.coordinates.map(function(p){ return p[0]; })
              : [];
    rings.forEach(function(ring) {
      var rPos = ring.map(function(c) { return Cesium.Cartesian3.fromDegrees(c[0], c[1], 0.5); });
      var poly = _viewer.entities.add({
        polygon: {
          hierarchy:    new Cesium.PolygonHierarchy(rPos),
          material:     pathColor.withAlpha(0.12),
          outline:      true,
          outlineColor: pathColor.withAlpha(0.85),
          outlineWidth: 3,
          height:       0.5,
        },
      });
      _currentEntities.push(poly);
      // Clamped polyline for a crisp ground outline (Cesium polygon outlines can be thin on WebGL)
      var line = _viewer.entities.add({
        polyline: {
          positions:     [...rPos, rPos[0]],
          width:         3,
          material:      pathColor.withAlpha(0.85),
          clampToGround: true,
        },
      });
      _currentEntities.push(line);
    });
  }

  // ── 1. Flight path tubes ────────────────────────────────────────────────────
  // Octagonal cross-section; radius 0.8 m gives clear visibility at 60 m altitude.
  var tubeShape = [];
  for (var ti = 0; ti < 360; ti += 45) {
    var rad = Cesium.Math.toRadians(ti);
    tubeShape.push(new Cesium.Cartesian2(0.8 * Math.cos(rad), 0.8 * Math.sin(rad)));
  }

  for (var si = 0; si < positions.length - 1; si++) {
    _currentEntities.push(_viewer.entities.add({
      polylineVolume: {
        positions: [positions[si], positions[si + 1]],
        shape:     tubeShape,
        material:  pathColor,
      },
    }));
    // Joint sphere to smooth corners
    _currentEntities.push(_viewer.entities.add({
      position: positions[si],
      ellipsoid: {
        radii:    new Cesium.Cartesian3(0.8, 0.8, 0.8),
        material: pathColor,
      },
    }));
  }
  // End cap
  if (positions.length > 0) {
    _currentEntities.push(_viewer.entities.add({
      position: positions[positions.length - 1],
      ellipsoid: {
        radii:    new Cesium.Cartesian3(0.8, 0.8, 0.8),
        material: pathColor,
      },
    }));
  }

  // ── 2. Curtain (translucent wall below path) ────────────────────────────────
  var curtain = _viewer.entities.add({
    wall: {
      positions:    positions,
      material:     pathColor.withAlpha(0.08),
      outline:      true,
      outlineColor: pathColor.withAlpha(0.22),
    },
  });
  _currentEntities.push(curtain);

  // ── 3. DSM imagery overlay ──────────────────────────────────────────────────
  if (st.previewData && st.previewData.dsm_b64 && st.previewData.dsm_bounds) {
    var b = st.previewData.dsm_bounds; // [west, south, east, north]
    _dsmLayer = _viewer.imageryLayers.addImageryProvider(
      new Cesium.SingleTileImageryProvider({
        url:       'data:image/png;base64,' + st.previewData.dsm_b64,
        rectangle: Cesium.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]),
      })
    );
    _dsmLayer.alpha = 0.65;
  }

  // ── 4. Drone marker for playback ────────────────────────────────────────────
  var epoch = Cesium.JulianDate.fromIso8601('2020-01-01T00:00:00Z');
  _dronePositionProperty = new Cesium.SampledPositionProperty();
  waypoints.forEach(function(wp) {
    var jd = Cesium.JulianDate.addSeconds(epoch, wp.time, new Cesium.JulianDate());
    _dronePositionProperty.addSample(jd, Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, wp.height));
  });

  _currentEntities.push(_viewer.entities.add({
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
  }));

  /* eslint-enable no-undef */

  // Fly to scene
  _viewer.zoomTo(curtain);

  // Reset playback UI
  _playbackTime = 0;
  var slider = document.getElementById('cesium-slider');
  if (slider) { slider.value = 0; slider.max = _totalDuration; }
  _updateTimeDisplay();
  document.getElementById('cesium-play-btn').textContent = '▶';
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
}

function _positionOverlayBtn(show) {
  var btn = document.getElementById('cesium-2d-btn');
  if (!btn) return;
  if (!show) { btn.style.display = 'none'; return; }
  // Mirror the exact pixel position of the Leaflet toggle button.
  // _toggle3dBtn is visibility:hidden but still laid out, so its rect is valid.
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
