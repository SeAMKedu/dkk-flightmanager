// ── Launch sites (map-view route layer) ──────────────────────────────────────
//
// A *launch site* groups consecutive-flight-order jobs flown from one parking
// spot (takeoffs within ~50 m, clustered server-side by /api/launch_sites).
// Each site renders as one numbered dot at the takeoff centroid; the dots are
// joined in flight order by the amber dashed route line.
//
// The dot is labelled with the site's *first route index* so it lines up with
// the per-job route-index circles (timeline / high zoom) instead of a separate
// count. Below zoom 17 the sites render as aggregate dots joined by the dashed
// route line; at/above zoom 17 (where the route chevrons appear) each site
// breaks into its individual per-job route-index circles placed at the real
// takeoff spots, and the route lines are dropped — so the close-up view shows
// exactly where you launch each job.
//
// Hovering a dot/circle reveals the smallest enclosing circle over the site's
// survey polygons (crosshair at its centre) and fills the fixed bottom-left
// **announcement box** with everything: the member jobs plus the Flyk fields
// (centre coords, diameter/radius, max altitude, up-rounded duration). A white
// dotted connector links the box to the hovered marker (drawn beneath it).
// Clicking an aggregate dot selects all of that site's jobs; clicking a single
// high-zoom circle selects just that job.

import { escHtml } from './utils.js';
// Circular (map-view imports this module); only called at runtime:
import { mvSelectPaths } from './map-view.js';

// Zoom at/above which sites split into per-job circles (matches the route
// chevrons' _ARROW_MIN_ZOOM in route-planner.js).
var DETAIL_ZOOM = 17;

var _moveHandler = null;       // keeps the connector aligned while panning/zooming
var _panel = null;             // singleton announcement box (#ls-announce)
var _connector = null;         // singleton dotted box→dot polyline (white, on top)
var _connectorHalo = null;     // dark halo beneath it, so it reads on any basemap

// Live render state — lets the zoomend handler re-render in place and lets
// clearLaunchSites() fully tear down when leaving map view.
var _map = null;
var _sites = null;
var _group = null;             // the layer group owned by map-view (_mvRouteLayer)
var _hoverGroup = null;        // enclosing circle + crosshair for the hovered site
var _renderHandler = null;     // zoomend → re-render (overview ⇄ detail)

function _fmtM(m) {
  return m >= 1000 ? (m / 1000).toFixed(2) + ' km' : Math.round(m) + ' m';
}

// Round flight time *up* to the next half hour so the announced window has a
// margin (26.5 → 30 min, 35 → 1 h, 65 → 1 h 30 min).
function _roundUpDuration(min) {
  if (min == null) return null;
  var r = Math.ceil(min / 30) * 30;
  if (r < 60) return r + ' min';
  var h = Math.floor(r / 60), m = r % 60;
  return m ? (h + ' h ' + m + ' min') : (h + ' h');
}

function _rangeLabel(s) {
  // "#3–#5" from sort_orders (1-based), or empty when unordered.
  var so = (s.sort_orders || []).filter(function (v) { return v != null; });
  if (so.length) {
    var lo = Math.min.apply(null, so) + 1, hi = Math.max.apply(null, so) + 1;
    return lo === hi ? ('#' + lo) : ('#' + lo + '–#' + hi);
  }
  return '';
}

// ── Announcement box ─────────────────────────────────────────────────────────

function _ensurePanel(map) {
  if (_panel) return _panel;
  _panel = document.createElement('div');
  _panel.id = 'ls-announce';
  map.getContainer().appendChild(_panel);
  return _panel;
}

function _showAnnounce(map, s) {
  var p = _ensurePanel(map);
  var c = s.circle_center_4326;
  var dur = _roundUpDuration(s.flight_time_min);
  var range = _rangeLabel(s);
  var rows = [
    ['Centre', c[1].toFixed(5) + ', ' + c[0].toFixed(5)],
    ['Diameter', _fmtM(s.diameter_m) + '  (r ' + _fmtM(s.radius_m) + ')'],
  ];
  if (s.max_altitude_m != null) rows.push(['Max altitude', Math.round(s.max_altitude_m) + ' m']);
  if (dur) rows.push(['Duration', dur]);
  var jobs = (s.job_names || []).map(function (n) {
    var e = escHtml(n);
    return '<div title="' + e + '">' + e + '</div>';
  }).join('');
  p.innerHTML =
    '<div class="ls-an-title">Launch site' + (range ? ' · ' + range : '') + '</div>'
    + '<div class="ls-an-sub">' + s.member_count + (s.member_count === 1 ? ' job' : ' jobs')
    + '</div>'
    + rows.map(function (r) {
      return '<div class="ls-an-row"><span class="k">' + r[0]
        + '</span><span class="v">' + r[1] + '</span></div>';
    }).join('')
    + (jobs ? '<div class="ls-an-jobs">' + jobs + '</div>' : '');
  p.classList.add('show');
}

