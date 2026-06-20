// ── Settings panel ────────────────────────────────────────────────────────────

import { apiGet, apiPatch, ApiError } from './api.js';

var _cfgSections   = [];   // [{id, label, fields:[...]}]
var _cfgValues     = {};   // key → current (possibly edited) value
var _cfgOrigValues = {};   // key → value as loaded from server
var _cfgActiveSid  = null; // active section id
var _cfgSearchQ    = '';   // current search query

export async function openSettings() {
  var overlay = document.getElementById('cfg-overlay');
  overlay.style.display = 'flex';
  document.getElementById('cfg-search').value = '';
  _cfgSearchQ = '';
  try {
    var data = await apiGet('/api/settings');
    _cfgSections   = data.sections;
    _cfgValues     = {};
    _cfgOrigValues = {};
    for (var s of _cfgSections) {
      for (var f of s.fields) {
        _cfgValues[f.key]     = f.value;
        _cfgOrigValues[f.key] = f.value;
      }
    }
    _cfgRenderNav();
    _cfgActivate(_cfgSections[0]?.id);
  } catch(e) {
    _cfgStatus('Failed to load settings: ' + e.message, 'err');
  }
}

export function closeSettings() {
  if (_cfgIsDirty() && !confirm('Discard unsaved changes?')) return;
  _cfgClose();
}

export function discardSettings() { closeSettings(); }

function _cfgClose() {
  document.getElementById('cfg-overlay').style.display = 'none';
  _cfgValues = {}; _cfgOrigValues = {}; _cfgSections = [];
}

function _cfgIsDirty() {
  for (var key of Object.keys(_cfgValues)) {
    if (!_cfgValEq(_cfgValues[key], _cfgOrigValues[key])) return true;
  }
  return false;
}

function _cfgValEq(a, b) {
  if (a === b) return true;
  if (a === null || b === null) return false;
  return String(a) === String(b);
}

function _cfgSectionDirty(section) {
  return section.fields.some(function(f) {
    return !_cfgValEq(_cfgValues[f.key], _cfgOrigValues[f.key]);
  });
}

// ── Nav ───────────────────────────────────────────────────────────────────────
function _cfgRenderNav() {
  var nav = document.getElementById('cfg-nav');
  nav.innerHTML = '';
  for (var s of _cfgSections) {
    (function(section) {
      var btn = document.createElement('button');
      btn.className = 'cfg-nav-item';
      btn.dataset.sid = section.id;
      var lbl = document.createElement('span');
      lbl.textContent = section.label;
      var dot = document.createElement('span');
      dot.className = 'cfg-nav-dot';
      btn.appendChild(lbl);
      btn.appendChild(dot);
      btn.onclick = function() { _cfgActivate(section.id); };
      nav.appendChild(btn);
    })(s);
  }
}

function _cfgUpdateNavDots() {
  for (var s of _cfgSections) {
    var btn = document.querySelector('.cfg-nav-item[data-sid="' + s.id + '"]');
    if (btn) btn.classList.toggle('dirty', _cfgSectionDirty(s));
  }
}

function _cfgActivate(sid) {
  _cfgActiveSid = sid;
  document.querySelectorAll('.cfg-nav-item').forEach(function(b) {
    b.classList.toggle('active', b.dataset.sid === sid);
  });
  var section = _cfgSections.find(function(s) { return s.id === sid; });
  if (!section) return;

  if (_cfgSearchQ) {
    _cfgRenderSearch(_cfgSearchQ);
    return;
  }

  document.getElementById('cfg-section-title').textContent = section.label;
  var container = document.getElementById('cfg-fields');
  container.innerHTML = '';
  var visible = section.fields.filter(function(f) { return !f._hidden; });
  for (var field of visible) {
    container.appendChild(_cfgFieldEl(field));
  }
}

// ── Search ────────────────────────────────────────────────────────────────────
export function cfgSearch(q) {
  _cfgSearchQ = q.trim().toLowerCase();
  if (!_cfgSearchQ) {
    _cfgActivate(_cfgActiveSid);
    document.querySelectorAll('.cfg-nav-item').forEach(function(b) { b.style.display = ''; });
    return;
  }

  var matchingSids = new Set();
  for (var s of _cfgSections) {
    if (s.fields.some(function(f) { return _cfgFieldMatches(f, _cfgSearchQ); })) {
      matchingSids.add(s.id);
    }
  }
  document.querySelectorAll('.cfg-nav-item').forEach(function(b) {
    b.style.display = matchingSids.has(b.dataset.sid) ? '' : 'none';
  });

  _cfgRenderSearch(_cfgSearchQ);
}

