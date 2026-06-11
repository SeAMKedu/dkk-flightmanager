// ── Shared modal utilities: delete, move, route-rename ────────────────────────

// ── Delete confirm modal ──────────────────────────────────────────────────────

var _deleteCb = null;

export function openDeleteModal(message, onConfirm) {
  _deleteCb = onConfirm;
  document.getElementById('delete-msg').textContent = message;
  document.getElementById('delete-modal').classList.add('open');
}
export function closeDeleteModal() {
  document.getElementById('delete-modal').classList.remove('open');
}
export function confirmDeleteAction() {
  closeDeleteModal();
  var fn = _deleteCb; _deleteCb = null;
  if (fn) fn();
}

// ── Move modal ─────────────────────────────────────────────────────────────────

var _moveCb = null;

export function openMoveModal(title, metas, onMove) {
  _moveCb = onMove;
  document.getElementById('move-title').textContent = title;

  var folderNames = [];
  document.querySelectorAll('.jfolder-name').forEach(function(el) {
    var n = el.textContent.trim(); if (n) folderNames.push(n);
  });

  var body = document.getElementById('move-folders');
  body.innerHTML = '';

  var hasFolder = metas.some(function(m) { return m.folder; });
  if (hasFolder) {
    var rootBtn = document.createElement('button');
    rootBtn.textContent = 'Move to root';
    rootBtn.onclick = function() { closeMoveModal(); if (_moveCb) { var fn = _moveCb; _moveCb = null; fn(null); } };
    body.appendChild(rootBtn);
  }
  folderNames.forEach(function(name) {
    if (metas.length === 1 && metas[0].folder === name) return;
    var btn = document.createElement('button');
    btn.textContent = '→ ' + name;
    btn.onclick = function() { closeMoveModal(); if (_moveCb) { var fn = _moveCb; _moveCb = null; fn(name); } };
    body.appendChild(btn);
  });

  document.getElementById('move-newfolder-input').value = '';
  document.getElementById('move-error').textContent = '';
  document.getElementById('move-error').style.display = 'none';
  document.getElementById('move-modal').classList.add('open');
}

export function closeMoveModal() {
  document.getElementById('move-modal').classList.remove('open');
}

export function submitNewFolderMove() {
  var name = document.getElementById('move-newfolder-input').value.trim();
  if (!name) return;
  var errEl = document.getElementById('move-error');
  errEl.style.display = 'none';
  fetch('/api/folders', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: name})})
    .then(function(r) {
      if (!r.ok && r.status !== 409) {
        return r.json().catch(function() { return {detail: 'HTTP ' + r.status}; }).then(function(e) {
          errEl.textContent = e.detail || 'Failed'; errEl.style.display = 'block';
        });
      }
      closeMoveModal();
      if (_moveCb) { var fn = _moveCb; _moveCb = null; fn(name); }
    })
    .catch(function(e) { errEl.textContent = 'Failed: ' + e.message; errEl.style.display = 'block'; });
}

// ── Route rename confirm modal ─────────────────────────────────────────────────

var _routeRenameCb = null;

export function openRouteRenameModal(n, onConfirm) {
  _routeRenameCb = onConfirm;
  var today = new Date();
  var dd = today.getFullYear().toString()
    + String(today.getMonth() + 1).padStart(2, '0')
    + String(today.getDate()).padStart(2, '0');
  document.getElementById('route-rename-msg').textContent =
    'Rename ' + n + ' job' + (n !== 1 ? 's' : '') + ' with date prefix ' + dd + '?';
  document.getElementById('route-rename-modal').classList.add('open');
}
export function closeRouteRenameModal() {
  document.getElementById('route-rename-modal').classList.remove('open');
}
export function confirmRouteRenameAction() {
  closeRouteRenameModal();
  var fn = _routeRenameCb; _routeRenameCb = null;
  if (fn) fn();
}
