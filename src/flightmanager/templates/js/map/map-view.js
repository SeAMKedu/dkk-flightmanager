// ── Map view (in-place — reuses existing #map, hides #sb) ─────────────────────

import { st } from '../core/state.js';
import { map, editLayers, clearAllLayers } from './map-init.js';
import { escHtml, jobApiUrl } from '../core/utils.js';
import { apiGet, apiPost, apiPatch, apiDelete } from '../core/api.js';
import { showError } from '../editor/form-controls.js';
import { loadJobsList } from '../jobs/jobs-panel.js';
import { openDeleteModal, openMoveModal } from '../panels/modal-utils.js';
import { clearTakeoffForMapView, _hideVlos } from '../editor/takeoff.js';
import { getMvStatColor, statModeColorsJobs, statModeDims, clearMgrsLayer, renderStatPanel } from '../forecast/stat-view.js';
import { ensureRtkData, clearRtkLayer, rtkPopupHtml } from '../forecast/rtk-stations.js';
import { showBatteryTimeline, destroyBatteryTimeline } from '../forecast/battery-timeline.js';
import { showForecastBar, destroyForecastBar, setForecastBarShifted } from '../forecast/forecast-bar.js';
import { hideCesiumView } from '../three-d/cesium-view.js';
import { clearArrowLayer } from '../editor/route-planner.js';
import { drawLaunchSites, clearLaunchSites } from '../forecast/launch-sites.js';
// Circular — only called at runtime:
import { saveEdit } from '../editor/polygon-edit.js';
import { openJob as _openJobFn } from '../jobs/job-ops.js';
import { _selectedJobs, _selectedMeta, _updateSelBar, openMergeModal } from '../jobs/multi-select.js';
import { autoSortFolder } from '../jobs/drag-reorder.js';

// Map-view mode lives solely in st._mvMode now (single source of truth, read by
// polygon-edit/measurement/cesium/etc.). getMvMode() exposes it to callers that
// import it; no separate module-level copy to keep in sync. The other shared
// fields (fromEditor, currentFolder, selected, layers) live in st.mv; the
// purely internal handles below stay module-local.
var _mvJobGroup = null;
var _mvHoverPopup = null;
var _mvHoverPath = null;
var _mvHoverTimer = null;
var _mvAllFeatures = [];
var _mvRouteSeq = 0;
var _mvRouteVisible = true;
var _mvDimLayer = null;

export function getMvMode() { return st._mvMode; }

export function showFolderOnMap(e, folderName) {
  e.stopPropagation();
  openMapView(folderName || null);
}

export function openMapView(folderFilter) {
  hideCesiumView();
  var folderKey = folderFilter || null;
  var _comingFromEditor = st.mv.fromEditor && st._activeJobFolder === folderKey;
  var _skipFit = _comingFromEditor;
  st.mv.fromEditor = false;

  var folderChanged = st._mvMode && st.mv.currentFolder !== folderKey;

  st._mvMode = true;
  st.mv.currentFolder = folderKey;
  if (st.editMode) saveEdit();

  clearAllLayers();
  editLayers.clearLayers();
  clearArrowLayer();
  clearTakeoffForMapView();
  _hideVlos();

  document.getElementById('sb').classList.add('mv-hidden');
  document.getElementById('legend').classList.add('mv-hidden');
  document.getElementById('sp').classList.add('mv-hidden');
  document.getElementById('mv-right-panel').classList.add('visible');
  map.invalidateSize();
  var sel = document.getElementById('mv-stat-mode');
  if (sel) sel.value = st.stat.mode;
  document.querySelectorAll('.jfolder-map-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.folder === (folderFilter || ''));
  });

  if (folderChanged) {
    st.mv.selected.clear();
  }
  _mvUpdateSelBar();

  if (!_comingFromEditor) _mvRouteVisible = true;
  var routeBtn = document.getElementById('mv-route-btn');
  if (routeBtn) routeBtn.classList.toggle('active', _mvRouteVisible);

  if (!_mvJobGroup) { _mvJobGroup = L.layerGroup().addTo(map); }
  _mvLoad(folderFilter, _skipFit);
}

