/* DWD Regenradar – Anzeige. Lädt nur fertige PNGs, keine Rechenarbeit im Browser. */
(function () {
  'use strict';

  var cfg, map, frames = [], nowIdx = -1, cur = -1;
  var ovA, ovB, front, preload = [], manifest = null;
  var playing = true, timer = null, hideTimer = null, resetTimer = null;
  var lastGenerated = null;

  var $ = function (id) { return document.getElementById(id); };
  var fmt = function (iso) {
    return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  };
  function getJSON(url) {
    return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  // ---------------------------------------------------------------- map
  function pinIcon(p) {
    var home = p.home;
    var w = home ? 30 : 20, h = home ? 40 : 27;
    var fill = home ? '#173478' : '#4a5a66';
    var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 40">' +
      '<path d="M15 1C7.3 1 1 7.2 1 14.9 1 25.3 15 39 15 39s14-13.7 14-24.1C29 7.2 22.7 1 15 1z" fill="' + fill +
      '" stroke="#fff" stroke-width="2.2"/><circle cx="15" cy="15" r="' + (home ? 5.5 : 4.5) + '" fill="#fff"/></svg>';
    var lbl = (!home && p.name) ? '<span class="lbl">' + p.name.replace(/[<>&]/g, '') + '</span>' : '';
    var halo = home ? '<span class="home-halo"></span>' : '';
    return L.divIcon({ className: 'pin', html: halo + svg + lbl, iconSize: [w, h], iconAnchor: [w / 2, h] });
  }

  function initMap() {
    var b = L.latLngBounds(cfg.bounds);
    map = L.map('map', {
      zoomControl: false, attributionControl: true, center: cfg.center, zoom: cfg.zoom,
      minZoom: cfg.minZoom, maxZoom: cfg.maxZoom, maxBounds: b.pad(0.6), maxBoundsViscosity: 0.8,
      fadeAnimation: false, inertia: true, tap: false
    });
    map.attributionControl.setPrefix(false);

    map.createPane('hill').classList.add('hill-pane');
    map.getPane('hill').style.zIndex = 250;
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

    map.attributionControl.addAttribution('Radar: © Deutscher Wetterdienst');

    ovA = L.imageOverlay('data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==', b,
      { opacity: 0, interactive: false }).addTo(map);
    ovB = L.imageOverlay('data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==', b,
      { opacity: 0, interactive: false }).addTo(map);
    front = ovA;

    cfg.pins.forEach(function (p) {
      L.marker([p.lat, p.lon], { icon: pinIcon(p), interactive: false, keyboard: false,
        zIndexOffset: p.home ? 1000 : 0 }).addTo(map);
    });

    map.on('click', function () { showBar(); });
    map.on('movestart zoomstart', function (e) { armReset(); });
  }

  function armReset() {
    clearTimeout(resetTimer);
    if (!cfg.resetViewS) return;
    resetTimer = setTimeout(function () {
      map.setView(cfg.center, cfg.zoom, { animate: false });
    }, cfg.resetViewS * 1000);
  }

  // ------------------------------------------------------------- frames
  function frameOpacity(i) { return frames[i] && frames[i].k === 'f' ? 0.82 : 1; }

  function show(i) {
    if (i < 0 || i >= frames.length) return;
    cur = i;
    var f = frames[i];
    var back = front === ovA ? ovB : ovA;
    var url = f.u;
    var done = false;
    var swap = function () {
      if (done || cur !== i) return;
      done = true;
      back.setOpacity(frameOpacity(i));
      front.setOpacity(0);
      front = back;
    };
    back.once('load', swap);
    back.setUrl(url);
    // already-cached images may not fire load reliably on some WebViews
    var img = back.getElement();
    if (img && img.complete && img.naturalWidth > 1 && img.src.indexOf(url) >= 0) swap();
    updateStamp();
    updateKnob();
  }

  function updateStamp() {
    var el = $('stamp'), f = frames[cur];
    if (!f) { $('stamp-time').textContent = 'keine Daten'; return; }
    var stale = manifest && manifest.stale;
    $('stamp-time').textContent = fmt(f.t);
    var tag = '';
    if (stale && manifest.now) tag = 'Stand ' + fmt(manifest.now) + ' · keine neuen Daten';
    else if (f.k === 'f') tag = 'Vorhersage';
    $('stamp-tag').textContent = tag;
    $('stamp-tag').className = tag ? 'tag' : '';
    el.className = 'stamp' + (f.k === 'f' ? ' fc' : '') + (stale ? ' stale' : '');
  }

  function delayFor(i) {
    if (i === nowIdx) return cfg.holdNowMs;
    if (i === frames.length - 1) return cfg.holdEndMs;
    return cfg.frameMs;
  }

  function tick() {
    clearTimeout(timer);
    if (!playing || !frames.length || document.hidden) return;
    var next = cur + 1 >= frames.length ? 0 : cur + 1;
    show(next);
    timer = setTimeout(tick, delayFor(next));
  }

  function setPlaying(p) {
    playing = p;
    $('ico-pause').style.display = p ? '' : 'none';
    $('ico-play').style.display = p ? 'none' : '';
    clearTimeout(timer);
    if (p) timer = setTimeout(tick, delayFor(cur));
  }

  // ------------------------------------------------------------ timeline
  function pos(i) { return frames.length > 1 ? (i / (frames.length - 1)) * 100 : 0; }

  function buildTrack() {
    var n = frames.length;
    if (!n) return;
    var ni = nowIdx < 0 ? n - 1 : nowIdx;
    $('seg-past').style.left = '0%';
    $('seg-past').style.width = pos(ni) + '%';
    $('seg-fc').style.left = pos(ni) + '%';
    $('seg-fc').style.width = (100 - pos(ni)) + '%';
    $('seg-fc').style.display = ni < n - 1 ? '' : 'none';
    $('now').style.left = pos(ni) + '%';
    var ticks = $('ticks');
    ticks.innerHTML = '';
    frames.forEach(function (f, i) {
      var d = new Date(f.t), m = d.getMinutes();
      if (m % 30 !== 0) return;
      var t = document.createElement('div');
      t.className = 'tick' + (m === 0 ? ' lbl' : '');
      t.style.left = pos(i) + '%';
      if (m === 0) t.setAttribute('data-t', fmt(f.t));
      ticks.appendChild(t);
    });
  }

  function updateKnob() {
    if (cur < 0) return;
    $('knob').style.left = pos(cur) + '%';
    var f = frames[cur];
    $('knob-label').textContent = f ? fmt(f.t) : '';
  }

  function showBar() {
    var bar = $('bar');
    bar.classList.remove('hidden');
    bar.setAttribute('aria-hidden', 'false');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideBar, cfg.controlsHideS * 1000);
  }
  function hideBar() {
    $('bar').classList.add('hidden');
    $('bar').setAttribute('aria-hidden', 'true');
    if (!playing) setPlaying(true);   // kiosk: resume animation when controls disappear
  }

  function initBar() {
    $('play').addEventListener('click', function (e) {
      e.stopPropagation(); setPlaying(!playing); showBar();
    });
    var track = $('track'), drag = false;
    var seek = function (ev) {
      var r = track.getBoundingClientRect();
      var x = Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1);
      var i = Math.round(x * (frames.length - 1));
      if (i !== cur) show(i);
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
    var oldUrl = frames[cur] ? frames[cur].u : null;
    frames = m.frames || [];
    nowIdx = -1;
    for (var i = 0; i < frames.length; i++) if (frames[i].t === m.now && frames[i].k === 'o') nowIdx = i;
    // preload (keeps images in the memory cache); drop references of old frames
    preload = frames.map(function (f) { var im = new Image(); im.src = f.u; return im; });
    buildTrack();
    var idx = -1;
    for (var j = 0; j < frames.length; j++) if (frames[j].u === oldUrl) idx = j;
    if (cur < 0 || idx < 0) {
      show(0);
      if (playing) { clearTimeout(timer); timer = setTimeout(tick, delayFor(0)); }
    } else {
      cur = idx; updateStamp(); updateKnob();
    }
  }

  function poll() {
    getJSON('manifest.json').then(function (m) {
      // add-on stopped writing? mark as stale on the client side as well
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

  getJSON('config.json').then(function (c) {
    cfg = c;
    initMap();
    initBar();
    poll();
    setInterval(poll, 60000);
  }).catch(function (e) {
    $('stamp-time').textContent = 'Konfiguration fehlt (' + e.message + ')';
  });
})();
