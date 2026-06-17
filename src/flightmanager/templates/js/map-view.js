// ── Map view (in-place — reuses existing #map, hides #sb) ─────────────────────

import { st } from './state.js';
import { map, lrs, editLayers, resetLrs } from './map-init.js';
import { escHtml, jobApiUrl } from './utils.js';
import { apiGet, apiPost, apiPatch, apiDelete } from './api.js';
import { showError } from './form-controls.js';
import { loadJobsList } from './jobs-panel.js';
import { openDeleteModal, openMoveModal } from './modal-utils.js';
import { clearTakeoffForMapView, _hideVlos } from './takeoff.js';
import { getMvStatColor, getMvStatMode, statModeColorsJobs, clearMgrsLayer, renderStatPanel, _mvStatJobClick as _mvStatJobClickStat } from './stat-view.js';
import { showBatteryTimeline, hideBatteryTimeline, destroyBatteryTimeline } from './battery-timeline.js';
import { showForecastBar, destroyForecastBar, setForecastBarShifted } from './forecast-bar.js';
import { hideCesiumView } from './cesium-view.js';
// Circular — only called at runtime:
import { saveEdit } from './polygon-edit.js';
import { openJob as _openJobFn } from './job-ops.js';
import { _selectedJobs, _selectedMeta, _updateSelBar, openMergeModal } from './multi-select.js';
import { autoSortFolder } from './drag-reorder.js';

var _mvMode = false;
var _mvFromEditor = false;
var _mvJobGroup = null;
var _mvLayers = [];
var _mvHoverPopup = null;
var _mvHoverTimer = null;
var _mvSelected = new Set();
var _mvAllFeatures = [];
var _mvCurrentFolder = null;
var _DEFAULT_COLOR = '#3b82f6';
var _mvRouteLayer = null;
var _mvRouteVisible = true;
var _mvDimLayer = null;

export function getMvMode() { return _mvMode; }
export function getMvSelected() { return _mvSelected; }
export function getMvCurrentFolder() { return _mvCurrentFolder; }
export function getMvLayers() { return _mvLayers; }

export function showFolderOnMap(e, folderName) {
  e.stopPropagation();
  openMapView(folderName || null);
}

export function openMapView(folderFilter) {
  hideCesiumView();
  var folderKey = folderFilter || null;
  var _comingFromEditor = _mvFromEditor && st._activeJobFolder === folderKey;
  var _skipFit = _comingFromEditor;
  _mvFromEditor = false;

  var folderChanged = _mvMode && _mvCurrentFolder !== folderKey;

  _mvMode = true;
  st._mvMode = true;
  _mvCurrentFolder = folderKey;
  if (st.editMode) saveEdit();

  Object.values(lrs).forEach(function(l){ if (l) map.removeLayer(l); });
  resetLrs();
  editLayers.clearLayers();
  clearTakeoffForMapView();
  _hideVlos();

  document.getElementById('sb').classList.add('mv-hidden');
  document.getElementById('legend').classList.add('mv-hidden');
  document.getElementById('sp').classList.add('mv-hidden');
  document.getElementById('mv-right-panel').classList.add('visible');
  map.invalidateSize();
  var sel = document.getElementById('mv-stat-mode');
  if (sel) sel.value = getMvStatMode();
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.folder === (folderFilter || ''));
  });

  if (folderChanged) {
    _mvSelected.clear();
  }
  _mvUpdateSelBar();

  if (!_comingFromEditor) _mvRouteVisible = true;
  var routeBtn = document.getElementById('mv-route-btn');
  if (routeBtn) routeBtn.classList.toggle('active', _mvRouteVisible);

  if (!_mvJobGroup) { _mvJobGroup = L.layerGroup().addTo(map); }
  _mvLoad(folderFilter, _skipFit);
}