export function closeMapView() {
  if (!st._mvMode) return;
  st._mvMode = false;
  st.mv.currentFolder = null;
  clearTimeout(_mvHoverTimer);
  if (_mvHoverPopup) { map.closePopup(_mvHoverPopup); _mvHoverPopup = null; }
  destroyBatteryTimeline();
  destroyForecastBar();
  clearMgrsLayer();
  clearRtkLayer();
  _mvClearLayers();
  _mvHideDim();
  clearLaunchSites(map);
  clearArrowLayer();
  st.mv.selected.forEach(function(path) {
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) card.classList.remove('selected');
  });
  st.mv.selected.clear();
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

export async function _mvDrawRoute() {
  clearLaunchSites(map);          // tear down layers + detached zoom/move handlers
  // Sequence guard: _mvDrawRoute is fired from several places that can overlap
  // (e.g. _mvApplyFilter + _mvLoad on open). The async fetch below means a stale
  // call could otherwise create a second, untracked layer group that leaks.
  var seq = ++_mvRouteSeq;
  if (!_mvRouteVisible || !st._mvMode) return;

  // Launch sites: consecutive jobs flown from one parking spot are grouped
  // server-side into a single numbered dot (takeoff centroid) carrying its
  // flight-announcement circle. Hover a dot to see the operating-area radius.
  try {
    var url = '/api/launch_sites'
      + (st.mv.currentFolder ? ('?folder=' + encodeURIComponent(st.mv.currentFolder)) : '');
    var resp = await apiGet(url);
    if (seq !== _mvRouteSeq) return;            // superseded by a newer call
    if (!_mvRouteVisible || !st._mvMode) return;   // state may have changed during await
    drawLaunchSites(map, resp.sites || []);
  } catch (e) {
    console.error('[launch-sites]', e);
  }
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
    // Rebuild the polygon layers (not just the route dots): each layer's hover
    // popup closes over the feature's properties captured in _mvMakeLayer, so a
    // stale sort_order would otherwise persist in the mouseover after a reorder.
    // _mvApplyFilter recreates the layers (fresh closures) and redraws the route.
    _mvApplyFilter(st.mv.currentFolder, true);
  } catch(e) { console.error('[mv-refresh-route]', e); }
}

// Re-fetch features and rebuild the per-job layers for the current folder,
// keeping the current view (no re-fit). Call after an in-place mutation that
// changes job *paths* (route rename, reorder): otherwise st.mv.layers keeps the
// old paths and selection silently no-ops (_mvToggleSel can't find the layer)
// until a full page refresh. The selection set is preserved as-is — callers that
// renamed paths set the new ones first (mvReplaceSelection); _mvApplyFilter
// re-applies the highlight on rebuild and prunes any paths that no longer exist.
export async function mvReload() {
  if (!st._mvMode) return;
  await _mvLoad(st.mv.currentFolder, true);   // skipFit → preserve the current view
  _mvUpdateSelBar();
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
      st.mv.layers.push({path: f.properties.path, layer: layer, feature: f});
      try { bounds.push(layer.getBounds()); } catch {}
    }
  });
  if (bounds.length && !skipFit) {
    // Clone the first layer's bounds — getBounds() returns the layer's live
    // internal _bounds object, so extending it in place would permanently
    // inflate that layer's bounds to span the whole folder (breaking any later
    // fit-to-that-job).
    var combined = L.latLngBounds(bounds[0].getSouthWest(), bounds[0].getNorthEast());
    bounds.forEach(function(b){ combined.extend(b); });
    map.fitBounds(combined, {padding: [40, 40]});
  }
  _mvDrawRoute();
  // Re-apply selection visuals after layer rebuild
  var stale = [];
  st.mv.selected.forEach(function(path) {
    var item = st.mv.layers.find(function(i){ return i.path === path; });
    if (!item) { stale.push(path); return; }
    item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) {
      card.classList.add('selected');
      var chk = card.querySelector('.jcard-chk');
      if (chk) chk.checked = true;
    }
  });
  stale.forEach(function(p){ st.mv.selected.delete(p); });
  showBatteryTimeline(_mvAllFeatures, st.mv.selected, st.mv.currentFolder);
  showForecastBar(st.mv.currentFolder);
  // Prefetch RTK base stations (folder-keyed, cache-first server-side) so the
  // hover popups can show the nearest station synchronously.
  ensureRtkData(st.mv.currentFolder).catch(function(e){ console.error('[rtk]', e); });
  renderStatPanel(st.mv.layers.map(function(item) { return item.feature; }), st.mv.selected);
  if (statModeColorsJobs()) {
    st.mv.layers.forEach(function(item) {
      if (st.mv.selected.has(item.path)) return;
      var c = getMvStatColor(item.feature.properties);
      item.layer.setStyle({color: c, fillColor: c, weight: 2.5, opacity: 1, fillOpacity: 0.30});
    });
  }
  _mvUpdateDim();
}

