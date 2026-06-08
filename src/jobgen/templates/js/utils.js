// ── Utility helpers ────────────────────────────────────────────────────────────

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _escapeXml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&apos;');
}

function jobApiUrl(path, suffix) {
  var encoded = path.split('/').map(encodeURIComponent).join('/');
  return '/api/jobs/' + encoded + (suffix || '');
}

function _hexToKmlColor(hex, alpha) {
  // CSS #RRGGBB → KML AABBGGRR
  var h = (hex || '#3b82f6').replace('#', '');
  if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  return alpha + h.slice(4,6) + h.slice(2,4) + h.slice(0,2);
}
