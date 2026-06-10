// ── Map view (in-place — reuses existing #map, hides #sb) ─────────────────────

var _mvMode = false;
var _mvFromEditor = false;   // true only when entering map view directly from the job editor
var _mvJobGroup = null;      // L.LayerGroup on the main map
var _mvLayers = [];          // [{path, layer, feature}]
var _mvHoverPopup = null;    // currently open hover popup
var _mvHoverTimer = null;    // deferred-close timer
var _mvSelected = new Set();
var _mvAllFeatures = [];
var _mvCurrentFolder = null;
var _DEFAULT_COLOR = '#3b82f6';
var _mvRouteLayer = null;    // L.layerGroup for route polyline + numbered markers
var _mvRouteVisible = true; // toggled by the Route button; on by default

function showFolderOnMap(e, folderName) {
  e.stopPropagation();
  var f = folderName || null;
  if (_mvMode && _mvCurrentFolder === f) { closeMapView(); return; }
  openMapView(f);
}

function openMapView(folderFilter) {
  var _comingFromEditor = _mvFromEditor && _activeJobFolder === (folderFilter || null);
  var _skipFit = _comingFromEditor;
  _mvFromEditor = false;

  _mvMode = true;
  _mvCurrentFolder = folderFilter || null;
  if (editMode) saveEdit();

  Object.values(lrs).forEach(function(l){ if (l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null, route:null, coverage:null};
  editLayers.clearLayers();
  if (_takeoffMarker) map.removeLayer(_takeoffMarker);
  _hideVlos();

  document.getElementById('sb').classList.add('mv-hidden');
  document.getElementById('legend').classList.add('mv-hidden');
  document.getElementById('sp').classList.add('mv-hidden');
  document.getElementById('mv-status-legend').classList.add('visible');
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.folder === (folderFilter || ''));
  });

  _mvSelected.clear();
  _mvUpdateSelBar();

  if (!_comingFromEditor) _mvRouteVisible = true;
  var routeBtn = document.getElementById('mv-route-btn');
  if (routeBtn) routeBtn.classList.toggle('active', _mvRouteVisible);

  if (!_mvJobGroup) { _mvJobGroup = L.layerGroup().addTo(map); }
  _mvLoad(folderFilter, _skipFit);
}

function closeMapView() {
  if (!_mvMode) return;
  _mvMode = false;
  _mvCurrentFolder = null;
  clearTimeout(_mvHoverTimer);
  if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
  destroyBatteryTimeline();
  _mvClearLayers();
  if (_mvRouteLayer) { _mvRouteLayer.remove(); _mvRouteLayer = null; }
  _mvSelected.forEach(function(path) {
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) card.classList.remove('selected');
  });
  _mvSelected.clear();
  _mvUpdateSelBar();

  document.getElementById('sb').classList.remove('mv-hidden');
  document.getElementById('legend').classList.remove('mv-hidden');
  document.getElementById('sp').classList.remove('mv-hidden');
  document.getElementById('mv-status-legend').classList.remove('visible');
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) { btn.classList.remove('active'); });
  map.closePopup();
}

async function _mvLoad(folderFilter, skipFit) {
  try {
    var r = await fetch('/api/jobs/geojson');
    if (!r.ok) return;
    var fc = await r.json();
    _mvAllFeatures = fc.features || [];
    _mvApplyFilter(folderFilter, skipFit);
    _mvDrawRoute();
  } catch(e) { console.error('[mapview]', e); }
}

function _mvDrawRoute() {
  if (_mvRouteLayer) { _mvRouteLayer.remove(); _mvRouteLayer = null; }
  if (!_mvRouteVisible || !_mvMode) return;

  var features = _mvAllFeatures.filter(function(f){ return (f.properties.folder || null) === _mvCurrentFolder; });

  var routable = features.filter(function(f){ return f.properties.takeoff_point_4326 && !f.properties.skipped; });
  routable.sort(function(a, b) {
    var pa = a.properties, pb = b.properties;
    var soA = pa.sort_order, soB = pb.sort_order;
    if (soA != null && soB != null) return soA - soB;
    if (soA != null) return -1;
    if (soB != null) return 1;
    return 0;
  });

  if (routable.length < 2) return;

  var latlngs = routable.map(function(f){
    var tp = f.properties.takeoff_point_4326;
    return [tp[1], tp[0]];
  });

  _mvRouteLayer = L.layerGroup().addTo(map);

  L.polyline(latlngs, {
    color: '#f59e0b', weight: 2, opacity: 0.7, dashArray: '6,4',
  }).addTo(_mvRouteLayer);

  routable.forEach(function(f, i) {
    var tp = f.properties.takeoff_point_4326;
    var n = i + 1;
    var icon = L.divIcon({
      className: '',
      html: '<div style="background:#f59e0b;color:#000;font-size:10px;font-weight:700;'
        + 'width:18px;height:18px;border-radius:50%;display:flex;align-items:center;'
        + 'justify-content:center;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.5)">'
        + n + '</div>',
      iconSize: [18, 18], iconAnchor: [9, 9],
    });
    L.marker([tp[1], tp[0]], {icon: icon, interactive: false}).addTo(_mvRouteLayer);
  });
}

