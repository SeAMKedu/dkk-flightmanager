// ── Status panel ──────────────────────────────────────────────────────────────
var _dash = '<span style="color:#cbd5e1">—</span>';

function renderStatus(s) {
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
  if (_altCap !== null && !isNaN(curH) && curH >= _altCap) {
    rh += '<div class="ritem" style="color:#f97316">&#9888; Height '+curH.toFixed(0)+' m is at or above zone floor '+Math.round(_altCap)+' m AGL — fly below '+Math.round(_altCap)+' m or obtain authorisation</div>';
  }
  document.getElementById('spcontent').innerHTML =
    sh
   +'<div class="sgrid">'
   +'<div class="sbox"><div class="slbl">Area</div><div class="sval">'+fmt1(s&&s.final_area_ha)+' '+(s?'ha':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Height</div><div class="sval">'+fmt0(s&&s.flight_height_m)+' '+(s?'m':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">GSD</div><div class="sval">'+fmt2(s&&s.target_gsd_cm)+' '+(s?'cm':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Vertices</div><div class="sval">'+fmti(s&&s.survey_vertex_count)+'</div></div>'
   +'<div class="sbox"><div class="slbl">Lost</div><div class="sval">'+fmt1(s&&s.area_lost_pct)+' '+(s?'%':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Zones</div><div class="sval">'+zh+'</div></div>'
   +'</div>'
   +(rh ? '<div class="rlist">'+rh+'</div>' : '');
}
