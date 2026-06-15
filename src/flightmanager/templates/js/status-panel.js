// ── Status panel ──────────────────────────────────────────────────────────────

import { st } from './state.js';

var _dash = '<span style="color:#cbd5e1">—</span>';

export function renderStatus(s) {
  document.getElementById('sp').classList.toggle('sp-visible', !!s);
  var sh = !s ? ''
    : s.flight_ready ? '<div class="sh"><span class="sok">&#10003; FLIGHT READY</span></div>'
    : s.needs_review  ? '<div class="sh"><span class="swrn">&#9888; NEEDS REVIEW</span></div>'
                      : '<div class="sh"><span class="serr">&#10007; NOT FLIGHT READY</span></div>';
  var zh = !s ? _dash
    : !s.zones_checked ? '<span class="swrn">not checked</span>'
    : s.zones_clear    ? '<span class="sok">clear</span>'
                       : '<span class="serr">'+s.zone_count+' zone(s)</span>';
  function fmt1(v) { return s && v != null ? v.toFixed(1) : _dash; }
  function fmt0(v) { return s && v != null ? v.toFixed(0) : _dash; }
  function fmt2(v) { return s && v != null ? v.toFixed(2) : _dash; }
  function fmti(v) { return s && v != null ? String(v)    : _dash; }
  var rh = s ? (s.review_reasons||[]).map(function(r){
    return '<div class="ritem">&#9888; '+r+'</div>';
  }).join('') : '';
  // Client-side altitude cap warning
  var curH = parseFloat(document.getElementById('hgt').value);
  if (st._altCap !== null && !isNaN(curH) && curH >= st._altCap) {
    rh += '<div class="ritem" style="color:#f97316">&#9888; Height '+curH.toFixed(0)+' m is at or above zone floor '+Math.round(st._altCap)+' m AGL — fly below '+Math.round(st._altCap)+' m or obtain authorisation</div>';
  }
  var modeH = !s ? _dash
    : st._waypointMode ? '<span style="color:#a78bfa;font-weight:700">Variable alt</span>'
                       : '<span style="color:#94a3b8">Terrain follow</span>';
  document.getElementById('spcontent').innerHTML =
    sh
   +'<div class="sp-body">'
   +'<div class="sp-metrics">'
   +'<div class="sgrid">'
   +'<div class="sbox"><div class="slbl">Area</div><div class="sval">'+fmt1(s&&s.final_area_ha)+' '+(s?'ha':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Height</div><div class="sval">'+(s && st._waypointMode && st._altProfileMin != null ? Math.round(st._altProfileMin)+'–'+Math.round(st._altProfileMax)+' m' : fmt0(s&&s.flight_height_m)+' '+(s?'m':''))+' </div></div>'
   +'<div class="sbox"><div class="slbl">GSD</div><div class="sval">'+fmt2(s&&s.target_gsd_cm)+' '+(s?'cm':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Mode</div><div class="sval">'+modeH+'</div></div>'
   +'<div class="sbox"><div class="slbl">Lost</div><div class="sval">'+fmt1(s&&s.area_lost_pct)+' '+(s?'%':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Zones</div><div class="sval">'+zh+'</div></div>'
   +'</div>'
   +'<div class="sgrid">'
   +'<div class="sbox"><div class="slbl">Strips</div><div class="sval" id="rstat-strips">—</div></div>'
   +'<div class="sbox"><div class="slbl">Photos</div><div class="sval" id="rstat-photos">—</div></div>'
   +'<div class="sbox"><div class="slbl">Flight time</div><div class="sval" id="rstat-time">—</div></div>'
   +'</div>'
   +'<div class="sgrid">'
   +'<div class="sbox"><div class="slbl">Min speed</div><div class="sval" id="rstat-spd-min">—</div></div>'
   +'<div class="sbox"><div class="slbl">Avg speed</div><div class="sval" id="rstat-spd-avg">—</div></div>'
   +'<div class="sbox"><div class="slbl">Max speed</div><div class="sval" id="rstat-spd-max">—</div></div>'
   +'</div>'
   +'</div>'
   +(rh ? '<div class="rlist">'+rh+'</div>' : '')
   +'</div>';
}
