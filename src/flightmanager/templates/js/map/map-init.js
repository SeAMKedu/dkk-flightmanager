// ── Map initialisation ────────────────────────────────────────────────────────

import { st } from '../core/state.js';
// Circular imports for event handlers (safe — only called at runtime, not import-time)
import { markDirty } from '../core/dirty-tracking.js';
import { _setEditedPoly } from '../editor/form-controls.js';

export var map = L.map('map', {preferCanvas:true}).setView([64.5, 26.0], 5);
var _baseOSM = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap', maxZoom:19});
var _baseOrto = null;
var _baseLayerCtrl = null;
_baseOSM.addTo(map);

export function _initBaseLayers(mmlKey) {
  if (!mmlKey) return;
  var url = 'https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts'
    + '?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0'
    + '&LAYER=ortokuva&STYLE=default&TILEMATRIXSET=WGS84_Pseudo-Mercator'
    + '&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg'
    + '&api-key=' + mmlKey;
  // maxNativeZoom: MML WGS84_Pseudo-Mercator ortokuva tops out at zoom 15;
  // Leaflet upscales those tiles for higher zooms rather than showing empty tiles.
  _baseOrto = L.tileLayer(url, {attribution:'&copy; <a href="https://maanmittauslaitos.fi">MML</a>', maxZoom:21, maxNativeZoom:15});
  if (_baseLayerCtrl) map.removeControl(_baseLayerCtrl);

  var BaseLayerControl = L.Control.extend({
    options: {position: 'topleft'},
    onAdd: function() {
      var wrap = L.DomUtil.create('div', 'base-layer-ctrl');
      L.DomUtil.create('div', 'base-layer-icon', wrap);
      var panel = L.DomUtil.create('div', 'base-layer-panel', wrap);
      var btnMap  = L.DomUtil.create('button', 'base-layer-btn active', panel);
      var btnOrto = L.DomUtil.create('button', 'base-layer-btn', panel);
      btnMap.textContent  = 'Map';
      btnOrto.textContent = 'Ortho';
      btnMap.title  = 'OpenStreetMap';
      btnOrto.title = 'MML Ortokuva';
      L.DomEvent.disableClickPropagation(wrap);
      L.DomEvent.on(btnMap, 'click', function() {
        if (map.hasLayer(_baseOrto)) { map.removeLayer(_baseOrto); map.addLayer(_baseOSM); }
        btnMap.classList.add('active');
        btnOrto.classList.remove('active');
      });
      L.DomEvent.on(btnOrto, 'click', function() {
        if (map.hasLayer(_baseOSM)) { map.removeLayer(_baseOSM); map.addLayer(_baseOrto); }
        btnOrto.classList.add('active');
        btnMap.classList.remove('active');
      });
      return wrap;
    }
  });
  _baseLayerCtrl = new BaseLayerControl().addTo(map);
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

// Remove every tracked overlay layer from the map and reset the lrs registry.
// The job-open / new-job / map-view / re-render paths all start from this same
// clean slate, so it lives in one place rather than being re-spelled at each.
export function clearAllLayers() {
  Object.values(lrs).forEach(function(l){ if (l) map.removeLayer(l); });
  resetLrs();
}

export function layerGeom(layer) {
  var lls = layer.getLatLngs();
  var ring = (Array.isArray(lls[0]) ? lls[0] : lls).map(function(ll){return [ll.lng,ll.lat];});
  ring.push(ring[0]);
  return {type:'Polygon', coordinates:[ring]};
}
