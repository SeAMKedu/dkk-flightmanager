// ── Battery / flight-time timeline ────────────────────────────────────────────

import { st } from './state.js';
import { escHtml } from './utils.js';

var _btContainer = null;

export function showBatteryTimeline(mvAllFeatures, mvSelected, mvCurrentFolder, mvLayers) {
  if (!st._mvMode) return;

  var features = mvAllFeatures.filter(function(f) {
    return (f.properties.folder || null) === mvCurrentFolder;
  });

  if (mvSelected && mvSelected.size > 0) {
    features = features.filter(function(f) { return mvSelected.has(f.properties.path); });
  }

  var routable = features.filter(function(f) {
    var p = f.properties;
    return p.sort_order != null && !p.skipped && p.flight_time_min != null && p.flight_time_min > 0;
  });
  routable.sort(function(a, b) { return a.properties.sort_order - b.properties.sort_order; });

  if (routable.length === 0) { hideBatteryTimeline(); return; }

  var droneName = routable[0].properties.drone;
  var d = st.drones.find(function(x) { return x.name === droneName; }) || st.drones[0];
  var batCapMin = d ? d.battery_minutes * 0.85 : 20;

  var groups = _btComputeGroups(routable, batCapMin);
  _btRender(routable, groups, mvLayers);
}

export function hideBatteryTimeline() {
  if (_btContainer) _btContainer.style.display = 'none';
}

export function destroyBatteryTimeline() {
  if (_btContainer) { _btContainer.remove(); _btContainer = null; }
}

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

function _btPanToJob(path, mvLayers) {
  var item = mvLayers.find(function(i) { return i.path === path; });
  if (!item) return;
  try {
    import('./map-init.js').then(function(m){
      m.map.fitBounds(item.layer.getBounds(), {padding: [80, 80], maxZoom: 17});
    });
  } catch(e) {}
}

function _btEnsureContainer(mvLayers) {
  if (!_btContainer) {
    _btContainer = document.createElement('div');
    _btContainer.id = 'battery-timeline';
    document.getElementById('map').appendChild(_btContainer);
    _btContainer.addEventListener('click', function(e) {
      var path = e.target.dataset && e.target.dataset.path;
      if (path) _btPanToJob(path, mvLayers);
    });
  }
  _btContainer.style.display = 'block';
}

function _btRender(routable, groups, mvLayers) {
  _btEnsureContainer(mvLayers);

  var totalMin = routable.reduce(function(s, f) { return s + f.properties.flight_time_min; }, 0);

  var JOB_GAP = 2, BAT_GAP = 10, ICON_W = 18, ICON_H = 9, ICON_Y = 1;
  var BAR_Y = 16, BAR_H = 8, IDX_CY = 33, IDX_R = 7, SVG_H = 44;
  var PAD_L = 8, PAD_R = 6, LABEL_W = 52;

  var mapW = (document.getElementById('map').offsetWidth || 800);
  var maxW = Math.min(mapW * 0.72, 700);

  var nBatBounds = groups.length - 1;
  var nJobGaps   = routable.length - 1 - nBatBounds;
  var totalGaps  = nJobGaps * JOB_GAP + nBatBounds * BAT_GAP;

  var usableW = maxW - PAD_L - PAD_R - LABEL_W - totalGaps;
  var pxPerMin = usableW / totalMin;

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
  out.push('<svg width="' + svgW + '" height="' + SVG_H
    + '" xmlns="http://www.w3.org/2000/svg" pointer-events="none">');

  segments.forEach(function(seg, si) {
    var sx = seg.x;
    var cx = sx + seg.w / 2;

    if (seg.isBatStart) {
      _btBatteryIcon(out, sx, ICON_Y, ICON_W, ICON_H);
    }

    out.push('<rect x="' + sx + '" y="' + BAR_Y + '" width="' + seg.w
      + '" height="' + BAR_H + '" rx="1.5" fill="' + escHtml(seg.color)
      + '" opacity="0.88" pointer-events="auto" cursor="pointer"'
      + ' data-path="' + escHtml(seg.path) + '"/>');

    if (seg.isBatStart) {
      out.push('<circle cx="' + cx + '" cy="' + IDX_CY + '" r="' + IDX_R
        + '" fill="#f59e0b" stroke="#fff" stroke-width="1.5"/>');
      out.push('<text x="' + cx + '" y="' + IDX_CY
        + '" text-anchor="middle" dy="0.35em" font-size="8" '
        + 'font-family="sans-serif" fill="#000" font-weight="700">'
        + seg.rank + '</text>');
    }
  });

  var labelX = x - JOB_GAP + PAD_R;
  var totalRnd = Math.round(totalMin);
  out.push('<text x="' + labelX + '" y="' + (BAR_Y + BAR_H / 2)
    + '" dominant-baseline="middle" font-size="11" font-family="sans-serif" '
    + 'fill="#cbd5e1" font-weight="600">' + totalRnd + ' min</text>');

  out.push('</svg>');
  _btContainer.innerHTML = out.join('');
}

function _btBatteryIcon(out, x, y, w, h) {
  var body = w - 2;
  out.push('<rect x="' + x + '" y="' + y + '" width="' + body + '" height="' + h
    + '" rx="1.5" stroke="#94a3b8" stroke-width="1" fill="none"/>');
  out.push('<rect x="' + (x + body) + '" y="' + (y + (h - 4) / 2)
    + '" width="2" height="4" rx="0.5" fill="#94a3b8"/>');
}
