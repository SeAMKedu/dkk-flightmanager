// ── Dirty tracking & unsaved-changes guard ────────────────────────────────────

function markDirty() { _dirty = true; }

function confirmIfDirty(onConfirm) {
  if (!_dirty) { onConfirm(); return; }
  document.getElementById('confirm-msg').textContent =
    'You have unsaved changes. Discard them and continue?';
  document.getElementById('confirm-modal').style.display = 'flex';
  document.getElementById('confirm-discard').onclick = function() {
    hideConfirmModal(); _dirty = false; onConfirm();
  };
}
function hideConfirmModal() {
  document.getElementById('confirm-modal').style.display = 'none';
}
window.addEventListener('beforeunload', function(e) {
  if (_dirty) { e.preventDefault(); e.returnValue = ''; }
});
