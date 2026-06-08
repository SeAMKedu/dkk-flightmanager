// ── Batch dialog ──────────────────────────────────────────────────────────────

var _batchType = 'parcels';

function openBatchDialog() {
  var today = new Date();
  var iso = today.getFullYear() + '-'
    + String(today.getMonth()+1).padStart(2,'0') + '-'
    + String(today.getDate()).padStart(2,'0');
  var folderEl = document.getElementById('batch-folder');
  if (!folderEl.value) folderEl.value = 'batch-' + iso;

  var bdr = document.getElementById('batch-drone');
  if (!bdr.options.length) {
    var defOpt = document.createElement('option');
    defOpt.value = ''; defOpt.textContent = '(default)';
    bdr.appendChild(defOpt);
    drones.forEach(function(d) {
      var o = document.createElement('option');
      o.value = d.name; o.textContent = d.name;
      bdr.appendChild(o);
    });
  }

  document.getElementById('batch-form').style.display = 'flex';
  document.getElementById('batch-progress').style.display = 'none';
  document.getElementById('batch-modal').classList.add('open');
  _updateBatchCount();
}

function closeBatchDialog() {
  document.getElementById('batch-modal').classList.remove('open');
}

var _batchPlaceholders = {
  parcels:    'One parcel ID per line\n5241087453\n5241087454\n\nOr paste comma-separated',
  properties: 'One property ID per line\n214-407-3-22\n214-407-3-23\n\nOr paste comma-separated'
};

function setBatchType(type) {
  _batchType = type;
  document.getElementById('btype-parcels').classList.toggle('active', type === 'parcels');
  document.getElementById('btype-props').classList.toggle('active', type === 'properties');
  document.getElementById('batch-ids').placeholder = _batchPlaceholders[type];
}

function _parseBatchIds() {
  var raw = document.getElementById('batch-ids').value;
  var ids = [];
  raw.split('\n').forEach(function(line) {
    line = line.trim();
    if (!line || line.startsWith('#')) return;
    line.split(',').forEach(function(part) {
      var id = part.trim();
      if (id) ids.push(id);
    });
  });
  return ids;
}

function _updateBatchCount() {
  var n = _parseBatchIds().length;
  document.getElementById('batch-count').textContent = n;
  document.getElementById('batch-n').textContent = n;
  document.getElementById('batch-submit').disabled = n === 0;
}

document.getElementById('batch-ids').addEventListener('input', _updateBatchCount);

document.getElementById('batch-file-input').addEventListener('change', function(e) {
  var file = e.target.files[0];
  if (!file) return;
  document.getElementById('batch-file-name').textContent = file.name;
  var reader = new FileReader();
  reader.onload = function(ev) {
    var existing = document.getElementById('batch-ids').value.trim();
    var added = ev.target.result;
    document.getElementById('batch-ids').value = existing ? existing + '\n' + added : added;
    _updateBatchCount();
  };
  reader.readAsText(file);
  e.target.value = '';
});

async function submitBatch() {
  var ids = _parseBatchIds();
  if (!ids.length) return;

  var folder = document.getElementById('batch-folder').value.trim() || null;
  var drone  = document.getElementById('batch-drone').value || null;
  var height = parseFloat(document.getElementById('batch-height').value) || null;
  var sub    = document.getElementById('batch-sub').value;

  var params = {
    drone: drone, height_m: height, subcategory: sub,
    offset_m: 0, simplify: 'auto', keepout: true, preview_radius_m: null,
  };

  document.getElementById('batch-form').style.display = 'none';
  document.getElementById('batch-progress').style.display = 'flex';
  document.getElementById('batch-prog-title').textContent = 'Creating ' + ids.length + ' jobs…';
  document.getElementById('bpgfill').style.width = '0%';
  document.getElementById('bpgmsg').textContent = 'Starting…';
  document.getElementById('batch-results').innerHTML = '';
  document.getElementById('batch-prog-close').disabled = true;

  var res;
  try {
    res = await fetch('/api/batch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: ids, id_type: _batchType, folder: folder, params: params})
    });
  } catch(e) {
    _batchError('Network error: ' + e.message); return;
  }
  if (!res.ok) {
    var e2 = await res.json().catch(function(){return{detail:'HTTP '+res.status};});
    _batchError(e2.detail || 'Batch failed'); return;
  }

  var jobId = (await res.json()).job_id;
  var sse = new EventSource('/api/progress/' + jobId);

  sse.onmessage = function(ev) {
    var data = JSON.parse(ev.data);
    if (data.stage === 'keepalive') return;
    if (data.stage === 'batch') {
      document.getElementById('bpgfill').style.width = data.pct + '%';
      document.getElementById('bpgmsg').textContent = data.msg || '';
    } else if (data.stage === 'done') {
      sse.close();
      _batchDone(data.payload);
    } else if (data.stage === 'error') {
      sse.close();
      _batchError(data.msg || 'Unknown error');
    }
  };
  sse.onerror = function() {
    sse.close();
    _batchError('Connection lost');
  };
}

function _batchDone(payload) {
  var results = payload.results || [];
  document.getElementById('bpgfill').style.width = '100%';
  document.getElementById('batch-prog-title').textContent =
    'Done — ' + payload.created + ' created, ' + payload.skipped + ' skipped, ' + payload.failed + ' failed';
  document.getElementById('bpgmsg').textContent = '';
  document.getElementById('batch-prog-close').disabled = false;

  var container = document.getElementById('batch-results');
  results.forEach(function(r) {
    var row = document.createElement('div');
    row.className = 'bres-row ' + r.status;
    var icon = r.status === 'ok' ? '✓' : r.status === 'skipped' ? '–' : '✗';
    row.innerHTML = '<span class="bres-icon">' + icon + '</span>'
      + '<span class="bres-id">' + escHtml(r.id) + '</span>'
      + (r.reason ? '<span class="bres-reason" title="' + escHtml(r.reason) + '">' + escHtml(r.reason) + '</span>' : '');
    container.appendChild(row);
  });

  loadJobsList();
}

function _batchError(msg) {
  document.getElementById('batch-prog-title').textContent = 'Error';
  document.getElementById('bpgmsg').textContent = msg;
  document.getElementById('batch-prog-close').disabled = false;
}

// Modal backdrop / keyboard handlers
document.getElementById('batch-modal').addEventListener('click', function(e) {
  if (e.target === this) closeBatchDialog();
});

document.getElementById('folder-modal').addEventListener('click', function(e) {
  if (e.target === this) closeFolderDialog();
});

document.getElementById('folder-name-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') submitFolder();
  if (e.key === 'Escape') closeFolderDialog();
});
