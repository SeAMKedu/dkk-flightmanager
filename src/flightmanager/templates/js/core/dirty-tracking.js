// ── Dirty tracking & unsaved-changes guard ────────────────────────────────────

import { st } from './state.js';
import { cancelEdit } from '../editor/polygon-edit.js';

export function xbUpdate() {
  var xb = document.getElementById('xb');
  if (!xb) return;
  xb.disabled = !(st.previewData && (st._dirty || !st._activeJob));
}

// Reactive wiring: the Save button is a pure function of these three state
// fields, so subscribe once and let any write to them refresh it. Callers no
// longer have to remember to call xbUpdate() alongside a dirty change — and the
// several sites that set st._dirty = false WITHOUT calling xbUpdate (preview
// completion, bridge ops, export) now refresh the button correctly too.
st.subscribe(['_dirty', 'previewData', '_activeJob'], xbUpdate);

export function markDirty() { st._dirty = true; }

export function confirmIfDirty(onConfirm) {
  // An open polygon edit session counts as unsaved work: vertex drags don't set
  // the dirty flag until saveEdit() bakes them in, so a mid-edit job switch would
  // otherwise discard the geometry without a prompt.
  if (!st._dirty && !st.editMode) { onConfirm(); return; }
  document.getElementById('confirm-msg').textContent =
    'You have unsaved changes. Discard them and continue?';
  document.getElementById('confirm-modal').style.display = 'flex';
  document.getElementById('confirm-discard').onclick = function() {
    // Abandon any in-progress polygon edit (no-op if not editing). Without this,
    // an onConfirm that navigates via openMapView would hit its `if (st.editMode)
    // saveEdit()` path, re-baking the just-discarded geometry and re-dirtying —
    // so the next exit would prompt again with no real changes.
    cancelEdit();
    hideConfirmModal(); st._dirty = false; xbUpdate(); onConfirm();
  };
}
export function hideConfirmModal() {
  document.getElementById('confirm-modal').style.display = 'none';
}
window.addEventListener('beforeunload', function(e) {
  if (st._dirty) { e.preventDefault(); e.returnValue = ''; }
});