export function closeMapView() {
  if (!_mvMode) return;
  _mvMode = false;
  st._mvMode = false;
  _mvCurrentFolder = null;
  clearTimeout(_mvHoverTimer);
  if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
  destroyBatteryTimeline();
  destroyForecastBar();
  clearMgrsLayer();
  _mvClearLayers();
  _mvHideDim();
  if (_mvRouteLayer) { _mvRouteLayer.remove(); _mvRouteLayer = null; }
  _mvSelected.forEach(function(path) {
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) card.classList.remove('selected');
  });
  _mvSelected.clear();
  _mvUpdateSelBar();
  _updateSelBar(); // re-show list-mode toast if jobs were selected in the panel

  document.getElementById('sb').classList.remove('mv-hidden');
  document.getElementById('legend').classList.remove('mv-hidden');
  document.getElementById('sp').classList.remove('mv-hidden');
  document.getElementById('mv-right-panel').classList.remove('visible');
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) { btn.classList.remove('active'); });
  map.closePopup();
}

async function _mvLoad(folderFilter, skipFit) {
  try {
    var fc = await apiGet('/api/jobs/geojson');
    _mvAllFeatures = fc.features || [];
    _mvApplyFilter(folderFilter, skipFit);
    _mvDrawRoute();
  } catch(e) { console.error('[mapview]', e); }
}

export function _mvDrawRoute() {
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
  L.polyline(latlngs, {color: '#f59e0b', weight: 2, opacity: 0.7, dashArray: '6,4'}).addTo(_mvRouteLayer);

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

export function toggleMvRoute() {
  _mvRouteVisible = !_mvRouteVisible;
  var btn = document.getElementById('mv-route-btn');
  if (btn) btn.classList.toggle('active', _mvRouteVisible);
  _mvDrawRoute();
}

export async function _mvRefreshRouteData() {
  try {
    var fc = await apiGet('/api/jobs/geojson');
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
  // Re-apply selection visuals after layer rebuild
  var stale = [];
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (!item) { stale.push(path); return; }
    item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) {
      card.classList.add('selected');
      var chk = card.querySelector('.jcard-chk');
      if (chk) chk.checked = true;
    }
  });
  stale.forEach(function(p){ _mvSelected.delete(p); });
  showBatteryTimeline(_mvAllFeatures, _mvSelected, _mvCurrentFolder, _mvLayers);
  showForecastBar(_mvCurrentFolder);
  renderStatPanel(_mvLayers.map(function(item) { return item.feature; }), _mvSelected);
  if (statModeColorsJobs()) {
    _mvLayers.forEach(function(item) {
      if (_mvSelected.has(item.path)) return;
      var c = getMvStatColor(item.feature.properties);
      item.layer.setStyle({color: c, fillColor: c, weight: 2.5, opacity: 1, fillOpacity: 0.30});
    });
  }
  _mvUpdateDim();
}

function _mvClearLayers() {
  _mvLayers.forEach(function(item){ if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); });
  _mvLayers = [];
}