function toggleMvRoute() {
  _mvRouteVisible = !_mvRouteVisible;
  var btn = document.getElementById('mv-route-btn');
  if (btn) btn.classList.toggle('active', _mvRouteVisible);
  _mvDrawRoute();
}

async function _mvRefreshRouteData() {
  try {
    var r = await fetch('/api/jobs/geojson');
    if (!r.ok) return;
    var fc = await r.json();
    _mvAllFeatures = fc.features || [];
    _mvDrawRoute();
  } catch(e) { console.error('[mv-refresh-route]', e); }
}

function _mvApplyFilter(folderFilter, skipFit) {
  _mvClearLayers();
  var bounds = [];
  var features = _mvAllFeatures.filter(function(f){ return (f.properties.folder || null) === (folderFilter || null); });
  features.forEach(function(f) {
    if (!f.geometry) return;
    var layer = _mvMakeLayer(f);
    if (layer) {
      _mvJobGroup.addLayer(layer);
      _mvLayers.push({path: f.properties.path, layer: layer, feature: f});
      try { bounds.push(layer.getBounds()); } catch(e) {}
    }
  });
  if (bounds.length && !skipFit) {
    var combined = bounds[0];
    bounds.forEach(function(b){ combined = combined.extend(b); });
    map.fitBounds(combined, {padding: [40, 40]});
  }
  _mvDrawRoute();
  showBatteryTimeline();
}

function _mvClearLayers() {
  _mvLayers.forEach(function(item){ if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); });
  _mvLayers = [];
}

function _mvMakeLayer(feature) {
  var p = feature.properties;
  var color = p.color || _DEFAULT_COLOR;
  var dashArray = _mvDash(p);
  var geom = feature.geometry;
  try {
    var coords;
    if (geom.type === 'Polygon') {
      coords = geom.coordinates[0].map(function(c){ return [c[1], c[0]]; });
    } else if (geom.type === 'MultiPolygon') {
      coords = geom.coordinates.map(function(poly){
        return poly[0].map(function(c){ return [c[1], c[0]]; });
      });
    } else { return null; }

    var layer = L.polygon(coords, {
      color: color, weight: 2.5, fillColor: color, fillOpacity: 0.18, dashArray: dashArray,
    });

    if (p.skipped) { layer.setStyle({opacity: 0.35, fillOpacity: 0.07}); }

    layer.on('mouseover', function(e) {
      clearTimeout(_mvHoverTimer);
      _mvOpenHoverPopup(e.latlng, p);
    });
    layer.on('mouseout', function() {
      _mvHoverTimer = setTimeout(function() {
        if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
      }, 150);
    });
    layer.on('click', function(e) {
      L.DomEvent.stopPropagation(e);
      _mvToggleSel(p.path);
    });
    layer.on('dblclick', function(e) {
      L.DomEvent.stopPropagation(e);
      mvOpenJob(p.path);
    });
    return layer;
  } catch(e) { return null; }
}

function _mvDash(p) {
  if (p.status === 'failed') return '2, 6';
  if (p.flight_ready === true) return null;
  if (p.needs_review === true) return '10, 5';
  if (p.untouched) return '4, 4';
  return null;
}

