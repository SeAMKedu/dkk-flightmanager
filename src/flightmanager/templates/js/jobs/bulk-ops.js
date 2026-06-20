// ── Bulk move / KML export / Google Maps / route rename / export route / bulk delete ──

import { st } from '../core/state.js';
import { jobApiUrl } from '../core/utils.js';
import { apiGet, apiPost, apiPatch, apiDelete } from '../core/api.js';
import { showError } from '../editor/form-controls.js';
import { loadJobsList } from './jobs-panel.js';
import { _selectedJobs, _selectedMeta, clearSelection, openMergeModal } from './multi-select.js';
import { closeCardMenu } from './card-menu.js';
import { openDeleteModal, openMoveModal, openRouteRenameModal } from '../panels/modal-utils.js';
// Circular — only called at runtime:
import { getMvMode, getMvSelected, getMvCurrentFolder,
         mvMerge, mvBulkMove, mvBulkDelete, mvClearSel } from '../map/map-view.js';
import { setForecastBarPdf } from '../forecast/forecast-bar.js';

export function bulkMove() {
  if (!_selectedJobs.size) return;
  closeCardMenu();
  var metas = Array.from(_selectedMeta.values());
  var title = metas.length === 1 ? 'Move "' + metas[0].name + '"' : 'Move ' + metas.length + ' Jobs';
  openMoveModal(title, metas, function(toFolder) { _bulkMoveToFolder(toFolder, metas); });
}

async function _bulkMoveToFolder(toFolder, metas) {
  for (var i = 0; i < metas.length; i++) {
    var j = metas[i];
    try {
      var data = await apiPost(jobApiUrl(j.path, '/move'), {folder: toFolder});
      if (st._activeJob === j.path) { st._activeJob = data.path; st._activeJobFolder = data.folder || null; }
    } catch(err) { showError('Move failed for ' + j.name + ': ' + (err.detail || err.message)); }
  }
  clearSelection();
  await loadJobsList();
}

async function _loadSelectedJobs() {
  var paths = getMvMode() ? Array.from(getMvSelected()) : Array.from(_selectedJobs);
  if (!paths.length) return null;
  var jobs = [];
  for (var i = 0; i < paths.length; i++) {
    try {
      var data = await apiGet(jobApiUrl(paths[i]));
      jobs.push({path: paths[i], params: data.params});
    } catch { /* skip */ }
  }
  if (!jobs.length) return null;
  jobs.sort(function(a, b) {
    var soA = a.params.sort_order, soB = b.params.sort_order;
    var tpA = a.params.takeoff_point_4326 != null, tpB = b.params.takeoff_point_4326 != null;
    var tierA = soA != null ? 0 : tpA ? 1 : 2;
    var tierB = soB != null ? 0 : tpB ? 1 : 2;
    if (tierA !== tierB) return tierA - tierB;
    if (soA != null && soB != null) return soA - soB;
    return 0;
  });
  paths = jobs.map(function(j){ return j.path; });
  return {paths: paths, jobs: jobs};
}

var _pdfBusy = false;

function _ppShow() {
  var el = document.getElementById('pdf-progress');
  if (el) { el.classList.remove('hidden'); _ppSet(0, 'Starting'); }
  setForecastBarPdf(true);
}
function _ppSet(pct, msg) {
  var f = document.getElementById('pp-fill');
  var m = document.getElementById('pp-msg');
  if (f && typeof pct === 'number') f.style.width = Math.max(3, Math.min(100, pct)) + '%';
  if (m && msg != null) m.textContent = msg;
}
function _ppHide() {
  var el = document.getElementById('pdf-progress');
  if (el) el.classList.add('hidden');
  setForecastBarPdf(false);
}

// Stream a report job's SSE progress into the overlay, then download the PDF.
function _streamReport(jobId, fileName) {
  return new Promise(function(resolve, reject) {
    var es = new EventSource('/api/report/progress/' + jobId);
    es.onmessage = async function(ev) {
      var d; try { d = JSON.parse(ev.data); } catch { return; }
      if (d.stage === 'keepalive') return;
      if (d.stage === 'error') { es.close(); reject(new Error(d.msg || 'generation error')); return; }
      if (d.stage === 'done') {
        es.close(); _ppSet(100, 'Downloading');
        try {
          var r = await fetch('/api/report/result/' + jobId);
          if (!r.ok) { reject(new Error('result HTTP ' + r.status)); return; }
          var blob = await r.blob();
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url; a.download = fileName; a.click();
          URL.revokeObjectURL(url);
          resolve();
        } catch (e) { reject(e); }
        return;
      }
      if (typeof d.pct === 'number') _ppSet(d.pct, d.msg || '');
    };
    es.onerror = function() { es.close(); reject(new Error('progress stream lost')); };
  });
}

export async function exportPdf() {
  if (_pdfBusy) return;                       // one generation at a time
  _pdfBusy = true;                            // claim before any await
  _ppShow();
  try {
    var result = await _loadSelectedJobs();
    if (!result) return;
    var paths = result.paths;
    var folders = new Set(paths.map(function(p){ var s = p.indexOf('/'); return s >= 0 ? p.slice(0, s) : null; }));
    var folder = (folders.size === 1) ? Array.from(folders)[0] : null;
    var today = new Date().toLocaleDateString('en-CA');   // YYYY-MM-DD, local
    var fileName = today + '_' + (paths.length === 1 ? paths[0].split('/').pop() : (folder || 'jobs')) + '.pdf';

    var start = await apiPost('/api/report/start', {paths: paths, folder: folder});
    await _streamReport(start.job_id, fileName);
  } catch (e) {
    showError('PDF generation failed: ' + (e && e.message ? e.message : e));
  } finally {
    _pdfBusy = false;
    _ppHide();
  }
}

