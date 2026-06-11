// ── Map initialisation ────────────────────────────────────────────────────────

import { st } from './state.js';
// Circular imports for event handlers (safe — only called at runtime, not import-time)
import { markDirty } from './dirty-tracking.js';
import { _setEditedPoly } from './form-controls.js';

export var map = L.map('map', {preferCanvas:true}).setView([64.5, 26.0], 5);
var _baseOSM = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap', maxZoom:19});
var _baseOrto = null;
var _baseLayerCtrl = null;
var _mmlApiKey = '';           // set from /api/config in init()
_baseOSM.addTo(map);

export function _initBaseLayers(mmlKey) {
  if (!mmlKey) return;
  var url = 'https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts'
    + '?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0'
    + '&LAYER=ortokuva&STYLE=default&TILEMATRIXSET=WGS84_Pseudo-Mercator'
    + '&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg'
    + '&api-key=' + mmlKey;
  // maxNativeZoom: MML WGS84_Pseudo-Mercator ortokuva tops out at zoom 15;
  // Leaflet upscales those tiles for zooms 16–19 rather than showing empty tiles.
  // maxNativeZoom: MML WGS84_Pseudo-Mercator ortokuva tops out at zoom 15;
  // Leaflet upscales those tiles for higher zooms rather than showing empty tiles.
  _baseOrto = L.tileLayer(url, {attribution:'&copy; <a href="https://maanmittauslaitos.fi">MML</a>', maxZoom:21, maxNativeZoom:15});
  if (_baseLayerCtrl) map.removeControl(_baseLayerCtrl);
  _baseLayerCtrl = L.control.layers({'Map': _baseOSM, 'Ortho': _baseOrto}, null, {position:'topleft', collapsed:true}).addTo(map);
}

export function resetMapToUserLocation() {
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      function(pos) { map.setView([pos.coords.latitude, pos.coords.longitude], 15); },
      function()    { map.setView([64.5, 26.0], 5); }
    );
  } else {
    map.setView([64.5, 26.0], 5);
  }
}
resetMapToUserLocation();

// DSM pane sits below overlayPane (400) so vectors always render on top
map.createPane('dsmPane');
map.getPane('dsmPane').style.zIndex = 350;
map.getPane('dsmPane').style.pointerEvents = 'none';

export var editLayers = new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({draw:false, edit:{featureGroup:editLayers, remove:false}}));

map.on(L.Draw.Event.EDITED, function(e) {
  e.layers.eachLayer(function(l) {
    _setEditedPoly(layerGeom(l)); markDirty();
  });
  st.editMode = false;
  map.doubleClickZoom.enable();
  if (lrs.survey) lrs.survey.addTo(map);
});

// lrs is exported as a let so other modules can read its properties.
// resetLrs() is provided for callers that need to reset all properties
// (since imported bindings can't be reassigned from outside this module).
export let lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, plines:null, plko:null, zones:null, route:null, coverage:null};

export function resetLrs() {
  Object.keys(lrs).forEach(function(k){ lrs[k] = null; });
}

export function layerGeom(layer) {
  var lls = layer.getLatLngs();
  var ring = (Array.isArray(lls[0]) ? lls[0] : lls).map(function(ll){return [ll.lng,ll.lat];});
  ring.push(ring[0]);
  return {type:'Polygon', coordinates:[ring]};
}