function _mvOpenHoverPopup(latlng, p) {
  if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
  var statusChip = p.flight_ready === true ? '<span style="color:#4ade80">✓ Ready</span>'
    : p.needs_review === true ? '<span style="color:#fb923c">⚠ Review</span>'
    : p.untouched ? '<span style="color:#64748b">New</span>' : '<span>—</span>';
  var area = p.area_ha != null ? p.area_ha.toFixed(1) + ' ha' : '';
  var areaLost = '';
  if (p.area_lost_pct != null && Math.abs(p.area_lost_pct) >= 0.05) {
    var sign = p.area_lost_pct > 0 ? '−' : '+';
    var col  = p.area_lost_pct > 0 ? '#fb923c' : '#4ade80';
    areaLost = ' <span style="color:' + col + '">' + sign + Math.abs(p.area_lost_pct).toFixed(1) + '%</span>';
  }
  var _ic = function(id, col) {
    return '<svg class="mv-ic"' + (col ? ' style="color:' + col + ';opacity:1"' : '') + '><use href="#' + id + '"/></svg>';
  };
  var flightParts = [];
  if (p.height_m != null)      flightParts.push(_ic('ic-altitude') + ' ' + p.height_m.toFixed(0) + ' m');
  if (p.strip_speed_ms != null) flightParts.push(_ic('ic-gauge') + ' ' + (p.strip_speed_ms * 3.6).toFixed(1) + ' km/h');
  if (p.flight_time_min != null) flightParts.push(_ic('ic-timer') + ' ' + Math.round(p.flight_time_min) + ' min');
  if (p.over_one_battery)       flightParts.push(_ic('ic-battery-warn', '#fb923c') + ' <span style="color:#fb923c">2+ bat</span>');
  var flightInfo = flightParts.join('<span style="color:#475569"> · </span>');
  var photoInfo = p.photo_count != null ? _ic('ic-camera') + ' ' + p.photo_count + ' photos' : '';
  var skipLabel = p.skipped ? '⊘ Unskip' : '⊘ Skip';
  var html = '<div class="mv-tt-inner">'
    + '<div class="mv-tt-name">' + (p.skipped ? '⊘ ' : '') + escHtml(p.name)
    + (p.folder ? ' <span class="mv-tt-folder">(' + escHtml(p.folder) + ')</span>' : '') + '</div>'
    + '<div class="mv-tt-meta">' + statusChip + (area ? ' · ' + area : '') + areaLost + (p.skipped ? ' · <span style="color:#94a3b8">skipped</span>' : '') + '</div>'
    + (flightInfo ? '<div class="mv-tt-flight">' + flightInfo + '</div>' : '')
    + (photoInfo ? '<div class="mv-tt-flight">' + photoInfo + '</div>' : '')
    + '<div class="mv-tt-actions">'
    + '<button onclick="mvToggleSkip(\'' + escHtml(p.path) + '\',' + !!p.skipped + ')">' + skipLabel + '</button>'
    + '<button class="mv-tt-del" onclick="mvDeleteJob(\'' + escHtml(p.path) + '\',\'' + escHtml(p.name) + '\')">✕ Delete</button>'
    + '</div></div>';
  _mvHoverPopup = L.popup({
    closeButton: false, minWidth: 160, className: 'mv-popup',
    autoClose: false, closeOnClick: true, offset: [0, -4]
  }).setLatLng(latlng).setContent(html).openOn(map);
  setTimeout(function() {
    var el = _mvHoverPopup && _mvHoverPopup.getElement();
    if (!el) return;
    el.addEventListener('mouseenter', function() { clearTimeout(_mvHoverTimer); });
    el.addEventListener('mouseleave', function() {
      if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
    });
  }, 30);
}

// Legacy popup helper (currently unused but kept for potential future use)
function _mvShowPopup(latlng, feature, layer) {
  var p = feature.properties;
  var statusChip = p.flight_ready === true ? '<span style="color:#4ade80">✓ Ready</span>'
    : p.needs_review === true ? '<span style="color:#fb923c">⚠ Review</span>'
    : p.untouched ? '<span style="color:#64748b">New</span>'
    : '<span style="color:#94a3b8">—</span>';
  var area = p.area_ha != null ? p.area_ha.toFixed(1) + ' ha' : '';
  var skipLabel = p.skipped ? '⊘ Unskip' : '⊘ Skip';
  var html = '<div style="font-size:12px;line-height:1.6;min-width:150px">'
    + '<b style="font-size:13px">' + escHtml(p.name) + '</b><br>'
    + (p.folder ? '<span style="font-size:10px;color:#94a3b8">' + escHtml(p.folder) + '</span><br>' : '')
    + statusChip + (area ? ' &nbsp;' + area : '')
    + '<div style="display:flex;gap:5px;margin-top:8px">'
    + '<button onclick="mvOpenJob(\'' + escHtml(p.path) + '\')" style="flex:1;padding:4px;font-size:11px;background:#3b82f6;color:#fff;border:none;border-radius:3px;cursor:pointer">Open</button>'
    + '<button onclick="mvToggleSkip(\'' + escHtml(p.path) + '\',' + !!p.skipped + ')" style="flex:1;padding:4px;font-size:11px;background:#475569;color:#fff;border:none;border-radius:3px;cursor:pointer">' + skipLabel + '</button>'
    + '<button onclick="mvDeleteJob(\'' + escHtml(p.path) + '\',\'' + escHtml(p.name) + '\')" style="padding:4px 6px;font-size:11px;background:#dc2626;color:#fff;border:none;border-radius:3px;cursor:pointer">✕</button>'
    + '</div></div>';
  L.popup({closeButton: true, minWidth: 170, className: 'mv-popup'}).setLatLng(latlng).setContent(html).openOn(map);
}

function mvOpenJob(path) {
  map.closePopup();
  closeMapView();
  openJob(path);
}

