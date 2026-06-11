// ── Multi-select & bulk operations ────────────────────────────────────────────

import { escHtml } from './utils.js';
import { showError } from './form-controls.js';
import { loadJobsList } from './jobs-panel.js';
// Circular — only called at runtime:
import { getMvMode, getMvLayers, _mvToggleSel, mvClearSel } from './map-view.js';
import { openJob } from './job-ops.js';

export var _selectedJobs = new Set();
export var _selectedMeta = new Map();

export function toggleJobSelection(j, selected) {
  if (selected) {
    _selectedJobs.add(j.path);
    _selectedMeta.set(j.path, j);
  } else {
    _selectedJobs.delete(j.path);
    _selectedMeta.delete(j.path);
  }
  var card = document.querySelector('.jcard[data-path="' + CSS.escape(j.path) + '"]');
  if (card) card.classList.toggle('selected', selected);
  if (getMvMode()) {
    if (selected) {
      _mvToggleSel(j.path); // will add to _mvSelected and style
    } else {
      _mvToggleSel(j.path); // will remove from _mvSelected and style
    }
    // _mvUpdateSelBar is called by _mvToggleSel internally
  }
  _updateSelBar();
}

export function clearSelection() {
  _selectedJobs.clear();
  _selectedMeta.clear();
  document.querySelectorAll('.jcard.selected').forEach(function(c) {
    c.classList.remove('selected');
    var chk = c.querySelector('.jcard-chk');
    if (chk) chk.checked = false;
  });
  if (getMvMode()) {
    mvClearSel();
  }
  _updateSelBar();
}

export function _updateSelBar() {
  var n = _selectedJobs.size;
  var bar = document.getElementById('jp-sel-bar');
  bar.classList.toggle('visible', n > 0);
  document.getElementById('jp-sel-count').textContent = n + ' selected';
  document.getElementById('sel-merge-btn').disabled = n < 2;
}

export function openMergeModal() {
  if (_selectedJobs.size < 2) return;
  var jobs = Array.from(_selectedMeta.values());
  var names = jobs.map(function(j){ return j.name; });
  var allUntouched = jobs.every(function(j){ return j.untouched; });
  var strategyNote = allUntouched
    ? '(IDs will be combined — geometry re-fetched on preview)'
    : '(polygons will be unioned)';
  document.getElementById('merge-sources').innerHTML =
    'Merging: <b>' + names.map(escHtml).join(', ') + '</b><br>'
    + '<span style="font-size:9px;color:#64748b">' + strategyNote + '</span>';
  document.getElementById('merge-name').value = names[0] + '-merged';
  document.getElementById('merge-folder').value = '';
  document.getElementById('merge-del-src').checked = false;
  document.getElementById('merge-modal').classList.add('open');
  setTimeout(function(){ document.getElementById('merge-name').focus(); document.getElementById('merge-name').select(); }, 50);
}

export function closeMergeModal() {
  document.getElementById('merge-modal').classList.remove('open');
}

export async function submitMerge() {
  var newName = document.getElementById('merge-name').value.trim();
  if (!newName) { document.getElementById('merge-name').focus(); return; }
  var folder = document.getElementById('merge-folder').value.trim() || null;
  var delSrc  = document.getElementById('merge-del-src').checked;
  closeMergeModal();

  try {
    var r = await fetch('/api/merge', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        job_paths: Array.from(_selectedJobs),
        new_name: newName,
        folder: folder,
        delete_sources: delSrc
      })
    });
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Merge failed'); return;
    }
    var merged = await r.json().catch(function(){return null;});
    clearSelection();
    await loadJobsList();
    if (merged && merged.path) openJob(merged.path);
  } catch(e) { showError('Merge failed: ' + e.message); }
}
