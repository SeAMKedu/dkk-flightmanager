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

// Greedy nearest-neighbor starting from a given anchor (for continuing an existing route)
function _greedyTSPContinue(pts, anchorLat, anchorLng) {
  if (!pts.length) return [];
  var remaining = pts.slice();
  var route = [];
  var curLat = anchorLat, curLng = anchorLng;
  while (remaining.length) {
    var bestDist = Infinity, bestIdx = 0;
    for (var i = 0; i < remaining.length; i++) {
      var d = _haversineDeg(curLat, curLng, remaining[i].lat, remaining[i].lng);
      if (d < bestDist) { bestDist = d; bestIdx = i; }
    }
    var next = remaining.splice(bestIdx, 1)[0];
    route.push(next);
    curLat = next.lat; curLng = next.lng;
  }
  return route;
}

// ── Route confirmation modal ──────────────────────────────────────────────────
var _routeConfirmGroup = null;
var _routeConfirmFolderKey = null;

function closeRouteConfirmModal() {
  document.getElementById('route-confirm-modal').classList.remove('open');
  _routeConfirmGroup = null;
  _routeConfirmFolderKey = null;
}

async function autoSortFolder(group, folderKey) {
  var readyJobs = (group.jobs || []).filter(function(j){ return j.takeoff_point_4326 && !j.skipped; });
  if (readyJobs.length < 2) return;

  var routed   = readyJobs.filter(function(j){ return j.sort_order != null; });
  var unrouted = readyJobs.filter(function(j){ return j.sort_order == null; });

  if (!routed.length) {
    // No existing route positions — run directly
    await _doReRouteAll(readyJobs, folderKey);
    return;
  }

  // Some jobs already have route positions — show confirmation modal
  _routeConfirmGroup = group;
  _routeConfirmFolderKey = folderKey;

  var desc = document.getElementById('route-confirm-desc');
  var btns = document.getElementById('route-confirm-btns');

  desc.textContent = routed.length + ' of ' + readyJobs.length
    + ' jobs already have route positions.'
    + (unrouted.length ? ' ' + unrouted.length + ' are unrouted.' : '');

  btns.innerHTML = '';

  var makeBtn = function(label, cls, fn) {
    var b = document.createElement('button');
    b.className = cls; b.textContent = label;
    b.addEventListener('click', function() { closeRouteConfirmModal(); fn(); });
    btns.appendChild(b);
  };

  makeBtn('↺ Re-route all', 'rcb-reroute', function() {
    _doReRouteAll(readyJobs, folderKey);
  });

  if (unrouted.length) {
    makeBtn('+ Route remaining (' + unrouted.length + ')', 'rcb-remaining', function() {
      _doRouteRemaining(routed, unrouted, folderKey);
    });
  }

  makeBtn('✕ Clear route positions', 'rcb-clear', function() {
    _doClearRoute(routed, folderKey);
  });

  makeBtn('Cancel', 'rcb-cancel', function() {});

  document.getElementById('route-confirm-modal').classList.add('open');
}

async function _doReRouteAll(readyJobs, folderKey) {
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

async function _doRouteRemaining(routed, unrouted, folderKey) {
  // Sort existing route to find the last job (anchor for continuation)
  routed.sort(function(a, b){ return a.sort_order - b.sort_order; });
  var maxSo = routed[routed.length - 1].sort_order;
  var last = routed[routed.length - 1];
  var anchorLat = last.takeoff_point_4326[1];
  var anchorLng = last.takeoff_point_4326[0];

  var pts = unrouted.map(function(j){
    var tp = j.takeoff_point_4326;
    return {path: j.path, lat: tp[1], lng: tp[0]};
  });
  var sorted = _greedyTSPContinue(pts, anchorLat, anchorLng);

  try {
    for (var i = 0; i < sorted.length; i++) {
      await fetch(jobApiUrl(sorted[i].path), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sort_order: maxSo + 1 + i})
      });
    }
    await loadJobsList();
    if (_mvMode && _mvCurrentFolder === folderKey) await _mvRefreshRouteData();
  } catch(e) { console.error('[route-remaining]', e); }
}

async function _doClearRoute(routed, folderKey) {
  try {
    for (var i = 0; i < routed.length; i++) {
      await fetch(jobApiUrl(routed[i].path), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sort_order: null})
      });
    }
    await loadJobsList();
    if (_mvMode && _mvCurrentFolder === folderKey) await _mvRefreshRouteData();
  } catch(e) { console.error('[clear-route]', e); }
}