export async function exportKml() {
  var result = await _loadSelectedJobs();
  if (!result) return;
  var paths = result.paths;

  var r = await fetch('/api/export/kml', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths: paths})
  });
  if (!r.ok) { showError('KML export failed (HTTP ' + r.status + ')'); return; }
  var kmlText = await r.text();

  var folders = new Set(paths.map(function(p){ var s = p.indexOf('/'); return s >= 0 ? p.slice(0, s) : null; }));
  var fileName = (folders.size === 1 && Array.from(folders)[0] !== null)
    ? 'dkk-' + Array.from(folders)[0] + '.kml'
    : 'dkk-jobs.kml';

  var blob = new Blob([kmlText], {type: 'application/vnd.google-earth.kml+xml'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = fileName; a.click();
  URL.revokeObjectURL(url);
}

export async function openGoogleMaps() {
  var result = await _loadSelectedJobs();
  if (!result) return;
  var jobs = result.jobs;

  var navPoints = [];
  jobs.forEach(function(job) {
    var tp = job.params.takeoff_point_4326;
    if (tp) navPoints.push(tp[1] + ',' + tp[0]);
  });

  if (navPoints.length === 1) {
    window.open('https://www.google.com/maps/search/?api=1&query=' + navPoints[0], '_blank');
  } else if (navPoints.length >= 2) {
    var pts = navPoints.slice(0, 10);
    window.open('https://www.google.com/maps/dir/' + pts.join('/'), '_blank');
  }
}

var _ROUTE_PREFIX_RE = /^\d{8}-\d{2,}-/;

export async function routeRename() {
  var result = await _loadSelectedJobs();
  if (!result) return;
  var jobs = result.jobs.filter(function(j) {
    return j.params.sort_order != null || j.params.takeoff_point_4326 != null;
  });
  if (!jobs.length) return;
  openRouteRenameModal(jobs.length, function() { _doRouteRename(jobs); });
}

async function _doRouteRename(jobs) {
  var today = new Date();
  var dd = today.getFullYear().toString()
    + String(today.getMonth() + 1).padStart(2, '0')
    + String(today.getDate()).padStart(2, '0');
  var digits = jobs.length >= 100 ? 3 : 2;

  for (var i = 0; i < jobs.length; i++) {
    var job = jobs[i];
    var baseName = (job.params.job_name || job.path.replace(/^.*\//, ''))
      .replace(_ROUTE_PREFIX_RE, '');
    var idx = String(i + 1).padStart(digits, '0');
    var newName = dd + '-' + idx + '-' + baseName;
    if (newName === baseName) continue;
    try {
      var data = await apiPatch(jobApiUrl(job.path), {new_name: newName});
      if (st._activeJob === job.path) { st._activeJob = data.path; }
      job.path = data.path;
    } catch(err) { showError('Rename failed for ' + baseName + ': ' + (err.detail || err.message)); }
  }
  clearSelection();
  await loadJobsList();
}

export function exportRoute() {
  var modal = document.getElementById('export-route-modal');
  var desc  = document.getElementById('export-route-desc');
  var err   = document.getElementById('export-route-error');
  var scope = getMvCurrentFolder() ? 'folder "' + getMvCurrentFolder() + '"' : 'all folders';
  desc.textContent = 'Copies .kmz and homes KML for all route jobs in ' + scope + ' to a folder on disk.';
  err.style.display = 'none';
  document.getElementById('export-route-dest').value = '';
  modal.classList.add('open');
  setTimeout(function(){ document.getElementById('export-route-dest').focus(); }, 50);
}

export function closeExportRouteModal() {
  document.getElementById('export-route-modal').classList.remove('open');
}

export async function submitExportRoute() {
  var dest = document.getElementById('export-route-dest').value.trim();
  var err  = document.getElementById('export-route-error');
  if (!dest) { err.textContent = 'Please enter a destination path.'; err.style.display = 'block'; return; }
  err.style.display = 'none';

  var btn = document.getElementById('export-route-submit');
  btn.disabled = true;
  btn.textContent = 'Exporting…';

  try {
    var data = await apiPost('/api/export-route', {dest_dir: dest, folder: getMvCurrentFolder()});
    btn.textContent = '✓ ' + data.copied + ' file' + (data.copied !== 1 ? 's' : '') + ' copied';
    setTimeout(closeExportRouteModal, 1500);
  } catch(e) {
    err.textContent = e.detail || ('Export failed: ' + e.message);
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Export';
  }
}

export function bulkDelete() {
  var n = _selectedJobs.size;
  if (!n) return;
  var msg = 'Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '? This cannot be undone.';
  openDeleteModal(msg, async function() {
    var metas = Array.from(_selectedMeta.values());
    for (var i = 0; i < metas.length; i++) {
      var j = metas[i];
      try {
        await apiDelete(jobApiUrl(j.path));
        if (st._activeJob === j.path) {
          st._activeJob = null; st._activeJobFolder = null; st._dirty = false;
          import('../editor/form-controls.js').then(function(m){ m._doNewJob(); });
        }
      } catch(err) { showError(err.detail || ('Delete failed: ' + err.message)); }
    }
    clearSelection();
    await loadJobsList();
  });
}

export function unifiedMerge()      { if (getMvMode()) { mvMerge(); }       else { openMergeModal(); } }
export function unifiedBulkMove()   { if (getMvMode()) { mvBulkMove(); }    else { bulkMove(); } }
export function unifiedBulkDelete() { if (getMvMode()) { mvBulkDelete(); }  else { bulkDelete(); } }
export function unifiedClearSel()   { if (getMvMode()) { mvClearSel(); }    else { clearSelection(); } }
