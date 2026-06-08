// ── Battery / flight-time timeline (map view, bottom-centre overlay) ──────────
// Renders a proportional segment bar showing how each routable job fits into
// drone battery cycles. Battery capacity = 85 % of drone.battery_minutes.
// If _mvSelected has jobs, only those selected jobs that are on the route are
// shown; otherwise all routable jobs in the current folder are shown.
// Clicking a segment pans/zooms the map to that job's polygon.

var _btContainer = null;

// ── Public API ────────────────────────────────────────────────────────────────

function showBatteryTimeline() {
  if (!_mvMode) return;

  var folder = _mvCurrentFolder;
  var features = _mvAllFeatures.filter(function(f) {
    return (f.properties.folder || null) === folder;
  });

  // Honour multi-select: if anything is selected, restrict to those paths.
  if (_mvSelected.size > 0) {
    features = features.filter(function(f) { return _mvSelected.has(f.properties.path); });
  }

  // Routable = sort_order set, not skipped, has a flight time estimate.
  var routable = features.filter(function(f) {
    var p = f.properties;
    return p.sort_order != null && !p.skipped && p.flight_time_min != null && p.flight_time_min > 0;
  });
  routable.sort(function(a, b) { return a.properties.sort_order - b.properties.sort_order; });

  if (routable.length === 0) { hideBatteryTimeline(); return; }

  // Resolve battery capacity from the first job's drone, fall back to first in list.
  var droneName = routable[0].properties.drone;
  var d = drones.find(function(x) { return x.name === droneName; }) || drones[0];
  var batCapMin = d ? d.battery_minutes * 0.85 : 20;

  var groups = _btComputeGroups(routable, batCapMin);
  _btRender(routable, groups);
}

function hideBatteryTimeline() {
  if (_btContainer) _btContainer.style.display = 'none';
}

function destroyBatteryTimeline() {
  if (_btContainer) { _btContainer.remove(); _btContainer = null; }
}

// ── Battery grouping ──────────────────────────────────────────────────────────

function _btComputeGroups(routable, batCapMin) {
  var groups = [[]];
  var chargeLeft = batCapMin;
  routable.forEach(function(f) {
    var ft = f.properties.flight_time_min;
    if (ft > chargeLeft && groups[groups.length - 1].length > 0) {
      groups.push([]);
      chargeLeft = batCapMin;
    }
    groups[groups.length - 1].push(f);
    chargeLeft -= ft;
  });
  return groups;
}

// ── Pan to job ────────────────────────────────────────────────────────────────

function _btPanToJob(path) {
  var item = _mvLayers.find(function(i) { return i.path === path; });
  if (!item) return;
  try { map.fitBounds(item.layer.getBounds(), {padding: [80, 80], maxZoom: 17}); } catch(e) {}
}

// ── SVG rendering ─────────────────────────────────────────────────────────────

function _btEnsureContainer() {
  if (!_btContainer) {
    _btContainer = document.createElement('div');
    _btContainer.id = 'battery-timeline';
    document.getElementById('map').appendChild(_btContainer);
    // Event delegation: clicks on segment rects (which have data-path) pan the map.
    _btContainer.addEventListener('click', function(e) {
      var path = e.target.dataset && e.target.dataset.path;
      if (path) _btPanToJob(path);
    });
  }
  _btContainer.style.display = 'block';
}

