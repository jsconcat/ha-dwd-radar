/* DWD Regenradar – Anzeige. Lädt nur fertige PNGs, keine Rechenarbeit im Browser.
 * - Standard: pausiert auf "Aktuell". Play spielt Aktuell .. +2 h (Radar-Vorhersage) in Schleife.
 * - Zeitleiste mit festem Mittelpunkt: der Zeitstreifen wird darunter verschoben.
 * - URL-Parameter (pro Dashboard-Karte): days, return, zoom, lat, lon
 */
(function () {
  'use strict';

  var cfg, map, manifest = null, all = [], frames = [], nowIdx = -1, fcEnd = -1, cur = -1;
  var playing = false, timer = null, hideTimer = null, resetTimer = null, lastGenerated = null;
  var preload = [], radarSw = null, cloudSw = null, userActive = false;
  var BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
  var DAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];

  var $ = function (id) { return document.getElementById(id); };
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
  // built here (not in index.html, which browsers may cache for weeks)
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
    z.innerHTML = '<button id="zin" aria-label="Hineinzoomen">+</button><button id="zout" aria-label="Herauszoomen">&minus;</button>';
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
    if (bm.url) {
      var base = L.tileLayer(bm.url, {
        subdomains: bm.subdomains || 'abc', maxZoom: 19, maxNativeZoom: bm.maxNativeZoom, keepBuffer: 1,
        attribution: bm.attribution
      }).addTo(map);
      if (bm.filter) base.getContainer().style.filter = bm.filter;
    } else {
      map.attributionControl.addAttribution(bm.attribution);
    }
    if (bm.base) {
      L.tileLayer(bm.base, { pane: 'base', maxNativeZoom: bm.maxNativeZoom, minNativeZoom: 5, keepBuffer: 1 })
        .addTo(map);
    }
    if (cfg.hillshade) {
      L.tileLayer('hillshade/{z}/{x}/{y}.png?v=' + cfg.version, {
        pane: 'hill', maxNativeZoom: cfg.maxZoom, minNativeZoom: 5, keepBuffer: 1,
        attribution: 'Relief: AWS Terrain Tiles'
      }).addTo(map);
    }
    if (bm.labels) {
      L.tileLayer(bm.labels, { subdomains: bm.subdomains || 'abc', maxZoom: 19, pane: 'labels', keepBuffer: 1,
        maxNativeZoom: bm.own ? bm.maxNativeZoom : 19, minNativeZoom: bm.own ? 5 : 0 })
        .addTo(map);
    }
    map.attributionControl.addAttribution('Radar/Modelle: © Deutscher Wetterdienst');

    var o = function (p) { return { opacity: 0, interactive: false, pane: p }; };
    radarSw = new Swapper(L.imageOverlay(BLANK, b, o('radar')).addTo(map),
                          L.imageOverlay(BLANK, b, o('radar')).addTo(map));
    cloudSw = new Swapper(L.imageOverlay(BLANK, b, o('clouds')).addTo(map),
                          L.imageOverlay(BLANK, b, o('clouds')).addTo(map));

    cfg.pins.forEach(function (p) {
      L.marker([p.lat, p.lon], { icon: pinIcon(p), interactive: false, keyboard: false,
        zIndexOffset: p.home ? 1000 : 0 }).addTo(map);
    });

    map.on('click', function () { showControls(); });
    map.on('movestart zoomstart moveend zoomend', function () { armReset(); });
    $('zin').addEventListener('click', function (e) { e.stopPropagation(); map.zoomIn(); showControls(); });
    $('zout').addEventListener('click', function (e) { e.stopPropagation(); map.zoomOut(); showControls(); });
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

  // double-buffered overlay: the next image becomes visible only once loaded
  function Swapper(a, b) { this.a = a; this.b = b; this.front = a; this.url = null; }
  Swapper.prototype.set = function (url, op, bounds, isCurrent) {
    var self = this;
    if (!url) { this.a.setOpacity(0); this.b.setOpacity(0); this.url = null; return; }
    if (url === this.url) { this.front.setOpacity(op); return; }
    this.url = url;
    var back = this.front === this.a ? this.b : this.a, done = false;
    var swap = function () {
      if (done || !isCurrent()) return;
      done = true;
      back.setOpacity(op);
      self.front.setOpacity(0);
      self.front = back;
    };
    if (bounds) back.setBounds(bounds);
    back.once('load', swap);
    back.setUrl(url);
    var img = back.getElement();
    if (img && img.complete && img.naturalWidth > 1 && img.src.indexOf(url) >= 0) swap();
  };

  // ---------------------------------------------------------------- frames
  function show(i, keepStrip) {
    if (i < 0 || i >= frames.length) return;
    cur = i;
    var f = frames[i];
    var isCur = function () { return cur === i; };
    radarSw.set(f.u, f.k === 'o' ? 1 : 0.85, L.latLngBounds(f.k === 'm' ? manifest.mbounds : manifest.bounds), isCur);
    cloudSw.set(f.c || null, 1, L.latLngBounds(manifest.mbounds), isCur);
    updateStamp();
    if (!keepStrip) placeStrip(ms(f.t));
    updateBubble(ms(f.t));
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

  // -------------------------------------------------------------- playback
  function loopStart() { return nowIdx >= 0 ? nowIdx : 0; }
  function loopEnd() { return fcEnd >= 0 ? fcEnd : loopStart(); }
  function delayFor(i) {
    if (i === loopStart()) return cfg.holdNowMs;
    if (i === loopEnd()) return cfg.holdEndMs;
    return cfg.frameMs;
  }
  function tick() {
    clearTimeout(timer);
    if (!playing || !frames.length || document.hidden) return;
    var next = (cur < loopStart() || cur >= loopEnd()) ? loopStart() : cur + 1;
    show(next);
    timer = setTimeout(tick, delayFor(next));
  }
  function setPlaying(p) {
    playing = p;
    $('ico-pause').style.display = p ? '' : 'none';
    $('ico-play').style.display = p ? 'none' : '';
    clearTimeout(timer);
    if (p) {
      if (cur < loopStart() || cur >= loopEnd()) show(loopStart());
      timer = setTimeout(tick, delayFor(cur));
    }
  }

  // --------------------------------------------- timeline (fixed centre)
  // The strip is split into blocks per data source/resolution (like WarnWetter):
  // each block has its own scale; the time between blocks is compressed into a gap.
  var GAP = 14, PAD = 6;
  var GROUPS = {
    radar: { k: 2.0, label: 'Radar · 1 km · 5 min', cls: 'g-radar' },
    'd2-1': { k: 0.6, label: 'ICON-D2 · 2 km · 1 h', cls: 'g-d2' },
    'eu-1': { k: 0.45, label: 'ICON-EU · 7 km · 1 h', cls: 'g-eu' },
    'eu-3': { k: 0.25, label: 'ICON-EU · 7 km · 3 h', cls: 'g-eu3' },
    'gl-1': { k: 0.45, label: 'ICON · 13 km · 1 h', cls: 'g-gl' },
    'gl-3': { k: 0.25, label: 'ICON · 13 km · 3 h', cls: 'g-gl3' }
  };
  var blocks = [], stripX = 0;
  function groupKey(f) { return f.k === 'm' ? f.s + '-' + (f.d >= 3 ? 3 : 1) : 'radar'; }

  function buildBlocks() {
    blocks = [];
    var x = PAD;
    frames.forEach(function (f, i) {
      var key = groupKey(f), t = ms(f.t), b = blocks[blocks.length - 1];
      if (!b || b.key !== key) {
        if (b) x = b.x0 + (b.t1 - b.t0) * b.k + PAD * 2 + GAP;
        var g = GROUPS[key] || { k: 0.3, label: key, cls: '' };
        b = { key: key, k: g.k / 60000, label: g.label, cls: g.cls, t0: t, t1: t, i0: i, i1: i, x0: x };
        blocks.push(b);
      }
      b.t1 = t; b.i1 = i;
    });
  }
  function xOf(t) {
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (t <= b.t1 || i === blocks.length - 1) return b.x0 + (Math.max(t, b.t0) - b.t0) * b.k;
      var nb = blocks[i + 1];
      if (t < nb.t0) {                 // between blocks: inside the gap
        var f = (t - b.t1) / (nb.t0 - b.t1);
        var xe = b.x0 + (b.t1 - b.t0) * b.k;
        return xe + f * (nb.x0 - xe);
      }
    }
    return 0;
  }
  function tOf(x) {
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i], xe = b.x0 + (b.t1 - b.t0) * b.k;
      if (x <= xe || i === blocks.length - 1) return b.t0 + Math.max(0, x - b.x0) / b.k;
      var nb = blocks[i + 1];
      if (x < nb.x0) return (x - xe) < (nb.x0 - xe) / 2 ? b.t1 : nb.t0;
    }
    return 0;
  }
  function stripEnd() { var b = blocks[blocks.length - 1]; return b ? b.x0 + (b.t1 - b.t0) * b.k : 0; }
  function tlWidth() { return $('tl').clientWidth || 300; }
  function moveStrip(x) {
    stripX = x;
    $('strip').style.transform = 'translate3d(' + stripX.toFixed(1) + 'px,0,0)';
    // keep each block label visible while its block is on screen ("sticky")
    var labels = $('strip').querySelectorAll('.blabel');
    for (var i = 0; i < labels.length && i < blocks.length; i++) {
      var b = blocks[i], bl = b.x0 - PAD, bw = (b.t1 - b.t0) * b.k + 2 * PAD;
      var lw = labels[i].offsetWidth || 120;
      var want = -stripX + 6 - bl;
      labels[i].style.left = Math.max(6, Math.min(bw - lw - 6, want)).toFixed(1) + 'px';
    }
  }
  function placeStrip(t) { moveStrip(tlWidth() / 2 - xOf(t)); }
  function updateBubble(t) {
    $('bubble-t').textContent = (frames[cur] && (frames[cur].k === 'm' || !sameDay(t, Date.now()))) ? dayhm(t) : hm(t);
    var away = Math.abs(xOf(t) - xOf(ms(frames[loopStart()].t))) > 45;
    $('bubble').className = 'bubble' + (cur !== loopStart() ? ' away' : '');
    $('tl').className = 'tl' + (away ? ' away' : '');
  }

  function buildStrip() {
    var strip = $('strip');
    if (!frames.length) { strip.innerHTML = ''; return; }
    buildBlocks();
    var tNow = ms(frames[loopStart()].t), tFc = ms(frames[loopEnd()].t);
    var html = [];
    var px = function (v) { return v.toFixed(1) + 'px'; };
    var seg = function (a, b, cls) {
      html.push('<div class="seg ' + cls + '" style="left:' + px(xOf(a)) + ';width:' + px(Math.max(0, xOf(b) - xOf(a))) + '"></div>');
    };
    var tick = function (t, cls, label) {
      html.push('<div class="tick ' + cls + '" style="left:' + px(xOf(t)) + '"' + (label ? ' data-t="' + label + '"' : '') + '></div>');
    };
    blocks.forEach(function (b, bi) {
      var xe = b.x0 + (b.t1 - b.t0) * b.k;
      html.push('<div class="block ' + b.cls + '" style="left:' + px(b.x0 - PAD) + ';width:' + px(xe - b.x0 + 2 * PAD) +
        '"><span class="blabel">' + b.label + '</span></div>');
      if (b.key === 'radar') {
        seg(b.t0, Math.min(tNow, b.t1), 'past');
        if (tFc > tNow) seg(tNow, Math.min(tFc, b.t1), 'fc');
      } else {
        seg(b.t0, b.t1, 'model ' + b.cls);
      }
      // ticks: density depends on the block scale
      var pxh = b.k * 3600000, minor, lab;
      if (pxh >= 100) { minor = 15; lab = 60; } else if (pxh >= 30) { minor = 60; lab = 360; } else { minor = 180; lab = 720; }
      // grid aligned to local clock time (local midnight + n * step)
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
  }

  function nearestFrame(t) {
    var best = 0, bd = Infinity;
    for (var i = 0; i < frames.length; i++) {
      var d = Math.abs(ms(frames[i].t) - t);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  function showControls() {
    userActive = true;
    $('bar').classList.remove('hidden');
    $('zoom').classList.remove('hidden');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideControls, cfg.controlsHideS * 1000);
    if (cur >= 0) placeStrip(ms(frames[cur].t));     // width known only when visible
  }
  function hideControls() {
    $('bar').classList.add('hidden');
    $('zoom').classList.add('hidden');
    userActive = false;
    if (!playing) show(loopStart());     // kiosk: back to "Aktuell"
  }

  function initBar() {
    $('play').addEventListener('click', function (e) { e.stopPropagation(); setPlaying(!playing); showControls(); });
    $('bubble').addEventListener('click', function (e) {
      e.stopPropagation(); setPlaying(false); show(loopStart()); showControls();
    });
    var tl = $('tl'), drag = null;
    tl.addEventListener('pointerdown', function (ev) {
      if (ev.target.closest && ev.target.closest('#bubble')) return;
      drag = { x0: ev.clientX, s0: stripX, moved: false };
      tl.setPointerCapture(ev.pointerId);
      setPlaying(false); showControls();
    });
    tl.addEventListener('pointermove', function (ev) {
      if (!drag) return;
      var dx = ev.clientX - drag.x0;
      if (Math.abs(dx) > 3) drag.moved = true;
      var half = tlWidth() / 2;
      moveStrip(Math.min(half - PAD, Math.max(half - stripEnd(), drag.s0 + dx)));
      var t = tOf(half - stripX);
      var i = nearestFrame(t);
      if (i !== cur) show(i, true);
      updateBubble(t);
      showControls();
    });
    var end = function (ev) {
      if (!drag) return;
      if (!drag.moved) {                       // tap on the strip: jump to that time
        var r = tl.getBoundingClientRect();
        var t = tOf(ev.clientX - r.left - stripX);
        show(nearestFrame(t));
      } else if (cur >= 0) {
        placeStrip(ms(frames[cur].t));         // snap to the frame
        updateBubble(ms(frames[cur].t));
      }
      drag = null;
    };
    tl.addEventListener('pointerup', end);
    tl.addEventListener('pointercancel', end);
    $('bar').addEventListener('click', function (e) { e.stopPropagation(); showControls(); });
    $('zoom').addEventListener('click', function (e) { e.stopPropagation(); });
    window.addEventListener('resize', function () { if (cur >= 0) placeStrip(ms(frames[cur].t)); });
  }

  // -------------------------------------------------------------- manifest
  function applyManifest(m) {
    manifest = m;
    var old = frames[cur] || null;
    var wasAtNow = cur < 0 || cur === nowIdx;
    all = m.frames || [];
    var limit = Date.now() + cfg.forecastDays * 86400000;
    frames = all.filter(function (f) { return f.k !== 'm' || (cfg.forecastDays > 0 && ms(f.t) <= limit); });
    nowIdx = -1; fcEnd = -1;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].k === 'o' && frames[i].t === m.now) nowIdx = i;
      if (frames[i].k === 'f') fcEnd = i;
    }
    if (nowIdx < 0) for (var j = frames.length - 1; j >= 0; j--) if (frames[j].k === 'o') { nowIdx = j; break; }
    if (fcEnd < 0) fcEnd = nowIdx;
    preload = [];
    frames.forEach(function (f) {
      if (f.k === 'm') return;
      var im = new Image(); im.src = f.u; preload.push(im);
      if (f.c) { var ic = new Image(); ic.src = f.c; preload.push(ic); }
    });
    buildStrip();
    if (playing) return;
    if (wasAtNow || !userActive || !old) { show(loopStart()); return; }
    var idx = -1;
    for (var k = 0; k < frames.length; k++) if (frames[k].t === old.t && frames[k].k === old.k) idx = k;
    show(idx >= 0 ? idx : loopStart());
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

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && playing) { clearTimeout(timer); timer = setTimeout(tick, 300); }
  });

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
