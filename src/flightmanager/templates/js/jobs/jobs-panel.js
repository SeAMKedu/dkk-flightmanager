// ── Jobs panel ────────────────────────────────────────────────────────────────

import { st } from '../core/state.js';
import { escHtml } from '../core/utils.js';
import { apiGet, apiPost } from '../core/api.js';
import { showError, updateFolderHint } from '../editor/form-controls.js';
// Circular — only called at runtime:
import { toggleJobSelection, _updateSelBar, _selectedJobs, _selectedMeta } from './multi-select.js';
import { toggleCardMenu } from './card-menu.js';
import { openJob } from './job-ops.js';

var _jpOpen = localStorage.getItem('jp-open') !== 'false';
export var _jobsCache = [];
export var _jobsGroups = [];
var _dragPath = null;
var _dragFolder = null;

function _jpStickyRefresh() {
  var list = document.getElementById('jp-list');
  if (!list) return;
  var listRect = list.getBoundingClientRect();
  var offset = 0;
  list.querySelectorAll(':scope > .jfolder').forEach(function(folder) {
    var hdr = folder.querySelector(':scope > .jfolder-hdr');
    if (!hdr) return;
    hdr.style.top = offset + 'px';
    var fr = folder.getBoundingClientRect();
    var top = fr.top - listRect.top;
    var bottom = fr.bottom - listRect.top;
    var hdrH = hdr.offsetHeight;
    if (top < offset && bottom > offset + hdrH) offset += hdrH;
  });
}
document.getElementById('jp-list').addEventListener('scroll', _jpStickyRefresh);

export function setJpOpen(open) {
  _jpOpen = open;
  localStorage.setItem('jp-open', open ? 'true' : 'false');
  document.getElementById('jp').classList.toggle('closed', !open);
  document.getElementById('jp-tog').innerHTML = open ? '&#9664;' : '&#9654;';
  document.getElementById('jp-tog').title = open ? 'Hide jobs panel' : 'Show jobs panel';
}
export function toggleJp() { setJpOpen(!_jpOpen); }

document.getElementById('jp-filter').addEventListener('input', function() {
  renderJobsList(_jobsGroups);
});

export async function loadJobsList() {
  try {
    var data;
    try { data = await apiGet('/api/jobs'); } catch { return; }
    _jobsGroups = data.groups || [];
    _jobsCache = [];
    _jobsGroups.forEach(function(g){ _jobsCache = _jobsCache.concat(g.jobs || []); });
    var validPaths = new Set(_jobsCache.map(function(j){return j.path;}));
    _selectedJobs.forEach(function(p){ if (!validPaths.has(p)) { _selectedJobs.delete(p); _selectedMeta.delete(p); } });
    _updateSelBar();
    if (_jobsCache.length > 0 && localStorage.getItem('jp-open') === null) {
      setJpOpen(true);
    }
    renderJobsList(_jobsGroups);
  } catch(e) { console.error('[loadJobsList]', e); }
}

function renderJobsList(groups) {
  var list = document.getElementById('jp-list');
  var filter = (document.getElementById('jp-filter').value || '').toLowerCase();
  list.innerHTML = '';

  if (filter) {
    var matched = _jobsCache.filter(function(j){ return j.name.toLowerCase().includes(filter); });
    if (!matched.length) {
      list.innerHTML = '<div style="padding:16px 8px;color:#475569;font-size:11px;text-align:center">No matches</div>';
      return;
    }
    matched.forEach(function(j){ list.appendChild(buildJobCard(j)); });
    return;
  }

  var hasNamedGroups = groups.some(function(g){ return g.name !== null; });
  if (!_jobsCache.length && !hasNamedGroups) {
    list.innerHTML = '<div style="padding:16px 8px;color:#475569;font-size:11px;text-align:center">No saved jobs yet</div>';
    return;
  }

  groups.forEach(function(g) { list.appendChild(buildFolderSection(g)); });
  _jpStickyRefresh();
}

