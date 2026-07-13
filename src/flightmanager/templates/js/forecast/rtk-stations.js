// ── RTK base stations (NTRIP) ───────────────────────────────────────────────
//
// Data + layer + legend for the "RTK base stations" stat mode, plus the
// nearest-station line in the map-view job popup. Station data comes from
// GET /api/rtk_stations (sourcetables cached server-side); it is fetched once
// per folder (folder-keyed cache, request-sequence guard) and shared by the
// stat layer and the popups.

import { escHtml } from '../core/utils.js';
import { apiGet } from '../core/api.js';

var _rtkCache = { folder: undefined, data: null };
var _rtkPromise = null;      // in-flight fetch (deduplicates ensureRtkData calls)
var _rtkLayer = null;        // Leaflet layer group: dots + dashed range circles
var _rtkFitLayer = null;     // selection fit circle + centre crosshair
var _fitCache = { key: undefined, fit: null };  // last selection's fit circle
var _rtkMarkers = {};        // "lat_lon" -> {highlight, unhighlight}, for list-row hover

export function getRtkData(folder) {
  return (_rtkCache.folder === folder) ? _rtkCache.data : null;
}

// Fetch (or reuse) the station payload for a folder. Safe to fire-and-forget:
// popups read the cache synchronously and simply omit the RTK line until the
// data lands.
export function ensureRtkData(folder) {
  if (_rtkCache.folder === folder && _rtkCache.data) return Promise.resolve(_rtkCache.data);
  if (_rtkPromise && _rtkPromise._folder === folder) return _rtkPromise;
  var url = '/api/rtk_stations' + (folder ? '?folder=' + encodeURIComponent(folder) : '');
  var p = apiGet(url).then(function (data) {
    _rtkCache = { folder: folder, data: data };
    if (_rtkPromise === p) _rtkPromise = null;
    return data;
  }, function (e) {
    if (_rtkPromise === p) _rtkPromise = null;
    throw e;
  });
  p._folder = folder;
  _rtkPromise = p;
  return p;
}

export function clearRtkLayer() {
  if (_rtkLayer) { _rtkLayer.remove(); _rtkLayer = null; }
  _rtkMarkers = {};
  clearRtkFit();
}

export function clearRtkFit() {
  if (_rtkFitLayer) { _rtkFitLayer.remove(); _rtkFitLayer = null; }
}

// True when the drawn layer is current for *data* (guards folder switches).
export function hasRtkLayer(data) { return !!_rtkLayer && _rtkLayerData === data; }
var _rtkLayerData = null;

// Draw every in-range station as a network-coloured dot with a dashed circle of
// the usable-baseline radius (circle_radius_km) around it. Hovering a dot
// highlights its circle (solid stroke + low-opacity fill) so the coverage of
// that one station stands out from the overlapping rings.
export function drawRtkLayer(data) {
  import('../map/map-init.js').then(function (m) {
    clearRtkLayer();
    var grp = L.layerGroup();
    var radiusM = (data.circle_radius_km || 20) * 1000;
    (data.stations || []).forEach(function (s) {
      var ll = [s.lat, s.lon];
      var ring = L.circle(ll, {
        radius: radiusM, color: s.color, weight: 2.5, opacity: 1,
        dashArray: '8,6', fill: false, interactive: false,
      }).addTo(grp);
      var dot = L.circleMarker(ll, {
        radius: 6.5, color: '#fff', weight: 2, fillColor: s.color, fillOpacity: 1,
      }).bindTooltip(
        '<b>' + escHtml(s.mountpoint) + '</b> (' + escHtml(s.network) + ')<br>'
        + escHtml(s.identifier || '') + '<br>'
        + escHtml(s.format || '') + ' · ' + escHtml(s.nav_system || '') + '<br>'
        + s.dist_km + ' km from nearest job',
        { direction: 'top', offset: [0, -6] }
      ).addTo(grp);
      function highlight() {
        ring.setStyle({ weight: 3.5, opacity: 1, dashArray: null,
                        fill: true, fillColor: s.color, fillOpacity: 0.18 });
        ring.bringToFront();
        dot.setStyle({ radius: 8.5 });
      }
      function unhighlight() {
        ring.setStyle({ weight: 2.5, opacity: 1, dashArray: '8,6', fill: false });
        dot.setStyle({ radius: 6.5 });
      }
      dot.on('mouseover', highlight);
      dot.on('mouseout', unhighlight);
      _rtkMarkers[s.lat + '_' + s.lon] = { highlight: highlight, unhighlight: unhighlight };
    });
    grp.addTo(m.map);
    _rtkLayer = grp;
    _rtkLayerData = data;
  });
}

