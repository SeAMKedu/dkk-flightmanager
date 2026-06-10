// ── Application entry point ───────────────────────────────────────────────────

async function init() {
  document.getElementById('jname').value = defaultJobName();

  try {
    var r = await fetch('/api/drones');
    if (!r.ok) throw new Error('drones ' + r.status);
    drones = await r.json();
    var sel = document.getElementById('dsel');
    drones.forEach(function(d) {
      var o = document.createElement('option');
      o.value = d.name; o.textContent = d.label;
      sel.appendChild(o);
    });

    var cr = await fetch('/api/config');
    if (!cr.ok) throw new Error('config ' + cr.status);
    var cfg = await cr.json();

    outputDir = cfg.output_dir || '';
    updateFolderHint();

    if (cfg.default_drone) sel.value = cfg.default_drone;
    if (cfg.subcategory) setSub(cfg.subcategory, true);
    if (cfg.offset_m !== undefined) document.getElementById('offset').value = cfg.offset_m;
    if (cfg.height_m) {
      var h0 = Math.round(cfg.height_m);
      document.getElementById('hgt').value = h0;
      document.getElementById('warn-radius').value = 3 * h0;
    }
    if (cfg.simplify && cfg.simplify !== 'auto') {
      setSimpManual(parseFloat(cfg.simplify) || 0, true);
    }
    document.getElementById('kochk').checked = cfg.keepout !== false;
    if (cfg.vlos_range_m) _vlosRange = cfg.vlos_range_m;
    if (cfg.overlap_front_pct) _cfgOverlapFront = cfg.overlap_front_pct;
    if (cfg.overlap_side_pct)  _cfgOverlapSide  = cfg.overlap_side_pct;
    if (cfg.auto_flight_speed_ms) {
      _cfgDefaultSpeedMs = cfg.auto_flight_speed_ms;
    }
    updateGsd();
    _mmlApiKey = cfg.mml_api_key || '';
    if (_mmlApiKey) _initBaseLayers(_mmlApiKey);
    if (cfg.color_palette) initColorPalette(cfg.color_palette);
    if (cfg.max_area_loss_pct != null) _cfgMaxAreaLossPct = cfg.max_area_loss_pct;
    console.log('[init] config loaded, outputDir='+outputDir+', drone='+cfg.default_drone);
  } catch(e) {
    console.error('[init] failed:', e);
  }
  renderStatus(null);
  focusArea();
  setJpOpen(_jpOpen);
  loadJobsList();
  _initEventStream();
}

// Bootstrap — called after all modules are loaded
init();
