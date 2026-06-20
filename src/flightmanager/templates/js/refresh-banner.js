// ── Stale-jobs refresh banner ────────────────────────────────────────────────
// Shows "N jobs can be refreshed" when GET /api/refresh/scan finds jobs whose
// pipeline_version is behind or whose source data the cache now has newer copies
// of. "Refresh all" POSTs /api/refresh and streams progress; on completion it
// reloads the list and re-scans (banner hides when nothing is stale).

import { apiGet, apiPost } from './api.js';
import { loadJobsList } from './jobs-panel.js';
import { showError } from './form-controls.js';

var _dismissed = false;
var _busy = false;

export async function checkStaleJobs() {
  if (_dismissed || _busy) return;
  var banner = document.getElementById('refresh-banner');
  if (!banner) return;
  var data;
  try { data = await apiGet('/api/refresh/scan'); } catch { return; }
  var stale = data.stale || [];
  if (!stale.length) { banner.style.display = 'none'; return; }

  banner.dataset.paths = JSON.stringify(stale.map(function (s) { return s.path; }));
  banner.innerHTML =
    '<span class="rb-text">' + stale.length + ' job' + (stale.length > 1 ? 's' : '')
    + ' can be refreshed</span>'
    + '<span class="rb-actions">'
    + '<button id="rb-go">Refresh all</button>'
    + '<button id="rb-dismiss" title="Dismiss">&times;</button></span>';
  banner.style.display = 'flex';
  document.getElementById('rb-go').onclick = refreshAllStale;
  document.getElementById('rb-dismiss').onclick = function () {
    _dismissed = true; banner.style.display = 'none';
  };
}

export async function refreshAllStale() {
  var banner = document.getElementById('refresh-banner');
  if (!banner || _busy) return;
  var paths = JSON.parse(banner.dataset.paths || '[]');
  if (!paths.length) return;

  _busy = true;
  banner.innerHTML = '<span class="rb-text" id="rb-msg">Refreshing 0/' + paths.length + ' …</span>';
  var data;
  try {
    data = await apiPost('/api/refresh', { paths: paths });
  } catch (e) {
    _busy = false;
    showError(e.detail || ('Refresh failed: ' + e.message));
    checkStaleJobs();
    return;
  }

  var sse = new EventSource('/api/progress/' + data.job_id);
  sse.onmessage = function (ev) {
    var d = JSON.parse(ev.data);
    if (d.stage === 'keepalive') return;
    if (d.stage === 'refresh') {
      var msg = document.getElementById('rb-msg');
      if (msg) msg.textContent = d.msg || 'Refreshing…';
    } else if (d.stage === 'done') {
      sse.close(); _busy = false; _dismissed = false;
      var p = d.payload || {};
      banner.innerHTML = '<span class="rb-text">Refreshed ' + (p.recomputed || 0) + ' job'
        + ((p.recomputed === 1) ? '' : 's')
        + (p.flipped ? ' — ' + p.flipped + ' changed flight-ready/review status' : '')
        + (p.failed ? ' — ' + p.failed + ' failed' : '') + '.</span>';
      loadJobsList();
      setTimeout(checkStaleJobs, 1500);  // re-scan; hides banner if all clear
    } else if (d.stage === 'error') {
      sse.close(); _busy = false;
      showError('Refresh failed: ' + (d.msg || 'unknown'));
      checkStaleJobs();
    }
  };
  sse.onerror = function () {
    if (sse.readyState === EventSource.CLOSED) _busy = false;
  };
}