function _btRender(routable, groups) {
  _btEnsureContainer();

  var totalMin = routable.reduce(function(s, f) { return s + f.properties.flight_time_min; }, 0);

  // Layout constants (px)
  var JOB_GAP = 2;   // between jobs in the same battery
  var BAT_GAP = 10;  // extra gap at battery boundary (replaces JOB_GAP)
  var ICON_W  = 18;  // battery icon outer width (incl. nub)
  var ICON_H  = 9;
  var ICON_Y  = 1;
  var BAR_Y   = 16;
  var BAR_H   = 8;
  var IDX_CY  = 33;
  var IDX_R   = 7;
  var SVG_H   = 44;
  var PAD_L   = 8;   // left padding inside container
  var PAD_R   = 6;   // right padding before total label
  var LABEL_W = 52;  // reserved width for "NNN min"

  var mapW = (document.getElementById('map').offsetWidth || 800);
  var maxW = Math.min(mapW * 0.72, 700);

  // Total gap space consumed by separators
  var nBatBounds = groups.length - 1;
  var nJobGaps   = routable.length - 1 - nBatBounds; // gaps within groups
  var totalGaps  = nJobGaps * JOB_GAP + nBatBounds * BAT_GAP;

  var usableW = maxW - PAD_L - PAD_R - LABEL_W - totalGaps;
  var pxPerMin = usableW / totalMin;

  // Build segment list with x positions
  var segments = [];
  var x = PAD_L;
  var routeRank = 0;
  groups.forEach(function(group, gi) {
    group.forEach(function(f, ji) {
      var segW = Math.max(f.properties.flight_time_min * pxPerMin, 4);
      segments.push({
        x: x, w: segW,
        color: f.properties.color || '#3b82f6',
        rank: routeRank + 1,
        path: f.properties.path,
        isBatStart: ji === 0,
      });
      x += segW + JOB_GAP;
      routeRank++;
    });
    if (gi < groups.length - 1) x += BAT_GAP - JOB_GAP;
  });

  var svgW = x - JOB_GAP + PAD_R + LABEL_W;
  var out  = [];
  // pointer-events="none" on SVG so empty SVG areas stay transparent to the map.
  // Individual segment rects override with pointer-events="auto".
  out.push('<svg width="' + svgW + '" height="' + SVG_H
    + '" xmlns="http://www.w3.org/2000/svg" pointer-events="none">');

  segments.forEach(function(seg) {
    var sx = seg.x;
    var cx = sx + seg.w / 2;

    // Battery icon (outline only) above each new battery group
    if (seg.isBatStart) {
      _btBatteryIcon(out, sx, ICON_Y, ICON_W, ICON_H);
    }

    // Segment bar — clickable, stores path in data-path
    out.push('<rect x="' + sx + '" y="' + BAR_Y + '" width="' + seg.w
      + '" height="' + BAR_H + '" rx="1.5" fill="' + escHtml(seg.color)
      + '" opacity="0.88" pointer-events="auto" cursor="pointer"'
      + ' data-path="' + escHtml(seg.path) + '"/>');

    // Route index circle — orange to match map markers, only when wide enough
    if (seg.w >= IDX_R * 2 + 2) {
      out.push('<circle cx="' + cx + '" cy="' + IDX_CY + '" r="' + IDX_R
        + '" fill="#f59e0b" stroke="#fff" stroke-width="1.5"/>');
      out.push('<text x="' + cx + '" y="' + IDX_CY
        + '" text-anchor="middle" dy="0.35em" font-size="8" '
        + 'font-family="sans-serif" fill="#000" font-weight="700">'
        + seg.rank + '</text>');
    }
  });

  // Total flight time label, vertically centred on the bar
  var labelX = x - JOB_GAP + PAD_R;
  var totalRnd = Math.round(totalMin);
  out.push('<text x="' + labelX + '" y="' + (BAR_Y + BAR_H / 2)
    + '" dominant-baseline="middle" font-size="11" font-family="sans-serif" '
    + 'fill="#cbd5e1" font-weight="600">' + totalRnd + ' min</text>');

  out.push('</svg>');
  _btContainer.innerHTML = out.join('');
}

// Draw battery icon as outline only (no fill) — visible on dark background.
function _btBatteryIcon(out, x, y, w, h) {
  var body = w - 2;  // nub takes 2px
  out.push('<rect x="' + x + '" y="' + y + '" width="' + body + '" height="' + h
    + '" rx="1.5" stroke="#94a3b8" stroke-width="1" fill="none"/>');
  out.push('<rect x="' + (x + body) + '" y="' + (y + (h - 4) / 2)
    + '" width="2" height="4" rx="0.5" fill="#94a3b8"/>');
}