// Selection fit: the smallest enclosing circle over the selected jobs (same
// fitting as a launch site's announcement circle), fetched from /api/fit_circle
// and cached by the selection key. Distances in the legend are measured from
// its centre.
export function fetchFitCircle(paths) {
  var key = paths.slice().sort().join(',');
  if (_fitCache.key === key && _fitCache.fit) return Promise.resolve(_fitCache.fit);
  return apiGet('/api/fit_circle?paths=' + encodeURIComponent(key)).then(function (fit) {
    _fitCache = { key: key, fit: fit };
    return fit;
  });
}

export function drawRtkFit(fit) {
  import('../map/map-init.js').then(function (m) {
    clearRtkFit();
    var grp = L.layerGroup();
    var c = [fit.center_4326[1], fit.center_4326[0]];
    L.circle(c, {
      radius: fit.radius_m, color: '#f59e0b', weight: 2, opacity: 0.9,
      fill: true, fillColor: '#f59e0b', fillOpacity: 0.06, interactive: false,
    }).addTo(grp);
    L.marker(c, {
      interactive: false,
      icon: L.divIcon({ className: 'rtk-fit-center', html: '+', iconSize: [0, 0] }),
    }).addTo(grp);
    grp.addTo(m.map);
    _rtkFitLayer = grp;
  });
}

