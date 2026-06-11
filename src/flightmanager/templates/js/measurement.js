// ── Measurement tool ──────────────────────────────────────────────────────────
// Right-click + Ctrl + drag: draw a dimensioning line with perpendicular end caps.
// Shift modifier: draw a radius line + unfilled circle instead.
// Measurements persist until cleared. Does not activate in edit / bridge mode.

import { st } from './state.js';
import { map } from './map-init.js';

var _measItems   = [];      // [{startLL, endLL, shift}] committed measurements
var _measTemp    = null;    // {startLL, endLL, shift} during current drag
var _measSvg     = null;    // <svg> overlay element inside #map
var _measActive  = false;   // right-drag in progress
var _measShift   = false;   // shift key held at drag start
var _measStartPx = null;    // {x,y} client coords at right mousedown
var _measDragged = false;   // crossed 5 px threshold → treat as measurement drag

function _initMeasSvg() {
  var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'meas-svg';
  svg.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:650;overflow:visible';
  document.getElementById('map').appendChild(svg);
  _measSvg = svg;
  map.on('move zoom viewreset resize', _redrawMeas);

  var MeasClearControl = L.Control.extend({
    options: {position: 'topleft'},
    onAdd: function() {
      var btn = L.DomUtil.create('button', 'meas-clear-ctrl');
      btn.id = 'meas-clear-btn';
      btn.innerHTML = '&#10005;';
      btn.title = 'Clear measurements (Ctrl + right-click drag to measure)';
      L.DomEvent.on(btn, 'click', L.DomEvent.stopPropagation);
      L.DomEvent.on(btn, 'click', clearMeasurements);
      return btn;
    }
  });
  new MeasClearControl().addTo(map);
}

function _initMeasEvents() {
  var container = map.getContainer();

  container.addEventListener('mousedown', function(e) {
    if (e.button !== 2 || !e.ctrlKey) return;
    if (st.editMode || st._bridgeMode) return;
    _measStartPx = {x: e.clientX, y: e.clientY};
    _measShift   = e.shiftKey;
    _measDragged = false;
    _measActive  = false;
  }, false);

  document.addEventListener('mousemove', function(e) {
    if (!_measStartPx) return;
    if (st.editMode || st._bridgeMode) { _measStartPx = null; return; }
    var dx = e.clientX - _measStartPx.x;
    var dy = e.clientY - _measStartPx.y;
    if (!_measDragged && Math.sqrt(dx*dx + dy*dy) > 5) {
      _measDragged = true;
      _measActive  = true;
      var rect = container.getBoundingClientRect();
      var cp = L.point(_measStartPx.x - rect.left, _measStartPx.y - rect.top);
      _measTemp = {
        startLL: map.containerPointToLatLng(cp),
        endLL:   map.containerPointToLatLng(cp),
        shift:   _measShift
      };
    }
    if (_measActive && _measTemp) {
      var rect = container.getBoundingClientRect();
      var cp = L.point(e.clientX - rect.left, e.clientY - rect.top);
      _measTemp.endLL = map.containerPointToLatLng(cp);
      _redrawMeas();
    }
  }, false);

  document.addEventListener('mouseup', function(e) {
    if (e.button !== 2) return;
    var wasDragging = _measActive;
    if (_measActive && _measTemp) {
      var rect = container.getBoundingClientRect();
      var cp = L.point(e.clientX - rect.left, e.clientY - rect.top);
      _measTemp.endLL = map.containerPointToLatLng(cp);
      if (_measTemp.startLL.distanceTo(_measTemp.endLL) > 0.5) {
        _measItems.push(_measTemp);
      }
      _measTemp  = null;
      _measActive = false;
      _redrawMeas();
    }
    _measStartPx = null;
    if (!wasDragging) _measDragged = false;
  }, false);

  // Suppress contextmenu immediately after a measurement drag
  container.addEventListener('contextmenu', function(e) {
    if (_measDragged) {
      e.stopPropagation();
      e.preventDefault();
      _measDragged = false;
    }
  }, true);
}

