// ── Canvas-based HSV color picker ─────────────────────────────────────────────

var _cpH = 210, _cpS = 0.71, _cpV = 0.96; // default: #3b82f6
var _cpDraggingSV = false, _cpDraggingHue = false;

function _hsvToRgb(h, s, v) {
  var c = v*s, x = c*(1 - Math.abs((h/60) % 2 - 1)), m = v-c;
  var r,g,b;
  if      (h<60)  {r=c;g=x;b=0;}
  else if (h<120) {r=x;g=c;b=0;}
  else if (h<180) {r=0;g=c;b=x;}
  else if (h<240) {r=0;g=x;b=c;}
  else if (h<300) {r=x;g=0;b=c;}
  else            {r=c;g=0;b=x;}
  return [Math.round((r+m)*255), Math.round((g+m)*255), Math.round((b+m)*255)];
}

function _rgbToHsv(r, g, b) {
  r/=255; g/=255; b/=255;
  var max=Math.max(r,g,b), min=Math.min(r,g,b), d=max-min;
  var h=0, s=max?d/max:0, v=max;
  if (d) {
    if      (max===r) h=((g-b)/d)%6;
    else if (max===g) h=(b-r)/d+2;
    else              h=(r-g)/d+4;
    h*=60; if(h<0) h+=360;
  }
  return [h, s, v];
}

function _rgbToHex(r, g, b) {
  return '#'+[r,g,b].map(function(n){return ('0'+n.toString(16)).slice(-2);}).join('');
}

function _hexToRgb(hex) {
  var m=/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex||'');
  return m?[parseInt(m[1],16),parseInt(m[2],16),parseInt(m[3],16)]:null;
}

function _cpDrawSV() {
  var cv=document.getElementById('cp-sv'); if(!cv) return;
  var ctx=cv.getContext('2d'), w=cv.width, h=cv.height;
  var hRgb=_hsvToRgb(_cpH,1,1);
  var gS=ctx.createLinearGradient(0,0,w,0);
  gS.addColorStop(0,'#fff'); gS.addColorStop(1,'rgb('+hRgb.join(',')+')');
  ctx.fillStyle=gS; ctx.fillRect(0,0,w,h);
  var gV=ctx.createLinearGradient(0,0,0,h);
  gV.addColorStop(0,'rgba(0,0,0,0)'); gV.addColorStop(1,'rgba(0,0,0,1)');
  ctx.fillStyle=gV; ctx.fillRect(0,0,w,h);
  var hx=_cpS*w, hy=(1-_cpV)*h;
  ctx.beginPath(); ctx.arc(hx,hy,5,0,2*Math.PI);
  ctx.strokeStyle=_cpV>0.4?'#fff':'#555'; ctx.lineWidth=1.5; ctx.stroke();
}

function _cpDrawHue() {
  var cv=document.getElementById('cp-hue'); if(!cv) return;
  var ctx=cv.getContext('2d'), w=cv.width, h=cv.height;
  var g=ctx.createLinearGradient(0,0,w,0);
  for(var i=0;i<=6;i++){var rgb=_hsvToRgb(i*60,1,1); g.addColorStop(i/6,'rgb('+rgb.join(',')+')');}
  ctx.fillStyle=g; ctx.fillRect(0,0,w,h);
  var hx=(_cpH/360)*w;
  ctx.beginPath(); ctx.moveTo(hx,0); ctx.lineTo(hx,h);
  ctx.strokeStyle='#fff'; ctx.lineWidth=2; ctx.stroke();
}

function _cpUpdateDisplay() {
  _cpDrawSV();
  _cpDrawHue();
  var rgb=_hsvToRgb(_cpH,_cpS,_cpV);
  var hex=_rgbToHex(rgb[0],rgb[1],rgb[2]);
  document.getElementById('cp-r').value=rgb[0];
  document.getElementById('cp-g').value=rgb[1];
  document.getElementById('cp-b').value=rgb[2];
  document.getElementById('job-color-hex').value=hex;
  document.getElementById('color-preview').style.background=hex;
  document.getElementById('color-btn').style.background=hex;
  _syncPaletteActive(hex);
}

function _cpSetFromHex(hex) {
  var rgb=_hexToRgb(hex); if(!rgb) return;
  var hsv=_rgbToHsv(rgb[0],rgb[1],rgb[2]);
  _cpH=hsv[0]; _cpS=hsv[1]; _cpV=hsv[2];
  _cpUpdateDisplay();
}

function _cpCommit() {
  var hex=_rgbToHex.apply(null,_hsvToRgb(_cpH,_cpS,_cpV));
  document.getElementById('job-color').value=hex;
  document.getElementById('job-color').dispatchEvent(new Event('change'));
}

// SV canvas drag
(function() {
  var sv=document.getElementById('cp-sv'); if(!sv) return;
  function pick(e) {
    var r=sv.getBoundingClientRect();
    _cpS=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
    _cpV=1-Math.max(0,Math.min(1,(e.clientY-r.top)/r.height));
    _cpUpdateDisplay();
  }
  sv.addEventListener('mousedown',function(e){e.preventDefault();_cpDraggingSV=true;pick(e);});
  document.addEventListener('mousemove',function(e){if(_cpDraggingSV)pick(e);});
  document.addEventListener('mouseup',function(){if(_cpDraggingSV){_cpDraggingSV=false;_cpCommit();}});
})();

// Hue slider drag
(function() {
  var hue=document.getElementById('cp-hue'); if(!hue) return;
  function pick(e) {
    var r=hue.getBoundingClientRect();
    _cpH=Math.max(0,Math.min(360,((e.clientX-r.left)/r.width)*360));
    _cpUpdateDisplay();
  }
  hue.addEventListener('mousedown',function(e){e.preventDefault();_cpDraggingHue=true;pick(e);});
  document.addEventListener('mousemove',function(e){if(_cpDraggingHue)pick(e);});
  document.addEventListener('mouseup',function(){if(_cpDraggingHue){_cpDraggingHue=false;_cpCommit();}});
})();

// RGB inputs
(function() {
  ['cp-r','cp-g','cp-b'].forEach(function(id) {
    var el=document.getElementById(id); if(!el) return;
    el.addEventListener('change',function() {
      var r=Math.max(0,Math.min(255,parseInt(document.getElementById('cp-r').value)||0));
      var g=Math.max(0,Math.min(255,parseInt(document.getElementById('cp-g').value)||0));
      var b=Math.max(0,Math.min(255,parseInt(document.getElementById('cp-b').value)||0));
      var hsv=_rgbToHsv(r,g,b);
      _cpH=hsv[0]; _cpS=hsv[1]; _cpV=hsv[2];
      _cpUpdateDisplay();
      _cpCommit();
    });
  });
})();

// Hex input
(function() {
  var hexEl=document.getElementById('job-color-hex'); if(!hexEl) return;
  hexEl.addEventListener('input',function() {
    var v=this.value.trim();
    if(/^#[0-9a-fA-F]{6}$/.test(v)) {
      document.getElementById('color-preview').style.background=v;
      _syncPaletteActive(v);
    }
  });
  function commitHex() {
    var v=hexEl.value.trim();
    if(!/^#[0-9a-fA-F]{6}$/.test(v)){hexEl.value=document.getElementById('job-color').value;return;}
    _cpSetFromHex(v);
    _cpCommit();
  }
  hexEl.addEventListener('blur',commitHex);
  hexEl.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();commitHex();}});
})();