function _cfgFieldMatches(field, q) {
  return (field.label + ' ' + field.description + ' ' + field.key).toLowerCase().includes(q);
}

function _cfgRenderSearch(q) {
  document.getElementById('cfg-section-title').textContent = 'Search results';
  var container = document.getElementById('cfg-fields');
  container.innerHTML = '';
  var found = false;
  for (var s of _cfgSections) {
    var matches = s.fields.filter(function(f) { return _cfgFieldMatches(f, q); });
    if (!matches.length) continue;
    found = true;
    var hdr = document.createElement('div');
    hdr.className = 'cfg-search-section-hdr';
    hdr.textContent = s.label;
    container.appendChild(hdr);
    for (var field of matches) {
      container.appendChild(_cfgFieldEl(field));
    }
  }
  if (!found) {
    var msg = document.createElement('div');
    msg.className = 'cfg-no-results';
    msg.textContent = 'No settings match "' + q + '"';
    container.appendChild(msg);
  }
}

// ── Field rendering ───────────────────────────────────────────────────────────
function _cfgFieldEl(field) {
  var wrap = document.createElement('div');
  wrap.className = 'cfg-field';

  var labelRow = document.createElement('div');
  labelRow.className = 'cfg-field-label';
  var lbl = document.createElement('span');
  lbl.textContent = field.label;
  labelRow.appendChild(lbl);
  if (field.unit) {
    var unit = document.createElement('span');
    unit.className = 'cfg-field-unit';
    unit.textContent = field.unit;
    labelRow.appendChild(unit);
  }
  wrap.appendChild(labelRow);

  if (field.description) {
    var desc = document.createElement('div');
    desc.className = 'cfg-field-desc';
    desc.textContent = field.description;
    wrap.appendChild(desc);
  }

  var row = document.createElement('div');
  row.className = 'cfg-field-row';

  var currentVal = _cfgValues[field.key];
  var input = _cfgMakeInput(field, currentVal);
  row.appendChild(input);

  if (field.nullable && currentVal !== null) {
    var clrBtn = document.createElement('button');
    clrBtn.className = 'cfg-nullable-clear';
    clrBtn.textContent = 'Use default';
    clrBtn.onclick = function() {
      _cfgValues[field.key] = null;
      input.value = '';
      input.placeholder = 'default';
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    };
    row.appendChild(clrBtn);
  }

  wrap.appendChild(row);
  return wrap;
}

