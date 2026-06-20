// ── Card menu & folder operations ─────────────────────────────────────────────

import { st } from './state.js';
import { escHtml, jobApiUrl } from './utils.js';
import { apiPost } from './api.js';
import { showError } from './form-controls.js';
import { loadJobsList } from './jobs-panel.js';
import { openMoveModal } from './modal-utils.js';
// Circular — only called at runtime:
import { openJob } from './job-ops.js';
import { revealJob, startRename, confirmDeleteJob } from './job-ops.js';

var _openMenu = null;
export function getOpenMenu() { return _openMenu; }
export function setOpenMenu(m) { _openMenu = m; }

export function toggleCardMenu(e, j) {
  e.stopPropagation();
  closeCardMenu();
  var btn = e.currentTarget;
  var menu = document.createElement('div');
  menu.className = 'jmenu';
  var items = j.status === 'failed'
    ? [['Delete', function(){ confirmDeleteJob(j); }]]
    : [
        ['Open',            function(){ openJob(j.path); }],
        ['Show folder',     function(){ revealJob(j.path); }],
        ['Move to Folder',  function(){ showMoveMenu(btn, j); }],
        ['Clone',           function(){ import('./job-ops.js').then(function(m){ m.cloneJob(j.path); }); }],
        ['Rename',          function(){ startRename(j); }],
        ['Delete',          function(){ confirmDeleteJob(j); }],
      ];
  items.forEach(function(it) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item' + (it[0] === 'Delete' ? ' danger' : '');
    mi.textContent = it[0];
    mi.addEventListener('click', function(ev) { ev.stopPropagation(); closeCardMenu(); it[1](); });
    menu.appendChild(mi);
  });
  btn.closest('.jcard-right').appendChild(menu);
  _openMenu = menu;
  setTimeout(function() { document.addEventListener('click', closeCardMenu, {once:true}); }, 0);
}

export function closeCardMenu() {
  if (_openMenu) { _openMenu.remove(); _openMenu = null; }
}

export function showMoveMenu(btn, j) {
  closeCardMenu();
  openMoveModal(
    'Move "' + j.name + '"',
    [{path: j.path, name: j.name, folder: j.folder || null}],
    function(toFolder) { doMoveJob(j, toFolder); }
  );
}

export async function doMoveJob(j, toFolder) {
  try {
    var data = await apiPost(jobApiUrl(j.path, '/move'), {folder: toFolder});
    if (st._activeJob === j.path) {
      st._activeJob = data.path;
      st._activeJobFolder = data.folder || null;
    }
    await loadJobsList();
  } catch(e) { showError(e.detail || ('Move failed: ' + e.message)); }
}

export function createFolder() {
  document.getElementById('folder-name-input').value = '';
  document.getElementById('folder-modal').classList.add('open');
  setTimeout(function(){ document.getElementById('folder-name-input').focus(); }, 50);
}

export function closeFolderDialog() {
  document.getElementById('folder-modal').classList.remove('open');
}

export async function submitFolder() {
  var name = document.getElementById('folder-name-input').value.trim();
  if (!name) return;
  var errEl = document.getElementById('folder-error');
  errEl.style.display = 'none';
  var btn = document.getElementById('folder-submit');
  btn.disabled = true;
  try {
    await apiPost('/api/folders', {name: name});
    closeFolderDialog();
    await loadJobsList();
  } catch(e) {
    errEl.textContent = e.detail || ('Failed: ' + e.message);
    errEl.style.display = 'block';
  } finally { btn.disabled = false; }
}