function _hideAnnounce() {
  if (_panel) _panel.classList.remove('show');
}

// ── Box → dot connector (faint dotted line, beneath the dots) ─────────────────

function _ensureConnector(map) {
  if (!map.getPane('lsLine')) {
    map.createPane('lsLine');
    var pane = map.getPane('lsLine');
    pane.style.zIndex = 450;            // above the circle (overlayPane 400), below markers (600)
    pane.style.pointerEvents = 'none';
  }
  if (!_connector) {
    _connectorHalo = L.polyline([], {
      pane: 'lsLine', color: '#1e293b', weight: 4, opacity: 0.4, interactive: false,
    });
    _connector = L.polyline([], {
      pane: 'lsLine', color: '#fff', weight: 2, opacity: 0.95,
      dashArray: '3,4', interactive: false,
    });
  }
  return _connector;
}

function _boxCornerLatLng(map) {
  if (!_panel) return null;
  var mr = map.getContainer().getBoundingClientRect();
  var br = _panel.getBoundingClientRect();
  // Top-right corner of the box, in container pixels → a (moving) lat/lng.
  return map.containerPointToLatLng([br.right - mr.left, br.top - mr.top]);
}

function _showConnector(map, dotLatLng) {
  _ensureConnector(map);
  _connectorHalo.addTo(map);            // halo first so the white dashes sit on top
  _connector.addTo(map);
  var update = function () {
    var corner = _boxCornerLatLng(map);
    if (corner) { _connectorHalo.setLatLngs([dotLatLng, corner]); _connector.setLatLngs([dotLatLng, corner]); }
  };
  update();
  if (_moveHandler) map.off('move zoom', _moveHandler);
  _moveHandler = update;                // box is screen-fixed → recompute its latlng on pan/zoom
  map.on('move zoom', _moveHandler);
}

function _hideConnector(map) {
  if (_moveHandler) { map.off('move zoom', _moveHandler); _moveHandler = null; }
  if (_connectorHalo) map.removeLayer(_connectorHalo);
  if (_connector) map.removeLayer(_connector);
}

// ── Overlap de-collision ──────────────────────────────────────────────────────

// Nudge dot markers that render closer than minPx apart so overlapping sites
// stay legible. Display-only — circles/centres keep their true positions.
function _pushOutDots(map, markers, sites, minPx) {
  var pts = sites.map(function (s) {
    return map.latLngToLayerPoint([s.dot_4326[1], s.dot_4326[0]]);
  });
  for (var iter = 0; iter < 30; iter++) {
    var moved = false;
    for (var i = 0; i < pts.length; i++) {
      for (var j = i + 1; j < pts.length; j++) {
        var dx = pts[j].x - pts[i].x, dy = pts[j].y - pts[i].y;
        var d = Math.hypot(dx, dy) || 0.01;
        if (d < minPx) {
          var push = (minPx - d) / 2, ux = dx / d, uy = dy / d;
          pts[i].x -= ux * push; pts[i].y -= uy * push;
          pts[j].x += ux * push; pts[j].y += uy * push;
          moved = true;
        }
      }
    }
    if (!moved) break;
  }
  markers.forEach(function (mk, i) { mk.setLatLng(map.layerPointToLatLng(pts[i])); });
}

function _circleIcon(label, multi) {
  return L.divIcon({
    className: '',
    html: '<div style="background:#f59e0b;color:#000;font-size:10px;font-weight:700;'
      + 'width:18px;height:18px;border-radius:50%;display:flex;align-items:center;'
      + 'justify-content:center;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.5)'
      + (multi ? ';outline:2px solid rgba(245,158,11,.45);outline-offset:1px' : '') + '">'
      + label + '</div>',
    iconSize: [18, 18], iconAnchor: [9, 9],
  });
}

