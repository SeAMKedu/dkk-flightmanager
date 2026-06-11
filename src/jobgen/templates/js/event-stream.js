// ── External-change event stream ──────────────────────────────────────────────

import { st } from './state.js';
import { loadJobsList } from './jobs-panel.js';
// Circular — only called at runtime:
import { openJob } from './job-ops.js';

export function _initEventStream() {
  var es = new EventSource('/api/events');
  var _debounceTimer = null;

  es.onmessage = function(e) {
    var evt;
    try { evt = JSON.parse(e.data); } catch(ex) { return; }
    if (evt.type !== 'jobs_changed') return;

    if (_debounceTimer) clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(function() {
      loadJobsList();
      if (st._activeJob && !st.isRunning && evt.paths && evt.paths.indexOf(st._activeJob) !== -1) {
        if (st._activeJob === st._ownSavedJob) {
          st._ownSavedJob = null;
        } else {
          showExtModifiedNotice();
        }
      }
    }, 800);
  };

  es.onerror = function() {
    // EventSource reconnects automatically
  };
}

export function showExtModifiedNotice() {
  var el = document.getElementById('ext-modified-notice');
  el.innerHTML = '<span style="flex:1">Job modified externally.</span>'
    + '<button class="ext-mod-btn" onclick="reloadCurrentJob()">Reload</button>'
    + '<button class="ext-mod-btn" onclick="hideExtModifiedNotice()">✕</button>';
  el.classList.add('visible');
}

export function hideExtModifiedNotice() {
  var el = document.getElementById('ext-modified-notice');
  el.classList.remove('visible');
  el.innerHTML = '';
}

export function reloadCurrentJob() {
  hideExtModifiedNotice();
  if (st._activeJob) openJob(st._activeJob);
}