function _cfgMakeInput(field, currentVal) {
  var input;
  if (field.type === 'boolean') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'cfg-input cfg-input-bool';
    input.checked = currentVal === true;
    input.addEventListener('change', function() {
      _cfgValues[field.key] = input.checked;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  } else if (field.type === 'enum') {
    input = document.createElement('select');
    input.className = 'cfg-input';
    for (var opt of (field.options || [])) {
      var o = document.createElement('option');
      o.value = opt;
      o.textContent = (field.option_labels && field.option_labels[opt]) ? field.option_labels[opt] + ' (' + opt + ')' : opt;
      if (opt === currentVal) o.selected = true;
      input.appendChild(o);
    }
    input.addEventListener('change', function() {
      _cfgValues[field.key] = input.value;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  } else if (field.type === 'number' || field.type === 'integer') {
    input = document.createElement('input');
    input.type = 'number';
    input.className = 'cfg-input';
    input.value = currentVal !== null && currentVal !== undefined ? currentVal : '';
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    input.step = field.step !== undefined ? field.step : 1;
    if (field.nullable) input.placeholder = 'default';
    input.addEventListener('input', function() {
      var v = input.value === '' && field.nullable ? null
        : field.type === 'integer' ? parseInt(input.value, 10)
        : parseFloat(input.value);
      _cfgValues[field.key] = v;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.className = 'cfg-input';
    input.value = currentVal !== null && currentVal !== undefined ? currentVal : '';
    if (field.nullable) input.placeholder = 'default';
    input.addEventListener('input', function() {
      _cfgValues[field.key] = input.value === '' && field.nullable ? null : input.value;
      _cfgMarkModified(input, field.key);
      _cfgUpdateNavDots();
    });
  }
  return input;
}

function _cfgMarkModified(input, key) {
  var isModified = !_cfgValEq(_cfgValues[key], _cfgOrigValues[key]);
  if (input.type === 'checkbox') return;
  input.classList.toggle('cfg-modified', isModified);
}

// ── Save / Reset ──────────────────────────────────────────────────────────────
export async function saveSettings() {
  var changes = {};
  for (var key of Object.keys(_cfgValues)) {
    if (!_cfgValEq(_cfgValues[key], _cfgOrigValues[key])) {
      changes[key] = _cfgValues[key];
    }
  }
  if (!Object.keys(changes).length) {
    _cfgStatus('No changes to save.', 'ok');
    return;
  }

  var btn = document.getElementById('cfg-save-btn');
  btn.disabled = true;
  try {
    await apiPatch('/api/settings', changes);
    _cfgOrigValues = Object.assign({}, _cfgValues);
    document.querySelectorAll('.cfg-input.cfg-modified').forEach(function(el) {
      el.classList.remove('cfg-modified');
    });
    _cfgUpdateNavDots();
    _cfgStatus('Settings saved. Some changes (output dir, cache TTLs) take effect immediately; drone/flight defaults apply to new jobs.', 'ok');
  } catch(e) {
    if (e instanceof ApiError) _cfgStatus('Save failed: ' + e.detail, 'err');
    else _cfgStatus('Network error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function _cfgStatus(msg, kind) {
  var el = document.getElementById('cfg-status-msg');
  el.textContent = msg;
  el.className = kind || '';
  if (msg) setTimeout(function() { if (el.textContent === msg) el.textContent = ''; }, 5000);
}

// ── About modal ───────────────────────────────────────────────────────────────

var _STAT_LABELS = {
  dem:        'DEM tiles',
  buildings:  'Buildings',
  parcels:    'Parcels',
  properties: 'Properties',
  zones:      'UAS zones',
};

function _fmtBytes(n) {
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function _renderStats(data) {
  var el = document.getElementById('about-stats');
  if (!el) return;

  var totalDl = 0, totalHits = 0, totalBytes = 0;
  var rows = [];
  var sources = ['dem', 'buildings', 'parcels', 'properties', 'zones'];
  for (var src of sources) {
    var v = data[src] || {downloads: 0, hits: 0, bytes: 0};
    if (!v.downloads && !v.hits) continue;
    totalDl    += v.downloads;
    totalHits  += v.hits;
    totalBytes += v.bytes || 0;
    var parts = [];
    if (v.downloads) {
      var b = _fmtBytes(v.bytes);
      parts.push(v.downloads + ' fetched' + (b ? ' (' + b + ')' : ''));
    }
    if (v.hits) parts.push(v.hits + ' cached');
    var total = v.downloads + v.hits;
    if (total > 1 && v.hits) parts.push(Math.round(100 * v.hits / total) + '% cache rate');
    rows.push([_STAT_LABELS[src] || src, parts.join(',  ')]);
  }

  if (!rows.length) { el.innerHTML = ''; return; }

  b = _fmtBytes(totalBytes);
  var summary = totalDl + ' fetched,  ' + totalHits + ' cached' + (b ? ',  ' + b : '');

  var html = '<div class="about-stats-title">Session statistics</div>'
    + '<table class="about-stats-table">';
  for (var row of rows) {
    html += '<tr><td>' + row[0] + '</td><td>' + row[1] + '</td></tr>';
  }
  html += '<tr class="about-stats-total"><td>Total</td><td>' + summary + '</td></tr>';
  var diskB = _fmtBytes(data.cache_disk_bytes || 0);
  if (diskB) {
    html += '<tr class="about-stats-total"><td>Cache on disk</td><td>' + diskB + '</td></tr>';
  }
  html += '</table>';
  el.innerHTML = html;
}

export async function openAbout() {
  var modal = document.getElementById('about-modal');
  modal.style.display = 'flex';
  try {
    var d = await apiGet('/api/version');
    document.getElementById('about-version').textContent = 'v' + d.version;
  } catch(e) {}
  try {
    _renderStats(await apiGet('/api/stats'));
  } catch(e) {}
}

export function closeAbout() {
  document.getElementById('about-modal').style.display = 'none';
}
