// ── Bulk move / KML export / Google Maps / route rename / export route / bulk delete ──

import { st } from './state.js';
import { escHtml, jobApiUrl, _escapeXml, _hexToKmlColor } from './utils.js';
import { showError } from './form-controls.js';
import { loadJobsList } from './jobs-panel.js';
import { _selectedJobs, _selectedMeta, clearSelection, openMergeModal } from './multi-select.js';
import { closeCardMenu, getOpenMenu, setOpenMenu } from './card-menu.js';
// Circular — only called at runtime:
import { getMvMode, getMvSelected, getMvCurrentFolder, openMapView,
         mvMerge, mvBulkMove, mvBulkDelete, mvClearSel } from './map-view.js';
import { openJob } from './job-ops.js';

export function bulkMove() {
  if (!_selectedJobs.size) return;
  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el){
    var n = el.textContent.trim(); if (n) folderNames.push(n);
  });

  var btn = document.getElementById('mv-move-btn');
  closeCardMenu();
  var sub = document.createElement('div');
  sub.className = 'jmenu';
  sub.style.cssText = 'position:fixed;z-index:9999';
  var rect = btn.getBoundingClientRect();
  sub.style.top = (rect.bottom + 4) + 'px';
  sub.style.left = rect.left + 'px';

  var makeItem = function(label, fn) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item'; mi.textContent = label;
    mi.addEventListener('click', function(ev){ ev.stopPropagation(); sub.remove(); fn(); });
    sub.appendChild(mi);
  };

  makeItem('Move to root', function(){ _bulkMoveToFolder(null); });
  folderNames.forEach(function(name){ makeItem('→ ' + name, function(){ _bulkMoveToFolder(name); }); });
  makeItem('+ New folder…', function(){
    var name = window.prompt('New folder name:');
    if (!name || !name.trim()) return;
    name = name.trim();
    fetch('/api/folders', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})})
      .then(function(){ _bulkMoveToFolder(name); })
      .catch(function(e){ showError('Failed: ' + e.message); });
  });

  document.body.appendChild(sub);
  setOpenMenu(sub);
  setTimeout(function(){ document.addEventListener('click', closeCardMenu, {once:true}); }, 0);
}

