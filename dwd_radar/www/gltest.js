/* WebGL feasibility test for the DWD radar page.
 * MapLibre GL JS map with our own tiles plus a custom WebGL layer that renders synthetic rain/cloud
 * keyframes with motion interpolation (flow warping) and WarnWetter colour classes in the shader. */
(async function () {
  'use strict';
  const info = document.getElementById('info');
  const lines = {};
  const set = (k, v) => { lines[k] = v; info.textContent = Object.keys(lines).map(n => n + ': ' + lines[n]).join('\n'); };
  window.addEventListener('error', e => set('Fehler', e.message));

  // ------------------------------------------------------------------ capability probe
  (function probe() {
    const c = document.createElement('canvas');
    let gl = c.getContext('webgl2'); const v2 = !!gl;
    if (!gl) gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) { set('WebGL', 'nicht verfügbar'); return; }
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    set('WebGL', v2 ? '2.0' : '1.0 (kein WebGL 2)');
    set('GPU', dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
    set('Max. Textur', gl.getParameter(gl.MAX_TEXTURE_SIZE) + ' px, ' + gl.getParameter(gl.MAX_TEXTURE_IMAGE_UNITS) + ' Einheiten');
    set('Ableitungen', v2 || gl.getExtension('OES_standard_derivatives') ? 'ja' : 'nein');
    set('Float-Texturen', (v2 ? !!gl.getExtension('EXT_color_buffer_float') : !!gl.getExtension('OES_texture_float')) ? 'ja' : 'nein');
    const lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext();
  })();
  const chrome = (navigator.userAgent.match(/Chrome\/([\d.]+)/) || [])[1];
  set('Browser', (chrome ? 'Chromium ' + chrome : navigator.userAgent.slice(0, 60)) + ', DPR ' + devicePixelRatio);

  // ------------------------------------------------------------------ config + theme
  const cfg = await (await fetch('config.json?v=' + Date.now())).json();
  const q = new URLSearchParams(location.search);
  const theme = q.get('theme') || cfg.theme || 'auto';
  function haDark() {
    try {
      const ha = window.parent !== window && window.parent.document.querySelector('home-assistant');
      if (ha && ha.hass && ha.hass.themes && typeof ha.hass.themes.darkMode === 'boolean') return ha.hass.themes.darkMode;
    } catch (e) { /* not embedded */ }
    return null;
  }
  const dark = theme === 'dark' || (theme === 'auto' && (haDark() ?? matchMedia('(prefers-color-scheme: dark)').matches));
  document.documentElement.classList.toggle('dark', dark);

  const baseUrl = location.href.replace(/[?#].*$/, '').replace(/[^/]*$/, '');
  const abs = u => baseUrl + u;
  const bm = cfg.basemap;
  const [[la0, lo0], [la1, lo1]] = cfg.bounds;
  const pl = (la1 - la0) * 0.75, po = (lo1 - lo0) * 0.75;
  const wide = [lo0 - po, la0 - pl, lo1 + po, la1 + pl];
  const tileOpts = { type: 'raster', tileSize: 256, minzoom: 5, maxzoom: bm.maxNativeZoom || 12, bounds: wide };

  const sources = {}, layers = [{ id: 'bg', type: 'background', paint: { 'background-color': dark ? '#1b2026' : '#e3e8dc' } }];
  if (bm.own) {
    sources.base = Object.assign({ tiles: [abs(dark ? bm.baseDark : bm.base)] }, tileOpts);
    layers.push({ id: 'base', type: 'raster', source: 'base', paint: { 'raster-fade-duration': 0 } });
  }
  if (cfg.hillshade) {
    sources.hill = Object.assign({ tiles: [abs('hillshade/{z}/{x}/{y}.png?v=' + cfg.version)] }, tileOpts);
    layers.push({ id: 'hill', type: 'raster', source: 'hill', paint: { 'raster-opacity': dark ? 0.55 : 1, 'raster-fade-duration': 0 } });
  }

  const pad = 0.6;
  const map = new maplibregl.Map({
    container: 'map',
    style: { version: 8, sources, layers },
    center: [cfg.center[1], cfg.center[0]], zoom: cfg.zoom - 1,
    minZoom: cfg.minZoom - 1, maxZoom: cfg.maxZoom - 0.5,
    maxBounds: [[lo0 - (lo1 - lo0) * pad, la0 - (la1 - la0) * pad], [lo1 + (lo1 - lo0) * pad, la1 + (la1 - la0) * pad]],
    dragRotate: false, pitchWithRotate: false, touchPitch: false, renderWorldCopies: false, fadeDuration: 0,
    attributionControl: { compact: true, customAttribution: '© OpenStreetMap · Natural Earth · GeoNames · Relief: AWS · Testdaten (synthetisch)' }
  });
  map.touchZoomRotate.disableRotation();
  set('MapLibre', maplibregl.getVersion ? maplibregl.getVersion() : '?');

  // ------------------------------------------------------------------ synthetic data
  // Encoding as on the server side later: byte 0 = dry, else 1 + 24*log2(rate/0.05)
  const enc = r => r < 0.05 ? 0 : Math.max(1, Math.min(255, Math.round(1 + 24 * Math.log2(r / 0.05))));
  const CLASSES = [[0.1, [126, 251, 253]], [0.2, [96, 201, 158]], [0.5, [67, 151, 66]], [1, [36, 112, 44]],
    [2, [247, 232, 52]], [5, [247, 156, 32]], [10, [226, 44, 36]], [20, [150, 20, 30]], [50, [196, 52, 196]]];

  // keyframes generated server-side (same encoding the real pipeline will use), 5 min apart
  const meta = await (await fetch('gltest/meta.json?v=' + cfg.version)).json();
  const N = meta.n, W = meta.w, H = meta.h;
  const loadImg = src => new Promise((res, rej) => {
    const im = new Image(); im.onload = () => res(im); im.onerror = () => rej(new Error('Bild fehlt: ' + src)); im.src = src;
  });

  // ------------------------------------------------------------------ custom WebGL layer
  const state = { t: 0, ready: false };
  const VS = 'attribute vec2 a_pos; attribute vec2 a_uv; uniform mat4 u_matrix; varying vec2 v_uv;\n' +
    'void main() { v_uv = a_uv; gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0); }';
  const FS =
    'varying vec2 v_uv;\n' +
    'uniform sampler2D u_a; uniform sampler2D u_b; uniform sampler2D u_flow;\n' +
    'uniform float u_w; uniform vec2 u_fscale; uniform float u_th[9]; uniform vec3 u_col[9];\n' +
    'uniform float u_alpha; uniform vec3 u_ccol; uniform float u_calpha;\n' +
    'void main() {\n' +
    '  vec2 f = (TEX(u_flow, v_uv).rg * 255.0 - 128.0) * 0.25 * u_fscale;\n' +
    '  vec4 a = TEX(u_a, v_uv - u_w * f);\n' +
    '  vec4 b = TEX(u_b, v_uv + (1.0 - u_w) * f);\n' +
    '  vec4 s = mix(a, b, u_w);\n' +
    '  float r = s.r;\n' +
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
    '  float ra = cov * u_alpha;\n' +
    '  float ca = smoothstep(0.1, 1.0, s.g) * u_calpha * (1.0 - ra);\n' +
    '  FRAG = vec4(col * ra + u_ccol * ca, ra + ca);\n' +
    '}';

  const wxLayer = {
    id: 'wx', type: 'custom', renderingMode: '2d',
    onAdd(m, gl) {
      const v2 = typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext;
      const deriv = v2 || !!gl.getExtension('OES_standard_derivatives');
      set('Karte nutzt', v2 ? 'WebGL 2' : 'WebGL 1');
      const vh = v2 ? '#version 300 es\n#define attribute in\n#define varying out\n' : '';
      const fh = (v2 ? '#version 300 es\nprecision highp float;\n#define varying in\n#define TEX texture\nout vec4 outColor;\n#define FRAG outColor\n'
        : (deriv ? '#extension GL_OES_standard_derivatives : enable\n' : '') + 'precision highp float;\n#define TEX texture2D\n#define FRAG gl_FragColor\n') +
        (deriv ? '#define HAS_DERIV 1\n' : '');
      const sh = (type, src) => {
        const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error('Shader: ' + gl.getShaderInfoLog(s));
        return s;
      };
      const p = gl.createProgram();
      gl.attachShader(p, sh(gl.VERTEX_SHADER, vh + VS)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fh + FS));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error('Link: ' + gl.getProgramInfoLog(p));
      this.p = p;
      this.loc = {};
      ['u_matrix', 'u_a', 'u_b', 'u_flow', 'u_w', 'u_fscale', 'u_th', 'u_col', 'u_alpha', 'u_ccol', 'u_calpha']
        .forEach(n => { this.loc[n] = gl.getUniformLocation(p, n); });
      this.aPos = gl.getAttribLocation(p, 'a_pos'); this.aUv = gl.getAttribLocation(p, 'a_uv');
      const nw = maplibregl.MercatorCoordinate.fromLngLat([lo0, la1]), se = maplibregl.MercatorCoordinate.fromLngLat([lo1, la0]);
      this.buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
        nw.x, nw.y, 0, 0, se.x, nw.y, 1, 0, nw.x, se.y, 0, 1, se.x, se.y, 1, 1]), gl.STATIC_DRAW);
      this.gl = gl; this.tex = []; this.map = m;
      const mk = img => {
        const t = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false); gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
        gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return t;
      };
      // load all keyframes once and keep them as textures (no browser image cache involved)
      const t0 = performance.now(); let done = 0;
      const urls = ['gltest/flow.png'].concat(Array.from({ length: N }, (_, k) => 'gltest/k' + String(k).padStart(2, '0') + '.png'));
      Promise.all(urls.map(u => loadImg(u + '?v=' + cfg.version).then(im => {
        set('Testdaten', (++done) + '/' + urls.length + ' geladen'); return im;
      }))).then(imgs => {
        const t1 = performance.now();
        this.flow = mk(imgs[0]);
        this.tex = imgs.slice(1).map(mk);
        state.ready = true;
        set('Testdaten', N + ' Bilder ' + W + '×' + H + ': Laden ' + Math.round(t1 - t0) + ' ms, GPU-Upload ' + Math.round(performance.now() - t1) + ' ms');
        m.triggerRepaint();
      }).catch(e => set('Fehler', e.message));
    },
    render(gl, args) {
      if (!state.ready) return;
      const m = args && args.defaultProjectionData ? args.defaultProjectionData.mainMatrix
        : (args && args.modelViewProjectionMatrix) || args;
      const L = this.loc;
      let i = Math.floor(state.t), w = state.t - i;
      if (i >= N - 1) { i = N - 2; w = 1; }
      gl.useProgram(this.p);
      gl.uniformMatrix4fv(L.u_matrix, false, m);
      [[this.tex[i], 'u_a'], [this.tex[i + 1], 'u_b'], [this.flow, 'u_flow']].forEach((e, n) => {
        gl.activeTexture(gl.TEXTURE0 + n); gl.bindTexture(gl.TEXTURE_2D, e[0]); gl.uniform1i(L[e[1]], n);
      });
      gl.uniform1f(L.u_w, w);
      gl.uniform2f(L.u_fscale, 1 / W, 1 / H);
      gl.uniform1fv(L.u_th, CLASSES.map(c => (enc(c[0]) - 0.5) / 255));
      gl.uniform3fv(L.u_col, [].concat(...CLASSES.map(c => c[1].map(x => x / 255))));
      gl.uniform1f(L.u_alpha, 0.88);
      gl.uniform3fv(L.u_ccol, dark ? [0.80, 0.83, 0.86] : [0.97, 0.98, 0.99]);
      gl.uniform1f(L.u_calpha, dark ? 0.42 : 0.62);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.enableVertexAttribArray(this.aPos); gl.enableVertexAttribArray(this.aUv);
      gl.vertexAttribPointer(this.aPos, 2, gl.FLOAT, false, 16, 0);
      gl.vertexAttribPointer(this.aUv, 2, gl.FLOAT, false, 16, 8);
      gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  };

  map.on('load', () => {
    map.addLayer(wxLayer);
    if (bm.own) {
      map.addSource('labels', Object.assign({ tiles: [abs(dark ? bm.labelsDark : bm.labels)] }, tileOpts));
      map.addLayer({ id: 'labels', type: 'raster', source: 'labels', paint: { 'raster-fade-duration': 0 } });
    }
    cfg.pins.forEach(p => {
      const home = p.home, w = home ? 24 : 18, h = home ? 32 : 24;
      const fill = dark ? (home ? '#3d74d6' : '#7d8b96') : (home ? '#173478' : '#4a5a66');
      const el = document.createElement('div'); el.className = 'pin';
      el.innerHTML = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 40"><path d="M15 1C7.3 1 1 7.2 1 14.9 1 25.3 15 39 15 39s14-13.7 14-24.1C29 7.2 22.7 1 15 1z" fill="' +
        fill + '" stroke="#fff" stroke-width="2.2"/><circle cx="15" cy="15" r="' + (home ? 5.5 : 4.5) + '" fill="#fff"/></svg>';
      new maplibregl.Marker({ element: el, anchor: 'bottom' }).setLngLat([p.lon, p.lat]).addTo(map);
    });
  });
  map.on('error', e => { if (e && e.error && !/404|Failed to fetch|AJAXError/.test(String(e.error.message || e.error))) set('Kartenfehler', String(e.error.message || e.error)); });

  // ------------------------------------------------------------------ controls
  const slider = document.getElementById('t'), label = document.getElementById('tl'), btn = document.getElementById('play');
  slider.max = String(N - 1);
  let playing = false, last = 0;
  const show = () => { label.textContent = '+' + (state.t * 5).toFixed(1).replace('.0', '') + ' min'; slider.value = String(state.t); map.triggerRepaint(); };
  slider.addEventListener('input', () => { state.t = parseFloat(slider.value); show(); });
  btn.addEventListener('click', () => { playing = !playing; btn.textContent = playing ? '❚❚' : '▶'; last = 0; if (playing) requestAnimationFrame(tick); });
  function tick(ts) {
    if (!playing) return;
    if (last) { state.t += (ts - last) / 450; if (state.t > N - 1) state.t = 0; show(); }
    last = ts;
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ fps meter (map frames actually drawn)
  let frames = 0, worst = 0, prev = 0, win = performance.now();
  map.on('render', () => {
    const now = performance.now();
    if (prev) worst = Math.max(worst, now - prev);
    prev = now; frames++;
  });
  setInterval(() => {
    const now = performance.now(), dt = now - win;
    set('Bildrate', frames ? Math.round(frames * 1000 / dt) + ' fps (längster Abstand ' + Math.round(worst) + ' ms)' : '– (Karte steht still)');
    frames = 0; worst = 0; prev = 0; win = now;
  }, 1000);
})();