// Show the hovered site's enclosing circle + announcement box, with the
// connector anchored at the marker the cursor is on (the dot, or a takeoff
// circle at high zoom).
function _showSite(s, anchorLatLng) {
  _hoverGroup.clearLayers();
  var c = [s.circle_center_4326[1], s.circle_center_4326[0]];
  L.circle(c, {
    radius: s.radius_m, color: '#f59e0b', weight: 1.5, opacity: 0.9,
    fillColor: '#f59e0b', fillOpacity: 0.07, interactive: false,
  }).addTo(_hoverGroup);
  var cross = L.divIcon({
    className: '',
    html: '<div style="width:14px;height:14px;position:relative">'
      + '<div style="position:absolute;left:6px;top:0;width:2px;height:14px;background:#f59e0b"></div>'
      + '<div style="position:absolute;top:6px;left:0;width:14px;height:2px;background:#f59e0b"></div>'
      + '</div>',
    iconSize: [14, 14], iconAnchor: [7, 7],
  });
  L.marker(c, { icon: cross, interactive: false }).addTo(_hoverGroup);
  _showAnnounce(_map, s);
  _showConnector(_map, anchorLatLng);
}

function _clearSite() {
  if (_hoverGroup) _hoverGroup.clearLayers();
  _hideAnnounce();
  _hideConnector(_map);
}

// Re-draw the current sites for the current zoom into _group (cleared first).
function _render() {
  if (!_group) return;
  _group.clearLayers();
  _clearSite();
  if (!_sites || !_sites.length) return;

  _hoverGroup = L.layerGroup().addTo(_group);
  var detail = _map.getZoom() >= DETAIL_ZOOM;

  if (detail) {
    // Per-job route-index circles at each takeoff; no route lines, no push-out.
    _sites.forEach(function (s) {
      (s.members || []).forEach(function (mem) {
        var tp = mem.takeoff_4326;
        if (!tp) return;
        var ll = [tp[1], tp[0]];
        var m = L.marker(ll, { icon: _circleIcon(mem.route_index != null ? mem.route_index : '·', false) }).addTo(_group);
        m.on('mouseover', function () { _showSite(s, ll); });
        m.on('mouseout', function () { _clearSite(); });
        m.on('click', function (e) {
          mvSelectPaths([mem.path]);          // select just this job
          if (e && e.originalEvent) L.DomEvent.stopPropagation(e);
        });
      });
    });
    return;
  }

  // Overview: route line between site centroids + one aggregate dot per site.
  if (_sites.length >= 2) {
    L.polyline(_sites.map(function (s) { return [s.dot_4326[1], s.dot_4326[0]]; }), {
      color: '#f59e0b', weight: 2, opacity: 0.7, dashArray: '6,4',
    }).addTo(_group);
  }

  var markers = _sites.map(function (s) {
    var ll = [s.dot_4326[1], s.dot_4326[0]];
    var label = s.first_route_index != null ? s.first_route_index : s.index;
    var m = L.marker(ll, { icon: _circleIcon(label, s.member_count > 1) }).addTo(_group);
    m.on('mouseover', function () { _showSite(s, [s.dot_4326[1], s.dot_4326[0]]); });
    m.on('mouseout', function () { _clearSite(); });
    m.on('click', function (e) {
      mvSelectPaths(s.job_paths);             // select every job flown from this spot
      if (e && e.originalEvent) L.DomEvent.stopPropagation(e);
    });
    return m;
  });
  _pushOutDots(_map, markers, _sites, 22);
}

/**
 * Render launch sites onto the map. Returns an L.layerGroup (already added to
 * the map) that the caller owns; call clearLaunchSites(map) to tear down.
 */
export function drawLaunchSites(map, sites) {
  clearLaunchSites(map);
  _map = map;
  _sites = sites || [];
  _group = L.layerGroup().addTo(map);
  _render();
  _renderHandler = function () { _render(); };   // overview ⇄ detail on zoom; push-out re-fits
  map.on('zoomend', _renderHandler);
  return _group;
}

/** Remove all launch-site layers and detach map handlers (call on map-view exit). */
export function clearLaunchSites(map) {
  var m = map || _map;
  if (_renderHandler && m) { m.off('zoomend', _renderHandler); _renderHandler = null; }
  if (m) _hideConnector(m);
  _hideAnnounce();
  if (_group && m) m.removeLayer(_group);
  _group = null; _hoverGroup = null; _sites = null;
}
