// ── Map setup ─────────────────────────────────────────────────────────────────
var map = L.map('map').setView([CENTER_LAT, CENTER_LON], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 19
}).addTo(map);

map.invalidateSize();

// Pins pane sits above the default overlayPane (z 400) so dots always render on top
map.createPane('pinsPane');
map.getPane('pinsPane').style.zIndex = 450;

var surveyGroup = L.layerGroup().addTo(map);
var yellowGroup = L.layerGroup().addTo(map);
var redGroup    = L.layerGroup().addTo(map);
var parcelGroup = L.layerGroup().addTo(map);
var vertexGroup = L.layerGroup().addTo(map);
var pinsGroup   = L.layerGroup().addTo(map);

// ── DSM overlay ───────────────────────────────────────────────────────────────
if (DSM_B64) {
  map.createPane('dsmPane');
  map.getPane('dsmPane').style.zIndex = 350;
  map.getPane('dsmPane').style.pointerEvents = 'none';
  var dsmGroup = L.layerGroup().addTo(map);
  L.imageOverlay(
    'data:image/png;base64,' + DSM_B64,
    [[DSM_BOUNDS[1], DSM_BOUNDS[0]], [DSM_BOUNDS[3], DSM_BOUNDS[2]]],
    {opacity: 0.65, interactive: false, pane: 'dsmPane'}
  ).addTo(dsmGroup);
  map.removeLayer(dsmGroup);
  eyeTog('eye-dsm',
    function() { dsmGroup.addTo(map); },
    function() { map.removeLayer(dsmGroup); });
}

// ── Survey polygon ────────────────────────────────────────────────────────────
var surveyLayer = L.geoJSON(surveyData, {
  style: { color: '#16a34a', weight: 2, fillColor: '#4ade80', fillOpacity: 0.35 }
}).addTo(surveyGroup);

if (surveyLayer.getLayers().length > 0) {
  map.fitBounds(surveyLayer.getBounds().pad(0.15));
}

// ── Warning radius circles (keep-out buildings only) ─────────────────────────
pins.forEach(function(p) {
  if (!p.keepout) return;
  L.circle([p.lat, p.lon], {
    radius: PREVIEW_RADIUS, color: '#ca8a04', weight: 1,
    fillColor: '#fef08a', fillOpacity: 0.25, dashArray: '4 4'
  }).addTo(yellowGroup);
});

// ── Keep-out circles ──────────────────────────────────────────────────────────
pins.forEach(function(p) {
  if (!p.keepout) return;
  L.circle([p.lat, p.lon], {
    radius: HOME_BUFFER, color: '#dc2626', weight: 1,
    fillColor: '#fca5a5', fillOpacity: 0.20, dashArray: '4 4'
  }).addTo(redGroup);
});

// ── Original parcel outlines ──────────────────────────────────────────────────
parcels.forEach(function(f) {
  L.geoJSON(f, {
    style: { color: '#374151', weight: 1.5, dashArray: '5 5', fill: false }
  }).addTo(parcelGroup);
});

// ── Survey polygon vertex dots ────────────────────────────────────────────────
(function() {
  var geom = surveyData.geometry;
  var rings = geom.type === 'Polygon' ? geom.coordinates
            : geom.type === 'MultiPolygon' ? geom.coordinates.reduce(function(a, poly) { return a.concat(poly); }, [])
            : [];
  var seen = {};
  rings.forEach(function(ring) {
    ring.forEach(function(coord) {
      var key = coord[0].toFixed(7) + ',' + coord[1].toFixed(7);
      if (seen[key]) return;
      seen[key] = true;
      L.circleMarker([coord[1], coord[0]], {
        radius: 3, color: '#1d4ed8', weight: 1,
        fillColor: '#93c5fd', fillOpacity: 0.9, interactive: false
      }).addTo(vertexGroup);
    });
  });
})();

// ── Building pins ─────────────────────────────────────────────────────────────
pins.forEach(function(p) {
  L.circleMarker([p.lat, p.lon], {
    radius: 7, color: '#fff', weight: 1.5,
    fillColor: p.colour, fillOpacity: 0.9,
    pane: 'pinsPane'
  }).bindPopup(p.label).addTo(pinsGroup);
});

// ── UAS restriction zones ─────────────────────────────────────────────────────
zones.forEach(function(z) {
  L.geoJSON(JSON.parse(z.geojson), {
    style: { color: '#f97316', weight: 2, fillColor: '#fed7aa', fillOpacity: 0.4 }
  }).bindPopup('<b>' + z.name + '</b><br>' + z.type).addTo(map);
});

// ── Eye-button toggles ────────────────────────────────────────────────────────
function eyeTog(btnId, showFn, hideFn) {
  document.getElementById(btnId).addEventListener('click', function() {
    if (this.classList.toggle('off')) { hideFn(); } else { showFn(); }
  });
}
eyeTog('eye-parcel',
  function() { parcelGroup.addTo(map); },
  function() { map.removeLayer(parcelGroup); });
eyeTog('eye-survey',
  function() { surveyGroup.addTo(map); },
  function() { map.removeLayer(surveyGroup); });
eyeTog('eye-yellow-c',
  function() { yellowGroup.addTo(map); },
  function() { map.removeLayer(yellowGroup); });
eyeTog('eye-red-c',
  function() { redGroup.addTo(map); },
  function() { map.removeLayer(redGroup); });
eyeTog('eye-vertices',
  function() { vertexGroup.addTo(map); },
  function() { map.removeLayer(vertexGroup); });
eyeTog('eye-pins',
  function() { pinsGroup.addTo(map); },
  function() { map.removeLayer(pinsGroup); });
