// ── Template Settings modal: Capture / Safety / Terrain tabs ─────────────────

import { markDirty } from './dirty-tracking.js';

var _defaults = {
  overlap_front_pct: 80,
  overlap_side_pct: 70,
  takeoff_security_height_m: 50,
  rth_height_m: 80,
  rc_lost_action: 'goBack',
  finish_action: 'goHome',
  advanced_mode: false,
  adv_min_height_m: 30,
  adv_max_height_m: null,
  adv_powerline_clearance_m: 70,
  adv_slope_f: 0.30,
};

function _el(id) { return document.getElementById(id); }

export function initTplDefaults(cfg) {
  if (cfg.overlap_front_pct != null)          _defaults.overlap_front_pct          = cfg.overlap_front_pct;
  if (cfg.overlap_side_pct  != null)          _defaults.overlap_side_pct           = cfg.overlap_side_pct;
  if (cfg.takeoff_security_height_m != null)  _defaults.takeoff_security_height_m  = cfg.takeoff_security_height_m;
  if (cfg.rth_height_m != null)               _defaults.rth_height_m               = cfg.rth_height_m;
  if (cfg.finish_action  != null)             _defaults.finish_action              = cfg.finish_action;
  if (cfg.rc_lost_action != null)             _defaults.rc_lost_action             = cfg.rc_lost_action;
  if (cfg.adv_slope_f    != null)             _defaults.adv_slope_f                = cfg.adv_slope_f;
  if (cfg.adv_min_dip_m  != null)             _defaults.adv_min_dip_m              = cfg.adv_min_dip_m;
  restoreTplSettings({});
}

export function initTplModal() {
  _el('tpl-ovf-minus').addEventListener('click', function() { _stepOvf(-1); });
  _el('tpl-ovf-plus').addEventListener('click',  function() { _stepOvf(+1); });
  _el('tpl-ovs-minus').addEventListener('click', function() { _stepOvs(-1); });
  _el('tpl-ovs-plus').addEventListener('click',  function() { _stepOvs(+1); });

  _el('tpl-advanced').addEventListener('change', function() {
    _updateAdvancedState();
    markDirty();
  });

  ['tpl-takeoff-sec', 'tpl-rth-height', 'tpl-rc-lost', 'tpl-finish-action',
   'tpl-adv-min-height', 'tpl-adv-max-height', 'tpl-adv-powerline', 'tpl-adv-slope-f',
  ].forEach(function(id) {
    _el(id).addEventListener('change', markDirty);
  });

  _el('tpl-modal').addEventListener('click', function(e) {
    if (e.target === _el('tpl-modal')) closeTplModal();
  });
}

export function openTplModal() {
  _el('tpl-modal').classList.add('open');
}

export function closeTplModal() {
  _el('tpl-modal').classList.remove('open');
}

export function tplTab(name) {
  document.querySelectorAll('.tpl-tab').forEach(function(b) {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tpl-panel').forEach(function(p) {
    p.style.display = p.id === 'tpl-panel-' + name ? 'block' : 'none';
  });
}

export function getTplSettings() {
  return {
    overlap_front_pct:         parseInt(_el('tpl-overlap-front').value)  || _defaults.overlap_front_pct,
    overlap_side_pct:          parseInt(_el('tpl-overlap-side').value)   || _defaults.overlap_side_pct,
    takeoff_security_height_m: parseFloat(_el('tpl-takeoff-sec').value)  || _defaults.takeoff_security_height_m,
    rth_height_m:              parseFloat(_el('tpl-rth-height').value)   || _defaults.rth_height_m,
    rc_lost_action:            _el('tpl-rc-lost').value,
    finish_action:             _el('tpl-finish-action').value,
    advanced_mode:             _el('tpl-advanced').checked,
    adv_min_height_m:          parseFloat(_el('tpl-adv-min-height').value) || _defaults.adv_min_height_m,
    adv_max_height_m:          parseFloat(_el('tpl-adv-max-height').value) || null,
    adv_powerline_clearance_m: parseFloat(_el('tpl-adv-powerline').value)  || _defaults.adv_powerline_clearance_m,
    adv_slope_f:               parseFloat(_el('tpl-adv-slope-f').value)    || _defaults.adv_slope_f,
  };
}

export function restoreTplSettings(s) {
  var d = _defaults;
  function v(key, fallback) { return (s && s[key] != null) ? s[key] : fallback; }

  var ovf = v('overlap_front_pct', d.overlap_front_pct);
  var ovs = v('overlap_side_pct',  d.overlap_side_pct);
  _el('tpl-overlap-front').value = ovf;
  _el('tpl-overlap-side').value  = ovs;
  _el('tpl-ovf-val').textContent = ovf + '%';
  _el('tpl-ovs-val').textContent = ovs + '%';

  _el('tpl-takeoff-sec').value   = v('takeoff_security_height_m', d.takeoff_security_height_m);
  _el('tpl-rth-height').value    = v('rth_height_m',               d.rth_height_m);
  _el('tpl-rc-lost').value       = v('rc_lost_action',             d.rc_lost_action);
  _el('tpl-finish-action').value = v('finish_action',              d.finish_action);

  _el('tpl-advanced').checked          = !!v('advanced_mode', false);
  _el('tpl-adv-min-height').value      = v('adv_min_height_m',          d.adv_min_height_m);
  _el('tpl-adv-max-height').value      = v('adv_max_height_m',          '') || '';
  _el('tpl-adv-powerline').value       = v('adv_powerline_clearance_m', d.adv_powerline_clearance_m);
  _el('tpl-adv-slope-f').value         = v('adv_slope_f',               d.adv_slope_f);
  _updateAdvancedState();
}

function _updateAdvancedState() {
  var adv = _el('tpl-advanced').checked;
  ['tpl-adv-min-height', 'tpl-adv-max-height', 'tpl-adv-powerline', 'tpl-adv-slope-f'].forEach(function(id) {
    _el(id).disabled = !adv;
  });
  var dot = _el('adv-dot');
  if (dot) dot.style.display = adv ? 'inline-block' : 'none';
  import('./state.js').then(function(m){ m.st._waypointMode = adv; });
}

function _stepOvf(delta) {
  var cur = parseInt(_el('tpl-overlap-front').value) || _defaults.overlap_front_pct;
  var v = Math.min(99, Math.max(1, cur + delta));
  _el('tpl-overlap-front').value = v;
  _el('tpl-ovf-val').textContent = v + '%';
  markDirty();
}

function _stepOvs(delta) {
  var cur = parseInt(_el('tpl-overlap-side').value) || _defaults.overlap_side_pct;
  var v = Math.min(99, Math.max(1, cur + delta));
  _el('tpl-overlap-side').value = v;
  _el('tpl-ovs-val').textContent = v + '%';
  markDirty();
}