export function clearMeasurements() {
  _measItems  = [];
  _measTemp   = null;
  _measActive = false;
  _redrawMeas();
}

function _redrawMeas() {
  if (!_measSvg) return;
  while (_measSvg.firstChild) _measSvg.removeChild(_measSvg.firstChild);
  var items = _measItems.slice();
  if (_measTemp) items.push(_measTemp);
  items.forEach(_drawMeasItem);
}

function _drawMeasItem(item) {
  var p1 = map.latLngToContainerPoint(item.startLL);
  var p2 = map.latLngToContainerPoint(item.endLL);
  var dist = item.startLL.distanceTo(item.endLL);
  if (dist < 0.5) return;

  var distLabel = dist < 1000
    ? Math.round(dist) + ' m'
    : (dist / 1000).toFixed(2) + ' km';

  var dx = p2.x - p1.x, dy = p2.y - p1.y;
  var len = Math.sqrt(dx*dx + dy*dy);
  if (len < 3) return;

  var ux = dx/len, uy = dy/len;
  var perpX = -uy, perpY = ux;

  var mx = (p1.x + p2.x) / 2;
  var my = (p1.y + p2.y) / 2;
  var angleDeg = Math.atan2(dy, dx) * 180 / Math.PI;
  if (angleDeg >  90) angleDeg -= 180;
  if (angleDeg < -90) angleDeg += 180;

  var g = _measSvgEl('g', {});

  if (item.shift) {
    g.appendChild(_measSvgEl('line', {
      x1:p1.x, y1:p1.y, x2:p2.x, y2:p2.y,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'butt'
    }));
    g.appendChild(_measSvgEl('circle', {
      cx:p1.x, cy:p1.y, r:len,
      stroke:'#111', 'stroke-width':1.5, fill:'none'
    }));
    _measSvgLabel(g, mx, my, distLabel, angleDeg);
  } else {
    var CAP = 7;
    g.appendChild(_measSvgEl('line', {
      x1:p1.x, y1:p1.y, x2:p2.x, y2:p2.y,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'butt'
    }));
    g.appendChild(_measSvgEl('line', {
      x1:p1.x + perpX*CAP, y1:p1.y + perpY*CAP,
      x2:p1.x - perpX*CAP, y2:p1.y - perpY*CAP,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'square'
    }));
    g.appendChild(_measSvgEl('line', {
      x1:p2.x + perpX*CAP, y1:p2.y + perpY*CAP,
      x2:p2.x - perpX*CAP, y2:p2.y - perpY*CAP,
      stroke:'#111', 'stroke-width':1.5, 'stroke-linecap':'square'
    }));
    _measSvgLabel(g, mx, my, distLabel, angleDeg);
  }
  _measSvg.appendChild(g);
}

function _measSvgEl(tag, attrs) {
  var el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  var keys = Object.keys(attrs);
  for (var i = 0; i < keys.length; i++) el.setAttribute(keys[i], attrs[keys[i]]);
  return el;
}

function _measSvgLabel(parent, x, y, text, angleDeg) {
  var g = _measSvgEl('g', {
    transform: 'translate(' + x + ',' + y + ') rotate(' + angleDeg + ')'
  });
  var commonAttrs = [
    ['text-anchor',       'middle'],
    ['dominant-baseline', 'middle'],
    ['dy',                '-6'],
    ['font-size',         '11'],
    ['font-family',       'system-ui,sans-serif'],
    ['font-weight',       '600']
  ];
  function applyAttrs(el, extra) {
    commonAttrs.forEach(function(kv) { el.setAttribute(kv[0], kv[1]); });
    extra.forEach(function(kv) { el.setAttribute(kv[0], kv[1]); });
    el.textContent = text;
  }
  var bg = _measSvgEl('text', {});
  applyAttrs(bg, [['fill','#fff'],['stroke','#fff'],['stroke-width','3'],['paint-order','stroke']]);
  var fg = _measSvgEl('text', {});
  applyAttrs(fg, [['fill','#111']]);
  g.appendChild(bg);
  g.appendChild(fg);
  parent.appendChild(g);
}

_initMeasSvg();
_initMeasEvents();