async function _bulkMoveToFolder(toFolder) {
  var metas = Array.from(_selectedMeta.values());
  for (var i = 0; i < metas.length; i++) {
    var j = metas[i];
    try {
      var r = await fetch(jobApiUrl(j.path, '/move'), {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({folder: toFolder})
      });
      if (!r.ok) {
        var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
        showError('Move failed for ' + j.name + ': ' + (e.detail||''));
      } else {
        var data = await r.json();
        if (st._activeJob === j.path) { st._activeJob = data.path; st._activeJobFolder = data.folder || null; }
      }
    } catch(err) { showError('Move failed: ' + err.message); }
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
      var r = await fetch(jobApiUrl(paths[i]));
      if (!r.ok) continue;
      var data = await r.json();
      jobs.push({path: paths[i], params: data.params});
    } catch (e) { /* skip */ }
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

export async function exportKml() {
  var result = await _loadSelectedJobs();
  if (!result) return;
  var jobs = result.jobs, paths = result.paths;

  var kml = ['<?xml version="1.0" encoding="UTF-8"?>',
    '<kml xmlns="http://www.opengis.net/kml/2.2">',
    '<Document><name>DKK Jobs</name>'];

  jobs.forEach(function(job) {
    var p = job.params;
    var name = p.job_name || job.path;
    var lineColor = _hexToKmlColor(p.color, 'ff');
    var fillColor = _hexToKmlColor(p.color, '55');

    kml.push('<Folder><name>' + _escapeXml(name) + '</name>');

    var poly = p.custom_polygon_4326;
    if (poly && poly.coordinates && poly.coordinates[0]) {
      var ring = poly.coordinates[0];
      var coords = ring.map(function(c){ return c[0]+','+c[1]+',0'; }).join(' ');
      kml.push('<Placemark>');
      kml.push('<name>' + _escapeXml(name) + '</name>');
      kml.push('<Style>');
      kml.push('<LineStyle><color>' + lineColor + '</color><width>2</width></LineStyle>');
      kml.push('<PolyStyle><color>' + fillColor + '</color></PolyStyle>');
      kml.push('</Style>');
      kml.push('<Polygon><outerBoundaryIs><LinearRing>');
      kml.push('<coordinates>' + coords + '</coordinates>');
      kml.push('</LinearRing></outerBoundaryIs></Polygon>');
      kml.push('</Placemark>');
    }

    var tp = p.takeoff_point_4326;
    if (tp) {
      kml.push('<Placemark>');
      kml.push('<name>' + _escapeXml(name) + '</name>');
      kml.push('<Style><IconStyle><color>' + lineColor + '</color></IconStyle></Style>');
      kml.push('<Point><coordinates>' + tp[0] + ',' + tp[1] + ',0</coordinates></Point>');
      kml.push('</Placemark>');
    }

    kml.push('</Folder>');
  });

  kml.push('</Document></kml>');

  var folders = new Set(paths.map(function(p){ var s = p.indexOf('/'); return s >= 0 ? p.slice(0, s) : null; }));
  var fileName = (folders.size === 1 && Array.from(folders)[0] !== null)
    ? 'dkk-' + Array.from(folders)[0] + '.kml'
    : 'dkk-jobs.kml';

  var blob = new Blob([kml.join('\n')], {type: 'application/vnd.google-earth.kml+xml'});
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
  var n = jobs.length;
  if (!n) return;

  var today = new Date();
  var dd = today.getFullYear().toString()
    + String(today.getMonth() + 1).padStart(2, '0')
    + String(today.getDate()).padStart(2, '0');
  var digits = n >= 100 ? 3 : 2;

  for (var i = 0; i < jobs.length; i++) {
    var job = jobs[i];
    var baseName = (job.params.job_name || job.path.replace(/^.*\//, ''))
      .replace(_ROUTE_PREFIX_RE, '');
    var idx = String(i + 1).padStart(digits, '0');
    var newName = dd + '-' + idx + '-' + baseName;
    if (newName === baseName) continue;
    try {
      var r = await fetch(jobApiUrl(job.path), {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({new_name: newName})
      });
      if (!r.ok) {
        var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
        showError('Rename failed for ' + baseName + ': ' + (e.detail || ''));
      } else {
        var data = await r.json();
        if (st._activeJob === job.path) { st._activeJob = data.path; }
        job.path = data.path;
      }
    } catch(err) { showError('Rename failed: ' + err.message); }
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
    var r = await fetch('/api/export-route', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dest_dir: dest, folder: getMvCurrentFolder()})
    });
    var data = await r.json();
    if (!r.ok) {
      err.textContent = data.detail || 'Export failed (HTTP ' + r.status + ')';
      err.style.display = 'block';
      return;
    }
    btn.textContent = '✓ ' + data.copied + ' file' + (data.copied !== 1 ? 's' : '') + ' copied';
    setTimeout(closeExportRouteModal, 1500);
  } catch(e) {
    err.textContent = 'Export failed: ' + e.message;
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Export';
  }
}

export async function bulkDelete() {
  var n = _selectedJobs.size;
  if (!n) return;
  if (!window.confirm('Delete ' + n + ' selected job' + (n > 1 ? 's' : '') + '? This cannot be undone.')) return;
  var metas = Array.from(_selectedMeta.values());
  for (var i = 0; i < metas.length; i++) {
    var j = metas[i];
    try {
      var r = await fetch(jobApiUrl(j.path), {method:'DELETE'});
      if (r.ok && st._activeJob === j.path) {
        st._activeJob = null; st._activeJobFolder = null; st._dirty = false;
        import('./form-controls.js').then(function(m){ m._doNewJob(); });
      }
    } catch(err) { showError('Delete failed: ' + err.message); }
  }
  clearSelection();
  await loadJobsList();
}

export function unifiedMerge()      { if (getMvMode()) { mvMerge(); }       else { openMergeModal(); } }
export function unifiedBulkMove()   { if (getMvMode()) { mvBulkMove(); }    else { bulkMove(); } }
export function unifiedBulkDelete() { if (getMvMode()) { mvBulkDelete(); }  else { bulkDelete(); } }
export function unifiedClearSel()   { if (getMvMode()) { mvClearSel(); }    else { clearSelection(); } }
