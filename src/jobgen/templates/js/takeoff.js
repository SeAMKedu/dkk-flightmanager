// ── Takeoff marker & VLOS rings ───────────────────────────────────────────────

import { map } from './map-init.js';

var _takeoffAuto = null;        // [lng, lat] suggested by server
var _takeoffPt   = null;        // [lng, lat] current (auto or user-dragged)
var _takeoffUserMoved = false;  // true once user drags the marker
var _takeoffMarker = null;      // Leaflet draggable marker
var _vlosRange   = 300;         // metres, set from /api/config
var _vlosOuter   = null;        // L.circle — full VLOS range ring
var _vlosInner   = null;        // L.circle — half VLOS range ring
var _vlosVisible = false;       // toggled by click on marker

export function getTakeoffAuto() { return _takeoffAuto; }
export function setTakeoffAuto(v) { _takeoffAuto = v; }
export function getTakeoffPt() { return _takeoffPt; }
export function setTakeoffPt(v) { _takeoffPt = v; }
export function getTakeoffUserMoved() { return _takeoffUserMoved; }
export function setTakeoffUserMoved(v) { _takeoffUserMoved = v; }
export function setVlosRange(v) { _vlosRange = v; }

// Helper for map-view.js to clear takeoff without importing private vars
export function clearTakeoffForMapView() {
  if (_takeoffMarker) map.removeLayer(_takeoffMarker);
  _hideVlos();
}

function _vlosCircleOpts(full) {
  return full
    ? {radius: _vlosRange,       color:'#ffffff', weight:2,   dashArray:'8 6', fillOpacity:0.08, fillColor:'#ffffff', interactive:false}
    : {radius: _vlosRange / 2,   color:'#ffffff', weight:1.5, dashArray:'4 5', fillOpacity:0.05, fillColor:'#ffffff', interactive:false};
}

function _showVlos(ll) {
  _hideVlos();
  _vlosOuter = L.circle(ll, _vlosCircleOpts(true)).addTo(map);
  _vlosInner = L.circle(ll, _vlosCircleOpts(false)).addTo(map);
}

export function _hideVlos() {
  if (_vlosOuter) { map.removeLayer(_vlosOuter); _vlosOuter = null; }
  if (_vlosInner) { map.removeLayer(_vlosInner); _vlosInner = null; }
  _vlosVisible = false;
}

function _moveVlos(ll) {
  if (_vlosOuter) _vlosOuter.setLatLng(ll);
  if (_vlosInner) _vlosInner.setLatLng(ll);
}

export function _renderTakeoffMarker(lngLat) {
  if (_takeoffMarker) { map.removeLayer(_takeoffMarker); _takeoffMarker = null; }
  _hideVlos();
  var row = document.getElementById('leg-takeoff-row');
  if (!lngLat) { if (row) row.style.display = 'none'; return; }
  _takeoffPt = lngLat;
  var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
    + '<line x1="4" y1="4" x2="20" y2="20" stroke="#0f172a" stroke-width="5" stroke-linecap="round"/>'
    + '<line x1="20" y1="4" x2="4" y2="20" stroke="#0f172a" stroke-width="5" stroke-linecap="round"/>'
    + '<line x1="4" y1="4" x2="20" y2="20" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>'
    + '<line x1="20" y1="4" x2="4" y2="20" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>'
    + '</svg>';
  _takeoffMarker = L.marker([lngLat[1], lngLat[0]], {
    icon: L.divIcon({className:'takeoff-icon', html:svg, iconSize:[24,24], iconAnchor:[12,12], tooltipAnchor:[0,-14]}),
    draggable: true,
    zIndexOffset: 1000,
  }).addTo(map);
  _takeoffMarker.bindTooltip('Takeoff / Landing', {permanent:false, direction:'top', className:'takeoff-tooltip'});
  _takeoffMarker.on('click', function() {
    if (_vlosVisible) { _hideVlos(); } else { _showVlos(this.getLatLng()); _vlosVisible = true; }
  });
  _takeoffMarker.on('dragstart', function() {
    _takeoffUserMoved = true;
    _showVlos(this.getLatLng()); _vlosVisible = true;
  });
  _takeoffMarker.on('drag', function() { _moveVlos(this.getLatLng()); });
  _takeoffMarker.on('dragend', function() {
    var ll = _takeoffMarker.getLatLng();
    _takeoffPt = [ll.lng, ll.lat];
    _hideVlos();
  });
  if (row) row.style.display = '';
  document.getElementById('takeoff-recalc-btn').disabled = false;
  var btn = document.getElementById('leg-takeoff');
  if (btn && btn.classList.contains('off')) map.removeLayer(_takeoffMarker);
}

export function recalcTakeoff() {
  if (!_takeoffAuto) return;
  _takeoffUserMoved = false;
  _renderTakeoffMarker(_takeoffAuto);
}

export function _clearTakeoff() {
  _takeoffAuto = null; _takeoffPt = null; _takeoffUserMoved = false;
  _renderTakeoffMarker(null);
  document.getElementById('takeoff-recalc-btn').disabled = true;
}

// Eye-toggle for takeoff marker (not in lrs, handled separately)
document.getElementById('leg-takeoff').addEventListener('click', function() {
  if (!_takeoffMarker) return;
  if (this.classList.toggle('off')) { map.removeLayer(_takeoffMarker); _hideVlos(); }
  else _takeoffMarker.addTo(map);
});
