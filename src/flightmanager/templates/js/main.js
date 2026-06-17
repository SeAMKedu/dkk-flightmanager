// ── Application entry point ───────────────────────────────────────────────────
// Imports all modules (side effects: event listeners, map init, etc.) and runs init().

import { st } from './state.js';
import { defaultJobName, updateFolderHint, updateGsd, setSub, setSimpManual,
         focusArea, setRadiusLinked, _doNewJob, newJob, toggleSec,
         clearPolyEdit, onIdBlur, simpStep, setSimpAuto, scheduleAutoUpdate,
         getParams, showError, clearError, parseIds } from './form-controls.js';
import { markDirty, confirmIfDirty, hideConfirmModal } from './dirty-tracking.js';
import { setJpOpen, toggleJp, loadJobsList, buildJobCard } from './jobs-panel.js';
import { renderStatus } from './status-panel.js';
import { openSettings, closeSettings, discardSettings, saveSettings, cfgSearch, openAbout, closeAbout } from './settings-panel.js';
import { _initBaseLayers, resetMapToUserLocation } from './map-init.js';
import { startPreview, startExport } from './preview-runner.js';
import { toggleEdit, saveEdit, resetPoly } from './polygon-edit.js';
import { exitBridgeMode, commitSplit } from './polygon-bridge.js';
import { openJob, goBackToMap, revealJob, cloneJob, confirmDeleteJob, deleteJob,
         startRename, doRename, showStaleNotice, hideStaleNotice,
         toggleColorPopup, _applyColor, initColorPalette, _setColorPicker } from './job-ops.js';
import { showFolderOnMap, openMapView, closeMapView, mvOpenJob, mvToggleSkip,
         mvDeleteJob, toggleMvRoute, mvMerge, mvBulkMove, mvBulkDelete, mvClearSel,
         mvAutoRoute } from './map-view.js';
import { toggleJobSelection, clearSelection, openMergeModal, closeMergeModal, submitMerge } from './multi-select.js';
import { closeCardMenu, createFolder, closeFolderDialog, submitFolder, showMoveMenu, doMoveJob } from './card-menu.js';
import { autoSortFolder, closeRouteConfirmModal } from './drag-reorder.js';
import { bulkMove, bulkDelete, exportKml, openGoogleMaps, routeRename,
         exportRoute, closeExportRouteModal, submitExportRoute,
         unifiedMerge, unifiedBulkMove, unifiedBulkDelete, unifiedClearSel } from './bulk-ops.js';
import { openBatchDialog, closeBatchDialog, setBatchType, submitBatch } from './batch-modal.js';
import { routeAngleAuto, routeAngleStep, speedAuto, speedStep, updateRouteStats } from './route-planner.js';
import { onStatModeChange, _mvStatJobClick } from './stat-view.js';
import { _initEventStream, showExtModifiedNotice, hideExtModifiedNotice, reloadCurrentJob } from './event-stream.js';
import { _cpSetFromHex, _syncPaletteActive } from './color-picker.js';
import { closeDeleteModal, confirmDeleteAction, closeMoveModal, submitNewFolderMove,
         closeRouteRenameModal, confirmRouteRenameAction } from './modal-utils.js';
import { setVlosRange } from './takeoff.js';
import { clearMeasurements } from './measurement.js';
import { initCesiumView, toggle3dView } from './cesium-view.js';
import { openTplModal, closeTplModal, tplTab, initTplModal, initTplDefaults } from './tpl-modal.js';
import { apiGet } from './api.js';
import { checkStaleJobs } from './refresh-banner.js';