function _mvMakeLayer(feature) {
  var p = feature.properties;
  var color = getMvStatColor(p);
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
      color: color, weight: 2.5, fillColor: color, fillOpacity: 0.30, dashArray: dashArray,
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
  var _ic = function(id, col) {
    return '<svg class="mv-ic"' + (col ? ' style="color:' + col + ';opacity:1"' : '') + '><use href="#' + id + '"/></svg>';
  };
  var areaLostHtml = '';
  if (p.area_lost_pct != null && Math.abs(p.area_lost_pct) >= 0.05) {
    var sign = p.area_lost_pct > 0 ? '−' : '+';
    var col  = p.area_lost_pct > 0 ? '#fb923c' : '#4ade80';
    areaLostHtml = ' <span style="color:' + col + '">' + sign + Math.abs(p.area_lost_pct).toFixed(1) + '%</span>';
  }
  var _stat = function(content) { return '<span style="white-space:nowrap">' + content + '</span>'; };
  var flightParts = [];
  if (p.area_ha != null)        flightParts.push(_stat(_ic('ic-area') + ' ' + p.area_ha.toFixed(1) + ' ha' + areaLostHtml));
  if (p.waypoint_mode && p.adv_min_height_m != null && p.adv_max_height_m != null)
    flightParts.push(_stat(_ic('ic-altitude') + ' ' + Math.round(p.adv_min_height_m) + '–' + Math.round(p.adv_max_height_m) + ' m'));
  else if (p.height_m != null)
    flightParts.push(_stat(_ic('ic-altitude') + ' ' + p.height_m.toFixed(0) + ' m'));
  if (p.strip_speed_ms != null) flightParts.push(_stat(_ic('ic-gauge') + ' ' + (p.strip_speed_ms * 3.6).toFixed(1) + ' km/h'));
  if (p.flight_time_min != null) flightParts.push(_stat(_ic('ic-timer') + ' ' + Math.round(p.flight_time_min) + ' min'));
  if (p.over_one_battery)       flightParts.push(_stat(_ic('ic-battery-warn', '#fb923c') + ' <span style="color:#fb923c">2+ bat</span>'));
  var flightInfo = flightParts.join('<span style="color:#475569"> · </span>');
  var photoInfo = p.photo_count != null ? _stat(_ic('ic-camera') + ' ' + p.photo_count + ' photos') : '';
  var routeIndex = (p.sort_order != null && !p.skipped)
    ? '<span style="display:inline-flex;align-items:center;justify-content:center;background:#f59e0b;color:#000;font-size:9px;font-weight:700;width:16px;height:16px;border-radius:50%;border:1.5px solid rgba(255,255,255,0.25);box-shadow:0 1px 2px rgba(0,0,0,.5);vertical-align:middle;line-height:1;flex-shrink:0">' + (p.sort_order + 1) + '</span>'
    : '';
  var skipLabel = p.skipped ? '⊘ Unskip' : '⊘ Skip';
  var html = '<div class="mv-tt-inner">'
    + '<div class="mv-tt-name">' + (p.skipped ? '⊘ ' : '') + escHtml(p.name)
    + (p.folder ? ' <span class="mv-tt-folder">(' + escHtml(p.folder) + ')</span>' : '') + '</div>'
    + '<div class="mv-tt-meta">' + (routeIndex ? routeIndex + ' · ' : '') + statusChip + (p.skipped ? ' · <span style="color:#94a3b8">skipped</span>' : '') + '</div>'
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

export function mvOpenJob(path) {
  map.closePopup();
  closeMapView();
  _openJobFn(path);
}

export async function mvToggleSkip(path, currentSkipped) {
  try {
    await apiPatch(jobApiUrl(path), {skipped: !currentSkipped});
    if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
    try {
      _mvAllFeatures = (await apiGet('/api/jobs/geojson')).features || [];
      _mvApplyFilter(_mvCurrentFolder, true);
    } catch(e) { /* geojson refresh best-effort */ }
    loadJobsList();
  } catch(e) { showError(e.detail || ('Failed: ' + e.message)); }
}

export function mvDeleteJob(path, name) {
  map.closePopup();
  if (_mvHoverPopup) { _mvHoverPopup = null; }
  openDeleteModal('Delete "' + name + '"? This cannot be undone.', async function() {
    try {
      await apiDelete(jobApiUrl(path));
      _mvLayers = _mvLayers.filter(function(item) {
        if (item.path === path) { _mvJobGroup.removeLayer(item.layer); return false; }
        return true;
      });
      _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== path; });
      if (st._activeJob === path) { st._activeJob = null; st._activeJobFolder = null; }
      loadJobsList();
    } catch(e) { showError(e.detail || ('Delete failed: ' + e.message)); }
  });
}

function _mvUpdateDim() {
  if (statModeColorsJobs() && _mvMode) { _mvShowDim(); } else { _mvHideDim(); }
}

function _mvShowDim() {
  if (_mvDimLayer) return;
  if (!map.getPane('mvDimPane')) {
    map.createPane('mvDimPane').style.zIndex = 300;
  }
  _mvDimLayer = L.rectangle([[-90, -180], [90, 180]], {
    pane: 'mvDimPane', stroke: false, fillColor: '#000', fillOpacity: 0.30, interactive: false,
  }).addTo(map);
}

function _mvHideDim() {
  if (_mvDimLayer) { map.removeLayer(_mvDimLayer); _mvDimLayer = null; }
}

export function _mvToggleSel(path) {
  var item = _mvLayers.find(function(i){ return i.path === path; });
  if (!item) return;
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
  var chk = card && card.querySelector('.jcard-chk');
  if (_mvSelected.has(path)) {
    _mvSelected.delete(path);
    var origColor = getMvStatColor(item.feature.properties);
    item.layer.setStyle({weight: 2.5, opacity: 1, color: origColor, fillColor: origColor});
    if (card) card.classList.remove('selected');
    if (chk) chk.checked = false;
  } else {
    _mvSelected.add(path);
    item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    if (card) {
      card.classList.add('selected');
      if (_mvSelected.size === 1) card.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    if (chk) chk.checked = true;
  }
  _mvUpdateSelBar();
}

export function mvClearSel() {
  _mvSelected.forEach(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    if (item) { var c = getMvStatColor(item.feature.properties); item.layer.setStyle({weight: 2.5, opacity: 1, color: c, fillColor: c}); }
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) {
      card.classList.remove('selected');
      var chk = card.querySelector('.jcard-chk');
      if (chk) chk.checked = false;
    }
  });
  _mvSelected.clear();
  _mvUpdateSelBar();
}

