// ── Preview & Export runners ──────────────────────────────────────────────────

import { st } from '../core/state.js';
import { apiPost } from '../core/api.js';
import { xbUpdate } from '../core/dirty-tracking.js';
import { getParams, showError, clearError } from './form-controls.js';
import { onPreviewDone } from '../map/map-layers.js';
import { renderStatus } from '../panels/status-panel.js';
import { loadJobsList, setJpOpen } from '../jobs/jobs-panel.js';
import { updateRouteStats } from './route-planner.js';

export async function startPreview() {
  if (st.isRunning || st.editMode) return;  // don't preview while editing — renderMap clears editLayers
  clearError();
  var p = getParams();
  if (st.polyModified) p.custom_polygon = st.editedPoly;
  // Send the user's pinned/saved takeoff so the route's home legs anchor there;
  // leave null until the user moves it so auto-suggest keeps tracking polygon edits.
  if (st.takeoff.userMoved) p.takeoff_point_4326 = st.takeoff.pt || null;
  if (!p.parcel_ids.length && !p.property_ids.length && !p.custom_polygon) {
    showError('Enter at least one parcel ID or property ID.'); return;
  }
  await runJob('/api/preview', p, 'Preview', onPreviewDone);
}

export async function startExport() {
  if (st.isRunning) return;
  clearError();
  var jn = document.getElementById('jname').value.trim();
  if (!jn) { showError('Enter a job name.'); return; }
  if (st.editMode) {
    // import saveEdit at runtime to avoid circular issues
    var pe = await import('./polygon-edit.js');
    pe.saveEdit();  // commit any pending vertex edits before saving
  }
  var colorEl = document.getElementById('job-color');
  var p = Object.assign(getParams(), {
    job_name: jn,
    folder: st._activeJobFolder || null,
    color: colorEl.value !== _DEFAULT_JOB_COLOR ? colorEl.value : null,
    custom_polygon: st.polyModified ? st.editedPoly : null,
    takeoff_point_4326: st.takeoff.pt || null
  });
  await runJob('/api/export', p, 'Saving…', onSaveDone);
}

var _DEFAULT_JOB_COLOR = '#3b82f6';

async function runJob(endpoint, params, label, onDone) {
  st.isRunning = true;
  document.getElementById('xb').disabled = true;
  showToast(label + '…', 0, 'Starting…');
  showPg(true, 0, 'Starting…');

  params.session_id = st.sessionId;
  var data;
  try {
    data = await apiPost(endpoint, params);
  } catch(e) {
    if (e.status) onErr((e.detail || 'Server error') + ' (HTTP ' + e.status + ')');
    else onErr('Network error: ' + e.message);
    return;
  }

  var jid = data.job_id;
  console.log('[' + label + '] job_id=' + jid);

  if (st.currentSSE) st.currentSSE.close();
  st.currentSSE = new EventSource('/api/progress/' + jid);

  st.currentSSE.onmessage = function(e) {
    var d;
    try { d = JSON.parse(e.data); } catch { console.error('SSE parse error', e.data); return; }
    console.log('[sse]', d.stage, d.pct + '%', d.msg || '');
    if (d.stage === 'keepalive') return;
    if (d.stage === 'error') {
      st.currentSSE.close(); onErr(d.msg);
    } else if (d.stage === 'done') {
      st.currentSSE.close(); finishRun(); onDone(d.payload);
    } else {
      showPg(true, d.pct, d.msg);
      showToast(null, d.pct, d.msg);
    }
  };

  st.currentSSE.onerror = function(ev) {
    console.error('[sse] onerror', ev, 'readyState='+st.currentSSE.readyState);
    if (st.currentSSE.readyState === EventSource.CLOSED) return;
    st.currentSSE.close();
    onErr('SSE connection lost (check server terminal for details).');
  };
}

function showPg(on, pct, msg) {
  var wrap = document.getElementById('pgwrap');
  wrap.style.opacity = on ? '1' : '0';
  wrap.style.pointerEvents = on ? '' : 'none';
  document.getElementById('pgfill').style.width = (pct||0) + '%';
  document.getElementById('pgmsg').textContent = on ? (msg || '') : '';
}
function showToast(title, pct, msg) {
  var t = document.getElementById('toast');
  t.style.display = 'block';
  if (title) document.getElementById('ttitle').textContent = title;
  document.getElementById('tfill').style.width = (pct||0) + '%';
  document.getElementById('tmsg').textContent = msg || '';
}
function finishRun() {
  st.isRunning = false;
  // xb state is owned by each completion callback (onPreviewDone/onSaveDone/onErr)
  document.getElementById('toast').style.display = 'none';
  showPg(false, 0, '');
  if (st._pendingPreview) { st._pendingPreview = false; startPreview(); }
}
function onErr(msg) {
  console.error('[err]', msg);
  finishRun();
  xbUpdate();
  document.getElementById('toast').style.display = 'none';
  showError(msg);
}

// ── Save completion callback ──────────────────────────────────────────────────
async function onSaveDone(payload) {
  console.log('[save done]', payload);
  st._activeJob = payload.job_name ? (payload.folder ? payload.folder + '/' + payload.job_name : payload.job_name) : null;
  st._activeJobFolder = payload.folder || null;
  st._ownSavedJob = st._activeJob;
  st._dirty = false;
  xbUpdate();
  if (payload.stats) {
    st._waypointMode = !!payload.stats.waypoint_mode;
    renderStatus(payload.stats);
  }
  // renderStatus rebuilds the DOM — restore route stats the pipeline doesn't include
  var _lastRouteStats = (await import('./route-planner.js'))._getLastRouteStats();
  if (_lastRouteStats) updateRouteStats(_lastRouteStats);
  setJpOpen(true);
  loadJobsList();
}
