// ── Card menu & folder operations ─────────────────────────────────────────────

import { st } from './state.js';
import { escHtml, jobApiUrl } from './utils.js';
import { showError } from './form-controls.js';
import { loadJobsList } from './jobs-panel.js';
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
  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el){
    var n = el.textContent.trim();
    if (n) folderNames.push(n);
  });

  var sub = document.createElement('div');
  sub.className = 'jmenu jmenu-sub';

  var makeItem = function(label, fn) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item';
    mi.textContent = label;
    mi.addEventListener('click', function(ev){ ev.stopPropagation(); sub.remove(); fn(); });
    sub.appendChild(mi);
  };

  if (j.folder) {
    makeItem('Move to root', function(){ doMoveJob(j, null); });
  }
  folderNames.forEach(function(name) {
    if (name !== j.folder) {
      makeItem('→ ' + name, function(){ doMoveJob(j, name); });
    }
  });
  makeItem('+ New folder…', function(){ promptNewFolderForJob(j); });

  btn.closest('.jcard-right').appendChild(sub);
  setTimeout(function(){ document.addEventListener('click', function(){ sub.remove(); }, {once:true}); }, 0);
}

export async function doMoveJob(j, toFolder) {
  try {
    var r = await fetch(jobApiUrl(j.path, '/move'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({folder: toFolder})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(err.detail || 'Move failed'); return;
    }
    var data = await r.json();
    if (st._activeJob === j.path) {
      st._activeJob = data.path;
      st._activeJobFolder = data.folder || null;
    }
    await loadJobsList();
  } catch(e) { showError('Move failed: ' + e.message); }
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
    var r = await fetch('/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      errEl.textContent = err.detail || 'Could not create folder';
      errEl.style.display = 'block';
      return;
    }
    closeFolderDialog();
    await loadJobsList();
  } catch(e) {
    errEl.textContent = 'Failed: ' + e.message;
    errEl.style.display = 'block';
  } finally { btn.disabled = false; }
}

export async function promptNewFolderForJob(j) {
  var name = window.prompt('New folder name:');
  if (!name || !name.trim()) return;
  name = name.trim();
  try {
    var r = await fetch('/api/folders', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      if (r.status !== 409) { showError(err.detail || 'Could not create folder'); return; }
    }
    await doMoveJob(j, name);
  } catch(e) { showError('Failed: ' + e.message); }
}