function _mvUpdateSelBar() {
  var n = _mvSelected.size;
  document.getElementById('mv-actions').classList.toggle('visible', _mvMode && n > 0);
  setForecastBarShifted(_mvMode && n > 0);
  document.getElementById('mv-sel-count').textContent = n + ' selected';
  document.getElementById('mv-merge-btn').disabled = n < 2;
  var openBtn = document.getElementById('mv-open-btn');
  if (openBtn) {
    openBtn.style.display = n === 1 ? '' : 'none';
    if (n === 1) openBtn.dataset.path = Array.from(_mvSelected)[0];
  }
  showBatteryTimeline(_mvAllFeatures, _mvSelected, _mvCurrentFolder, _mvLayers);
  renderStatPanel(_mvLayers.map(function(item) { return item.feature; }), _mvSelected);
}

export function mvMerge() {
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

export function mvBulkMove() {
  var paths = Array.from(_mvSelected);
  var metas = paths.map(function(path) {
    var item = _mvLayers.find(function(i){ return i.path === path; });
    return item ? {path: path, name: item.feature.properties.name, folder: item.feature.properties.folder} : null;
  }).filter(Boolean);
  if (!metas.length) return;
  var title = metas.length === 1 ? 'Move "' + metas[0].name + '"' : 'Move ' + metas.length + ' Jobs';
  openMoveModal(title, metas, async function(dest) {
    for (var i = 0; i < metas.length; i++) {
      try {
        await apiPost(jobApiUrl(metas[i].path, '/move'), {folder: dest});
      } catch(e) { showError(e.detail || ('Move failed: ' + e.message)); }
    }
    mvClearSel();
    await loadJobsList();
    openMapView(dest);
  });
}

export function mvBulkDelete() {
  var n = _mvSelected.size;
  if (!n) return;
  var msg = 'Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '? This cannot be undone.';
  openDeleteModal(msg, async function() {
    var paths = Array.from(_mvSelected);
    for (var i = 0; i < paths.length; i++) {
      try {
        await apiDelete(jobApiUrl(paths[i]));
        _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== paths[i]; });
        _mvLayers = _mvLayers.filter(function(item) {
          if (item.path === paths[i]) { if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); return false; }
          return true;
        });
      } catch(e) { showError('Delete failed: ' + e.message); }
    }
    mvClearSel();
    loadJobsList();
  });
}

// Called by stat-view.js to pan to a job
export function _mvStatJobClickInternal(path) {
  var item = _mvLayers.find(function(i) { return i.path === path; });
  if (!item) return;
  try { map.fitBounds(item.layer.getBounds(), {padding: [60, 60], maxZoom: 16}); } catch(e) {}
  if (!_mvSelected.has(path)) _mvToggleSel(path);
}

// Called by stat-view.js when stat mode changes
export function _onStatModeChangeInternal(mode) {
  renderStatPanel(_mvLayers.map(function(item) { return item.feature; }), _mvSelected);
  _mvLayers.forEach(function(item) {
    if (_mvSelected.has(item.path)) return;
    var c = getMvStatColor(item.feature.properties);
    item.layer.setStyle({color: c, fillColor: c, weight: 2.5, opacity: 1, fillOpacity: 0.30});
  });
  _mvUpdateDim();
}

// Set _mvFromEditor flag (called by job-ops when closing a job to go back to map)
export function setMvFromEditor(v) { _mvFromEditor = v; }

export async function mvAutoRoute() {
  var folderKey = _mvCurrentFolder;
  var features = _mvAllFeatures.filter(function(f){ return (f.properties.folder || null) === folderKey; });
  var group = { jobs: features.map(function(f){ return f.properties; }) };
  await autoSortFolder(group, folderKey);
}