function _asOf(iso) {
  if (!iso) return '';
  try {
    var d = new Date(iso);
    return d.toLocaleDateString('en-CA') + ' ' +
      d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

// Legend header for #mv-stat-body: one row per network (count within range +
// as-of time or fetch error). Always shown, selection or not.
export function rtkNetworksHtml(data) {
  var r = '<div class="mv-st-div">Base stations within ' + Math.round(data.search_radius_km) + ' km</div>';
  (data.networks || []).forEach(function (n) {
    var note = n.error ? 'error' : (n.station_count + (n.station_count === 1 ? ' station' : ' stations'));
    r += '<div class="mv-st-brow"><span class="mv-st-sw" style="background:' + n.color
      + '"></span><span class="mv-st-bl">' + escHtml(n.name) + '</span>'
      + '<span class="mv-st-bc"' + (n.error ? ' title="' + escHtml(n.error) + '" style="color:#f87171"' : '') + '>'
      + note + '</span></div>';
    if (n.fetched_at && !n.error) {
      r += '<div class="mv-st-nodata" style="padding:0 0 4px 18px">as of ' + escHtml(_asOf(n.fetched_at)) + '</div>';
    }
  });
  return r;
}

// Nearest stations overall (not tied to any selection) as clickable rows.
// Shown only when no jobs are selected - rtkSelectionHtml() replaces this
// with distances from the fitted selection circle otherwise.
export function rtkNearestHtml(data) {
  var r = '';
  var sts = (data.stations || []).slice(0, 10);
  if (sts.length) {
    r += '<div class="mv-st-div">Nearest (dashed ring = ' + Math.round(data.circle_radius_km) + ' km)</div>';
    sts.forEach(function (s) {
      r += '<div class="mv-st-job" onclick="_rtkStationClick(' + s.lat + ',' + s.lon + ')" '
        + 'onmouseenter="_rtkStationHover(' + s.lat + ',' + s.lon + ',true)" '
        + 'onmouseleave="_rtkStationHover(' + s.lat + ',' + s.lon + ',false)" title="'
        + escHtml(s.identifier || s.mountpoint) + '">'
        + '<span class="mv-st-jdot" style="background:' + s.color + '"></span>'
        + '<span class="mv-st-jname">' + escHtml(s.mountpoint) + '</span>'
        + '<span class="mv-st-jval">' + s.dist_km + ' km</span></div>';
    });
  } else if (!(data.networks || []).some(function (n) { return n.error; })) {
    r += '<div class="mv-st-nodata">No stations in range</div>';
  }
  return r;
}

export function rtkFooterHtml() {
  return '<div class="mv-st-nodata">Sourcetables list online stations only - re-check on flight day</div>';
}

export function _rtkStationClick(lat, lon) {
  import('../map/map-init.js').then(function (m) {
    m.map.setView([lat, lon], Math.max(m.map.getZoom(), 11));
  });
}

// Mirrors the dot's own mouseover/mouseout highlight, driven from a list row.
export function _rtkStationHover(lat, lon, on) {
  var m = _rtkMarkers[lat + '_' + lon];
  if (!m) return;
  if (on) m.highlight(); else m.unhighlight();
}

// Legend section for an active selection: nearest stations measured from the
// fitted circle's centre (not from any single job).
export function rtkSelectionHtml(data, fit) {
  var c = fit.center_4326;  // [lon, lat]
  var ranked = (data.stations || []).map(function (s) {
    return { s: s, d: distKm(c[1], c[0], s.lat, s.lon) };
  }).sort(function (a, b) { return a.d - b.d; });
  var r = '<div class="mv-st-div">Selection · fitted circle r '
    + (fit.radius_m / 1000).toFixed(1) + ' km</div>';
  if (!ranked.length) return r + '<div class="mv-st-nodata">No stations in range</div>';
  ranked.slice(0, 5).forEach(function (e) {
    var far = e.d > data.circle_radius_km;
    r += '<div class="mv-st-job" onclick="_rtkStationClick(' + e.s.lat + ',' + e.s.lon + ')" '
      + 'onmouseenter="_rtkStationHover(' + e.s.lat + ',' + e.s.lon + ',true)" '
      + 'onmouseleave="_rtkStationHover(' + e.s.lat + ',' + e.s.lon + ',false)" title="'
      + escHtml(e.s.identifier || e.s.mountpoint) + '">'
      + '<span class="mv-st-jdot" style="background:' + e.s.color + '"></span>'
      + '<span class="mv-st-jname">' + escHtml(e.s.mountpoint) + '</span>'
      + '<span class="mv-st-jval"' + (far ? ' style="color:#fb923c"' : '') + '>'
      + e.d.toFixed(1) + ' km' + (far ? ' !' : '') + '</span></div>';
  });
  r += '<div class="mv-st-nodata">Distances from the circle centre · ! = beyond '
    + Math.round(data.circle_radius_km) + ' km</div>';
  return r;
}

export function distKm(lat1, lon1, lat2, lon2) {
  var R = 6371.0088, rad = Math.PI / 180;
  var dp = (lat2 - lat1) * rad, dl = (lon2 - lon1) * rad;
  var a = Math.sin(dp / 2) * Math.sin(dp / 2)
    + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dl / 2) * Math.sin(dl / 2);
  return 2 * R * Math.asin(Math.sqrt(a));
}

// Popup line(s) for one job: every station within circle_radius_km of the job's
// takeoff (up to 3), or the single nearest one with its distance. Returns ''
// while the data hasn't loaded yet (or nothing is in range at all).
export function rtkPopupHtml(folder, refLatLng) {
  var data = getRtkData(folder);
  if (!data || !refLatLng || !(data.stations || []).length) return '';
  var ranked = data.stations.map(function (s) {
    return { s: s, d: distKm(refLatLng[0], refLatLng[1], s.lat, s.lon) };
  }).sort(function (a, b) { return a.d - b.d; });
  var inRange = ranked.filter(function (r) { return r.d <= data.circle_radius_km; }).slice(0, 3);
  var show = inRange.length ? inRange : ranked.slice(0, 1);
  var parts = show.map(function (r) {
    return '<span style="white-space:nowrap">'
      + '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:'
      + r.s.color + ';margin-right:3px;vertical-align:baseline"></span>'
      + escHtml(r.s.mountpoint) + ' ' + r.d.toFixed(1) + ' km</span>';
  });
  return '<div class="mv-tt-flight" title="RTK base stations (' +
    (inRange.length ? 'within ' + Math.round(data.circle_radius_km) + ' km' : 'nearest') + ')">'
    + 'RTK: ' + parts.join('<span style="color:#475569"> · </span>') + '</div>';
}
