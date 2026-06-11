// ── Map rendering ─────────────────────────────────────────────────────────────

import { st } from './state.js';
import { map, lrs, editLayers, resetLrs } from './map-init.js';
import { _legendUserVis, resetLegend, redrawRings } from './legend.js';
import { getFitBoundsFlag, setFitBoundsFlag, getLastPreviewedIds, setLastPreviewedIds,
         getRadiusLinked, setRadiusLinked, idsKey, updateGsd, clearAreaFocus, showError } from './form-controls.js';
import { getTakeoffAuto, setTakeoffAuto, getTakeoffUserMoved, _renderTakeoffMarker } from './takeoff.js';
import { renderStatus } from './status-panel.js';
import { _buildVertexLayer, exitBridgeMode } from './polygon-bridge.js';
import { updateRouteOverlay, updateRouteStats, _renderAngleControl } from './route-planner.js';
// toggleEdit — circular but runtime-safe (only called in event handlers)
import { toggleEdit } from './polygon-edit.js';

export function onPreviewDone(payload) {
  console.log('[preview done]', payload.stats);
  st.previewData = payload;
  setLastPreviewedIds(idsKey());
  clearAreaFocus();
  document.getElementById('xb').disabled = false;
  document.getElementById('rstbtn').disabled = false;
  // Compute the lowest zone floor across zones that directly intersect the survey area.
  // Buffer-only and context-only zones are excluded — they don't constrain the flight altitude.
  st._altCap = null;
  (payload.zone_hits||[]).forEach(function(z) {
    if (z.context_only || z.buffer_only) return;
    if (z.lower_ref === 'AGL' && z.lower_limit != null && z.lower_limit > 0) {
      var m = z.lower_uom === 'FT' ? z.lower_limit * 0.3048 : parseFloat(z.lower_limit);
      if (!isNaN(m) && (st._altCap === null || m < st._altCap)) st._altCap = m;
    }
  });
  if (st._altCap !== null) {
    var currentH = parseFloat(document.getElementById('hgt').value);
    if (isNaN(currentH) || currentH > st._altCap) {
      document.getElementById('hgt').value = st._altCap;
      updateGsd();
      if (getRadiusLinked()) setRadiusLinked(true);
    }
  }
  try {
    // Auto-enable zones/areas on first appearance (when not yet in user prefs)
    if ((payload.zone_hits||[]).length && !lrs.zones && !('zones' in _legendUserVis))
      _legendUserVis.zones = true;
    if ((payload.original_areas||[]).length && !lrs.areas && !('areas' in _legendUserVis))
      _legendUserVis.areas = true;
    renderMap(payload);
    redrawRings();
    // Draw route BEFORE resetLegend so lrs.route is populated and visibility
    // can be applied correctly — route is computed after renderMap clears lrs.
    if (payload.stats && payload.stats.route_angle_deg_auto != null) {
      st._routeAngleAuto = payload.stats.route_angle_deg_auto;
      _renderAngleControl();
    }
    updateRouteOverlay();
    // _legendUserVis persists user eye choices across renders and job switches.
    // Empty on first render → resetLegend applies startOff defaults.
    resetLegend(_legendUserVis);
    renderStatus(payload.stats);
    if (payload.takeoff_point_4326) {
      setTakeoffAuto(payload.takeoff_point_4326);
      if (!getTakeoffUserMoved()) _renderTakeoffMarker(getTakeoffAuto());
    }
    if (payload.stats) {
      updateRouteStats({
        strip_count:      payload.stats.route_strip_count,
        photo_count:      payload.stats.route_photo_count,
        flight_time_min:  payload.stats.route_flight_time_min,
      });
    }
  } catch(e) {
    console.error('[onPreviewDone]', e);
    showError('Render error: ' + e.message);
  }
}

// Convert a GeoJSON geometry (Polygon or MultiPolygon) to an array of L.polygon layers.
export function geomToPolys(geom, style) {
  var out = [];
  if (!geom) return out;
  if (geom.type === 'Polygon') {
    var lls = geom.coordinates[0].map(function(c){return [c[1],c[0]];});
    out.push(L.polygon(lls, style));
  } else if (geom.type === 'MultiPolygon') {
    geom.coordinates.forEach(function(pc) {
      var lls = pc[0].map(function(c){return [c[1],c[0]];});
      out.push(L.polygon(lls, style));
    });
  }
  return out;
}

