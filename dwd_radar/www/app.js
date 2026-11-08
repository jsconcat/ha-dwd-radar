/* DWD Regenradar – Anzeige. Lädt nur fertige PNGs, keine Rechenarbeit im Browser.
 * Standard: pausiert auf "Aktuell". Play spielt Aktuell .. +2 h (Radar-Vorhersage) in Schleife.
 * Zeitleiste: Vergangenheit + 2 h Radar breit, Modellbereich (bis 7 Tage) gestaucht.
 */
(function () {
  'use strict';

  var cfg, map, manifest = null, frames = [], nowIdx = -1, fcEnd = -1, cur = -1;
  var playing = false, timer = null, hideTimer = null, resetTimer = null, lastGenerated = null;
  var preload = [], radarSw = null, cloudSw = null, userActive = false;
  var BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
  var DAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];

  var $ = function (id) { return document.getElementById(id); };
  var hm = function (iso) {
    return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  };
  var dayhm = function (iso) { return DAYS[new Date(iso).getDay()] + ' ' + hm(iso); };
  function sameDay(a, b) { return new Date(a).toDateString() === new Date(b).toDateString(); }
  function getJSON(url) {
    return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // ------------------------------------------------------------------ map
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

    map.on('click', function () { showBar(); });
    map.on('movestart zoomstart', function () { armReset(); });
  }

  function armReset() {
    clearTimeout(resetTimer);
    if (!cfg.resetViewS) return;
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

  // --------------------------------------------------------------- frames
  function boundsFor(f) {
    return L.latLngBounds(f.k === 'm' ? manifest.mbounds : manifest.bounds);
  }

  function show(i) {
    if (i < 0 || i >= frames.length) return;
    cur = i;
    var f = frames[i];
    var isCur = function () { return cur === i; };
    radarSw.set(f.u, f.k === 'o' ? 1 : 0.85, boundsFor(f), isCur);
    cloudSw.set(f.c || null, 1, L.latLngBounds(manifest.mbounds), isCur);
    updateStamp();
    updateKnob();
  }

  function sourceLabel(f) {
    return (manifest.sources && manifest.sources[f.s]) || 'Modell';
  }

  function updateStamp() {
    var el = $('stamp'), f = frames[cur];
    if (!f) { $('stamp-time').textContent = 'keine Daten'; return; }
    var stale = manifest && manifest.stale;
    var showDay = f.k === 'm' || !sameDay(f.t, Date.now());
    $('stamp-time').textContent = showDay ? dayhm(f.t) : hm(f.t);
    var tag = '';
    if (stale && manifest.now) tag = 'Stand ' + hm(manifest.now) + ' · keine neuen Radardaten';
    else if (f.k === 'f') tag = 'Radar-Vorhersage';
    else if (f.k === 'm') tag = 'Modell ' + sourceLabel(f);
    $('stamp-tag').textContent = tag;
    $('stamp-tag').className = tag ? 'tag' : '';
    el.className = 'stamp' + (f.k !== 'o' ? ' fc' : '') + (stale ? ' stale' : '');
  }

  // ------------------------------------------------------------- playback
  // Play loops Aktuell .. end of radar nowcast (+2 h); history and model only via scrubbing.
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

  // ------------------------------------------------------------- timeline
  // piecewise time axis: [start..now] 0–20 %, [now..+2 h] 20–45 %, [+2 h..end] 45–100 %
  var axis = null;
  function buildAxis() {
    var t = function (i) { return new Date(frames[i].t).getTime(); };
    var n = frames.length, t0 = t(0), tn = t(n - 1);
    var tNow = t(loopStart()), tFc = t(loopEnd());
    var segs = [[t0, tNow, 0, 0.2], [tNow, tFc, 0.2, 0.45], [tFc, tn, 0.45, 1]];
    if (tn <= tFc) segs = [[t0, tNow, 0, 0.45], [tNow, tFc, 0.45, 1]];
    axis = function (ms) {
      for (var k = 0; k < segs.length; k++) {
        var s = segs[k];
        if (ms <= s[1] || k === segs.length - 1) {
          var span = Math.max(1, s[1] - s[0]);
          return (s[2] + (Math.min(Math.max(ms, s[0]), s[1]) - s[0]) / span * (s[3] - s[2])) * 100;
        }
      }
      return 100;
    };
  }
  function pos(i) { return axis ? axis(new Date(frames[i].t).getTime()) : 0; }

  function buildTrack() {
    if (!frames.length) return;
    buildAxis();
    var pn = pos(loopStart()), pf = pos(loopEnd());
    $('seg-past').style.left = '0%'; $('seg-past').style.width = pn + '%';
    $('seg-fc').style.left = pn + '%'; $('seg-fc').style.width = (pf - pn) + '%';
    $('seg-model').style.left = pf + '%'; $('seg-model').style.width = (100 - pf) + '%';
    $('seg-model').style.display = pf < 99.5 ? '' : 'none';
    $('now').style.left = pn + '%';
    var ticks = $('ticks');
    ticks.innerHTML = '';
    var add = function (p, cls, label) {
      var d = document.createElement('div');
      d.className = 'tick ' + cls;
      d.style.left = p + '%';
      if (label) d.setAttribute('data-t', label);
      ticks.appendChild(d);
    };
    frames.forEach(function (f, i) {
      if (i <= loopEnd() && new Date(f.t).getMinutes() === 0) add(pos(i), 'lbl', hm(f.t).slice(0, 2));
    });
    // model range: day marks at local midnight, minor marks at noon (independent of frame spacing)
    var tFc = new Date(frames[loopEnd()].t).getTime(), tEnd = new Date(frames[frames.length - 1].t).getTime();
    var d = new Date(tFc); d.setHours(12, 0, 0, 0);
    if (d.getTime() <= tFc) d.setDate(d.getDate() + 1);
    if (d.getHours() !== 12) d.setHours(12);
    var mid = new Date(d.getTime()); mid.setHours(0, 0, 0, 0);
    for (var k = 0; k < 10; k++) {
      var m0 = new Date(mid.getFullYear(), mid.getMonth(), mid.getDate() + k, 0, 0, 0);
      var n0 = new Date(mid.getFullYear(), mid.getMonth(), mid.getDate() + k, 12, 0, 0);
      if (m0.getTime() > tFc && m0.getTime() <= tEnd) add(axis(m0.getTime()), 'lbl day', DAYS[m0.getDay()]);
      if (n0.getTime() > tFc && n0.getTime() <= tEnd) add(axis(n0.getTime()), 'minor');
    }
  }

  function updateKnob() {
    if (cur < 0) return;
    $('knob').style.left = pos(cur) + '%';
    var f = frames[cur];
    $('knob-label').textContent = f ? (f.k === 'm' || !sameDay(f.t, Date.now()) ? dayhm(f.t) : hm(f.t)) : '';
  }

  function showBar() {
    userActive = true;
    $('bar').classList.remove('hidden');
    $('bar').setAttribute('aria-hidden', 'false');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideBar, cfg.controlsHideS * 1000);
  }
  function hideBar() {
    $('bar').classList.add('hidden');
    $('bar').setAttribute('aria-hidden', 'true');
    userActive = false;
    // kiosk: when nobody uses it, a running animation keeps playing; otherwise back to "Aktuell"
    if (!playing) show(loopStart());
  }

  function initBar() {
    $('play').addEventListener('click', function (e) {
      e.stopPropagation(); setPlaying(!playing); showBar();
    });
    var track = $('track'), drag = false;
    var seek = function (ev) {
      var r = track.getBoundingClientRect();
      var x = Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1) * 100;
      var best = 0, bd = 1e9;
      for (var i = 0; i < frames.length; i++) {
        var d = Math.abs(pos(i) - x);
        if (d < bd) { bd = d; best = i; }
      }
      if (best !== cur) show(best);
    };
    track.addEventListener('pointerdown', function (ev) {
      drag = true; track.setPointerCapture(ev.pointerId); setPlaying(false); seek(ev); showBar();
    });
    track.addEventListener('pointermove', function (ev) { if (drag) { seek(ev); showBar(); } });
    var end = function () { drag = false; };
    track.addEventListener('pointerup', end);
    track.addEventListener('pointercancel', end);
    $('bar').addEventListener('click', function (e) { e.stopPropagation(); showBar(); });
  }

  // ------------------------------------------------------------ manifest
  function applyManifest(m) {
    manifest = m;
    var old = frames[cur] || null;
    var wasAtNow = cur < 0 || cur === nowIdx;
    frames = m.frames || [];
    nowIdx = -1; fcEnd = -1;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].k === 'o' && frames[i].t === m.now) nowIdx = i;
      if (frames[i].k === 'f') fcEnd = i;
    }
    if (nowIdx < 0) for (var j = frames.length - 1; j >= 0; j--) if (frames[j].k === 'o') { nowIdx = j; break; }
    if (fcEnd < 0) fcEnd = nowIdx;
    // preload radar + nowcast frames only (model frames load when needed)
    preload = [];
    frames.forEach(function (f) {
      if (f.k === 'm') return;
      var im = new Image(); im.src = f.u; preload.push(im);
      if (f.c) { var ic = new Image(); ic.src = f.c; preload.push(ic); }
    });
    buildTrack();
    if (playing) return;
    if (wasAtNow || !userActive || !old) { show(loopStart()); return; }
    var idx = -1;
    for (var k = 0; k < frames.length; k++) if (frames[k].t === old.t && frames[k].k === old.k) idx = k;
    show(idx >= 0 ? idx : loopStart());
  }

  function poll() {
    getJSON('manifest.json').then(function (m) {
      var age = (Date.now() - new Date(m.generated).getTime()) / 60000;
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

  // index.html may be an old cached copy (/local is cached for 31 days): add missing elements
  function patchDom() {
    if (!$('seg-model') && $('seg-fc')) {
      var d = document.createElement('div');
      d.id = 'seg-model'; d.className = 'seg model';
      $('seg-fc').parentNode.insertBefore(d, $('seg-fc').nextSibling);
    }
  }

  getJSON('config.json').then(function (c) {
    cfg = c;
    patchDom();
    initMap();
    initBar();
    setPlaying(false);
    poll();
    setInterval(poll, 60000);
  }).catch(function (e) {
    $('stamp-time').textContent = 'Konfiguration fehlt (' + e.message + ')';
  });
})();
