// ── Takeoff marker & VLOS rings ───────────────────────────────────────────────

import { map } from '../map/map-init.js';
import { markDirty } from '../core/dirty-tracking.js';
import { st } from '../core/state.js';

// Shared takeoff state lives in st.takeoff: auto ([lng,lat] suggested by server),
// pt ([lng,lat] current, auto or user-dragged), userMoved (true once dragged),
// vlosRange (metres, from /api/config). The Leaflet handles below stay
// module-local — they're per-view objects, not shared application state.
var _takeoffMarker = null;      // Leaflet draggable marker
var _vlosOuter   = null;        // L.circle — full VLOS range ring
var _vlosInner   = null;        // L.circle — half VLOS range ring
var _vlosVisible = false;       // toggled by click on marker

// Helper for map-view.js to clear takeoff without importing private vars
export function clearTakeoffForMapView() {
  if (_takeoffMarker) map.removeLayer(_takeoffMarker);
  _hideVlos();
}

function _vlosCircleOpts(full) {
  return full
    ? {radius: st.takeoff.vlosRange,       color:'#ffffff', weight:2,   dashArray:'8 6', fillOpacity:0.08, fillColor:'#ffffff', interactive:false}
    : {radius: st.takeoff.vlosRange / 2,   color:'#ffffff', weight:1.5, dashArray:'4 5', fillOpacity:0.05, fillColor:'#ffffff', interactive:false};
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
  st.takeoff.pt = lngLat;
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
    st.takeoff.userMoved = true;
    _showVlos(this.getLatLng()); _vlosVisible = true;
  });
  _takeoffMarker.on('drag', function() { _moveVlos(this.getLatLng()); });
  _takeoffMarker.on('dragend', function() {
    var ll = _takeoffMarker.getLatLng();
    st.takeoff.pt = [ll.lng, ll.lat];
    _hideVlos();
    markDirty();
  });
  if (row) row.style.display = '';
  document.getElementById('takeoff-recalc-btn').disabled = false;
  var btn = document.getElementById('leg-takeoff');
  if (btn && btn.classList.contains('off')) map.removeLayer(_takeoffMarker);
}

export function recalcTakeoff() {
  if (!st.takeoff.auto) return;
  st.takeoff.userMoved = false;
  _renderTakeoffMarker(st.takeoff.auto);
}

export function _clearTakeoff() {
  st.takeoff.auto = null; st.takeoff.pt = null; st.takeoff.userMoved = false;
  _renderTakeoffMarker(null);
  document.getElementById('takeoff-recalc-btn').disabled = true;
}

// Eye-toggle for takeoff marker (not in lrs, handled separately)
document.getElementById('leg-takeoff').addEventListener('click', function() {
  if (!_takeoffMarker) return;
  if (this.classList.toggle('off')) { map.removeLayer(_takeoffMarker); _hideVlos(); }
  else _takeoffMarker.addTo(map);
});
