// ── Preview & Export runners ──────────────────────────────────────────────────

async function startPreview() {
  if (isRunning || editMode) return;  // don't preview while editing — renderMap clears editLayers
  clearError();
  var p = getParams();
  if (polyModified) p.custom_polygon = editedPoly;
  if (!p.parcel_ids.length && !p.property_ids.length && !p.custom_polygon) {
    showError('Enter at least one parcel ID or property ID.'); return;
  }
  await runJob('/api/preview', p, 'Preview', onPreviewDone);
}

async function startExport() {
  if (isRunning) return;
  clearError();
  var jn = document.getElementById('jname').value.trim();
  if (!jn) { showError('Enter a job name.'); return; }
  if (editMode) saveEdit();  // commit any pending vertex edits before saving
  var colorEl = document.getElementById('job-color');
  var p = Object.assign(getParams(), {
    job_name: jn,
    folder: _activeJobFolder || null,
    color: colorEl.value !== _DEFAULT_JOB_COLOR ? colorEl.value : null,
    custom_polygon: polyModified ? editedPoly : null,
    takeoff_point_4326: _takeoffPt || null
  });
  await runJob('/api/export', p, 'Saving…', onSaveDone);
}

async function runJob(endpoint, params, label, onDone) {
  isRunning = true;
  document.getElementById('xb').disabled = true;
  showToast(label + '…', 0, 'Starting…');
  showPg(true, 0, 'Starting…');

  var res;
  try {
    res = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(params)
    });
  } catch(e) { onErr('Network error: ' + e.message); return; }

  if (!res.ok) {
    var e2 = await res.json().catch(function(){return {detail:'HTTP ' + res.status};});
    onErr((e2.detail || 'Server error') + ' (HTTP ' + res.status + ')'); return;
  }

  var data = await res.json();
  var jid = data.job_id;
  console.log('[' + label + '] job_id=' + jid);

  if (currentSSE) currentSSE.close();
  currentSSE = new EventSource('/api/progress/' + jid);

  currentSSE.onmessage = function(e) {
    var d;
    try { d = JSON.parse(e.data); } catch(ex) { console.error('SSE parse error', e.data); return; }
    console.log('[sse]', d.stage, d.pct + '%', d.msg || '');
    if (d.stage === 'keepalive') return;
    if (d.stage === 'error') {
      currentSSE.close(); onErr(d.msg);
    } else if (d.stage === 'done') {
      currentSSE.close(); finishRun(); onDone(d.payload);
    } else {
      showPg(true, d.pct, d.msg);
      showToast(null, d.pct, d.msg);
    }
  };

  currentSSE.onerror = function(ev) {
    console.error('[sse] onerror', ev, 'readyState='+currentSSE.readyState);
    if (currentSSE.readyState === EventSource.CLOSED) return;
    currentSSE.close();
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
  isRunning = false;
  // xb state is owned by each completion callback (onPreviewDone/onSaveDone/onErr)
  document.getElementById('toast').style.display = 'none';
  showPg(false, 0, '');
  if (_pendingPreview) { _pendingPreview = false; startPreview(); }
}
function onErr(msg) {
  console.error('[err]', msg);
  finishRun();
  document.getElementById('xb').disabled = !previewData;
  document.getElementById('toast').style.display = 'none';
  showError(msg);
}

// ── Save completion callback ──────────────────────────────────────────────────
function onSaveDone(payload) {
  console.log('[save done]', payload);
  document.getElementById('xb').disabled = false;
  _activeJob = payload.job_name ? (payload.folder ? payload.folder + '/' + payload.job_name : payload.job_name) : null;
  _activeJobFolder = payload.folder || null;
  _ownSavedJob = _activeJob;
  _dirty = false;
  if (payload.stats) renderStatus(payload.stats);
  setJpOpen(true);
  loadJobsList();
}