function buildFolderSection(group) {
  var frag = document.createDocumentFragment();
  var isRoot = group.name === null || group.name === undefined;
  var folderKey = isRoot ? null : group.name;
  var storageKey = 'jf-open-' + (isRoot ? '__root__' : group.name);
  var isOpen = localStorage.getItem(storageKey) !== 'false';

  var displayName = isRoot ? (st.outputDir.split('/').pop() || 'output') : group.name;
  var dataFolder = isRoot ? '' : escHtml(group.name);

  var hdr = document.createElement('div');
  hdr.className = 'jfolder-hdr';
  hdr.innerHTML = '<span class="jfolder-caret' + (isOpen ? ' open' : '') + '">&#9658;</span>'
    + '<span class="jfolder-name" title="' + escHtml(displayName) + '">' + escHtml(displayName) + '</span>'
    + '<span class="jfolder-count">' + group.jobs.length + '</span>'
    + '<button class="jfolder-sel-all-btn" title="Select all in folder">&#10003;</button>'
    + '<button class="jfolder-map-btn" data-folder="' + dataFolder + '" title="Show jobs on map"'
    + ' onclick="showFolderOnMap(event,' + (isRoot ? 'null' : '\'' + escHtml(group.name) + '\'') + ')">Map</button>';

  var container = document.createElement('div');
  container.className = 'jfolder';
  var jobs = document.createElement('div');
  jobs.className = 'jfolder-jobs' + (isOpen ? '' : ' hidden');

  hdr.querySelector('.jfolder-sel-all-btn').addEventListener('click', function(e) {
    e.stopPropagation();
    var folderJobs = group.jobs || [];
    var allSelected = folderJobs.length > 0 && folderJobs.every(function(j){ return _selectedJobs.has(j.path); });
    folderJobs.forEach(function(j){ toggleJobSelection(j, !allSelected); });
    var chks = jobs.querySelectorAll('.jcard-chk');
    chks.forEach(function(chk){ chk.checked = !allSelected; });
  });

  hdr.addEventListener('click', function(e) {
    if (e.target.closest('button')) return;
    isOpen = !isOpen;
    localStorage.setItem(storageKey, isOpen ? 'true' : 'false');
    jobs.classList.toggle('hidden', !isOpen);
    hdr.querySelector('.jfolder-caret').classList.toggle('open', isOpen);
  });

  // Double-click the name to rename the folder (real folders only, not root).
  if (!isRoot) {
    var nameEl = hdr.querySelector('.jfolder-name');
    nameEl.title = 'Double-click to rename';
    nameEl.addEventListener('dblclick', function(e) {
      e.stopPropagation();
      _startFolderRename(group.name, nameEl);
    });
  }

  jobs.addEventListener('dragover', function(e) { e.preventDefault(); });
  jobs.addEventListener('drop', function(e) {
    e.preventDefault();
    if (!_dragPath) return;
    import('./drag-reorder.js').then(function(m){ m._finishDrop(group, folderKey, null, 'after'); });
  });

  (group.jobs || []).forEach(function(j){ jobs.appendChild(buildJobCard(j, group, folderKey)); });
  container.appendChild(hdr);
  container.appendChild(jobs);
  frag.appendChild(container);
  return frag;
}

function _startFolderRename(oldName, nameEl) {
  var input = document.createElement('input');
  input.className = 'jcard-rename-input';
  input.value = oldName;
  // The header swallows single clicks to toggle; keep them off the input.
  input.addEventListener('click', function(e){ e.stopPropagation(); });
  nameEl.replaceWith(input);
  input.focus(); input.select();
  var committed = false;
  function commit() {
    if (committed) return; committed = true;
    var newName = input.value.trim();
    if (!newName || newName === oldName) { loadJobsList(); return; }
    _doFolderRename(oldName, newName);
  }
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', function(e) {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { committed = true; loadJobsList(); }
  });
}

async function _doFolderRename(oldName, newName) {
  try {
    await apiPost('/api/folders/' + encodeURIComponent(oldName) + '/rename', { new_name: newName });
    // If the open job lives in the renamed folder, repoint its path + breadcrumb.
    if (st._activeJob && st._activeJob.indexOf(oldName + '/') === 0) {
      st._activeJob = newName + st._activeJob.slice(oldName.length);
    }
    if (st._activeJobFolder === oldName) {
      st._activeJobFolder = newName;
      updateFolderHint();
    }
    await loadJobsList();
  } catch (e) {
    showError(e.detail || ('Folder rename failed: ' + e.message));
    await loadJobsList();
  }
}

