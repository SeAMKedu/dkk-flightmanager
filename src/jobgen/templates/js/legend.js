// ── Warning rings ─────────────────────────────────────────────────────────────
function redrawRings() {
  if (lrs.rings) { map.removeLayer(lrs.rings); lrs.rings = null; }
  var warnR = parseFloat(document.getElementById('warn-radius').value) || 0;
  var row = document.getElementById('leg-rings-row');
  var lbl = document.getElementById('leg-rings-label');
  if (!previewData || !previewData.buildings || !warnR) {
    if (row) row.style.display = 'none';
    return;
  }
  var wg = L.layerGroup();
  var count = 0;
  previewData.buildings.forEach(function(b) {
    if (!b.is_keepout) return;
    var pt = centroid(b.geojson);
    if (!pt) return;
    L.circle(pt, {
      radius: warnR, color: '#ca8a04', weight: 1.5,
      fillColor: '#fef08a', fillOpacity: 0.25, dashArray: '4 4', interactive: false
    }).addTo(wg);
    count++;
  });
  if (!count) { if (row) row.style.display = 'none'; return; }
  lrs.rings = wg;
  if (lbl) lbl.textContent = warnR + ' m radius';
  var btn = document.getElementById('leg-rings');
  if (!btn || !btn.classList.contains('off')) lrs.rings.addTo(map);
  if (row) row.style.display = '';
}

// ── Legend ────────────────────────────────────────────────────────────────────

// Persisted user eye-toggle choices — survives preview refreshes and job switches.
// Keys are lrKey strings; values are booleans (true = visible).
var _legendUserVis = {};

(function initLegend() {
  var rows = [
    {btnId:'leg-dsm',      lrKey:'dsm',      rowId:'leg-dsm-row',   startOff:true},
    {btnId:'leg-areas',    lrKey:'areas',    rowId:null},
    {btnId:'leg-survey',   lrKey:'survey',   rowId:null},
    {btnId:'leg-vertices', lrKey:'vertices', rowId:null},
    {btnId:'leg-rings',    lrKey:'rings',    rowId:'leg-rings-row'},
    {btnId:'leg-ko',       lrKey:'ko',       rowId:'leg-ko-row'},
    {btnId:'leg-bldgs',    lrKey:'bldgs',    rowId:'leg-bldgs-row'},
    {btnId:'leg-plines',   lrKey:'plines',   rowId:'leg-plines-row'},
    {btnId:'leg-plko',     lrKey:'plko',     rowId:'leg-plko-row'},
    {btnId:'leg-zones',    lrKey:'zones',    rowId:'leg-zones-row'},
    {btnId:'leg-route',    lrKey:'route',    rowId:'leg-route-row'},
    {btnId:'leg-coverage', lrKey:'coverage', rowId:'leg-coverage-row', startOff:true},
  ];
  rows.forEach(function(r) {
    document.getElementById(r.btnId).addEventListener('click', function() {
      var layer = lrs[r.lrKey];
      if (!layer) return;
      var nowOff = this.classList.toggle('off');
      if (nowOff) { map.removeLayer(layer); } else { layer.addTo(map); }
      _legendUserVis[r.lrKey] = !nowOff;
    });
  });
  document.getElementById('legend').classList.add('inactive');
  window._legendRows = rows;
})();

// savedVis: optional {lrKey: bool} map of user-chosen visibility to restore.
// When omitted (e.g. first render, open-job), defaults are applied (startOff for DSM).
function resetLegend(savedVis) {
  window._legendRows.forEach(function(r) {
    var btn = document.getElementById(r.btnId);
    var hasLayer = !!lrs[r.lrKey];
    if (r.rowId) {
      document.getElementById(r.rowId).style.display = hasLayer ? '' : 'none';
    }
    if (!hasLayer) { btn.classList.add('off'); return; }
    var visible = (savedVis && r.lrKey in savedVis) ? savedVis[r.lrKey] : !r.startOff;
    btn.classList.toggle('off', !visible);
    if (!visible) map.removeLayer(lrs[r.lrKey]);
  });
  document.getElementById('legend').classList.remove('inactive');
}
