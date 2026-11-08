/* DWD Regenradar – Anzeige. Lädt nur fertige PNGs, keine Rechenarbeit im Browser außer Überblendung.
 * - Kontinuierliche Zeitposition: zwischen zwei Bildern wird passend zur Position überblendet.
 * - Standard: pausiert auf "Aktuell". Play läuft stufenlos Aktuell .. +2 h in Schleife.
 * - Zeitleiste mit festem Mittelpunkt, Blöcke je Quelle/Auflösung, Schwung + weiches Einrasten.
 * - URL-Parameter (pro Dashboard-Karte): days, return, zoom, lat, lon
 */
(function () {
  'use strict';

  var cfg, map, manifest = null, frames = [], times = [], nowIdx = -1, fcEnd = -1;
  var pos = 0, cur = -1;                 // continuous frame position, nearest frame index
  var playing = false, hideTimer = null, resetTimer = null, lastGenerated = null;
  var preload = [], radarBl = null, cloudBl = null, userActive = false;
  var BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
  var DAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];

  var $ = function (id) { return document.getElementById(id); };
  var IN_POPUP = new URLSearchParams(location.search).get('popup') === '1';
  var ms = function (iso) { return new Date(iso).getTime(); };
  var hm = function (t) { return new Date(t).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }); };
  var dayhm = function (t) { return DAYS[new Date(t).getDay()] + ' ' + hm(t); };
  var sameDay = function (a, b) { return new Date(a).toDateString() === new Date(b).toDateString(); };
  function getJSON(url) {
    return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // ------------------------------------------------- per-card URL overrides
  function applyUrlParams() {
    var q = new URLSearchParams(location.search);
    var num = function (k) { var v = parseFloat(q.get(k)); return isNaN(v) ? null : v; };
    if (num('days') !== null) cfg.forecastDays = Math.max(0, Math.min(7, num('days')));
    if (num('return') !== null) cfg.resetViewS = Math.max(0, Math.min(120, num('return')));
    if (num('zoom') !== null) cfg.zoom = num('zoom');
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
      '<div class="zgroup"><button id="zin" aria-label="Hineinzoomen">+</button>' +
      '<button id="zout" aria-label="Herauszoomen">&minus;</button></div>';
    document.body.appendChild(z);
  }

  // ------------------------------------------------------------------- map
  function pinIcon(p) {
    var home = p.home;
    var w = home ? 24 : 18, h = home ? 32 : 24;
    var fill = home ? '#173478' : '#4a5a66';
    var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 40">' +
      '<path d="M15 1C7.3 1 1 7.2 1 14.9 1 25.3 15 39 15 39s14-13.7 14-24.1C29 7.2 22.7 1 15 1z" fill="' + fill +
      '" stroke="#fff" stroke-width="2.2"/><circle cx="15" cy="15" r="' + (home ? 5.5 : 4.5) + '" fill="#fff"/></svg>';
    var lbl = (!home && p.name) ? '<span class="lbl">' + p.name.replace(/[<>&]/g, '') + '</span>' : '';
    var halo = home ? '<span class="home-halo"></span>' : '';
    return L.divIcon({ className: 'pin', html: halo + svg + lbl, iconSize: [w, h], iconAnchor: [w / 2, h] });
  }

  function pane(name, z) {
    map.createPane(name).classList.add('hill-pane');
    map.getPane(name).style.zIndex = z;
  }

  function initMap() {
    var b = L.latLngBounds(cfg.bounds);
    map = L.map('map', {
      zoomControl: false, attributionControl: true, center: cfg.center, zoom: cfg.zoom,
      minZoom: cfg.minZoom, maxZoom: cfg.maxZoom, maxBounds: b.pad(0.6), maxBoundsViscosity: 0.8,
      fadeAnimation: false, inertia: true, tap: false
    });
    map.attributionControl.setPrefix(false);
    pane('base', 240); pane('hill', 250); pane('clouds', 390); pane('radar', 400);
    map.createPane('labels').classList.add('labels-pane');
    map.getPane('labels').style.zIndex = 450;

    var bm = cfg.basemap;
    var tileLayers = [];
    var track = function (layer) {
      tileLayers.push(layer);
      // a tile that failed to load is retried twice (e.g. busy server right after start)
      layer.on('tileerror', function (e) {
        var t = e.tile, n = (t._retries || 0) + 1;
        if (n > 2) return;
        t._retries = n;
        setTimeout(function () {
          var src = t.src.replace(/[?&]r=\d+/, '');
          t.src = src + (src.indexOf('?') < 0 ? '?' : '&') + 'r=' + Date.now();
        }, 1500 * n);
      });
      return layer;
    };
    if (bm.url) {
      var base = L.tileLayer(bm.url, {
        subdomains: bm.subdomains || 'abc', maxZoom: 19, maxNativeZoom: bm.maxNativeZoom, keepBuffer: 1,
        attribution: bm.attribution
      }).addTo(map);
      track(base);
      if (bm.filter) base.getContainer().style.filter = bm.filter;
    } else {
      map.attributionControl.addAttribution(bm.attribution);
    }
    if (bm.base) {
      track(L.tileLayer(bm.base, { pane: 'base', maxNativeZoom: bm.maxNativeZoom, minNativeZoom: 5, keepBuffer: 1 })
        .addTo(map));
    }
    if (cfg.hillshade) {
      track(L.tileLayer('hillshade/{z}/{x}/{y}.png?v=' + cfg.version, {
        pane: 'hill', maxNativeZoom: cfg.maxZoom, minNativeZoom: 5, keepBuffer: 1,
        attribution: 'Relief: AWS Terrain Tiles'
      }).addTo(map));
    }
    if (bm.labels) {
      track(L.tileLayer(bm.labels, { subdomains: bm.subdomains || 'abc', maxZoom: 19, pane: 'labels', keepBuffer: 1,
        maxNativeZoom: bm.own ? bm.maxNativeZoom : 19, minNativeZoom: bm.own ? 5 : 0 })
        .addTo(map));
    }
    map.attributionControl.addAttribution('Radar/Modelle: © Deutscher Wetterdienst');

    radarBl = new Blender('radar', b);
    cloudBl = new Blender('clouds', b);

    cfg.pins.forEach(function (p) {
      L.marker([p.lat, p.lon], { icon: pinIcon(p), interactive: false, keyboard: false,
        zIndexOffset: p.home ? 1000 : 0 }).addTo(map);
    });

    // The iframe may get its final size only after Home Assistant has laid out the dashboard.
    // Re-measure on every container size change (and every 15 s as a fallback) and load missing tiles.
    var el = $('map'), resizeT = null;
    var refit = function () {
      var s0 = map.getSize();
      if (s0.x === el.clientWidth && s0.y === el.clientHeight) return;
      map.invalidateSize({ pan: false });
      map.fire('moveend');
      measure();
      moveStrip(stripXForPos(pos));
    };
    var refitSoon = function () { clearTimeout(resizeT); resizeT = setTimeout(refit, 150); };
    if (window.ResizeObserver) new ResizeObserver(refitSoon).observe(el);
    window.addEventListener('resize', refitSoon);
    setInterval(refit, 15000);
    setTimeout(refit, 1000); setTimeout(refit, 4000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) refitSoon(); });

    map.on('click', function () { showControls(); });
    map.on('movestart zoomstart moveend zoomend', function () { armReset(); });
    $('zin').addEventListener('click', function (e) { e.stopPropagation(); map.zoomIn(); showControls(); });
    $('zout').addEventListener('click', function (e) { e.stopPropagation(); map.zoomOut(); showControls(); });
    $('zfs').addEventListener('click', function (e) { e.stopPropagation(); toggleFullscreen(); });
  }

  function atHome() {
    var c = map.getCenter();
    return map.getZoom() === cfg.zoom && Math.abs(c.lat - cfg.center[0]) < 1e-3 && Math.abs(c.lng - cfg.center[1]) < 1e-3;
  }
  function armReset() {
    clearTimeout(resetTimer);
    if (!cfg.resetViewS || atHome()) return;
    resetTimer = setTimeout(function () { map.setView(cfg.center, cfg.zoom, { animate: false }); },
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
  document.addEventListener('fullscreenchange', function () { setTimeout(function () { if (map) map.invalidateSize({ pan: false }); }, 200); });

  // ------------------------------------------------- cross-fading image layer
  // A small pool of image overlays. An image becomes visible only once it is loaded AND decoded;
  // until then the previously visible images stay (no flicker).
  function Blender(paneName, bounds) {
    this.pool = [];
    for (var k = 0; k < 4; k++) {
      var ov = L.imageOverlay(BLANK, bounds, { opacity: 0, interactive: false, pane: paneName }).addTo(map);
      ov._u = null; ov._ready = false; ov._used = 0; ov._op = 0;
      this.pool.push(ov);
    }
    this.want = null;
  }
  Blender.prototype._get = function (url, bounds, keep) {
    var i, ov;
    for (i = 0; i < this.pool.length; i++) if (this.pool[i]._u === url) { this.pool[i]._used = performance.now(); return this.pool[i]; }
    var best = null;
    for (i = 0; i < this.pool.length; i++) {
      ov = this.pool[i];
      if (keep.indexOf(ov) >= 0 || ov._op > 0) continue;
      if (!best || ov._used < best._used) best = ov;
    }
    if (!best) return null;
    ov = best;
    ov._u = url; ov._ready = false; ov._used = performance.now();
    ov.setBounds(bounds);
    var self = this, img = ov.getElement();
    img.onload = function () {
      var fin = function () { if (ov._u === url) { ov._ready = true; self.apply(); } };
      if (img.decode) img.decode().then(fin, fin); else fin();
    };
    ov.setUrl(url);
    return ov;
  };
  // ua/ub: urls of the two neighbouring frames, w: blend weight of b (0..1)
  Blender.prototype.show = function (ua, ba, oa, ub, bb, ob, w) {
    this.want = { ua: ua, ba: ba, oa: oa, ub: ub, bb: bb, ob: ob, w: w };
    this.apply();
  };
  Blender.prototype.apply = function () {
    var q = this.want;
    if (!q) return;
    var keep = [];
    var A = q.ua ? this._get(q.ua, q.ba, keep) : null;
    if (A) keep.push(A);
    var B = (q.ub && q.w > 0.002) ? this._get(q.ub, q.bb, keep) : null;
    var vis = [];
    var w = q.w;
    if (A && A._ready && B && B._ready) {
      vis = [[A, Math.cos(w * Math.PI / 2) * q.oa], [B, Math.sin(w * Math.PI / 2) * q.ob]];
    } else if (A && A._ready && (!B || w < 0.5)) {
      vis = [[A, q.oa]];
    } else if (B && B._ready) {
      vis = [[B, q.ob]];
    } else if (A && A._ready) {
      vis = [[A, q.oa]];
    } else if (!q.ua && !q.ub) {
      vis = [];
    } else {
      return;                               // nothing ready yet: keep what is visible
    }
    for (var i = 0; i < this.pool.length; i++) {
      var ov = this.pool[i], op = 0;
      for (var j = 0; j < vis.length; j++) if (vis[j][0] === ov) op = vis[j][1];
      if (Math.abs(op - ov._op) > 0.004 || (op === 0) !== (ov._op === 0)) { ov.setOpacity(op); ov._op = op; }
    }
  };

  // ------------------------------------------------------------- rendering
  function op(f) { return f.k === 'o' ? 1 : 0.85; }
  function bnd(f) { return L.latLngBounds(f.k === 'm' ? manifest.mbounds : manifest.bounds); }
  function render(p) {
    if (!frames.length) return;
    pos = Math.max(0, Math.min(frames.length - 1, p));
    var i = Math.floor(pos), w = pos - i;
    if (i >= frames.length - 1) { i = frames.length - 1; w = 0; }
    var a = frames[i], b = frames[i + 1] || null;
    radarBl.show(a.u, bnd(a), op(a), b && b.u, b && bnd(b), b && op(b), w);
    var mb = L.latLngBounds(manifest.mbounds);
    cloudBl.show(a.cc, mb, 1, b && b.cc, mb, 1, w);
    var n = Math.round(pos);
    if (n !== cur) { cur = n; updateStamp(); }
    // preload the neighbourhood (model frames are not preloaded globally)
    for (var k = Math.max(0, i - 2); k <= Math.min(frames.length - 1, i + 3); k++) warm(frames[k]);
  }
  var warmed = {};
  function warm(f) {
    if (warmed[f.u]) return;
    warmed[f.u] = true;
    var im = new Image(); im.src = f.u; preload.push(im);
    if (f.cc) { var ic = new Image(); ic.src = f.cc; preload.push(ic); }
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
    var away = Math.abs(pos - loopStart()) > 0.5;
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
        var np2 = pos + dtp / cfg.frameMs;
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
  function snap() { glideTo(Math.round(posForStripX(stripX)), 200); }
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
    var oldT = frames.length ? timeOfPos(pos) : null;
    var wasAtNow = !frames.length || Math.abs(pos - nowIdx) < 0.01;
    var limit = Date.now() + cfg.forecastDays * 86400000;
    frames = (m.frames || []).filter(function (f) {
      return f.k !== 'm' || (cfg.forecastDays > 0 && ms(f.t) <= limit);
    });
    times = frames.map(function (f) { return ms(f.t); });
    // every frame gets a cloud image: own one, else the nearest frame's (no blank background)
    var lastC = null;
    frames.forEach(function (f) { f.cc = f.c || lastC; if (f.c) lastC = f.c; });
    lastC = null;
    for (var r = frames.length - 1; r >= 0; r--) { if (frames[r].c) lastC = frames[r].c; if (!frames[r].cc) frames[r].cc = lastC; }
    nowIdx = -1; fcEnd = -1;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].k === 'o' && frames[i].t === m.now) nowIdx = i;
      if (frames[i].k === 'f') fcEnd = i;
    }
    if (nowIdx < 0) for (var j = frames.length - 1; j >= 0; j--) if (frames[j].k === 'o') { nowIdx = j; break; }
    if (fcEnd < 0) fcEnd = nowIdx;
    preload = []; warmed = {};
    frames.forEach(function (f) { if (f.k !== 'm') warm(f); });
    buildStrip();
    cur = -1;
    if (playing) return;
    if (wasAtNow || !userActive || oldT === null) jump(loopStart());
    else jump(posOfTime(oldT));
  }

  function poll() {
    getJSON('manifest.json').then(function (m) {
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
    buildDom();
    initMap();
    initBar();
    setPlaying(false);
    poll();
    setInterval(poll, 60000);
  }).catch(function (e) {
    $('stamp-time').textContent = 'Konfiguration fehlt (' + e.message + ')';
  });
})();