export function buildJobCard(j, group, folderKey) {
  var card = document.createElement('div');
  var isActive = j.path === st._activeJob;
  var isSelected = _selectedJobs.has(j.path);
  var isReady = !!j.takeoff_point_4326;
  card.className = 'jcard'
    + (isActive ? ' active' : '')
    + (j.status === 'failed' ? ' failed' : '')
    + (isSelected ? ' selected' : '');
  card.dataset.path = j.path;
  var date = j.saved_at || j.run_at || '';
  var dateStr = date ? new Date(date).toLocaleString('fi-FI',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
  var meta = [dateStr, j.area_ha != null ? j.area_ha.toFixed(1)+' ha' : '', j.drone||''].filter(Boolean).join(' · ');

  var priorityBadge = '';
  if (isReady) {
    var readyJobs = group ? (group.jobs || []).filter(function(x){ return x.takeoff_point_4326; }) : [];
    var idx = readyJobs.indexOf(j);
    if (j.sort_order != null) {
      priorityBadge = '<span class="jbadge priority">' + (idx + 1) + '</span>';
    } else {
      priorityBadge = '<span class="jbadge priority" style="opacity:.45">' + (idx + 1) + '</span>';
    }
  }

  var badge = j.status === 'failed' ? '<span class="jbadge fail">!</span>'
    : j.untouched              ? '<span class="jbadge untouched">new</span>'
    : j.flight_ready === true  ? '<span class="jbadge ok">&#10003;</span>'
    : j.needs_review === true  ? '<span class="jbadge wrn">!</span>'
    : '';
  var koLine = (j.area_lost_pct != null && j.area_lost_pct >= st._cfgMaxAreaLossPct)
    ? '<div class="jcard-ko">−' + j.area_lost_pct.toFixed(0) + '% keepout</div>'
    : '';
  var dragHandle = isReady
    ? '<span class="jcard-drag" title="Drag to reorder">&#8942;&#8942;</span>'
    : '<span class="jcard-drag" style="visibility:hidden" aria-hidden="true">&#8942;&#8942;</span>';
  var thumb = j.thumbnail_svg || '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#1e293b"/><text x="32" y="40" text-anchor="middle" font-size="28" fill="#334155">?</text></svg>';
  card.innerHTML =
    '<label class="jcard-sel" title="Select"><input type="checkbox" class="jcard-chk"' + (isSelected ? ' checked' : '') + '></label>'
    + dragHandle
    + '<div class="jcard-thumb">' + thumb + '</div>'
    + '<div class="jcard-body">'
    +   '<div class="jcard-name">' + escHtml(j.name) + '</div>'
    +   '<div class="jcard-meta">' + escHtml(meta) + '</div>'
    +   koLine
    + '</div>'
    + '<div class="jcard-right">' + priorityBadge + badge
    +   '<button class="jcard-menu-btn" title="Actions">&#8942;</button>'
    + '</div>';

  card.querySelector('.jcard-menu-btn').addEventListener('click', function(e) {
    toggleCardMenu(e, j);
  });

  var chk = card.querySelector('.jcard-chk');
  chk.addEventListener('change', function(e) {
    e.stopPropagation();
    toggleJobSelection(j, chk.checked);
  });

  if (j.status !== 'failed') {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.jcard-menu-btn') || e.target.closest('.jmenu') || e.target.closest('.jcard-sel') || e.target.closest('.jcard-drag')) return;
      openJob(j.path);
    });
  }

  if (isReady && group) {
    card.draggable = true;
    card.addEventListener('dragstart', function(e) {
      _dragPath = j.path;
      _dragFolder = folderKey;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    card.addEventListener('dragend', function() {
      card.classList.remove('dragging');
      document.querySelectorAll('.jcard').forEach(function(c){
        c.classList.remove('drag-over-top', 'drag-over-bottom');
      });
    });
    card.addEventListener('dragover', function(e) {
      if (!_dragPath || _dragFolder !== folderKey) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      var rect = card.getBoundingClientRect();
      var mid = rect.top + rect.height / 2;
      document.querySelectorAll('.jcard').forEach(function(c){
        c.classList.remove('drag-over-top', 'drag-over-bottom');
      });
      card.classList.add(e.clientY < mid ? 'drag-over-top' : 'drag-over-bottom');
    });
    card.addEventListener('dragleave', function() {
      card.classList.remove('drag-over-top', 'drag-over-bottom');
    });
    card.addEventListener('drop', function(e) {
      e.preventDefault();
      e.stopPropagation();
      if (!_dragPath || _dragPath === j.path) return;
      var rect = card.getBoundingClientRect();
      var pos = e.clientY < rect.top + rect.height / 2 ? 'before' : 'after';
      card.classList.remove('drag-over-top', 'drag-over-bottom');
      import('./drag-reorder.js').then(function(m){ m._finishDrop(group, folderKey, j.path, pos); });
    });
  }

  return card;
}

export function getDragPath() { return _dragPath; }
export function getDragFolder() { return _dragFolder; }
