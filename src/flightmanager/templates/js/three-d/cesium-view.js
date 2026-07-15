// ── Cesium 3D flight path view ─────────────────────────────────────────────
// Visualises the current job's flat-altitude lawnmower route in 3D.
// Activated via the 2D/3D toggle button (custom Leaflet control, topleft).
// Data source: last accurate route estimate (strips_geojson + transits_geojson)
// stored here via notifyCesiumRouteReady(); no separate API call on activation.

import { st } from '../core/state.js';
import { map } from '../map/map-init.js';

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
var _entityGroups = {area: [], path: [], curtain: [], drone: [], keepout: [], powerline: [], zone: []};

// Layer visibility state — persists across re-renders
var _layerVis = {dsm: true, area: true, path: true, curtain: true, drone: true, keepout: true, powerline: true, zone: true};

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
   
  var e = _viewer.entities.add(entityDef);
   
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

/** Return [lng, lat] centroid of a GeoJSON Polygon or Point geometry. */
function _bldgCenter(geom) {
  try {
    if (geom.type === 'Point') return [geom.coordinates[0], geom.coordinates[1]];
    if (geom.type === 'Polygon') {
      var cs = geom.coordinates[0];
      var lng = cs.reduce(function(s, c) { return s + c[0]; }, 0) / cs.length;
      var lat = cs.reduce(function(s, c) { return s + c[1]; }, 0) / cs.length;
      return [lng, lat];
    }
  } catch {}
  return null;
}