// ── Assign all functions needed in HTML onclick= attributes to window ─────────
Object.assign(window, {
  // form-controls
  defaultJobName, updateFolderHint, updateGsd, setSub, setSimpManual,
  focusArea, setRadiusLinked, _doNewJob, newJob, toggleSec,
  clearPolyEdit, onIdBlur, simpStep, setSimpAuto, scheduleAutoUpdate,
  getParams, showError, clearError, parseIds,

  // dirty-tracking
  markDirty, confirmIfDirty, hideConfirmModal,

  // jobs-panel
  setJpOpen, toggleJp, loadJobsList,

  // status-panel
  renderStatus,

  // settings-panel
  openSettings, closeSettings, discardSettings, saveSettings, cfgSearch, openAbout, closeAbout,

  // preview-runner
  startPreview, startExport,

  // polygon-edit
  toggleEdit, saveEdit, resetPoly,

  // polygon-bridge
  exitBridgeMode, commitSplit,

  // job-ops
  openJob, goBackToMap, revealJob, cloneJob, confirmDeleteJob, deleteJob,
  startRename, doRename, showStaleNotice, hideStaleNotice,
  toggleColorPopup, _applyColor, initColorPalette,

  // map-view
  showFolderOnMap, openMapView, closeMapView, mvOpenJob, mvToggleSkip,
  mvDeleteJob, toggleMvRoute, mvMerge, mvBulkMove, mvBulkDelete, mvClearSel,
  mvAutoRoute,

  // multi-select
  toggleJobSelection, clearSelection, openMergeModal, closeMergeModal, submitMerge,

  // card-menu / folder ops
  closeCardMenu, createFolder, closeFolderDialog, submitFolder, showMoveMenu, doMoveJob,

  // modal-utils
  closeDeleteModal, confirmDeleteAction, closeMoveModal, submitNewFolderMove,
  closeRouteRenameModal, confirmRouteRenameAction,

  // drag-reorder
  autoSortFolder, closeRouteConfirmModal,

  // bulk-ops
  bulkMove, bulkDelete, exportKml, openGoogleMaps, routeRename,
  exportRoute, closeExportRouteModal, submitExportRoute,
  unifiedMerge, unifiedBulkMove, unifiedBulkDelete, unifiedClearSel,

  // batch-modal
  openBatchDialog, closeBatchDialog, setBatchType, submitBatch,

  // route-planner
  routeAngleAuto, routeAngleStep, speedAuto, speedStep,

  // stat-view
  onStatModeChange, _mvStatJobClick,

  // event-stream
  showExtModifiedNotice, hideExtModifiedNotice, reloadCurrentJob,

  // color-picker
  _cpSetFromHex, _syncPaletteActive,

  // measurement
  clearMeasurements,

  // cesium-view
  toggle3dView,

  // tpl-modal
  openTplModal, closeTplModal, tplTab,
});

// ── Application init ──────────────────────────────────────────────────────────

async function init() {
  document.getElementById('jname').value = defaultJobName();

  try {
    st.drones = await apiGet('/api/drones');
    var sel = document.getElementById('dsel');
    st.drones.forEach(function(d) {
      var o = document.createElement('option');
      o.value = d.name; o.textContent = d.label;
      sel.appendChild(o);
    });

    var cfg = await apiGet('/api/config');

    st.outputDir = cfg.output_dir || '';
    updateFolderHint();

    if (cfg.default_drone) sel.value = cfg.default_drone;
    if (cfg.subcategory) setSub(cfg.subcategory, true);
    if (cfg.offset_m !== undefined) document.getElementById('offset').value = cfg.offset_m;
    if (cfg.height_m) {
      var h0 = Math.round(cfg.height_m);
      document.getElementById('hgt').value = h0;
      document.getElementById('warn-radius').value = 3 * h0;
    }
    if (cfg.simplify && cfg.simplify !== 'auto') {
      setSimpManual(parseFloat(cfg.simplify) || 0, true);
    }
    document.getElementById('kochk').checked = cfg.keepout !== false;
    if (cfg.vlos_range_m) setVlosRange(cfg.vlos_range_m);
    if (cfg.overlap_front_pct) st._cfgOverlapFront = cfg.overlap_front_pct;
    if (cfg.overlap_side_pct)  st._cfgOverlapSide  = cfg.overlap_side_pct;
    if (cfg.auto_flight_speed_ms) {
      st._cfgDefaultSpeedMs = cfg.auto_flight_speed_ms;
    }
    updateGsd();
    if (cfg.mml_api_key) _initBaseLayers(cfg.mml_api_key);
    initCesiumView();
    if (cfg.color_palette) initColorPalette(cfg.color_palette);
    if (cfg.max_area_loss_pct != null) st._cfgMaxAreaLossPct = cfg.max_area_loss_pct;
    initTplDefaults(cfg);
    console.log('[init] config loaded, outputDir=' + st.outputDir + ', drone=' + cfg.default_drone);
  } catch(e) {
    console.error('[init] failed:', e);
  }
  initTplModal();
  renderStatus(null);
  focusArea();
  setJpOpen(localStorage.getItem('jp-open') !== 'false');
  loadJobsList();
  _initEventStream();
  checkStaleJobs();
}

init();