function _mvClearLayers() {
  st.mv.layers.forEach(function(item){ if (_mvJobGroup) _mvJobGroup.removeLayer(item.layer); });
  st.mv.layers = [];
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

    layer.on('mouseover', function() {
      clearTimeout(_mvHoverTimer);
      // Re-entering the same polygon (e.g. after visiting the popup's buttons)
      // keeps the popup where it is instead of re-opening it.
      if (_mvHoverPopup && map.hasLayer(_mvHoverPopup) && _mvHoverPath === p.path) return;
      _mvOpenHoverPopup(layer, p);
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
  } catch { return null; }
}

function _mvDash(p) {
  if (p.status === 'failed') return '2, 6';
  if (p.flight_ready === true) return null;
  if (p.needs_review === true) return '10, 5';
  if (p.untouched) return '4, 4';
  return null;
}

// Stable popup anchor: the on-screen bounding box of the polygon, clipped to
// the viewport. The popup opens above its top-centre so it floats over the map
// just clear of the shape - not at the mouse-entry point, which landed wherever
// the cursor crossed the edge and often covered the polygon or its neighbours.
function _mvPopupAnchor(layer) {
  var size = map.getSize();
  var b = layer.getBounds();
  var p1 = map.latLngToContainerPoint(b.getNorthWest());
  var p2 = map.latLngToContainerPoint(b.getSouthEast());
  var x0 = Math.max(Math.min(p1.x, p2.x), 0);
  var x1 = Math.min(Math.max(p1.x, p2.x), size.x);
  return {
    x: (x0 + x1) / 2,
    top: Math.max(Math.min(p1.y, p2.y), 0),
    bottom: Math.min(Math.max(p1.y, p2.y), size.y),
  };
}

// After opening, measure the rendered popup and reposition if needed: clamp
// horizontally, keep it out from under the forecast bar, and when there is no
// room above the polygon flip it below (or clamp inside the view as a last
// resort). Flipped placements hide the tip, which would point at nothing.
function _mvNudgeHoverPopup(anchor) {
  var el = _mvHoverPopup && _mvHoverPopup.getElement();
  if (!el) return;
  var size = map.getSize();
  var mapRect = map.getContainer().getBoundingClientRect();
  var r = el.getBoundingClientRect();
  var w = r.width;
  var hAbove = anchor.top - (r.top - mapRect.top); // popup extent above the anchor (incl. tip + offset)

  var x = Math.min(Math.max(anchor.x, w / 2 + 8), size.x - w / 2 - 8);

  var topLimit = 8;
  var bar = document.getElementById('forecast-bar');
  if (bar && bar.offsetWidth) {
    var br = bar.getBoundingClientRect();
    if (x + w / 2 > br.left - mapRect.left && x - w / 2 < br.right - mapRect.left) {
      topLimit = Math.max(topLimit, br.bottom - mapRect.top + 6);
    }
  }
  var bottomLimit = size.y - 8;
  var bt = document.getElementById('battery-timeline');
  if (bt && bt.offsetWidth && bt.style.display !== 'none') {
    var tr = bt.getBoundingClientRect();
    if (x + w / 2 > tr.left - mapRect.left && x - w / 2 < tr.right - mapRect.left) {
      bottomLimit = Math.min(bottomLimit, tr.top - mapRect.top - 6);
    }
  }

  var y = anchor.top, flip = false;
  if (y - hAbove < topLimit) {
    var yBelow = Math.max(anchor.bottom + 6, topLimit) + hAbove;
    if (yBelow <= bottomLimit) { y = yBelow; flip = true; }        // below the polygon
    else { y = topLimit + hAbove; flip = true; }                   // clamp inside the view
  }
  if (flip) el.classList.add('mv-popup-flip');
  if (x !== anchor.x || y !== anchor.top) {
    _mvHoverPopup.setLatLng(map.containerPointToLatLng([x, y]));
  }
}

function _mvOpenHoverPopup(layer, p) {
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
  // RTK line: stations within range of the takeoff (or polygon centre), or the
  // nearest one. Empty string until the folder's station data has loaded.
  var rtkRef = null;
  if (p.takeoff_point_4326) rtkRef = [p.takeoff_point_4326[1], p.takeoff_point_4326[0]];
  else { try { var cc = layer.getBounds().getCenter(); rtkRef = [cc.lat, cc.lng]; } catch {} }
  var rtkHtml = rtkPopupHtml(st.mv.currentFolder, rtkRef);
  var html = '<div class="mv-tt-inner">'
    + '<div class="mv-tt-name">' + (p.skipped ? '⊘ ' : '') + escHtml(p.name)
    + (p.folder ? ' <span class="mv-tt-folder">(' + escHtml(p.folder) + ')</span>' : '') + '</div>'
    + '<div class="mv-tt-meta">' + (routeIndex ? routeIndex + ' · ' : '') + statusChip + (p.skipped ? ' · <span style="color:#94a3b8">skipped</span>' : '') + '</div>'
    + (flightInfo ? '<div class="mv-tt-flight">' + flightInfo + '</div>' : '')
    + (photoInfo ? '<div class="mv-tt-flight">' + photoInfo + '</div>' : '')
    + rtkHtml
    + '<div class="mv-tt-actions">'
    + '<button onclick="mvToggleSkip(\'' + escHtml(p.path) + '\',' + !!p.skipped + ')">' + skipLabel + '</button>'
    + '<button class="mv-tt-del" onclick="mvDeleteJob(\'' + escHtml(p.path) + '\',\'' + escHtml(p.name) + '\')">✕ Delete</button>'
    + '</div></div>';
  var anchor = _mvPopupAnchor(layer);
  _mvHoverPopup = L.popup({
    closeButton: false, minWidth: 160, className: 'mv-popup',
    autoClose: false, closeOnClick: true, autoPan: false, offset: [0, -6]
  }).setLatLng(map.containerPointToLatLng([anchor.x, anchor.top])).setContent(html).openOn(map);
  _mvHoverPath = p.path;
  _mvNudgeHoverPopup(anchor);
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
      _mvApplyFilter(st.mv.currentFolder, true);
    } catch { /* geojson refresh best-effort */ }
    loadJobsList();
  } catch(e) { showError(e.detail || ('Failed: ' + e.message)); }
}

