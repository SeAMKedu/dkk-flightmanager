// ── Utility helpers ────────────────────────────────────────────────────────────

export function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

export function jobApiUrl(path, suffix) {
  var encoded = path.split('/').map(encodeURIComponent).join('/');
  return '/api/jobs/' + encoded + (suffix || '');
}
