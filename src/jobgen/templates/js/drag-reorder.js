// ── Drag-and-drop reordering ──────────────────────────────────────────────────

async function _finishDrop(group, folderKey, targetPath, pos) {
  var readyJobs = (group.jobs || []).filter(function(j){ return j.takeoff_point_4326 && !j.skipped; });
  var paths = readyJobs.map(function(j){ return j.path; });

  if (targetPath) {
    var fromIdx = paths.indexOf(_dragPath);
    var toIdx = paths.indexOf(targetPath);
    if (fromIdx === -1 || toIdx === -1) return;
    paths.splice(fromIdx, 1);
    toIdx = paths.indexOf(targetPath);
    paths.splice(pos === 'before' ? toIdx : toIdx + 1, 0, _dragPath);
  }

  _dragPath = null;
  _dragFolder = null;

  try {
    await fetch('/api/jobs/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths: paths})
    });
    await loadJobsList();
    if (_mvMode && _mvCurrentFolder === folderKey) await _mvRefreshRouteData();
  } catch(e) { console.error('[reorder]', e); }
}

// ── Greedy nearest-neighbor TSP ───────────────────────────────────────────────
function _haversineDeg(lat1, lng1, lat2, lng2) {
  var R = 6371000;
  var dLat = (lat2 - lat1) * Math.PI / 180;
  var dLng = (lng2 - lng1) * Math.PI / 180;
  var a = Math.sin(dLat/2)*Math.sin(dLat/2)
    + Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)
    * Math.sin(dLng/2)*Math.sin(dLng/2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

function _greedyTSP(pts) {
  if (pts.length <= 1) return pts.slice();
  var remaining = pts.slice();
  // Start from northwesternmost point
  remaining.sort(function(a, b) {
    return b.lat !== a.lat ? b.lat - a.lat : a.lng - b.lng;
  });
  var route = [remaining.shift()];
  while (remaining.length) {
    var last = route[route.length - 1];
    var bestDist = Infinity, bestIdx = 0;
    for (var i = 0; i < remaining.length; i++) {
      var d = _haversineDeg(last.lat, last.lng, remaining[i].lat, remaining[i].lng);
      if (d < bestDist) { bestDist = d; bestIdx = i; }
    }
    route.push(remaining.splice(bestIdx, 1)[0]);
  }
  return route;
}

async function autoSortFolder(group, folderKey) {
  var readyJobs = (group.jobs || []).filter(function(j){ return j.takeoff_point_4326 && !j.skipped; });
  if (readyJobs.length < 2) return;
  var pts = readyJobs.map(function(j){
    var tp = j.takeoff_point_4326;
    return {path: j.path, lat: tp[1], lng: tp[0]};
  });
  var sorted = _greedyTSP(pts);
  var paths = sorted.map(function(p){ return p.path; });
  try {
    await fetch('/api/jobs/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths: paths})
    });
    await loadJobsList();
    if (_mvMode && _mvCurrentFolder === folderKey) await _mvRefreshRouteData();
  } catch(e) { console.error('[autosort]', e); }
}
