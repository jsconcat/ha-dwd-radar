/* DWD Regenradar – WebGL-Anzeige (MapLibre GL JS + eigener Shader).
 * Der Server liefert pro echtem Zeitpunkt ein 8-bit-Rohdatenbild (Regen, log-skaliert) und pro Bildpaar
 * ein Bewegungsfeld. Der Shader verschiebt beide Nachbarbilder entlang der Bewegung, mischt sie und färbt
 * erst danach mit festen Farbstufen ein – stufenlos für jede Zeitposition. Wolken: eigene Stützstellen,
 * zeitlich überblendet. Alle Bilder liegen als Texturen auf der GPU (keine Browser-Bildcaches).
 * - Standard: pausiert auf "Aktuell". Play läuft stufenlos Aktuell .. +2 h in Schleife.
 * - Zeitleiste mit festem Mittelpunkt, Blöcke je Quelle/Auflösung, Schwung + weiches Einrasten.
 * - URL-Parameter (pro Dashboard-Karte): days, return, zoom, lat, lon, theme
 */
(function () {
  'use strict';

  var cfg, map, manifest = null, frames = [], times = [], nowIdx = -1, fcEnd = -1;
  var pos = 0, cur = -1;                 // continuous frame position, nearest frame index
  var playing = false, hideTimer = null, resetTimer = null, lastGenerated = null;
  var realIdx = [], ckT = [], ckU = [];
  var userActive = false, mapReady = false;
  var DAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];

  var $ = function (id) { return document.getElementById(id); };
  var IN_POPUP = new URLSearchParams(location.search).get('popup') === '1';
  var ms = function (iso) { return new Date(iso).getTime(); };
  var hm = function (t) { return new Date(t).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }); };
  var dayhm = function (t) { return DAYS[new Date(t).getDay()] + ' ' + hm(t); };
  var sameDay = function (a, b) { return new Date(a).toDateString() === new Date(b).toDateString(); };
  var BASE_URL = location.href.replace(/[?#].*$/, '').replace(/[^/]*$/, '');
  var abs = function (u) { return /^https?:/.test(u) ? u : BASE_URL + u; };
  function getJSON(url) {
    return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  // MapLibre zoom levels are one below Leaflet's for the same scale (512 px world tile)
  var Z = function (leafletZoom) { return leafletZoom - 1; };

  // ------------------------------------------------- per-card URL overrides
  function applyUrlParams() {
    var q = new URLSearchParams(location.search);
    var num = function (k) { var v = parseFloat(q.get(k)); return isNaN(v) ? null : v; };
    if (num('days') !== null) cfg.forecastDays = Math.max(0, Math.min(7, num('days')));
    if (num('return') !== null) cfg.resetViewS = Math.max(0, Math.min(120, num('return')));
    if (num('zoom') !== null) cfg.zoom = num('zoom');
    var th = q.get('theme');
    if (th === 'auto' || th === 'light' || th === 'dark') cfg.theme = th;
    if (num('lat') !== null && num('lon') !== null) {
      cfg.center = [num('lat'), num('lon')];
      cfg.pins.forEach(function (p) { if (p.home) { p.lat = num('lat'); p.lon = num('lon'); } });
    }
  }

  // ---------------------------------------------------------------- UI DOM
  function buildDom() {
    var bar = $('bar');
    bar.innerHTML =
      '<button id="play" class="play" aria-label="Wiedergabe">' +
      '<svg id="ico-pause" viewBox="0 0 24 24" style="display:none"><rect x="6" y="5" width="4" height="14" rx="1"/>' +
      '<rect x="14" y="5" width="4" height="14" rx="1"/></svg>' +
      '<svg id="ico-play" viewBox="0 0 24 24"><path d="M7 5v14l12-7z"/></svg></button>' +
      '<div id="tl" class="tl"><div id="strip" class="strip"></div>' +
      '<div class="center"></div><div id="bubble" class="bubble"><span id="bubble-t"></span>' +
      '<span id="bubble-now" class="bnow" title="Zurück zu Aktuell">&#8635;</span></div></div>';
    bar.className = 'bar hidden';
    var z = document.createElement('div');
    z.id = 'zoom';
    z.className = 'zoombox hidden';
    var fsIcon = IN_POPUP
      ? '<svg viewBox="0 0 24 24"><path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/></svg>'
      : '<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
    z.innerHTML = '<button id="zfs" class="fs" aria-label="' + (IN_POPUP ? 'Vollbild schließen' : 'Vollbild') + '">' + fsIcon + '</button>' +
      (cfg.temps === 'toggle' ? '<button id="ztemp" class="fs ttoggle" aria-label="Temperaturen">°C</button>' : '') +
      '<div class="zgroup"><button id="zin" aria-label="Hineinzoomen">+</button>' +
      '<button id="zout" aria-label="Herauszoomen">&minus;</button></div>' +
      '<button id="zhome" class="fs" aria-label="Zur Startansicht"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="6.5"/>' +
      '<circle cx="12" cy="12" r="2" class="dot"/><path d="M12 2.5v3.5M12 18v3.5M2.5 12h3.5M18 12h3.5"/></svg></button>';
    document.body.appendChild(z);
  }

  // ------------------------------------------------------------------- pins
  function pinHtml(p) {
    var home = p.home;
    var w = home ? 24 : 18, h = home ? 32 : 24;
    var fill = isDark ? (home ? '#3d74d6' : '#7d8b96') : (home ? '#173478' : '#4a5a66');
    var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 40">' +
      '<path d="M15 1C7.3 1 1 7.2 1 14.9 1 25.3 15 39 15 39s14-13.7 14-24.1C29 7.2 22.7 1 15 1z" fill="' + fill +
      '" stroke="#fff" stroke-width="2.2"/><circle cx="15" cy="15" r="' + (home ? 5.5 : 4.5) + '" fill="#fff"/></svg>';
    var lbl = (!home && p.name) ? '<span class="lbl">' + p.name.replace(/[<>&]/g, '') + '</span>' : '';
    var halo = home ? '<span class="home-halo"></span>' : '';
    return halo + svg + lbl;
  }

  // ----------------------------------------------------------------- theme
  // auto: follow Home Assistant's resolved dark mode (same origin), else the device setting
  var isDark = false, pinEls = [];
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  function haDark() {
    try {
      var ha = window.parent && window.parent !== window && window.parent.document.querySelector('home-assistant');
      if (ha && ha.hass && ha.hass.themes && typeof ha.hass.themes.darkMode === 'boolean') return ha.hass.themes.darkMode;
    } catch (e) { /* cross-origin or not embedded */ }
    return null;
  }
  function wantDark() {
    if (cfg.theme === 'dark') return true;
    if (cfg.theme === 'light') return false;
    var h = haDark();
    return h !== null ? h : !!(mq && mq.matches);
  }
  function themedUrl(kind) {
    var bm = cfg.basemap;
    if (kind === 'base') return isDark && bm.baseDark ? bm.baseDark : bm.base;
    return isDark && bm.labelsDark ? bm.labelsDark : bm.labels;
  }
  var BG = { light: '#e3e8dc', dark: '#1b2026' };
  function applyTheme(force) {
    var d = wantDark();
    if (d === isDark && !force) return;
    isDark = d;
    document.documentElement.classList.toggle('dark', d);
    if (!map || !mapReady) return;
    var bm = cfg.basemap;
    map.setPaintProperty('bg', 'background-color', d ? BG.dark : BG.light);
    if (bm.own) {
      map.getSource('base').setTiles([abs(themedUrl('base'))]);
      map.getSource('labels').setTiles([abs(themedUrl('labels'))]);
    } else if (map.getLayer('ext')) {
      map.setPaintProperty('ext', 'raster-brightness-max', d ? 0.42 : 1);
      map.setPaintProperty('ext', 'raster-saturation', d ? -0.8 : -0.4);
    }
    if (map.getLayer('hill')) map.setPaintProperty('hill', 'raster-opacity', d ? 0.55 : 1);
    pinEls.forEach(function (e) { e[0].innerHTML = pinHtml(e[1]); });
    map.triggerRepaint();
  }
  function watchTheme() {
    if (mq && mq.addEventListener) mq.addEventListener('change', function () { applyTheme(); });
    setInterval(function () { applyTheme(); }, 5000);     // HA theme changes have no event we can reach
  }

  // -------------------------------------------------------------------- map
  function wideBounds() {
    var b = cfg.bounds, pl = (b[1][0] - b[0][0]) * 0.75, po = (b[1][1] - b[0][1]) * 0.75;
    return [b[0][1] - po, b[0][0] - pl, b[1][1] + po, b[1][0] + pl];
  }
  function initMap() {
    var bm = cfg.basemap, b = cfg.bounds;
    var tileOpts = function (tiles, maxz) {
      return { type: 'raster', tiles: tiles, tileSize: 256, minzoom: 5, maxzoom: maxz || bm.maxNativeZoom || 12, bounds: wideBounds() };
    };
    var sources = {}, layers = [{ id: 'bg', type: 'background', paint: { 'background-color': isDark ? BG.dark : BG.light } }];
    if (bm.url) {
      sources.ext = { type: 'raster', tiles: [bm.url.replace('{s}', (bm.subdomains || 'abc').charAt(0)).replace('{r}', '')],
        tileSize: 256, maxzoom: bm.maxNativeZoom || 19 };
      layers.push({ id: 'ext', type: 'raster', source: 'ext', paint: { 'raster-fade-duration': 0,
        'raster-saturation': isDark ? -0.8 : -0.4, 'raster-brightness-max': isDark ? 0.42 : 1, 'raster-contrast': -0.2 } });
    }
    if (bm.own && bm.base) {
      sources.base = tileOpts([abs(themedUrl('base'))]);
      layers.push({ id: 'base', type: 'raster', source: 'base', paint: { 'raster-fade-duration': 0 } });
    }
    if (cfg.hillshade) {
      sources.hill = tileOpts([abs('hillshade/{z}/{x}/{y}.png?v=' + cfg.version)], cfg.maxZoom);
      layers.push({ id: 'hill', type: 'raster', source: 'hill', paint: { 'raster-fade-duration': 0, 'raster-opacity': isDark ? 0.55 : 1 } });
    }
    var pad = 0.6, dlat = (b[1][0] - b[0][0]) * pad, dlon = (b[1][1] - b[0][1]) * pad;
    map = new maplibregl.Map({
      container: 'map',
      style: { version: 8, sources: sources, layers: layers },
      center: [cfg.center[1], cfg.center[0]], zoom: Z(cfg.zoom),
      minZoom: Z(cfg.minZoom), maxZoom: Z(cfg.maxZoom) + 0.5,
      maxBounds: [[b[0][1] - dlon, b[0][0] - dlat], [b[1][1] + dlon, b[1][0] + dlat]],
      dragRotate: false, pitchWithRotate: false, touchPitch: false, renderWorldCopies: false, fadeDuration: 0,
      attributionControl: false
    });
    map.touchZoomRotate.disableRotation();
    // tiny attribution line like the Leaflet page (MapLibre's control is larger)
    var at = document.createElement('div');
    at.className = 'attrib';
    at.textContent = [bm.attribution || '', 'Relief: AWS Terrain Tiles', 'Radar/Modelle: © Deutscher Wetterdienst']
      .filter(Boolean).join(' · ');
    document.body.appendChild(at);
    map.keyboard.disable();
    map.on('load', function () {
      map.addLayer(wx);
      if (bm.labels) {
        map.addSource('labels', tileOpts([abs(bm.own ? themedUrl('labels') : bm.labels)], bm.own ? bm.maxNativeZoom : 19));
        map.addLayer({ id: 'labels', type: 'raster', source: 'labels', paint: { 'raster-fade-duration': 0 } });
      }
      mapReady = true;
      applyTheme(true);
      render(pos);
      loadTemps();
      setInterval(loadTemps, 600000);
    });
    map.on('error', function (e) {
      var m = e && e.error && String(e.error.message || e.error);
      if (m && !/404|Failed to fetch|AJAXError|NetworkError/.test(m)) console.warn('Karte:', m);
    });

    cfg.pins.forEach(function (p) {
      var el = document.createElement('div');
      el.className = 'pin';
      el.style.zIndex = p.home ? 2 : 1;
      el.innerHTML = pinHtml(p);
      new maplibregl.Marker({ element: el, anchor: 'bottom' }).setLngLat([p.lon, p.lat]).addTo(map);
      pinEls.push([el, p]);
    });

    // The iframe may get its final size only after Home Assistant has laid out the dashboard.
    var el = $('map'), resizeT = null, lastW = 0, lastH = 0;
    var refit = function () {
      if (el.clientWidth === lastW && el.clientHeight === lastH) return;
      lastW = el.clientWidth; lastH = el.clientHeight;
      map.resize();
      measure();
      moveStrip(stripXForPos(pos));
    };
    var refitSoon = function () { clearTimeout(resizeT); resizeT = setTimeout(refit, 120); };
    if (window.ResizeObserver) new ResizeObserver(refitSoon).observe(el);
    window.addEventListener('resize', refitSoon);
    setInterval(refit, 15000);
    setTimeout(refit, 1000); setTimeout(refit, 4000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) refitSoon(); });

    map.on('click', function () { showControls(); });
    map.on('movestart', function (e) { if (e.originalEvent) armReset(); });
    map.on('moveend', function () { armReset(); });
    $('zin').addEventListener('click', function (e) { e.stopPropagation(); map.zoomIn(); showControls(); });
    $('zout').addEventListener('click', function (e) { e.stopPropagation(); map.zoomOut(); showControls(); });
    $('zfs').addEventListener('click', function (e) { e.stopPropagation(); toggleFullscreen(); });
    $('zhome').addEventListener('click', function (e) {
      e.stopPropagation();
      clearTimeout(resetTimer);
      map.easeTo({ center: [cfg.center[1], cfg.center[0]], zoom: Z(cfg.zoom), duration: 450 });
      showControls();
    });
    map.on('zoomend', declutterTemps);
    map.on('moveend', declutterTemps);
    map.on('resize', declutterTemps);
    if ($('ztemp')) {
      var tb = $('ztemp');
      tb.classList.toggle('on', TP.on);
      tb.addEventListener('click', function (e) {
        e.stopPropagation();
        TP.on = !TP.on;
        try { localStorage.setItem('dwdRadarTemps', TP.on ? '1' : '0'); } catch (err) { /* ignore */ }
        tb.classList.toggle('on', TP.on);
        updateTemps(frames.length ? timeOfPos(pos) : Date.now());
        showControls();
      });
    }
  }

  function atHome() {
    var c = map.getCenter();
    return Math.abs(map.getZoom() - Z(cfg.zoom)) < 0.01 && Math.abs(c.lat - cfg.center[0]) < 1e-3 && Math.abs(c.lng - cfg.center[1]) < 1e-3;
  }
  function armReset() {
    clearTimeout(resetTimer);
    if (!cfg.resetViewS || atHome()) return;
    resetTimer = setTimeout(function () { map.jumpTo({ center: [cfg.center[1], cfg.center[0]], zoom: Z(cfg.zoom) }); },
      cfg.resetViewS * 1000);
  }

  // ------------------------------------------------------------ fullscreen
  // 1st choice: browser_mod popup (fullscreen) in the Home Assistant frontend (same origin as /local);
  // 2nd: Fullscreen API (needs the iframe to allow it); 3rd: open in a new tab.
  function haRoot() {
    try {
      if (window.parent && window.parent !== window && window.parent.document) return window.parent;
    } catch (e) { /* cross-origin */ }
    return null;
  }
  function browserMod(service, data) {
    var p = haRoot();
    if (!p) return false;
    var hasBM = false;
    try { hasBM = !!(p.browser_mod || (p.customElements && p.customElements.get('browser-mod-popup'))); } catch (e) {}
    if (!hasBM) return false;
    var ev = new p.CustomEvent('ll-custom', { bubbles: true, composed: true,
      detail: { action: 'fire-dom-event', browser_mod: { service: service, data: data || {} } } });
    p.document.body.dispatchEvent(ev);
    return true;
  }
  function popupUrl() {
    var q = new URLSearchParams(location.search);
    q.set('popup', '1');
    return location.pathname + '?' + q.toString();
  }
  function toggleFullscreen() {
    if (IN_POPUP) {
      if (browserMod('browser_mod.close_popup')) return;
      if (document.fullscreenElement) { document.exitFullscreen(); return; }
      window.close();
      return;
    }
    var p = haRoot();
    var ratio = p ? Math.round((p.innerHeight - 64) / p.innerWidth * 1000) / 10 : 60;
    var ok = browserMod('browser_mod.popup', {
      title: 'Regenradar', size: 'fullscreen',
      content: { type: 'iframe', url: popupUrl(), aspect_ratio: ratio + '%' }
    });
    if (ok) return;
    var el = document.documentElement;
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    if (el.requestFullscreen) {
      el.requestFullscreen().catch(function () { window.open(popupUrl(), '_blank'); });
    } else {
      window.open(popupUrl(), '_blank');
    }
  }
  document.addEventListener('fullscreenchange', function () { setTimeout(function () { if (map) map.resize(); }, 200); });

  // ------------------------------------------------------------- textures
  // Every data image is decoded once and kept as a GPU texture until it leaves the manifest.
  var TEX = {}, texQueue = [], texLoading = 0, glCtx = null;
  function texEntry(url, prio) {
    var e = TEX[url];
    if (e) { if (prio && !e.ready && !e.busy) bump(url); return e; }
    var name = url.split('/').pop();
    e = TEX[url] = { t: null, ready: false, busy: false, fails: 0,
      kind: /^fl_/.test(name) ? 'flow' : /^r_/.test(name) ? 'rain' : 'cloud' };
    if (prio) texQueue.unshift(url); else texQueue.push(url);
    pump();
    return e;
  }
  function bump(url) {
    var i = texQueue.indexOf(url);
    if (i > 0) { texQueue.splice(i, 1); texQueue.unshift(url); }
  }
  function pump() {
    while (glCtx && texLoading < 4 && texQueue.length) {
      var url = texQueue.shift(), e = TEX[url];
      if (!e || e.ready || e.busy) continue;
      load(url, e);
    }
  }
  function load(url, e) {
    e.busy = true; texLoading++;
    var im = new Image();
    im.onload = function () {
      var go = function () {
        texLoading--; e.busy = false;
        if (TEX[url] !== e) { pump(); return; }        // dropped meanwhile
        upload(e, im);
        pump();
      };
      if (im.decode) im.decode().then(go, go); else go();
    };
    im.onerror = function () {
      texLoading--; e.busy = false;
      if (TEX[url] === e && ++e.fails < 4) setTimeout(function () { if (TEX[url] === e) { texQueue.push(url); pump(); } }, 3000 * e.fails);
      pump();
    };
    im.src = url;
  }
  function upload(e, im) {
    var gl = glCtx, v2 = typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext;
    // rain: 16 bit in R/G (RG8), clouds: 8 bit (R8), motion fields: RGB
    var fmt, ifmt;
    if (e.kind === 'flow') { fmt = ifmt = gl.RGB; }
    else if (e.kind === 'rain') { fmt = v2 ? gl.RG : gl.RGB; ifmt = v2 ? gl.RG8 : gl.RGB; }
    else { fmt = v2 ? gl.RED : gl.LUMINANCE; ifmt = v2 ? gl.R8 : gl.LUMINANCE; }
    var t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, ifmt, fmt, gl.UNSIGNED_BYTE, im);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    e.t = t; e.ready = true;
    if (map) map.triggerRepaint();
  }
  // keep exactly the textures referenced by the manifest
  function syncTextures(urls) {
    var keep = {};
    urls.forEach(function (u) { keep[u] = true; });
    Object.keys(TEX).forEach(function (u) {
      if (keep[u]) return;
      var e = TEX[u];
      if (e.t && glCtx) glCtx.deleteTexture(e.t);
      delete TEX[u];
    });
    texQueue = texQueue.filter(function (u) { return keep[u]; });
    urls.forEach(function (u) { texEntry(u, false); });
  }
  function readyTex(url) { var e = url && TEX[url]; return e && e.ready ? e : null; }

  // ---------------------------------------------------------- WebGL layer
  var CLASSES = [[0.1, [126, 251, 253]], [0.2, [96, 201, 158]], [0.5, [67, 151, 66]], [1, [36, 112, 44]],
    [2, [247, 232, 52]], [5, [247, 156, 32]], [10, [226, 44, 36]], [20, [150, 20, 30]], [50, [196, 52, 196]]];
  var encRain = function (r) { return Math.max(1, Math.min(255, Math.round(1 + 24 * Math.log2(r / 0.05)))); };
  var VS =
    'attribute vec2 a_pos; uniform mat4 u_matrix;\n' +
    'uniform vec4 u_ga; uniform vec4 u_gb; uniform vec4 u_gc;\n' +
    'varying vec2 v_ua; varying vec2 v_ub; varying vec2 v_uc;\n' +
    'void main() {\n' +
    '  v_ua = (a_pos - u_ga.xy) / u_ga.zw; v_ub = (a_pos - u_gb.xy) / u_gb.zw; v_uc = (a_pos - u_gc.xy) / u_gc.zw;\n' +
    '  gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0);\n' +
    '}';
  var FS =
    'varying vec2 v_ua; varying vec2 v_ub; varying vec2 v_uc;\n' +
    'uniform sampler2D u_a; uniform sampler2D u_b; uniform sampler2D u_fl; uniform sampler2D u_ca; uniform sampler2D u_cb;\n' +
    'uniform float u_w; uniform float u_hasfl; uniform vec2 u_fscale; uniform float u_oa; uniform float u_ob;\n' +
    'uniform float u_th[9]; uniform vec3 u_col[9];\n' +
    'uniform float u_wc; uniform float u_hasc; uniform vec3 u_ccol; uniform float u_calpha;\n' +
    'float smp(sampler2D s, vec2 uv) {\n' +
    '  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) return 0.0;\n' +
    '  return TEX(s, uv).r;\n' +
    '}\n' +
    'float rain(sampler2D s, vec2 uv) {\n' +       // 16 bit: R high byte, G low byte -> 0..1 like an 8-bit value/255
    '  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) return 0.0;\n' +
    '  vec2 t = TEX(s, uv).rg;\n' +
    '  return t.r + t.g / 256.0;\n' +
    '}\n' +
    'void main() {\n' +
    '  vec2 ua = v_ua, ub = v_ub;\n' +
    '  if (u_hasfl > 0.5) {\n' +
    '    vec2 f = (TEX(u_fl, clamp(v_ua, 0.0, 1.0)).rg * 255.0 - 128.0) * u_fscale;\n' +
    '    ua -= u_w * f; ub += (1.0 - u_w) * f;\n' +
    '  }\n' +
    '  float r = mix(rain(u_a, ua), rain(u_b, ub), u_w);\n' +
    '#ifdef HAS_DERIV\n' +
    '  float fw = max(fwidth(r) * 0.75, 0.0008);\n' +
    '#else\n' +
    '  float fw = 0.004;\n' +
    '#endif\n' +
    '  vec3 col = u_col[0]; float cov = 0.0;\n' +
    '  for (int i = 0; i < 9; i++) {\n' +
    '    float k = smoothstep(u_th[i] - fw, u_th[i] + fw, r);\n' +
    '    if (i == 0) cov = k; else col = mix(col, u_col[i], k);\n' +
    '  }\n' +
    '  float ra = cov * mix(u_oa, u_ob, u_w);\n' +
    '  float c = mix(smp(u_ca, v_uc), smp(u_cb, v_uc), u_wc) * u_hasc;\n' +
    '  float ca = smoothstep(0.1, 1.0, c) * u_calpha * (1.0 - ra);\n' +
    '  FRAG = vec4(col * ra + u_ccol * ca, ra + ca);\n' +
    '}';

  // what to draw (set by render(), consumed by the layer); last drawn state is kept as fallback
  var want = null, drawn = null, drawnC = null;
  var wx = {
    id: 'wx', type: 'custom', renderingMode: '2d',
    onAdd: function (m, gl) {
      var v2 = typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext;
      var deriv = v2 || !!gl.getExtension('OES_standard_derivatives');
      var vh = v2 ? '#version 300 es\n#define attribute in\n#define varying out\n' : '';
      var fh = (v2 ? '#version 300 es\nprecision highp float;\n#define varying in\n#define TEX texture\nout vec4 outColor;\n#define FRAG outColor\n'
        : (deriv ? '#extension GL_OES_standard_derivatives : enable\n' : '') + 'precision highp float;\n#define TEX texture2D\n#define FRAG gl_FragColor\n') +
        (deriv ? '#define HAS_DERIV 1\n' : '');
      var sh = function (type, s) {
        var o = gl.createShader(type); gl.shaderSource(o, s); gl.compileShader(o);
        if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error('Shader: ' + gl.getShaderInfoLog(o));
        return o;
      };
      var p = gl.createProgram();
      gl.attachShader(p, sh(gl.VERTEX_SHADER, vh + VS));
      gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fh + FS));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error('Shader-Link: ' + gl.getProgramInfoLog(p));
      this.p = p; this.L = {};
      var self = this;
      ['u_matrix', 'u_ga', 'u_gb', 'u_gc', 'u_a', 'u_b', 'u_fl', 'u_ca', 'u_cb', 'u_w', 'u_hasfl', 'u_fscale', 'u_oa', 'u_ob',
        'u_th', 'u_col', 'u_wc', 'u_hasc', 'u_ccol', 'u_calpha'].forEach(function (n) { self.L[n] = gl.getUniformLocation(p, n); });
      this.aPos = gl.getAttribLocation(p, 'a_pos');
      this.buf = gl.createBuffer();
      this.quad = null;
      // dummy texture for unused samplers
      this.zero = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.zero);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, 1, 1, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, new Uint8Array([0]));
      glCtx = gl;
      pump();
    },
    setGrids: function (grids) {
      var gl = glCtx, g = {}, x0 = 1, y0 = 1, x1 = 0, y1 = 0;
      Object.keys(grids).forEach(function (k) {
        var b = grids[k].b;
        var nw = maplibregl.MercatorCoordinate.fromLngLat([b[0][1], b[1][0]]);
        var se = maplibregl.MercatorCoordinate.fromLngLat([b[1][1], b[0][0]]);
        g[k] = { v: [nw.x, nw.y, se.x - nw.x, se.y - nw.y], w: grids[k].w, h: grids[k].h };
        x0 = Math.min(x0, nw.x); y0 = Math.min(y0, nw.y); x1 = Math.max(x1, se.x); y1 = Math.max(y1, se.y);
      });
      this.grids = g;
      if (!gl) return;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([x0, y0, x1, y0, x0, y1, x1, y1]), gl.STATIC_DRAW);
      this.quad = true;
    },
    render: function (gl, args) {
      if (!this.quad && manifest) this.setGrids(manifest.grids);
      if (!this.quad || !want) return;
      var mtx = args && args.defaultProjectionData ? args.defaultProjectionData.mainMatrix
        : (args && args.modelViewProjectionMatrix) || args;
      // rain: both neighbours ready -> interpolate; else what is there; else last drawn state
      var q = want, A = readyTex(q.a && q.a.u), B = readyTex(q.b && q.b.u), F = readyTex(q.a && q.a.fl);
      var st = null;
      if (A && (B || !q.b || q.w < 0.002)) st = { A: A, B: B || A, w: B ? q.w : 0, fa: q.a, fb: B ? q.b : q.a, F: (B && F && q.a.g === q.b.g) ? F : null };
      else if (A && q.w < 0.5) st = { A: A, B: A, w: 0, fa: q.a, fb: q.a, F: null };
      else if (B) st = { A: B, B: B, w: 0, fa: q.b, fb: q.b, F: null };
      else if (!q.a) st = { none: true };
      if (st && !st.none && (!st.A.t || !st.B.t)) st = null;
      if (st) drawn = st; else st = drawn;
      var CA = readyTex(q.ca), CB = readyTex(q.cb), cst = null;
      if (CA && (CB || !q.cb)) cst = { A: CA, B: CB || CA, w: CB ? q.wc : 0 };
      else if (CA || CB) cst = { A: CA || CB, B: CA || CB, w: 0 };
      else if (!q.ca) cst = { none: true };
      if (cst) drawnC = cst; else cst = drawnC;
      if ((!st || st.none) && (!cst || cst.none)) return;

      var L = this.L, G = this.grids, g0 = G.r || G.m;
      gl.useProgram(this.p);
      gl.uniformMatrix4fv(L.u_matrix, false, mtx);
      var bind = function (unit, name, e) {
        gl.activeTexture(gl.TEXTURE0 + unit);
        gl.bindTexture(gl.TEXTURE_2D, e && e.t ? e.t : wx.zero);
        gl.uniform1i(L[name], unit);
      };
      var rainOn = st && !st.none, ga = rainOn ? G[st.fa.g] || g0 : g0, gb = rainOn ? G[st.fb.g] || g0 : g0;
      bind(0, 'u_a', rainOn ? st.A : null); bind(1, 'u_b', rainOn ? st.B : null); bind(2, 'u_fl', rainOn ? st.F : null);
      gl.uniform4fv(L.u_ga, ga.v); gl.uniform4fv(L.u_gb, gb.v);
      gl.uniform1f(L.u_w, rainOn ? st.w : 0);
      gl.uniform1f(L.u_hasfl, rainOn && st.F ? 1 : 0);
      var fs = rainOn && st.F ? (st.fa.fs || 1) : 1;
      gl.uniform2f(L.u_fscale, fs / ga.w, fs / ga.h);
      gl.uniform1f(L.u_oa, rainOn ? (st.fa.k === 'o' ? 1 : 0.88) : 0);
      gl.uniform1f(L.u_ob, rainOn ? (st.fb.k === 'o' ? 1 : 0.88) : 0);
      if (!this.th) {
        this.th = CLASSES.map(function (c) { return (encRain(c[0]) - 0.5) / 255; });
        this.col = [].concat.apply([], CLASSES.map(function (c) { return c[1].map(function (x) { return x / 255; }); }));
      }
      gl.uniform1fv(L.u_th, this.th);
      gl.uniform3fv(L.u_col, this.col);
      var cOn = cst && !cst.none, gc = G.m || g0;
      bind(3, 'u_ca', cOn ? cst.A : null); bind(4, 'u_cb', cOn ? cst.B : null);
      gl.uniform4fv(L.u_gc, gc.v);
      gl.uniform1f(L.u_wc, cOn ? cst.w : 0);
      gl.uniform1f(L.u_hasc, cOn ? 1 : 0);
      gl.uniform3fv(L.u_ccol, isDark ? [0.56, 0.6, 0.65] : [0.97, 0.98, 0.99]);
      gl.uniform1f(L.u_calpha, (isDark ? 0.36 : 0.62) * (cfg.cloudOpacity || 1));
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.enableVertexAttribArray(this.aPos);
      gl.vertexAttribPointer(this.aPos, 2, gl.FLOAT, false, 8, 0);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  };

  // ----------------------------------------------------------- temperatures
  // Station values (reports for the past, MOSMIX forecast after), hourly, interpolated to the timeline position.
  var TP = { data: null, t0: 0, els: [], on: true };
  try { TP.on = localStorage.getItem('dwdRadarTemps') !== '0'; } catch (e) { /* storage unavailable */ }
  function tempsWanted() { return cfg.temps === 'on' || (cfg.temps === 'toggle' && TP.on); }
  var TSTOPS = [[-15, [176, 124, 255]], [-5, [93, 139, 255]], [0, [63, 182, 242]], [5, [57, 181, 74]], [10, [140, 198, 63]],
    [15, [232, 194, 26]], [20, [247, 148, 29]], [25, [239, 65, 54]], [30, [193, 39, 45]]];
  function tColor(c) {
    if (c <= TSTOPS[0][0]) return 'rgb(' + TSTOPS[0][1] + ')';
    for (var i = 1; i < TSTOPS.length; i++) {
      if (c <= TSTOPS[i][0]) {
        var a = TSTOPS[i - 1], b = TSTOPS[i], w = (c - a[0]) / (b[0] - a[0]);
        return 'rgb(' + a[1].map(function (x, k) { return Math.round(x + (b[1][k] - x) * w); }).join(',') + ')';
      }
    }
    return 'rgb(' + TSTOPS[TSTOPS.length - 1][1] + ')';
  }
  function loadTemps() {
    if (!cfg.temps || cfg.temps === 'off') return;
    getJSON('temps.json').then(function (d) {
      if (TP.data && TP.data.generated === d.generated) return;
      TP.data = d; TP.t0 = ms(d.t0);
      TP.els.forEach(function (e) { e.mk.remove(); });
      TP.els = (d.st || []).slice().sort(function (a, b) { return a.p - b.p; }).map(function (st) {
        var el = document.createElement('div');
        el.className = 'temp';
        el.style.display = 'none';
        var mk = new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -6] }).setLngLat([st.lo, st.la]).addTo(map);
        return { st: st, el: el, mk: mk, fit: false, txt: '', vis: false };
      });
      declutterTemps();
    }).catch(function () { /* not yet available */ });
  }
  // greedy placement by priority; pins and already placed values keep their space
  function declutterTemps() {
    if (!map || !TP.els.length) return;
    var boxes = cfg.pins.map(function (p) { var q = map.project([p.lon, p.lat]); return [q.x - 16, q.y - 36, q.x + 16, q.y + 2]; });
    var W = $('map').clientWidth, H = $('map').clientHeight;
    TP.els.forEach(function (e) {
      var q = map.project([e.st.lo, e.st.la]);
      var b = [q.x - 17, q.y - 27, q.x + 17, q.y - 5];          // pill sits above the station point
      e.fit = q.x > 8 && q.y > 8 && q.x < W - 8 && q.y < H - 8 && boxes.every(function (o) {
        return b[2] < o[0] - 4 || b[0] > o[2] + 4 || b[3] < o[1] - 4 || b[1] > o[3] + 4;
      });
      if (e.fit) boxes.push(b);
    });
    updateTemps(frames.length ? timeOfPos(pos) : Date.now());
  }
  function updateTemps(t) {
    if (!TP.data) return;
    var on = tempsWanted(), f = (t - TP.t0) / 3600000, i = Math.floor(f), w = f - i;
    TP.els.forEach(function (e) {
      var v = null, a = e.st.v[i], b = e.st.v[i + 1];
      if (a != null && b != null) v = a + (b - a) * w; else if (a != null && w < 0.5) v = a; else if (b != null && w >= 0.5) v = b;
      var vis = on && e.fit && v != null;
      if (vis) {
        var txt = Math.round(v / 10) + '°';
        if (txt !== e.txt) { e.el.textContent = txt; e.el.style.color = tColor(v / 10); e.txt = txt; }
      }
      if (vis !== e.vis) { e.el.style.display = vis ? '' : 'none'; e.vis = vis; }
    });
  }

  // ------------------------------------------------------------- rendering
  function render(p) {
    if (!frames.length) return;
    pos = Math.max(0, Math.min(frames.length - 1, p));
    var i = Math.floor(pos), w = pos - i;
    if (i >= frames.length - 1) { i = frames.length - 1; w = 0; }
    var a = frames[i], b = frames[i + 1] || null;
    // clouds: own keyframes, crossfaded by time
    var t = timeOfPos(pos), ca = null, cb = null, cw = 0;
    if (ckT.length) {
      var lo = 0, hi = ckT.length - 1;
      if (t <= ckT[0]) ca = ckU[0];
      else if (t >= ckT[hi]) ca = ckU[hi];
      else {
        while (hi - lo > 1) { var m = (lo + hi) >> 1; if (ckT[m] <= t) lo = m; else hi = m; }
        ca = ckU[lo]; cb = ckU[hi]; cw = (t - ckT[lo]) / (ckT[hi] - ckT[lo]);
      }
    }
    want = { a: a, b: b, w: w, ca: ca, cb: cb, wc: cw };
    // the frames needed right now go first in the load queue
    [a.u, a.fl, b && b.u, ca, cb].forEach(function (u) { if (u) texEntry(u, true); });
    if (map) map.triggerRepaint();
    var n = Math.round(pos);
    if (n !== cur) { cur = n; updateStamp(); }
    updateTemps(t);
  }

  // time <-> continuous position
  function posOfTime(t) {
    if (!times.length) return 0;
    if (t <= times[0]) return 0;
    var n = times.length - 1;
    if (t >= times[n]) return n;
    var lo = 0, hi = n;
    while (hi - lo > 1) { var m = (lo + hi) >> 1; if (times[m] <= t) lo = m; else hi = m; }
    return lo + (t - times[lo]) / (times[hi] - times[lo]);
  }
  function timeOfPos(p) {
    var i = Math.floor(p), w = p - i;
    if (i >= times.length - 1) return times[times.length - 1];
    return times[i] + (times[i + 1] - times[i]) * w;
  }

  function sourceLabel(f) { return (manifest.sources && manifest.sources[f.s]) || 'Modell'; }
  function updateStamp() {
    var el = $('stamp'), f = frames[cur];
    if (!f) { $('stamp-time').textContent = 'keine Daten'; return; }
    var stale = manifest && manifest.stale;
    var t = ms(f.t);
    $('stamp-time').textContent = (f.k === 'm' || !sameDay(t, Date.now())) ? dayhm(t) : hm(t);
    var tag = '';
    if (stale && manifest.now) tag = 'Stand ' + hm(ms(manifest.now)) + ' · keine neuen Radardaten';
    else if (f.k === 'f') tag = 'Radar-Vorhersage';
    else if (f.k === 'm') tag = 'Modell ' + sourceLabel(f);
    $('stamp-tag').textContent = tag;
    $('stamp-tag').className = tag ? 'tag' : '';
    el.className = 'stamp' + (f.k !== 'o' ? ' fc' : '') + (stale ? ' stale' : '');
  }

  // --------------------------------------------- timeline (fixed centre)
  var GAP = 14, PAD = 6;
  var GROUPS = {
    radar: { k: 2.0, label: 'Radar · 1 km · 5 min', cls: 'g-radar' },
    'd2-1': { k: 0.3, label: 'ICON-D2 · 2 km · 1 h', cls: 'g-d2' },
    'eu-1': { k: 0.22, label: 'ICON-EU · 7 km · 1 h', cls: 'g-eu' },
    'eu-3': { k: 0.12, label: 'ICON-EU · 7 km · 3 h', cls: 'g-eu3' },
    'gl-1': { k: 0.22, label: 'ICON · 13 km · 1 h', cls: 'g-gl' },
    'gl-3': { k: 0.12, label: 'ICON · 13 km · 3 h', cls: 'g-gl3' }
  };
  var blocks = [], stripX = 0, labelEls = [], labelW = [], tlW = 0;
  function groupKey(f) { return f.k === 'm' ? f.s + '-' + (f.d >= 3 ? 3 : 1) : 'radar'; }

  function buildBlocks() {
    blocks = [];
    var x = PAD;
    frames.forEach(function (f, i) {
      var key = groupKey(f), t = times[i], b = blocks[blocks.length - 1];
      if (!b || b.key !== key) {
        if (b) x = b.x0 + (b.t1 - b.t0) * b.k + PAD * 2 + GAP;
        var g = GROUPS[key] || { k: 0.15, label: key, cls: '' };
        b = { key: key, k: g.k / 60000, label: g.label, cls: g.cls, t0: t, t1: t, x0: x };
        blocks.push(b);
      }
      b.t1 = t;
    });
  }
  function xOf(t) {
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (t <= b.t1 || i === blocks.length - 1) return b.x0 + (Math.max(t, b.t0) - b.t0) * b.k;
      var nb = blocks[i + 1];
      if (t < nb.t0) {
        var xe = b.x0 + (b.t1 - b.t0) * b.k;
        return xe + (t - b.t1) / (nb.t0 - b.t1) * (nb.x0 - xe);
      }
    }
    return 0;
  }
  function tOf(x) {                      // continuous: the gap maps linearly onto the time between blocks
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i], xe = b.x0 + (b.t1 - b.t0) * b.k;
      if (x <= xe || i === blocks.length - 1) return b.t0 + Math.max(0, x - b.x0) / b.k;
      var nb = blocks[i + 1];
      if (x < nb.x0) return b.t1 + (x - xe) / (nb.x0 - xe) * (nb.t0 - b.t1);
    }
    return 0;
  }
  function stripEnd() { var b = blocks[blocks.length - 1]; return b ? b.x0 + (b.t1 - b.t0) * b.k : 0; }
  function measure() {
    tlW = $('tl').clientWidth || tlW || 300;
    labelEls = Array.prototype.slice.call($('strip').querySelectorAll('.blabel'));
    labelW = labelEls.map(function (e) { return e.offsetWidth || 120; });
  }
  function moveStrip(x) {
    stripX = x;
    $('strip').style.transform = 'translate3d(' + stripX.toFixed(1) + 'px,0,0)';
    for (var i = 0; i < labelEls.length && i < blocks.length; i++) {
      var b = blocks[i], bl = b.x0 - PAD, bw = (b.t1 - b.t0) * b.k + 2 * PAD;
      var off = Math.max(0, Math.min(bw - labelW[i] - 12, -stripX - bl));
      labelEls[i].style.transform = 'translate3d(' + off.toFixed(1) + 'px,0,0)';
    }
  }
  function stripXForPos(p) { return tlW / 2 - xOf(timeOfPos(p)); }
  function posForStripX(x) { return posOfTime(tOf(tlW / 2 - x)); }

  var lastBubble = '', lastAway = null;
  function updateBubble() {
    var f = frames[cur];
    var txt = f ? ((f.k === 'm' || !sameDay(ms(f.t), Date.now())) ? dayhm(ms(f.t)) : hm(ms(f.t))) : '';
    if (txt !== lastBubble) { $('bubble-t').textContent = txt; lastBubble = txt; }
    var away = frames.length ? Math.abs(timeOfPos(pos) - times[loopStart()]) > 150000 : false;
    if (away !== lastAway) {
      $('bubble').className = 'bubble' + (away ? ' away' : '');
      $('tl').className = 'tl' + (away ? ' away' : '');
      lastAway = away;
    }
  }

  function buildStrip() {
    var strip = $('strip');
    if (!frames.length) { strip.innerHTML = ''; return; }
    buildBlocks();
    var tNow = times[loopStart()], tFc = times[loopEnd()];
    var html = [], px = function (v) { return v.toFixed(1) + 'px'; };
    var seg = function (a, b, cls) {
      html.push('<div class="seg ' + cls + '" style="left:' + px(xOf(a)) + ';width:' + px(Math.max(0, xOf(b) - xOf(a))) + '"></div>');
    };
    var tick = function (t, cls, label) {
      html.push('<div class="tick ' + cls + '" style="left:' + px(xOf(t)) + '"' + (label ? ' data-t="' + label + '"' : '') + '></div>');
    };
    blocks.forEach(function (b) {
      var xe = b.x0 + (b.t1 - b.t0) * b.k;
      html.push('<div class="block ' + b.cls + '" style="left:' + px(b.x0 - PAD) + ';width:' + px(xe - b.x0 + 2 * PAD) +
        '"><span class="blabel">' + b.label + '</span></div>');
      if (b.key === 'radar') {
        seg(b.t0, Math.min(tNow, b.t1), 'past');
        if (tFc > tNow) seg(tNow, Math.min(tFc, b.t1), 'fc');
      } else {
        seg(b.t0, b.t1, 'model ' + b.cls);
      }
      var pxh = b.k * 3600000, minor, lab;
      if (pxh >= 100) { minor = 15; lab = 60; }
      else if (pxh >= 15) { minor = 60; lab = 360; }
      else { minor = 180; lab = 720; }
      var step = minor * 60000, mid = new Date(b.t0); mid.setHours(0, 0, 0, 0);
      var t = mid.getTime() + Math.ceil((b.t0 - mid.getTime()) / step) * step;
      for (; t <= b.t1; t += step) {
        var d = new Date(t), mins = d.getHours() * 60 + d.getMinutes();
        if (b.key !== 'radar' && mins === 0) tick(t, 'lbl day', DAYS[d.getDay()] + '. ' + d.getDate() + '.');
        else if (mins % lab === 0) tick(t, 'lbl', hm(t));
        else tick(t, 'minor');
      }
    });
    html.push('<div class="nowmark" style="left:' + px(xOf(tNow)) + '"><span>Aktuell</span></div>');
    strip.innerHTML = html.join('');
    strip.style.width = Math.ceil(stripEnd() + PAD + 1) + 'px';
    measure();
  }

  function nearestFrame(t) { return Math.round(posOfTime(t)); }

  // ------------------------------------------------ motion (one rAF loop)
  // modes: drag (finger), fling (momentum), tween (eased glide), play (stufenlos), idle
  var mode = 'idle', anim = null, rafId = 0, drag = null;
  function kick() { if (!rafId) rafId = requestAnimationFrame(loop); }
  function barVisible() { return !$('bar').classList.contains('hidden'); }
  function loop(ts) {
    rafId = 0;
    var lo = tlW / 2 - stripEnd(), hi = tlW / 2 - PAD;
    if (mode === 'tween') {
      var p = Math.min(1, (ts - anim.t0) / anim.dur), e = 1 - Math.pow(1 - p, 3);
      var np = anim.from + (anim.to - anim.from) * e;
      render(np); moveStrip(stripXForPos(np));
      if (p >= 1) { mode = 'idle'; if (anim.done) anim.done(); }
    } else if (mode === 'fling') {
      var dt = Math.min(50, ts - anim.last); anim.last = ts;
      var nx = stripX + anim.v * dt;
      if (nx > hi || nx < lo) { nx = Math.max(lo, Math.min(hi, nx)); anim.v = 0; }
      moveStrip(nx); render(posForStripX(nx));
      anim.v *= Math.pow(0.94, dt / 16);
      if (Math.abs(anim.v) < 0.03) snap();
    } else if (mode === 'drag') {
      render(posForStripX(stripX));
    } else if (mode === 'play') {
      var dtp = Math.min(100, ts - anim.last); anim.last = ts;
      if (anim.hold > 0) {
        anim.hold -= dtp;
      } else {
        var np2 = posOfTime(timeOfPos(pos) + dtp * 300000 / cfg.frameMs);
        if (np2 >= loopEnd()) { np2 = loopEnd(); anim.hold = cfg.holdEndMs; anim.wrap = true; }
        render(np2);
      }
      if (anim.hold <= 0 && anim.wrap) { anim.wrap = false; render(loopStart()); anim.hold = cfg.holdNowMs; }
      if (barVisible()) moveStrip(stripXForPos(pos));
    }
    updateBubble();
    if (mode !== 'idle' && !document.hidden) rafId = requestAnimationFrame(loop);
  }
  function glideTo(p, dur, done) {
    mode = 'tween';
    anim = { from: pos, to: p, t0: performance.now(), dur: dur || 260, done: done };
    kick();
  }
  function nearestReal(p) {
    var t = timeOfPos(p), best = Math.round(p), bd = Infinity;
    for (var i = 0; i < realIdx.length; i++) {
      var d = Math.abs(times[realIdx[i]] - t);
      if (d < bd) { bd = d; best = realIdx[i]; } else if (times[realIdx[i]] > t) break;
    }
    return best;
  }
  function snap() { glideTo(nearestReal(posForStripX(stripX)), 200); }
  function jump(p) {                     // immediate (no motion), e.g. back to "Aktuell" while hidden
    mode = 'idle';
    render(p);
    if (tlW) moveStrip(stripXForPos(pos));
    updateBubble();
  }

  function setPlaying(p) {
    playing = p;
    $('ico-pause').style.display = p ? '' : 'none';
    $('ico-play').style.display = p ? 'none' : '';
    if (p) {
      if (pos < loopStart() || pos >= loopEnd()) render(loopStart());
      mode = 'play';
      anim = { last: performance.now(), hold: pos === loopStart() ? cfg.holdNowMs : 0, wrap: false };
      kick();
    } else if (mode === 'play') {
      mode = 'idle';
    }
  }

  function showControls() {
    userActive = true;
    $('bar').classList.remove('hidden');
    $('zoom').classList.remove('hidden');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideControls, cfg.controlsHideS * 1000);
    if (mode === 'idle' || mode === 'play') { measure(); moveStrip(stripXForPos(pos)); }
  }
  function hideControls() {
    if (mode === 'drag' || mode === 'fling' || mode === 'tween') { hideTimer = setTimeout(hideControls, 1000); return; }
    $('bar').classList.add('hidden');
    $('zoom').classList.add('hidden');
    userActive = false;
    if (!playing) jump(loopStart());       // kiosk: back to "Aktuell"
  }

  function initBar() {
    $('play').addEventListener('click', function (e) { e.stopPropagation(); setPlaying(!playing); showControls(); });
    $('bubble').addEventListener('click', function (e) {
      e.stopPropagation(); setPlaying(false); glideTo(loopStart(), 450); showControls();
    });
    var tl = $('tl');
    tl.addEventListener('pointerdown', function (ev) {
      if (ev.target.closest && ev.target.closest('#bubble')) return;
      setPlaying(false);
      mode = 'drag';
      drag = { x0: ev.clientX, s0: stripX, moved: false, hist: [[performance.now(), ev.clientX]] };
      tl.setPointerCapture(ev.pointerId);
      showControls();
      kick();
    });
    tl.addEventListener('pointermove', function (ev) {
      if (!drag) return;
      var dx = ev.clientX - drag.x0;
      if (Math.abs(dx) > 4) drag.moved = true;
      var lo = tlW / 2 - stripEnd(), hi = tlW / 2 - PAD, x = drag.s0 + dx;
      if (x > hi) x = hi + (x - hi) * 0.3;
      if (x < lo) x = lo + (x - lo) * 0.3;
      moveStrip(x);
      var now = performance.now();
      drag.hist.push([now, ev.clientX]);
      while (drag.hist.length > 2 && now - drag.hist[0][0] > 100) drag.hist.shift();
      kick();
    });
    var end = function (ev) {
      if (!drag) return;
      clearTimeout(hideTimer);
      hideTimer = setTimeout(hideControls, cfg.controlsHideS * 1000);
      if (!drag.moved) {
        var r = tl.getBoundingClientRect();
        glideTo(nearestFrame(tOf(ev.clientX - r.left - stripX)), 300);
      } else {
        var h = drag.hist, a = h[0], b = h[h.length - 1];
        var v = (b[0] - a[0]) > 0 ? (b[1] - a[1]) / (b[0] - a[0]) : 0;
        var lo = tlW / 2 - stripEnd(), hi = tlW / 2 - PAD;
        if (stripX > hi || stripX < lo || Math.abs(v) < 0.15) snap();
        else { mode = 'fling'; anim = { v: Math.max(-4, Math.min(4, v)), last: performance.now() }; kick(); }
      }
      drag = null;
    };
    tl.addEventListener('pointerup', end);
    tl.addEventListener('pointercancel', end);
    $('bar').addEventListener('click', function (e) { e.stopPropagation(); showControls(); });
    $('zoom').addEventListener('click', function (e) { e.stopPropagation(); });
    window.addEventListener('resize', function () { measure(); moveStrip(stripXForPos(pos)); });
  }

  function loopStart() { return nowIdx >= 0 ? nowIdx : 0; }
  function loopEnd() { return fcEnd >= 0 ? fcEnd : loopStart(); }

  // -------------------------------------------------------------- manifest
  function applyManifest(m) {
    manifest = m;
    if (wx.grids === undefined || JSON.stringify(m.grids) !== wx._gk) { wx._gk = JSON.stringify(m.grids); wx.quad = null; }
    var oldT = frames.length ? timeOfPos(pos) : null;
    var wasAtNow = !frames.length || Math.abs(pos - nowIdx) < 0.01;
    var limit = Date.now() + cfg.forecastDays * 86400000;
    frames = (m.frames || []).filter(function (f) {
      return f.k !== 'm' || (cfg.forecastDays > 0 && ms(f.t) <= limit);
    });
    // a flow link is only valid towards the directly following frame
    frames.forEach(function (f, i) { if (f.fl && (!frames[i + 1] || frames[i + 1].g !== f.g)) delete f.fl; });
    times = frames.map(function (f) { return ms(f.t); });
    realIdx = frames.map(function (f, i) { return i; });
    var tEnd = times.length ? times[times.length - 1] : 0;
    var clouds = (m.clouds || []).filter(function (c, i, arr) {
      return ms(c.t) <= tEnd + 3 * 3600000 || i === 0 || ms(arr[i - 1].t) <= tEnd;
    });
    ckT = clouds.map(function (c) { return ms(c.t); });
    ckU = clouds.map(function (c) { return c.u; });
    nowIdx = -1; fcEnd = -1;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].k === 'o' && frames[i].t === m.now) nowIdx = i;
      if (frames[i].k === 'f') fcEnd = i;
    }
    if (nowIdx < 0) for (var j = frames.length - 1; j >= 0; j--) if (frames[j].k === 'o') { nowIdx = j; break; }
    if (fcEnd < 0) fcEnd = nowIdx;
    // load order: "Aktuell" and the radar loop first, then clouds, then the past, then the models
    var urls = [], seen = {};
    var addU = function (u) { if (u && !seen[u]) { seen[u] = true; urls.push(u); } };
    var radar = frames.map(function (f, i) { return i; }).filter(function (i) { return frames[i].k !== 'm'; });
    radar.sort(function (x, y) { return (x >= nowIdx ? 0 : 1) - (y >= nowIdx ? 0 : 1) || (x >= nowIdx ? x - y : y - x); });
    radar.forEach(function (i) { addU(frames[i].u); addU(frames[i].fl); });
    ckU.forEach(addU);
    frames.forEach(function (f) { if (f.k === 'm') { addU(f.u); addU(f.fl); } });
    syncTextures(urls);
    buildStrip();
    cur = -1;
    if (playing) return;
    if (wasAtNow || !userActive || oldT === null) jump(loopStart());
    else jump(posOfTime(oldT));
  }

  function poll() {
    getJSON('manifest2.json').then(function (m) {
      var age = (Date.now() - ms(m.generated)) / 60000;
      if (age > 10) m.stale = true;
      if (m.generated !== lastGenerated) { lastGenerated = m.generated; applyManifest(m); }
      else if (manifest && manifest.stale !== m.stale) { manifest.stale = m.stale; updateStamp(); }
    }).catch(function () {
      if (manifest) { manifest.stale = true; updateStamp(); }
    });
  }

  document.addEventListener('visibilitychange', function () { if (!document.hidden && mode !== 'idle') kick(); });

  getJSON('config.json').then(function (c) {
    cfg = c;
    if (cfg.forecastDays === undefined) cfg.forecastDays = 7;
    applyUrlParams();
    isDark = wantDark();
    document.documentElement.classList.toggle('dark', isDark);
    buildDom();
    initMap();
    initBar();
    watchTheme();
    setPlaying(false);
    poll();
    setInterval(poll, 60000);
    if (/[?&]debug=1/.test(location.search)) window.__radar = { jump: jump, frames: function () { return frames; }, tex: TEX };
  }).catch(function (e) {
    $('stamp-time').textContent = 'Konfiguration fehlt (' + e.message + ')';
  });
})();