/** Build a Cesium PolygonHierarchy from a GeoJSON Polygon coordinate ring array. */
function _polyHierarchy(polyCoords) {
   
  var outer = polyCoords[0].map(function(c) {
    return Cesium.Cartesian3.fromDegrees(c[0], c[1]);
  });
  var holes = polyCoords.slice(1).map(function(ring) {
    return new Cesium.PolygonHierarchy(ring.map(function(c) {
      return Cesium.Cartesian3.fromDegrees(c[0], c[1]);
    }));
  });
  return new Cesium.PolygonHierarchy(outer, holes);
   
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
  return new Cesium.Color(  
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

  function _addPt(lon, lat, h, spd) {
    var last = pts.length > 0 ? pts[pts.length - 1] : null;
    if (last && Math.abs(last.lon - lon) < 1e-9 && Math.abs(last.lat - lat) < 1e-9) {
      // Duplicate lat/lon at a boundary: update height/speed instead of a zero-length segment
      // (which causes NaN in Cesium polylineVolume).
      last.height = h;
      last.speed = spd;
    } else {
      pts.push({lon: lon, lat: lat, height: h, speed: spd});
    }
  }

  function addCoords(coords, h, spd) {
    coords.forEach(function(c) { _addPt(c[0], c[1], h, spd); });
  }

  function addCoordsWithAlts(coords, alts, speeds, defaultSpd) {
    coords.forEach(function(c, k) {
      var h   = (alts[k]   !== undefined) ? alts[k]   : alts[alts.length - 1];
      var spd = (speeds && speeds[k] !== undefined) ? speeds[k] : defaultSpd;
      _addPt(c[0], c[1], h, spd);
    });
  }

  // Actual altitude at the START of strip idx (first wpt_alts entry, else strip min).
  function _startAlt(strip, idx) {
    var wa = strip.properties && strip.properties.wpt_alts;
    return (wa && wa.length > 0) ? wa[0] : stripAlts[idx];
  }

  // Actual altitude at the END of strip idx (last wpt_alts entry, else strip min).
  function _endAlt(strip, idx) {
    var wa = strip.properties && strip.properties.wpt_alts;
    return (wa && wa.length > 0) ? wa[wa.length - 1] : stripAlts[idx];
  }

  // Pre-compute level turn altitude for every inter-strip transition:
  // min(strip_end, next_strip_start) so consecutive U-turns stay at a
  // consistent height rather than oscillating with building-proximity deltas.
  var turnAlts = [];
  for (var ti = 0; ti < N - 1; ti++) {
    turnAlts.push(Math.min(_endAlt(strips[ti], ti), _startAlt(strips[ti + 1], ti + 1)));
  }

  // Add a strip's waypoints, levelling the first and last coord to the
  // adjacent turn altitude so strip–transit boundaries are seamless.
  function addStrip(strip, i) {
    var wptAlts   = strip.properties && strip.properties.wpt_alts;
    var wptSpeeds = strip.properties && strip.properties.wpt_speeds;
    var coords    = strip.geometry.coordinates;
    if (wptAlts && wptAlts.length === coords.length) {
      var alts = wptAlts.slice();
      if (i > 0)     alts[0]             = turnAlts[i - 1];
      if (i < N - 1) alts[alts.length-1] = turnAlts[i];
      addCoordsWithAlts(coords, alts, wptSpeeds, stripSpeeds[i]);
    } else {
      addCoords(coords, stripAlts[i], stripSpeeds[i]);
    }
  }

  // Add a transit segment flying level at the minimum safe altitude.
  // altitude_m from the feature is the 1:1 minimum over all sampled transit
  // points; fall back to turnAlts[i] when not present (simple mode).
  function addTransit(transit, i) {
    var coords  = transit.geometry.coordinates;
    var propAlt = transit.properties && transit.properties.altitude_m;
    var tAlt    = (propAlt != null) ? propAlt : turnAlts[i];
    var tSpd    = (stripSpeeds[i] + stripSpeeds[i + 1]) / 2;
    addCoords(coords, tAlt, tSpd);
  }

  if (hasHome) {
    addCoords(transits[0].geometry.coordinates, _startAlt(strips[0], 0), stripSpeeds[0]);
    strips.forEach(function(strip, i) {
      addStrip(strip, i);
      if (i < N - 1) addTransit(transits[i + 1], i);
    });
    addCoords(transits[N].geometry.coordinates, _endAlt(strips[N - 1], N - 1), stripSpeeds[N - 1]);
  } else {
    // No home transit in the GeoJSON (simple mode or export without home transits).
    // If a takeoff marker exists, bookend the animation so the drone starts and
    // returns to the takeoff/landing spot.
    var homePt = st.takeoff.pt || st.takeoff.auto;
    if (homePt && strips.length > 0) {
      _addPt(homePt[0], homePt[1], _startAlt(strips[0], 0), stripSpeeds[0]);
    }
    strips.forEach(function(strip, i) {
      addStrip(strip, i);
      if (i < N - 1) addTransit(transits[i], i);
    });
    if (homePt && strips.length > 0) {
      _addPt(homePt[0], homePt[1], _endAlt(strips[N - 1], N - 1), stripSpeeds[N - 1]);
    }
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

  // ── 5. Keepout volumes around buildings ─────────────────────────────────────
  //   A1/A3:      fixed 150 m radius × 150 m tall cylinder (aviation separation rule).
  //   A2 simple:  30 m minimum-separation cylinder (0→30 m, r=30 m), then a 1:1
  //               cone (radius = altitude) from there up to the 120 m open-category
  //               ceiling — the full A2 keep-out envelope, shown even at a flat
  //               flight altitude (a plain cylinder hid the 1:1 envelope).
  //   A2 advanced: straight to minH near the roof, then the 1:1 cone minH→maxH.
  var _stats = st.previewData && st.previewData.stats;
  var _advMode = _stats && _stats.advanced_mode;
  var _sub = (_stats && _stats.subcategory) || 'A3';
  var _flightAlt = (_stats && _stats.flight_height_m) || altM;
  if (st.previewData.buildings && st.previewData.buildings.length) {
    var redFill = Cesium.Color.fromCssColorString('#dc2626');
    var _A1A3_RADIUS_M = 150, _A1A3_HEIGHT_M = 150;
    var _A2_MIN_M = 30, _A2_MAX_ALT_M = 120;   // A2: 30 m min separation, then 1:1 to the 120 m ceiling

    var _addKeepoutCyl = function(ctr, length, baseAlt, bottomR, topR) {
      _addEntity('keepout', {
        position: Cesium.Cartesian3.fromDegrees(ctr[0], ctr[1], baseAlt + length / 2),
        cylinder: {
          length:       length,
          bottomRadius: bottomR,
          topRadius:    topR,
          material:     redFill.withAlpha(0.20),
          outline:      true,
          outlineColor: redFill.withAlpha(0.45),
          outlineWidth: 1,
        },
      });
    };

    st.previewData.buildings.forEach(function(b) {
      if (!b.is_keepout) return;
      var ctr = _bldgCenter(b.geojson);
      if (!ctr) return;
      var bldgH = b.height_m || 7;

      if (_sub !== 'A2') {
        // A1/A3 — fixed horizontal separation, altitude-independent → straight cylinder
        _addKeepoutCyl(ctr, _A1A3_HEIGHT_M, 0, _A1A3_RADIUS_M, _A1A3_RADIUS_M);
      } else if (_advMode) {
        // A2 advanced — straight to minH near the roof, then the 1:1 cone minH→maxH
        var minH = (_stats.home_buffer_m    || 30);
        var maxH = (_stats.home_buffer_max_m || minH);
        var rMin = Math.max(0.5, minH - bldgH);
        var rMax = Math.max(0.5, maxH - bldgH);
        _addKeepoutCyl(ctr, minH, 0, rMin, rMin);                       // ground → minH
        if (maxH > minH) _addKeepoutCyl(ctr, maxH - minH, minH, rMin, rMax);  // frustum minH → maxH
      } else {
        // A2 simple — 30 m minimum-separation cylinder, then the 1:1 cone
        // (radius = altitude) from 30 m up to the 120 m open-category ceiling.
        _addKeepoutCyl(ctr, _A2_MIN_M, 0, _A2_MIN_M, _A2_MIN_M);        // 0→30 m, r=30 m
        _addKeepoutCyl(ctr, _A2_MAX_ALT_M - _A2_MIN_M, _A2_MIN_M,
                       _A2_MIN_M, _A2_MAX_ALT_M);                       // frustum 30→120 m, r 30→120 m
      }
    });
  }

  // ── 6. Overhead power lines (rectangular keep-out pipe) ─────────────────────
  //   Rendered as a corridor 60 m wide (30 m buffer each side) extruded 0→40 m,
  //   which covers nearly all Finnish suurjännitejohto (110 kV+) overhead lines.
  var _PL_WIDTH_M = 60, _PL_HEIGHT_M = 40;
  if (st.previewData.power_lines && st.previewData.power_lines.length) {
    var plFill = Cesium.Color.fromCssColorString('#d97706');
    st.previewData.power_lines.forEach(function(pl) {
      if (!pl.is_overhead || !pl.geojson) return;
      var g = pl.geojson;
      var segs = g.type === 'LineString'      ? [g.coordinates]
               : g.type === 'MultiLineString' ? g.coordinates
               : [];
      segs.forEach(function(seg) {
        if (!seg || seg.length < 2) return;
        var positions = seg.map(function(c) { return Cesium.Cartesian3.fromDegrees(c[0], c[1]); });
        _addEntity('powerline', {
          corridor: {
            positions:      positions,
            width:          _PL_WIDTH_M,
            height:         0,
            extrudedHeight: _PL_HEIGHT_M,
            cornerType:     Cesium.CornerType.MITERED,
            material:       plFill.withAlpha(0.18),
            outline:        true,
            outlineColor:   plFill.withAlpha(0.5),
          },
        });
      });
    });
  }

  // ── 7. UAS restriction zones (extruded altitude bands → inverted pyramid) ────
  //   Each zone is extruded from its floor (lower_limit_m_agl, 0 if GND) to its
  //   ceiling (upper_limit_m_agl). Concentric airfield zones (A 0–50, C 50–120,
  //   D 120+) stack into the classic stepped inverted pyramid. Ceiling-less zones
  //   are capped at a viz height so they remain visible.
  var _ZONE_VIZ_CAP_M = 150;
  if (st.previewData.zone_hits && st.previewData.zone_hits.length) {
    var zoneFill = Cesium.Color.fromCssColorString('#f97316');
    st.previewData.zone_hits.forEach(function(z) {
      var g = z.geojson;
      if (!g) return;
      var floor = (z.lower_limit_m_agl != null) ? z.lower_limit_m_agl : 0;
      var ceil  = (z.upper_limit_m_agl != null) ? z.upper_limit_m_agl
                                                : Math.max(floor + 20, _ZONE_VIZ_CAP_M);
      if (ceil <= floor) ceil = floor + 10;
      var polys = g.type === 'Polygon'      ? [g.coordinates]
                : g.type === 'MultiPolygon' ? g.coordinates
                : [];
      polys.forEach(function(rings) {
        _addEntity('zone', {
          polygon: {
            hierarchy:      _polyHierarchy(rings),
            height:         floor,
            extrudedHeight: ceil,
            material:       zoneFill.withAlpha(z.context_only ? 0.08 : 0.14),
            outline:        true,
            outlineColor:   Cesium.Color.fromCssColorString('#ea580c').withAlpha(z.context_only ? 0.4 : 0.7),
          },
        });
      });
    });
  }

   

  // Fly to scene
  _viewer.zoomTo(curtainRef);

  // Reset playback UI
  _playbackTime = 0;
  var slider = document.getElementById('cesium-slider');
  if (slider) { slider.value = 0; slider.max = _totalDuration; }
  _updateTimeDisplay();
  document.getElementById('cesium-play-btn').textContent = '▶';

  // Build layer legend (after entities are placed so rows match reality)
  var _hasKeeput = _entityGroups.keepout.length > 0;
  var _hasPowerlines = _entityGroups.powerline.length > 0;
  var _hasZones = _entityGroups.zone.length > 0;
  _buildLegend(hasArea, hasDsm, jobColorHex, _useAltColor ? {min: _altMin, max: _altMax} : null, _hasKeeput, _hasPowerlines, _hasZones);
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

function _buildLegend(hasArea, hasDsm, colorHex, altRange, hasKeeput, hasPowerlines, hasZones) {
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
    // Variable-altitude mode: gradient icon row + separate gradient bar with min/max (mirrors 2D leg-alt-row)
    leg.appendChild(_legRow('path',
      '<div class="l-swatch" style="background:linear-gradient(to right,#440154,#3b528b,#21918c,#5ec962,#fde725);border:1px solid #9ca3af;"></div>',
      'Flight path'));
    var altRow = document.createElement('div');
    altRow.style.cssText = 'margin:2px 0 4px 20px;display:flex;flex-direction:column;gap:3px;';
    var altGrad = document.createElement('div');
    altGrad.className = 'leg-alt-grad';
    var altLabels = document.createElement('div');
    altLabels.style.cssText = 'display:flex;justify-content:space-between;font-size:9px;color:#64748b;padding:0 1px;';
    var minSpan = document.createElement('span');
    minSpan.textContent = Math.round(altRange.min) + ' m';
    var maxSpan = document.createElement('span');
    maxSpan.textContent = Math.round(altRange.max) + ' m';
    altLabels.appendChild(minSpan);
    altLabels.appendChild(maxSpan);
    altRow.appendChild(altGrad);
    altRow.appendChild(altLabels);
    leg.appendChild(altRow);
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

  if (hasKeeput) {
    leg.appendChild(_legRow('keepout',
      '<div class="l-swatch" style="background:#dc262655;border:1.5px solid #dc2626;"></div>',
      'Keepout zones'));
  }

  if (hasPowerlines) {
    leg.appendChild(_legRow('powerline',
      '<div class="l-swatch" style="background:#d9770633;border:1.5px solid #d97706;"></div>',
      'Power lines'));
  }

  if (hasZones) {
    leg.appendChild(_legRow('zone',
      '<div class="l-swatch" style="background:#f9731626;border:1.5px solid #ea580c;"></div>',
      'UAS zones'));
  }

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