export function mvDeleteJob(path, name) {
  map.closePopup();
  if (_mvHoverPopup) { _mvHoverPopup = null; }
  openDeleteModal('Delete "' + name + '"? This cannot be undone.', async function() {
    try {
      await apiDelete(jobApiUrl(path));
      st.mv.layers = st.mv.layers.filter(function(item) {
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
  if (statModeDims() && st._mvMode) { _mvShowDim(); } else { _mvHideDim(); }
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
  var item = st.mv.layers.find(function(i){ return i.path === path; });
  if (!item) return;
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
  var chk = card && card.querySelector('.jcard-chk');
  if (st.mv.selected.has(path)) {
    st.mv.selected.delete(path);
    var origColor = getMvStatColor(item.feature.properties);
    item.layer.setStyle({weight: 2.5, opacity: 1, color: origColor, fillColor: origColor});
    if (card) card.classList.remove('selected');
    if (chk) chk.checked = false;
  } else {
    st.mv.selected.add(path);
    item.layer.setStyle({weight: 4, opacity: 1, color: '#f59e0b', fillColor: '#f59e0b'});
    if (card) {
      card.classList.add('selected');
      if (st.mv.selected.size === 1) card.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    if (chk) chk.checked = true;
  }
  _mvUpdateSelBar();
}

// Select every given job path that isn't already selected (used by launch-site
// dot clicks to select all jobs flown from that parking spot).
export function mvSelectPaths(paths) {
  (paths || []).forEach(function(p) {
    if (!st.mv.selected.has(p)) _mvToggleSel(p);
  });
}

// Zoom/pan to fit the combined bounds of the given job paths' polygons (plus
// their takeoff points). Used by launch-site dot clicks so the whole site's
// jobs land in view together, mirroring the single-job zoom from stat-panel
// and battery-timeline clicks. Callers can cap maxZoom (e.g. launch sites stay
// below their own detail-view threshold so the fit doesn't flip the dot into
// per-job takeoff circles).
export function mvFitPaths(paths, maxZoom) {
  // Accumulate into a fresh LatLngBounds — never extend a layer's getBounds()
  // in place (it is the layer's live internal _bounds; mutating it would
  // permanently inflate that job's bounds).
  var combined = null;
  (paths || []).forEach(function(p) {
    var item = st.mv.layers.find(function(i){ return i.path === p; });
    if (!item) return;
    try {
      var b = item.layer.getBounds();
      if (!b.isValid()) return;
      if (!combined) combined = L.latLngBounds(b.getSouthWest(), b.getNorthEast());
      else combined.extend(b);
      var tp = item.feature && item.feature.properties.takeoff_point_4326;
      if (tp) combined.extend([tp[1], tp[0]]);
    } catch {}
  });
  if (combined) map.fitBounds(combined, {padding: [60, 60], maxZoom: maxZoom != null ? maxZoom : 17});
}

// Replace the map-view selection set wholesale, without touching layer styling.
// Used by route rename (which changes job paths): callers set the new paths here
// then reload the map; _mvApplyFilter re-applies the selection visuals on rebuild.
export function mvReplaceSelection(paths) {
  st.mv.selected.clear();
  (paths || []).forEach(function(p) { st.mv.selected.add(p); });
  _mvUpdateSelBar();
}

export function mvClearSel() {
  st.mv.selected.forEach(function(path) {
    var item = st.mv.layers.find(function(i){ return i.path === path; });
    if (item) { var c = getMvStatColor(item.feature.properties); item.layer.setStyle({weight: 2.5, opacity: 1, color: c, fillColor: c}); }
    var card = document.querySelector('.jcard[data-path="' + CSS.escape(path) + '"]');
    if (card) {
      card.classList.remove('selected');
      var chk = card.querySelector('.jcard-chk');
      if (chk) chk.checked = false;
    }
  });
  st.mv.selected.clear();
  _mvUpdateSelBar();
}

function _mvUpdateSelBar() {
  var n = st.mv.selected.size;
  document.getElementById('mv-actions').classList.toggle('visible', st._mvMode && n > 0);
  setForecastBarShifted(st._mvMode && n > 0);
  document.getElementById('mv-sel-count').textContent = n + ' selected';
  document.getElementById('mv-merge-btn').disabled = n < 2;
  var openBtn = document.getElementById('mv-open-btn');
  if (openBtn) {
    openBtn.style.display = n === 1 ? '' : 'none';
    if (n === 1) openBtn.dataset.path = Array.from(st.mv.selected)[0];
  }
  showBatteryTimeline(_mvAllFeatures, st.mv.selected, st.mv.currentFolder);
  renderStatPanel(st.mv.layers.map(function(item) { return item.feature; }), st.mv.selected);
}

export function mvMerge() {
  _selectedJobs.clear(); _selectedMeta.clear();
  st.mv.selected.forEach(function(path) {
    var item = st.mv.layers.find(function(i){ return i.path === path; });
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
  var paths = Array.from(st.mv.selected);
  var metas = paths.map(function(path) {
    var item = st.mv.layers.find(function(i){ return i.path === path; });
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
  var n = st.mv.selected.size;
  if (!n) return;
  var msg = 'Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '? This cannot be undone.';
  openDeleteModal(msg, async function() {
    var paths = Array.from(st.mv.selected);
    for (var i = 0; i < paths.length; i++) {
      try {
        await apiDelete(jobApiUrl(paths[i]));
        _mvAllFeatures = _mvAllFeatures.filter(function(f){ return f.properties.path !== paths[i]; });
        st.mv.layers = st.mv.layers.filter(function(item) {
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
  var item = st.mv.layers.find(function(i) { return i.path === path; });
  if (!item) return;
  try { map.fitBounds(item.layer.getBounds(), {padding: [60, 60], maxZoom: 16}); } catch {}
  if (!st.mv.selected.has(path)) _mvToggleSel(path);
}

// Called by stat-view.js when stat mode changes
export function _onStatModeChangeInternal(_mode) {
  renderStatPanel(st.mv.layers.map(function(item) { return item.feature; }), st.mv.selected);
  st.mv.layers.forEach(function(item) {
    if (st.mv.selected.has(item.path)) return;
    var c = getMvStatColor(item.feature.properties);
    item.layer.setStyle({color: c, fillColor: c, weight: 2.5, opacity: 1, fillOpacity: 0.30});
  });
  _mvUpdateDim();
}

// Set st.mv.fromEditor flag (called by job-ops when closing a job to go back to map)

export async function mvAutoRoute() {
  var folderKey = st.mv.currentFolder;
  var features = _mvAllFeatures.filter(function(f){ return (f.properties.folder || null) === folderKey; });
  var group = { jobs: features.map(function(f){ return f.properties; }) };
  await autoSortFolder(group, folderKey);
}