async function mvToggleSkip(path, currentSkipped) {
  try {
    var r = await fetch(jobApiUrl(path), {
      method: 'PATCH', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({skipped: !currentSkipped})
    });
    if (!r.ok) { showError('Could not update job'); return; }
    if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
    var geoR = await fetch('/api/jobs/geojson');
    if (geoR.ok) {
      _mvAllFeatures = (await geoR.json()).features || [];
      _mvApplyFilter(_mvCurrentFolder, true);
    }
    loadJobsList();
  } catch(e) { showError('Failed: ' + e.message); }
}

async function mvDeleteJob(path, name) {
  if (!window.confirm('Delete job "' + name + '"?')) return;
  try {
    var r = await fetch(jobApiUrl(path), {method: 'DELETE'});
    if (!r.ok) { showError('Delete failed'); return; }
    map.closePopup();
    _mvLayers = _mvLayers.filter(function(item) {
      if (item.path === path) { _mvJobGroup.removeLayer(item.layer); return false; }
      return true;
    });
    _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== path; });
    if (_activeJob === path) { _activeJob = null; _activeJobFolder = null; }
    loadJobsList();
  } catch(e) { showError('Delete failed: ' + e.message); }
}

// ── Map view multi-select ──────────────────────────────────────────────────────
function _mvToggleSel(path) {
  var item = _mvLayers.find(function(i){ return i.path === path; });
  if (!item) return;
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
  if (_mvSelected.has(path)) {
    _mvSelected.delete(path);
    var origColor = item.feature.properties.color || _DEFAULT_COLOR;
    item.layer.setStyle({weight: 2.5, opacity: 1, color: origColor, fillColor: origColor});
    if (card) card.classList.remove('selected');
  } else {
    _mvSelected.add(path);
    item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    if (card) {
      card.classList.add('selected');
      if (_mvSelected.size === 1) card.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
  }
  _mvUpdateSelBar();
}

function mvClearSel() {
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (item) { var c = item.feature.properties.color || _DEFAULT_COLOR; item.layer.setStyle({weight: 2.5, opacity: 1, color: c, fillColor: c}); }
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) card.classList.remove('selected');
  });
  _mvSelected.clear();
  _mvUpdateSelBar();
}

function _mvUpdateSelBar() {
  var n = _mvSelected.size;
  document.getElementById('mv-actions').classList.toggle('visible', _mvMode);
  document.getElementById('mv-sel-count').textContent = n + ' selected';
  document.getElementById('mv-merge-btn').disabled = n < 2;
  var openBtn = document.getElementById('mv-open-btn');
  if (openBtn) {
    openBtn.style.display = n === 1 ? '' : 'none';
    if (n === 1) openBtn.dataset.path = Array.from(_mvSelected)[0];
  }
  var totalArea = 0, hasArea = false;
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (item && item.feature.properties.area_ha != null) {
      totalArea += item.feature.properties.area_ha;
      hasArea = true;
    }
  });
  var areaEl = document.getElementById('mv-area-total');
  if (areaEl) areaEl.textContent = (n > 0 && hasArea) ? '· ' + totalArea.toFixed(1) + ' ha' : '';
  showBatteryTimeline();
}

function mvMerge() {
  _selectedJobs.clear(); _selectedMeta.clear();
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (item) {
      var p = item.feature.properties;
      _selectedJobs.add(path);
      _selectedMeta.set(path, {path: path, name: p.name, folder: p.folder, untouched: p.untouched});
    }
  });
  _updateSelBar();
  closeMapView();
  openMergeModal();
}

async function mvBulkMove() {
  var paths = Array.from(_mvSelected);
  var metas = paths.map(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    return item ? {path: path, name: item.feature.properties.name, folder: item.feature.properties.folder} : null;
  }).filter(Boolean);
  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el){
    var n = el.textContent.trim(); if (n) folderNames.push(n);
  });
  var dest = window.prompt('Move to folder (blank = root, or folder name):\n\nAvailable: ' + (folderNames.join(', ') || '(none)'));
  if (dest === null) return;
  dest = dest.trim() || null;
  for (var i = 0; i < metas.length; i++) {
    try {
      await fetch(jobApiUrl(metas[i].path, '/move'), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({folder: dest})
      });
    } catch(e) { showError('Move failed: ' + e.message); }
  }
  mvClearSel();
  await loadJobsList();
  openMapView(dest);
}

async function mvBulkDelete() {
  var n = _mvSelected.size;
  if (!window.confirm('Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '?')) return;
  var paths = Array.from(_mvSelected);
  for (var i = 0; i < paths.length; i++) {
    try {
      await fetch(jobApiUrl(paths[i]), {method: 'DELETE'});
      _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== paths[i]; });
      _mvLayers = _mvLayers.filter(function(item) {
        if (item.path === paths[i]) { if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); return false; }
        return true;
      });
    } catch(e) { showError('Delete failed: ' + e.message); }
  }
  mvClearSel();
  loadJobsList();
}