export function renderMap(data) {
  exitBridgeMode();
  Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
  resetLrs();
  editLayers.clearLayers();
  if (st._dataAttribution) { map.attributionControl.removeAttribution(st._dataAttribution); st._dataAttribution = ''; }

  // DSM grayscale overlay
  if (data.dsm_b64 && data.dsm_bounds) {
    var b = data.dsm_bounds;
    var dg = L.layerGroup();
    L.imageOverlay(
      'data:image/png;base64,' + data.dsm_b64,
      [[b[1], b[0]], [b[3], b[2]]],
      {opacity: 0.65, interactive: false, pane: 'dsmPane'}
    ).addTo(dg);
    lrs.dsm = dg;
  }

  // Parcel/property outlines (green dashed)
  if (data.original_areas && data.original_areas.length) {
    var fc = {type:'FeatureCollection', features:data.original_areas.map(function(g){
      return {type:'Feature', geometry:g, properties:{}};
    })};
    lrs.areas = L.geoJSON(fc, {
      style:{color:'#16a34a',weight:2,dashArray:'6 3',fillOpacity:.04}
    }).addTo(map);
  }

  // Keep-out circles
  var koBuf = data.stats && data.stats.home_buffer_m;
  if (koBuf && data.buildings && data.buildings.length) {
    var kg = L.layerGroup();
    data.buildings.forEach(function(b) {
      if (!b.is_keepout) return;
      var pt = centroid(b.geojson);
      if (!pt) return;
      L.circle(pt, {
        radius: koBuf, color: '#dc2626', weight: 1,
        fillColor: '#fca5a5', fillOpacity: 0.20, dashArray: '4 4'
      }).addTo(kg);
    });
    if (kg.getLayers().length) lrs.ko = kg.addTo(map);
  }

  // UAS restriction zones — sort largest→smallest so outer zones render first
  var zf = (data.zone_hits||[]).filter(function(z){return z.geojson;}).map(function(z){
    return {type:'Feature', geometry:z.geojson, properties:{
      name:z.name, r:z.restriction,
      upper_limit:z.upper_limit, upper_uom:z.upper_uom, upper_ref:z.upper_ref,
      lower_limit:z.lower_limit, lower_uom:z.lower_uom, lower_ref:z.lower_ref,
      contained_by:z.contained_by||[],
      context_only:!!z.context_only
    }};
  });
  zf.sort(function(a, b) {
    function bboxArea(f) {
      var c = f.geometry.type === 'Polygon' ? f.geometry.coordinates[0]
            : f.geometry.coordinates[0][0];
      var lons = c.map(function(p){return p[0];}), lats = c.map(function(p){return p[1];});
      return (Math.max.apply(null,lons)-Math.min.apply(null,lons)) *
             (Math.max.apply(null,lats)-Math.min.apply(null,lats));
    }
    return bboxArea(b) - bboxArea(a);
  });
  if (zf.length) {
    lrs.zones = L.geoJSON({type:'FeatureCollection', features:zf}, {
      style: function(f) {
        var ctx = f.properties.context_only;
        return {color:'#ea580c', weight:ctx?1.5:2, dashArray:ctx?'5,4':null,
                fillColor:'#f97316', fillOpacity:ctx?.08:.14};
      },
      onEachFeature:function(f,l){
        l.on('click', function(e) {
          L.DomEvent.stopPropagation(e);
          var pt = map.latLngToLayerPoint(e.latlng);
          var hits = [];
          lrs.zones.eachLayer(function(zl) {
            if (zl._containsPoint && zl._containsPoint(pt)) {
              hits.push(zl.feature.properties);
            }
          });
          if (hits.length) {
            var content = hits.map(function(p){
              var altLine = '';
              if (p.lower_ref === 'AGL' && p.lower_limit != null && p.lower_limit > 0) {
                var lo = p.lower_uom === 'FT' ? Math.round(p.lower_limit * 0.3048) : p.lower_limit;
                var hi = (p.upper_ref === 'AGL' && p.upper_limit != null)
                  ? (p.upper_uom === 'FT' ? Math.round(p.upper_limit * 0.3048) : p.upper_limit)
                  : null;
                altLine = '<br><small>Altitude: '+lo+(hi?' – '+hi:'+')+' m AGL — fly below '+lo+' m to exit</small>';
              } else if (p.upper_ref === 'AGL' && p.upper_limit != null) {
                var hi = p.upper_uom === 'FT' ? Math.round(p.upper_limit * 0.3048) : p.upper_limit;
                altLine = '<br><small>Ground to '+hi+' m AGL</small>';
              }
              var nestLine = p.contained_by && p.contained_by.length
                ? '<br><small style="color:#94a3b8">Within: '+p.contained_by.map(function(c){return c.name;}).join(', ')+'</small>'
                : '';
              var ctxNote = p.context_only ? ' <small style="color:#94a3b8">(nearby)</small>' : '';
              return '<b>'+p.name+'</b>'+ctxNote+'<br>'+p.r+altLine+nestLine;
            }).join('<hr style="margin:4px 0">');
            L.popup().setLatLng(e.latlng).setContent(content).openOn(map);
          }
        });
      }
    }).addTo(map);
  }

  // Power line keepout buffer (amber semi-transparent zone)
  if (data.powerlines_keepout) {
    var plkg = L.layerGroup();
    geomToPolys(data.powerlines_keepout, {
      color: '#b45309', weight: 1, dashArray: '4 3',
      fillColor: '#fde68a', fillOpacity: 0.30, interactive: false
    }).forEach(function(p){ p.addTo(plkg); });
    if (plkg.getLayers().length) lrs.plko = plkg.addTo(map);
  }

  // Power lines — solid amber = overhead (22312), dashed = underground cable (22311)
  if (data.power_lines && data.power_lines.length) {
    var plg = L.layerGroup();
    data.power_lines.forEach(function(pl) {
      var g = pl.geojson;
      if (!g) return;
      var coords;
      if (g.type === 'LineString') coords = [g.coordinates];
      else if (g.type === 'MultiLineString') coords = g.coordinates;
      else return;
      var style = pl.is_overhead
        ? {color:'#d97706', weight:2.5, opacity:0.9, interactive:false}
        : {color:'#d97706', weight:2, opacity:0.7, dashArray:'6 4', interactive:false};
      coords.forEach(function(seg) {
        var lls = seg.map(function(c){ return [c[1], c[0]]; });
        L.polyline(lls, style).addTo(plg);
      });
    });
    if (plg.getLayers().length) lrs.plines = plg.addTo(map);
  }

  // Buildings (red = keepout, yellow = info)
  if (data.buildings && data.buildings.length) {
    var bg = L.layerGroup();
    data.buildings.forEach(function(b) {
      var c = b.is_keepout ? '#dc2626' : '#FFBB00';
      var pt = centroid(b.geojson);
      if (pt) L.circleMarker(pt, {radius:5,color:c,fillColor:c,fillOpacity:.85,weight:1.5}).addTo(bg);
    });
    lrs.bldgs = bg.addTo(map);
  }

  // Survey polygon
  var surveyStyle = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  var surveyPolys = geomToPolys(data.survey, surveyStyle);
  if (surveyPolys.length) {
    lrs.survey = L.featureGroup(surveyPolys).addTo(map);
    lrs.survey.eachLayer(function(l) {
      l.on('dblclick', function(e) { L.DomEvent.stop(e); if (!st.editMode && !st._bridgeMode) toggleEdit(); });
    });
    console.log('[renderMap] survey bounds', lrs.survey.getBounds());
    if (getFitBoundsFlag()) { setFitBoundsFlag(false); map.fitBounds(lrs.survey.getBounds(), {padding:[40,40]}); }
  } else {
    console.warn('[renderMap] no survey polygons rendered, survey type:', data.survey && data.survey.type);
  }

  // Vertex dots
  if (data.survey) lrs.vertices = _buildVertexLayer(data.survey).addTo(map);

  // Data-source attribution
  var attrs = [];
  var s = data.stats || {};
  if (s.has_parcels) attrs.push('Parcels &copy; <a href="https://ruokavirasto.fi" target="_blank">Ruokavirasto</a>');
  if (s.has_properties) attrs.push('Properties &copy; <a href="https://maanmittauslaitos.fi" target="_blank">MML</a>');
  if (data.buildings && data.buildings.length || data.power_lines && data.power_lines.length) attrs.push('Topographic DB &amp; DEM &copy; <a href="https://maanmittauslaitos.fi" target="_blank">MML</a>');
  if (data.zone_hits) attrs.push('UAS zones &copy; <a href="https://traficom.fi" target="_blank">Traficom</a>');
  if (attrs.length) { st._dataAttribution = attrs.join(' | '); map.attributionControl.addAttribution(st._dataAttribution); }
}

export function centroid(geom) {
  try {
    if (geom.type==='Point') return [geom.coordinates[1], geom.coordinates[0]];
    if (geom.type==='Polygon') {
      var cs = geom.coordinates[0];
      return [cs.reduce(function(s,c){return s+c[1];},0)/cs.length,
              cs.reduce(function(s,c){return s+c[0];},0)/cs.length];
    }
  } catch(e){}
  return null;
}
