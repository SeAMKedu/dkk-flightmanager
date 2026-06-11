// ── Dirty tracking & unsaved-changes guard ────────────────────────────────────

import { st } from './state.js';

export function markDirty() { st._dirty = true; }

export function confirmIfDirty(onConfirm) {
  if (!st._dirty) { onConfirm(); return; }
  document.getElementById('confirm-msg').textContent =
    'You have unsaved changes. Discard them and continue?';
  document.getElementById('confirm-modal').style.display = 'flex';
  document.getElementById('confirm-discard').onclick = function() {
    hideConfirmModal(); st._dirty = false; onConfirm();
  };
}
export function hideConfirmModal() {
  document.getElementById('confirm-modal').style.display = 'none';
}
window.addEventListener('beforeunload', function(e) {
  if (st._dirty) { e.preventDefault(); e.returnValue = ''; }
});
