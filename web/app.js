/* ============================================================================
   J.A.R.V.I.S. HUD controller
   - Python pushes batched events into JARVIS.receive([...])
   - JS calls Python through window.pywebview.api.*
   - Opened in a plain browser, it loads devmock.js and runs a simulated backend.
   ========================================================================== */
(() => {
  'use strict';

  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  const TAU = Math.PI * 2;
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const BANDS = 48;
  const STATE_RGB = {
    IDLE: [42, 212, 255],
    LISTENING: [54, 255, 205],
    THINKING: [255, 182, 72],
    SPEAKING: [128, 234, 255],
  };

  // HUD colour schemes: the CSS colours, plus the reactor's per-state colours (cyan/ice/green/amber are the roles)
  const THEMES = {
    arc: { css: { cyan: '42 212 255', ice: '128 234 255', green: '54 255 205', amber: '255 182 72' },
      state: { IDLE: [42, 212, 255], LISTENING: [54, 255, 205], THINKING: [255, 182, 72], SPEAKING: [128, 234, 255] } },
    mark3: { css: { cyan: '255 112 64', ice: '255 205 130', green: '255 222 110', amber: '255 168 60' },
      state: { IDLE: [255, 112, 64], LISTENING: [255, 222, 110], THINKING: [255, 236, 200], SPEAKING: [255, 205, 130] } },
    stealth: { css: { cyan: '72 255 150', ice: '172 255 212', green: '205 255 120', amber: '255 200 80' },
      state: { IDLE: [72, 255, 150], LISTENING: [205, 255, 120], THINKING: [255, 200, 80], SPEAKING: [172, 255, 212] } },
    violet: { css: { cyan: '172 122 255', ice: '218 192 255', green: '122 232 255', amber: '255 150 214' },
      state: { IDLE: [172, 122, 255], LISTENING: [122, 232, 255], THINKING: [255, 150, 214], SPEAKING: [218, 192, 255] } },
    rose: { css: { cyan: '255 128 170', ice: '255 206 222', green: '255 196 140', amber: '196 160 255' },
      state: { IDLE: [255, 128, 170], LISTENING: [255, 196, 140], THINKING: [196, 160, 255], SPEAKING: [255, 206, 222] } },
  };
  function hexRGB(hex) { const n = parseInt(String(hex).slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function toHex(theme) {
    if (/^#[0-9a-f]{6}$/i.test(theme || '')) return theme.toLowerCase();
    return '#' + themeOf(theme).state.IDLE.map((v) => v.toString(16).padStart(2, '0')).join('');
  }
  function hsl2rgb(h, s, l) {
    const f = (n) => { const k = (n + h / 30) % 12; const a = s * Math.min(l, 1 - l); return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)))); };
    return [f(0), f(8), f(4)];
  }
  function rgb2hsl([r, g, b]) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
    if (!d) return [0, 0, l];
    const s = d / (1 - Math.abs(2 * l - 1));
    const h = max === r ? 60 * (((g - b) / d) % 6) : max === g ? 60 * ((b - r) / d + 2) : 60 * ((r - g) / d + 4);
    return [(h + 360) % 360, s, l];
  }
  /** A full HUD palette from one colour: the colour itself, a pale tint, and two companion hues for listening/thinking. */
  function themeOf(name) {
    if (THEMES[name]) return THEMES[name];
    if (!/^#[0-9a-f]{6}$/i.test(name || '')) return THEMES.arc;
    const base = hexRGB(name);
    let [h, s, l] = rgb2hsl(base);
    s = Math.max(s, 0.55); l = Math.min(Math.max(l, 0.55), 0.72);
    const main = hsl2rgb(h, s, l), ice = hsl2rgb(h, s * 0.8, Math.min(0.88, l + 0.2));
    const listen = hsl2rgb((h + 40) % 360, s, 0.66), think = hsl2rgb((h + 180) % 360, Math.max(s, 0.7), 0.66);
    const css = (c) => c.join(' ');
    return { css: { cyan: css(main), ice: css(ice), green: css(listen), amber: css(think) },
      state: { IDLE: main, LISTENING: listen, THINKING: think, SPEAKING: ice } };
  }
  let themeRGB = '42,212,255';
  function applyTheme(name) {
    const t = themeOf(name);
    const root = document.documentElement;
    Object.entries(t.css).forEach(([k, v]) => root.style.setProperty(`--${k}`, v));
    Object.assign(STATE_RGB, Object.fromEntries(Object.entries(t.state).map(([k, v]) => [k, v.slice()])));
    themeRGB = t.state.IDLE.join(',');
    document.body.dataset.theme = THEMES[name] ? name : /^#/.test(name || '') ? 'custom' : 'arc';
    if (typeof Col !== 'undefined') Col.tgt = (STATE_RGB[S.state] || STATE_RGB.IDLE).slice();
  }

  const S = {
    persona: null, personas: [], memory: null,
    state: 'IDLE', listenPhase: null, settings: {}, ollama: null, mic: null, audio: null,
    core: null, voiceOk: true, voices: null, booted: false, system: null, activity: null, wake: null,
  };

  let api = null;
  let reactorRef = null;
  async function call(name, ...args) {
    if (!api || typeof api[name] !== 'function') return null;
    try { return await api[name](...args); } catch (err) { console.error(`api.${name}`, err); return null; }
  }

  const title = () => (S.settings.user_title || 'sir');
  const voiceShort = (id) => (id || '').split('-').pop().replace('Neural', '').replace('Multilingual', '') || '—';
  const fmtTime = (d = new Date()) => d.toLocaleTimeString('en-GB', { hour12: false });
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ========================================================================= colour + audio feed
  const Col = {
    cur: STATE_RGB.IDLE.slice(), tgt: STATE_RGB.IDLE.slice(),
    update(dt) { const k = 1 - Math.exp(-dt * 4); for (let i = 0; i < 3; i++) this.cur[i] = lerp(this.cur[i], this.tgt[i], k); },
    rgba(a) { return `rgba(${this.cur[0] | 0},${this.cur[1] | 0},${this.cur[2] | 0},${a})`; },
  };

  /** Smoothed spectrum + loudness from whichever source is live: TTS clip, mic, or idle shimmer. */
  const Feed = {
    bands: new Float32Array(BANDS), target: new Float32Array(BANDS),
    level: 0, targetLevel: 0, slow: 0, onset: 0, clip: null, mic: null, micAt: 0,
    setClip(ev) { this.clip = { frames: ev.frames || [], levels: ev.levels || [], fps: ev.fps || 40, start: performance.now(), dur: (ev.duration || 0) * 1000 }; },
    stop() { this.clip = null; },
    setMic(ev) { this.mic = ev; this.micAt = performance.now(); },
    update(now, dt) {
      let live = false;
      const c = this.clip;
      if (c) {
        const t = now - c.start;
        if (t > c.dur + 150) this.clip = null;
        else {
          const i = clamp(Math.floor((t / 1000) * c.fps), 0, c.frames.length - 1);
          const f = c.frames[i];
          if (f) {
            for (let k = 0; k < BANDS; k++) this.target[k] = (f[k] || 0) / 100;
            this.targetLevel = (c.levels[i] || 0) / 100;
            live = true;
          }
        }
      }
      if (!live && S.state === 'LISTENING' && this.mic && now - this.micAt < 400) {
        const b = this.mic.bands || [];
        for (let k = 0; k < BANDS; k++) this.target[k] = (b[k] || 0) / 100;
        this.targetLevel = (this.mic.level || 0) / 100;
        live = true;
      }
      if (!live) {
        const base = S.state === 'THINKING' ? 0.12 : S.state === 'LISTENING' ? 0.06 : 0.04;
        for (let k = 0; k < BANDS; k++) {
          this.target[k] = base * (0.55 + 0.45 * Math.sin(now / 520 + k * 0.6) * Math.sin(now / 1400 + k * 0.21));
        }
        this.targetLevel = base * 0.5;
      }
      const atk = 1 - Math.exp(-dt * 30), rel = 1 - Math.exp(-dt * 8);
      for (let k = 0; k < BANDS; k++) {
        const v = this.bands[k], t = this.target[k];
        this.bands[k] = v + (t - v) * (t > v ? atk : rel);
      }
      this.level += (this.targetLevel - this.level) * (this.targetLevel > this.level ? atk : rel);
      this.slow += (this.level - this.slow) * (1 - Math.exp(-dt * 2.5));
      this.onset = Math.max(0, this.level - this.slow);
    },
  };

  // ========================================================================= arc reactor
  class Reactor {
    constructor(canvas) {
      this.c = canvas;
      this.ctx = canvas.getContext('2d');
      this.rot = 0; this.rot2 = 0; this.spin = 0; this.sweep = 0;
      this.rings = []; this.lastRing = 0; this.lastIdleRing = 0;
      this.power = 0; this.powerTarget = 0;
      this.flareAt = -1e9;
      const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
      this.particles = Array.from({ length: reduce ? 30 : 110 }, () => this.particle(true));
      this.resize();
      new ResizeObserver(() => this.resize()).observe(canvas);
    }
    resize() {
      // clientWidth ignores CSS transforms (the boot animation scales this column)
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      this.w = Math.max(1, this.c.clientWidth); this.h = Math.max(1, this.c.clientHeight);
      this.c.width = Math.round(this.w * dpr); this.c.height = Math.round(this.h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    particle(init) {
      return {
        a: Math.random() * TAU,
        r: init ? lerp(0.45, 1.45, Math.random()) : lerp(0.42, 0.62, Math.random()),
        v: (Math.random() * 0.35 + 0.05) * (Math.random() < 0.5 ? -1 : 1),
        s: Math.random() * 1.5 + 0.4, o: Math.random() * 0.6 + 0.2, d: Math.random() * 0.05 + 0.012,
      };
    }
    /** Wake-word acknowledgement: a bright shock ring and an anamorphic lens streak. */
    flare() { this.flareAt = performance.now(); this.rings.push({ r: this.lastR * 0.2 || 30, a: 1 }); }
    ring(ctx, r, stroke, lw) { ctx.beginPath(); ctx.arc(0, 0, r, 0, TAU); ctx.strokeStyle = stroke; ctx.lineWidth = lw; ctx.stroke(); }

    draw(now, dt) {
      const { ctx, w, h } = this;
      ctx.clearRect(0, 0, w, h);
      this.power += (this.powerTarget - this.power) * (1 - Math.exp(-dt * 1.8));
      const P = this.power;
      if (P < 0.003) return;

      const cx = w / 2, cy = h / 2 - 26;
      const R = Math.max(40, Math.min(w * 0.3, (h - 110) * 0.4)) * (0.82 + 0.18 * P);
      const C = (a) => Col.rgba(a * P);
      const E = Feed.level, st = S.state, bands = Feed.bands;
      const flare = Math.max(0, 1 - (now - this.flareAt) / 1100);
      this.lastR = R;

      this.rot += dt * (0.08 + E * 0.5);
      this.rot2 -= dt * (0.05 + E * 0.25);
      this.spin += dt * (st === 'THINKING' ? 4.4 : 0.5);
      this.sweep += dt * (st === 'LISTENING' ? 2.6 : 0.7);

      ctx.save();
      ctx.translate(cx, cy);
      ctx.globalCompositeOperation = 'lighter';

      // ambient bloom
      let g = ctx.createRadialGradient(0, 0, R * 0.1, 0, 0, R * 1.7);
      g.addColorStop(0, C(0.2 + 0.32 * E)); g.addColorStop(0.45, C(0.05 + 0.08 * E)); g.addColorStop(1, C(0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, R * 1.7, 0, TAU); ctx.fill();

      // soft halo band just outside the reactor
      g = ctx.createRadialGradient(0, 0, R * 1.22, 0, 0, R * 1.5);
      g.addColorStop(0, C(0)); g.addColorStop(0.35, C(0.05 + 0.12 * E + 0.35 * flare)); g.addColorStop(1, C(0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, R * 1.5, 0, TAU); ctx.fill();

      // outer degree ring + labels
      this.ring(ctx, R * 1.2, C(0.2), 1);
      for (let i = 0; i < 72; i++) {
        const a = (i / 72) * TAU, major = i % 6 === 0, len = major ? 8 : 3;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * R * 1.2, Math.sin(a) * R * 1.2);
        ctx.lineTo(Math.cos(a) * (R * 1.2 - len), Math.sin(a) * (R * 1.2 - len));
        ctx.strokeStyle = C(major ? 0.5 : 0.18); ctx.lineWidth = 1; ctx.stroke();
      }
      ctx.font = '10px "Share Tech Mono", monospace'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillStyle = C(0.5);
      [['000', -90], ['090', 0], ['180', 90], ['270', 180]].forEach(([t, d]) => {
        const a = (d * Math.PI) / 180; ctx.fillText(t, Math.cos(a) * R * 1.28, Math.sin(a) * R * 1.28);
      });

      // rotating bracket arcs
      ctx.lineWidth = 2;
      for (let i = 0; i < 3; i++) {
        const a0 = this.rot2 + (i * TAU) / 3, a1 = a0 + 0.62;
        ctx.beginPath(); ctx.arc(0, 0, R * 1.1, a0, a1); ctx.strokeStyle = C(0.55); ctx.stroke();
        for (const a of [a0, a1]) {
          ctx.beginPath();
          ctx.moveTo(Math.cos(a) * R * 1.07, Math.sin(a) * R * 1.07);
          ctx.lineTo(Math.cos(a) * R * 1.13, Math.sin(a) * R * 1.13); ctx.stroke();
        }
      }

      // listening radar sweep
      if (st === 'LISTENING' && ctx.createConicGradient) {
        const cg = ctx.createConicGradient(this.sweep, 0, 0);
        cg.addColorStop(0, C(0)); cg.addColorStop(0.82, C(0)); cg.addColorStop(0.995, C(0.22)); cg.addColorStop(1, C(0));
        ctx.fillStyle = cg; ctx.beginPath(); ctx.arc(0, 0, R * 1.0, 0, TAU); ctx.fill();
      }

      // tick ring
      for (let i = 0; i < 120; i++) {
        const a = this.rot + (i / 120) * TAU, major = i % 10 === 0;
        const r0 = R * 0.995, r1 = R * (major ? 0.94 : 0.975);
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
        ctx.strokeStyle = C(major ? 0.6 : 0.22); ctx.lineWidth = major ? 1.6 : 1; ctx.stroke();
      }
      this.ring(ctx, R, C(0.32), 1);

      // circular spectrum
      const N = BANDS * 2, r0 = R * 0.66;
      const bw = Math.max(1.6, ((TAU * r0) / N) * 0.48);
      for (let pass = 0; pass < 2; pass++) {
        ctx.lineCap = 'round';
        ctx.lineWidth = pass === 0 ? bw * 2.8 : bw;
        for (let i = 0; i < N; i++) {
          const k = i < BANDS ? i : N - 1 - i;
          const v = bands[k];
          const a = -Math.PI / 2 + ((i + 0.5) / N) * TAU;
          const len = R * (0.012 + v * 0.29);
          const ca = Math.cos(a), sa = Math.sin(a);
          ctx.beginPath(); ctx.moveTo(ca * r0, sa * r0); ctx.lineTo(ca * (r0 + len), sa * (r0 + len));
          ctx.strokeStyle = pass === 0 ? C(0.07 + 0.2 * v) : C(0.28 + 0.72 * v);
          ctx.stroke();
          if (pass === 1 && v > 0.18) {  // hot white tip on loud bands
            ctx.fillStyle = `rgba(255,255,255,${(v - 0.18) * 0.9 * P})`;
            ctx.beginPath(); ctx.arc(ca * (r0 + len), sa * (r0 + len), bw * 0.55, 0, TAU); ctx.fill();
          }
        }
      }
      ctx.lineCap = 'butt';

      // inner segmented ring (+ thinking comets)
      const rs = R * 0.595;
      if (st === 'THINKING' && ctx.createConicGradient) {
        for (let j = 0; j < 2; j++) {
          const cg = ctx.createConicGradient(this.spin + j * Math.PI, 0, 0);
          cg.addColorStop(0, C(0)); cg.addColorStop(0.3, C(0.05)); cg.addColorStop(0.495, C(0.95)); cg.addColorStop(0.5, C(0));
          cg.addColorStop(1, C(0));
          ctx.strokeStyle = cg; ctx.lineWidth = R * 0.04; ctx.beginPath(); ctx.arc(0, 0, rs, 0, TAU); ctx.stroke();
        }
      }
      ctx.lineWidth = R * 0.018;
      for (let i = 0; i < 24; i++) {
        const a0 = -this.rot * 0.7 + (i / 24) * TAU;
        ctx.beginPath(); ctx.arc(0, 0, rs, a0, a0 + (TAU / 24) * 0.7);
        ctx.strokeStyle = C(0.16 + E * 0.4 + (i % 6 === 0 ? 0.25 : 0)); ctx.stroke();
      }

      // housing
      this.ring(ctx, R * 0.5, C(0.45), 1.2);
      this.ring(ctx, R * 0.465, C(0.22), 1);
      for (let i = 0; i < 36; i++) {
        const a = (i / 36) * TAU + this.rot * 0.2;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * R * 0.465, Math.sin(a) * R * 0.465);
        ctx.lineTo(Math.cos(a) * R * 0.5, Math.sin(a) * R * 0.5);
        ctx.strokeStyle = C(0.22); ctx.lineWidth = 1; ctx.stroke();
      }

      // rotating hexagon frame
      ctx.save();
      ctx.rotate(this.rot2 * 0.6);
      ctx.beginPath();
      for (let i = 0; i <= 6; i++) {
        const a = (i / 6) * TAU, rr = R * 0.56;
        i ? ctx.lineTo(Math.cos(a) * rr, Math.sin(a) * rr) : ctx.moveTo(Math.cos(a) * rr, Math.sin(a) * rr);
      }
      ctx.setLineDash([R * 0.04, R * 0.03]);
      ctx.strokeStyle = C(0.18 + E * 0.25); ctx.lineWidth = 1; ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();

      // coils
      for (let i = 0; i < 10; i++) {
        const a = (i / 10) * TAU - Math.PI / 2 + this.rot * 0.12;
        let b = 0.22 + E * 0.55;
        if (st === 'THINKING') b = 0.15 + Math.pow(Math.max(0, Math.cos(a - this.spin)), 6) * 0.85;
        else if (st === 'SPEAKING') b = 0.25 + bands[(i * 4 + 2) % BANDS] * 0.75;
        const ri = R * 0.265, ro = R * 0.43, hi = 0.2, ho = 0.17;
        ctx.beginPath();
        ctx.arc(0, 0, ro, a - ho, a + ho);
        ctx.arc(0, 0, ri, a + hi, a - hi, true);
        ctx.closePath();
        ctx.fillStyle = C(0.06 + b * 0.38); ctx.fill();
        ctx.strokeStyle = C(0.3 + b * 0.6); ctx.lineWidth = 1.2; ctx.stroke();
      }

      // shockwaves
      if (Feed.onset > 0.12 && now - this.lastRing > 150 && (st === 'SPEAKING' || st === 'LISTENING')) {
        this.rings.push({ r: R * 0.22, a: clamp(Feed.onset * 3, 0.3, 0.9) });
        this.lastRing = now;
      }
      if (st === 'IDLE' && now - this.lastIdleRing > 3800) {
        this.rings.push({ r: R * 0.22, a: 0.22 });
        this.lastIdleRing = now;
      }
      for (const ring of this.rings) {
        ring.r += dt * R * 0.9; ring.a *= Math.exp(-dt * 1.5);
        this.ring(ctx, ring.r, C(ring.a), 1 + ring.a * 2.5);
      }
      this.rings = this.rings.filter((r) => r.r < R * 1.55 && r.a > 0.02);

      // particles
      for (const p of this.particles) {
        p.a += p.v * dt * (1 + E * 3);
        p.r += p.d * dt * (1 + E * 6);
        if (p.r > 1.5) Object.assign(p, this.particle(false));
        const fade = Math.min(1, (1.5 - p.r) * 3) * Math.min(1, (p.r - 0.4) * 6);
        ctx.fillStyle = C(p.o * fade * (0.5 + E));
        ctx.beginPath(); ctx.arc(Math.cos(p.a) * p.r * R, Math.sin(p.a) * p.r * R, p.s, 0, TAU); ctx.fill();
      }

      // core
      this.ring(ctx, R * 0.215, C(0.65), 1.5);
      const cr = R * (0.14 + 0.05 * E + 0.007 * Math.sin(now / 650));
      g = ctx.createRadialGradient(0, 0, 0, 0, 0, cr * 3.2);
      g.addColorStop(0, C(0.55)); g.addColorStop(0.4, C(0.14)); g.addColorStop(1, C(0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, cr * 3.2, 0, TAU); ctx.fill();
      g = ctx.createRadialGradient(0, 0, 0, 0, 0, cr * 1.2);
      g.addColorStop(0, `rgba(255,255,255,${0.98 * P})`); g.addColorStop(0.35, `rgba(225,250,255,${0.9 * P})`);
      g.addColorStop(0.65, C(0.8)); g.addColorStop(1, C(0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, cr * 1.2, 0, TAU); ctx.fill();
      ctx.save();
      ctx.rotate(-this.rot * 0.35);
      ctx.beginPath();
      for (let i = 0; i < 3; i++) {
        const a = -Math.PI / 2 + (i * TAU) / 3, rr = R * 0.17;
        i ? ctx.lineTo(Math.cos(a) * rr, Math.sin(a) * rr) : ctx.moveTo(Math.cos(a) * rr, Math.sin(a) * rr);
      }
      ctx.closePath(); ctx.strokeStyle = `rgba(255,255,255,${0.5 * P})`; ctx.lineWidth = 1.6; ctx.stroke();
      ctx.restore();

      // glass reflection on the core
      g = ctx.createRadialGradient(-R * 0.06, -R * 0.09, 0, -R * 0.06, -R * 0.09, R * 0.13);
      g.addColorStop(0, `rgba(255,255,255,${0.32 * P})`); g.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(-R * 0.06, -R * 0.09, R * 0.13, 0, TAU); ctx.fill();

      // anamorphic lens streak: always faint, bright when the wake word fires
      const streak = 0.05 + E * 0.18 + flare * 0.75;
      g = ctx.createLinearGradient(-R * 2.2, 0, R * 2.2, 0);
      g.addColorStop(0, C(0)); g.addColorStop(0.5, `rgba(235,252,255,${streak * P})`); g.addColorStop(1, C(0));
      ctx.fillStyle = g; ctx.fillRect(-R * 2.2, -1.2 - flare * 1.5, R * 4.4, 2.4 + flare * 3);
      if (flare > 0) {
        g = ctx.createRadialGradient(0, 0, 0, 0, 0, R * 0.9);
        g.addColorStop(0, `rgba(255,255,255,${0.35 * flare * P})`); g.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, R * 0.9, 0, TAU); ctx.fill();
      }

      ctx.restore();

      // HUD read-outs with leader lines
      if (w / 2 - R * 1.2 > 84 && h > R * 2.6) this.readouts(ctx, cx, cy, R, now, E, P);
    }

    readouts(ctx, cx, cy, R, now, E, P) {
      const C = (a) => Col.rgba(a * P);
      const { w } = this;
      const items = [
        [-138, 'CORE OUTPUT', `${(71 + E * 27 + Math.sin(now / 900) * 1.4).toFixed(1)} %`],
        [-42, 'SIGNAL', `${(-62 + E * 56).toFixed(1)} dB`],
        [138, 'MODE', S.state],
        [42, 'RESONANCE', `${(3.2 + E * 1.6 + Math.sin(now / 1300) * 0.05).toFixed(2)} THz`],
      ];
      ctx.save();
      ctx.textBaseline = 'alphabetic';
      for (const [deg, label, value] of items) {
        const a = (deg * Math.PI) / 180, left = Math.cos(a) < 0, up = Math.sin(a) < 0;
        const x0 = cx + Math.cos(a) * R * 1.2, y0 = cy + Math.sin(a) * R * 1.2;
        const y1 = y0 + (up ? -R * 0.16 : R * 0.16);
        const x1 = x0 + (left ? -1 : 1) * R * 0.16;
        const x2 = left ? 10 : w - 10;
        ctx.strokeStyle = C(0.32); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.lineTo(x2, y1); ctx.stroke();
        ctx.fillStyle = C(0.85);
        ctx.beginPath(); ctx.arc(x0, y0, 2.2, 0, TAU); ctx.fill();
        ctx.fillRect(left ? x2 : x2 - 6, y1 - 1, 6, 2);
        ctx.textAlign = left ? 'left' : 'right';
        ctx.fillStyle = C(0.5); ctx.font = '600 8.5px "Orbitron", sans-serif';
        ctx.fillText(label, x2, y1 - 22);
        ctx.fillStyle = C(0.95); ctx.font = '15px "Share Tech Mono", monospace';
        ctx.fillText(value, x2, y1 - 6);
      }
      ctx.restore();
    }
  }

  // ========================================================================= oscilloscope
  class Wave {
    constructor(canvas) {
      this.c = canvas; this.ctx = canvas.getContext('2d');
      this.resize(); new ResizeObserver(() => this.resize()).observe(canvas);
    }
    resize() {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      this.w = Math.max(1, this.c.clientWidth); this.h = Math.max(1, this.c.clientHeight);
      this.c.width = Math.round(this.w * dpr); this.c.height = Math.round(this.h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    draw(now) {
      const { ctx, w, h } = this, mid = h / 2, E = Feed.level;
      ctx.clearRect(0, 0, w, h);
      const fade = ctx.createLinearGradient(0, 0, w, 0);
      fade.addColorStop(0, Col.rgba(0)); fade.addColorStop(0.5, Col.rgba(0.3)); fade.addColorStop(1, Col.rgba(0));
      ctx.strokeStyle = fade; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke();
      for (let x = w / 2 % 40; x < w; x += 40) {
        ctx.beginPath(); ctx.moveTo(x, mid - 3); ctx.lineTo(x, mid + 3); ctx.stroke();
      }
      ctx.globalCompositeOperation = 'lighter';
      for (let L = 0; L < 3; L++) {
        const phase = (now / 1000) * (1.7 + L * 0.8) * (L % 2 ? -1 : 1);
        const freq = 3.2 + L * 2.1;
        const gain = L === 0 ? 1 : 0.62 - L * 0.12;
        ctx.beginPath();
        for (let x = 0; x <= w; x += 3) {
          const p = x / w;
          const env = Math.pow(Math.sin(Math.PI * p), 1.6);
          const k = Math.min(BANDS - 1, Math.floor(Math.abs(p - 0.5) * 2 * BANDS));
          const amp = (0.035 + Feed.bands[k] * 0.9 + E * 0.25) * (h * 0.46) * env * gain;
          const y = mid + amp * Math.sin(p * TAU * freq + phase);
          x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.strokeStyle = Col.rgba(L === 0 ? 0.95 : 0.4);
        ctx.lineWidth = L === 0 ? 1.7 : 1;
        ctx.stroke();
      }
      ctx.globalCompositeOperation = 'source-over';
    }
  }

  // ========================================================================= toasts
  const TOAST_ICON = { info: 'i', ok: '✓', warn: '!', error: '×' };
  function toast(text, level = 'info', ms = 4200) {
    const el = document.createElement('div');
    el.className = `toast ${level}`;
    el.innerHTML = `<b class="t-icon">${TOAST_ICON[level] || 'i'}</b><span class="t-text"></span><i class="t-bar" style="animation-duration:${ms}ms"></i>`;
    $('.t-text', el).textContent = text;
    $('#toasts').append(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 400); }, ms);
    return el;
  }

  // ========================================================================= chat log
  function renderMarkdown(src) {
    const links = (t) => t
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, text, url) => `<a href="#" data-url="${url}">${text}</a>`)
      .replace(/(^|[\s(])(https:\/\/(?:docs|mail)\.google\.com\/[^\s<)]+)/g, (m, pre, url) => `${pre}<a href="#" data-url="${url}">${url.length > 48 ? url.slice(0, 46) + '…' : url}</a>`);
    const blocks = (t) => t
      .replace(/^(#{1,3})[ \t]+(.+)$/gm, (m, h, text) => `<span class="md-h md-h${h.length}">${text}</span>`)
      .replace(/^[ \t]*[-*•][ \t]+/gm, '<span class="md-li">•</span>');
    const inline = (t) => blocks(links(esc(t)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*(?!\s)([^*\n]+?)\*(?!\w)/g, '$1<em>$2</em>')));
    const parts = src.split('```');
    return parts.map((part, i) => {
      if (i % 2 === 0) {
        if (i > 0) part = part.replace(/^[ \t]*\n/, '');
        if (i < parts.length - 1) part = part.replace(/\n[ \t]*$/, '');
        return inline(part);
      }
      const nl = part.indexOf('\n');
      const lang = nl >= 0 ? part.slice(0, nl).trim() : '';
      const code = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '');
      return `<pre class="code"><span class="lang">${esc(lang || 'code')}</span><code>${esc(code)}</code></pre>`;
    }).join('').trim();
  }

  class Typer {
    constructor(msg) {
      this.msg = msg; this.body = $('.msg-body', msg);
      this.full = ''; this.shown = 0; this.done = false; this.started = false;
      this.node = document.createTextNode(''); this.caret = document.createElement('span'); this.caret.className = 'caret';
    }
    push(text) {
      if (!this.started) {
        this.started = true;
        this.msg.classList.remove('pending');
        this.body.textContent = '';
        this.body.append(this.node, this.caret);
      }
      this.full += text;
    }
    /** advance the typewriter; returns 'final' when finished */
    tick(dt) {
      const backlog = this.full.length - this.shown;
      if (backlog > 0) {
        this.shown = Math.min(this.full.length, this.shown + (65 + backlog * 3.2) * dt);
        this.node.data = this.full.slice(0, Math.floor(this.shown));
        return 'typing';
      }
      if (this.done) {
        this.body.innerHTML = renderMarkdown(this.full);
        return 'final';
      }
      return 'idle';
    }
  }

  const Chat = {
    log: null, typers: new Map(), count: 0,
    init() {
      this.log = $('#log');
      this.empty();
    },
    empty() {
      this.log.innerHTML = '<div id="log-empty">NO TRANSMISSIONS<br>SPEAK OR TYPE TO BEGIN</div>';
      this.count = 0; this.updateCount();
    },
    pinned() { return this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight < 90; },
    scroll(force) { if (force || this.pinned()) this.log.scrollTop = this.log.scrollHeight; },
    updateCount() { $('#log-count').textContent = `${this.count} transmission${this.count === 1 ? '' : 's'}`; },
    make(kind, who, extra = '') {
      $('#log-empty')?.remove();
      const el = document.createElement('div');
      el.className = `msg ${kind}`;
      const avatar = kind.startsWith('jarvis')
        ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5.5"/><path d="M12 7.5 15.6 14H8.4Z"/></svg>'
        : '<svg viewBox="0 0 24 24"><circle cx="12" cy="9" r="3.6"/><path d="M5 19.5c1.2-3.4 3.8-5 7-5s5.8 1.6 7 5"/></svg>';
      el.innerHTML = `<div class="avatar">${avatar}</div><div class="msg-main"><div class="msg-meta"><span class="who">${who}</span><time>${fmtTime()}</time>${extra}</div><div class="msg-body"></div></div>`;
      const stick = this.pinned();
      this.log.append(el);
      while (this.log.children.length > 300) this.log.firstElementChild.remove();
      this.scroll(stick);
      return el;
    },
    user(ev) {
      const lang = ev.lang ? `<span class="src lang" title="Detected language: ${esc(ev.lang_name || ev.lang)}">${esc(ev.lang.toUpperCase())}</span>` : '';
      const src = ev.source === 'voice' ? 'VOICE' : ev.source === 'protocol' ? 'PROTOCOL' : 'TEXT';
      const el = this.make('user', ev.source === 'protocol' ? 'STEP' : 'YOU', `<span class="src">${src}</span>${lang}`);
      $('.msg-body', el).textContent = ev.text;
      this.count++; this.updateCount();
      this.scroll(true);
    },
    memoryNote(ev, forgotten = false) {
      const m = ev.memory || {};
      const el = document.createElement('div');
      el.className = `msg system mem-note${forgotten ? ' forgot' : ''}`;
      const label = forgotten ? 'FORGOTTEN' : ev.status === 'updated' ? 'MEMORY UPDATED' : ev.status === 'duplicate' ? 'ALREADY KNEW' : 'REMEMBERED';
      el.innerHTML = `<div class="msg-body"><b class="mem-tag">${label}</b><span class="mem-text"></span>`
        + (ev.status === 'duplicate' ? '' : '<button class="mem-act" data-act="undo">UNDO</button>')
        + (forgotten ? '' : '<button class="mem-act" data-act="edit">EDIT</button>') + '</div>';
      $('.mem-text', el).textContent = m.text || '';
      const undo = $('[data-act=undo]', el);
      if (undo) undo.onclick = async () => {
        const r = forgotten ? await call('memory_restore', m) : await call('memory_delete', m.id);
        if (r && r.ok) { el.classList.add('undone'); $$('.mem-act', el).forEach((b) => b.remove()); $('.mem-tag', el).textContent = forgotten ? 'RESTORED' : 'UNDONE'; }
      };
      const edit = $('[data-act=edit]', el);
      if (edit) edit.onclick = () => MemoryCore.open(m.id);
      $('#log-empty')?.remove();
      const stick = this.pinned();
      this.log.append(el);
      this.scroll(stick);
    },
    restored(turns) {
      if (!turns || !turns.length) return;
      $('#log-empty')?.remove();
      const name = esc((S.persona && S.persona.display) || 'J.A.R.V.I.S.');
      turns.forEach((t) => {
        const el = t.role === 'user' ? this.make('user restored', 'YOU', '<span class="src">EARLIER</span>') : this.make('jarvis restored', name);
        $('time', el).textContent = fmtTime(new Date(t.ts * 1000));
        if (t.role === 'user') $('.msg-body', el).textContent = t.text;
        else $('.msg-body', el).innerHTML = renderMarkdown(t.text);
      });
      this.system('Picked up where we left off. Press Ctrl+L to clear the screen, or Settings ▸ Clear this conversation to start fresh.');
      this.scroll(true);
    },
    system(text, level = 'info') {
      const el = document.createElement('div');
      el.className = `msg system ${level}`;
      el.innerHTML = '<div class="msg-body"></div>';
      $('.msg-body', el).textContent = text;
      $('#log-empty')?.remove();
      const stick = this.pinned();
      this.log.append(el);
      this.scroll(stick);
    },
    card(cls, html) {
      $('#log-empty')?.remove();
      const el = document.createElement('div');
      el.className = `card ${cls}`;
      el.innerHTML = html;
      const stick = this.pinned();
      this.log.append(el);
      this.scroll(stick || true);
      return el;
    },
    files(ev) {
      const kinds = { doc: ['DOC', 'cyan'], slides: ['SLIDES', 'amber'], sheet: ['SHEET', 'green'] };
      const el = this.card('files-card', '<div class="sg-kicker">YOUR RECENT GOOGLE FILES</div><div class="fl-list"></div>');
      const ago = (iso) => {
        const mins = Math.max(0, (Date.now() - Date.parse(iso)) / 60000);
        return mins < 60 ? `${Math.round(mins) || 1} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} d ago`;
      };
      (ev.files || []).forEach((f) => {
        const [label, tone] = kinds[f.kind] || ['FILE', 'cyan'];
        const row = document.createElement('div');
        row.className = 'fl-row';
        row.innerHTML = `<span class="fl-kind ${tone}">${label}</span><span class="fl-title"></span><span class="fl-ago">${f.updated ? ago(f.updated) : ''}</span>
          <button class="btn ghost sm fl-view">VIEW</button><button class="btn ghost sm fl-open">OPEN ↗</button>`;
        $('.fl-title', row).textContent = f.title;
        $('.fl-view', row).onclick = () => call('google_view', f.kind, f.id);
        $('.fl-open', row).onclick = () => call('open_url', f.url);
        $('.fl-list', el).append(row);
      });
    },
    document(ev) {
      const slides = ev.doc_kind === 'slides', sheet = ev.doc_kind === 'sheet';
      const icon = slides
        ? '<svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="12" rx="1.5"/><path d="M8 20h8M12 17v3M7 9h6M7 12h10"/></svg>'
        : sheet ? '<svg viewBox="0 0 24 24"><rect x="4" y="3.5" width="16" height="17" rx="1.5"/><path d="M4 9h16M4 14.5h16M10 9v11.5"/></svg>'
          : '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4M9 11h6M9 14h6M9 17h4"/></svg>';
      const el = this.card(`doc-card ${slides ? 'slides' : sheet ? 'sheet' : 'doc'}`, `
        <div class="dc-icon">${icon}</div>
        <div class="dc-main"><div class="dc-kicker">${slides ? 'GOOGLE SLIDES' : sheet ? 'GOOGLE SHEET' : 'GOOGLE DOC'} · SAVED</div><div class="dc-title"></div></div>
        <div class="dc-btns"><button class="btn ghost sm dc-view" title="Read it here, no browser or sign-in needed">VIEW</button>
        <button class="btn ghost sm dc-open">OPEN ↗</button></div>`);
      $('.dc-title', el).textContent = ev.title || 'Untitled';
      $('.dc-open', el).onclick = () => call('open_url', ev.url);
      $('.dc-view', el).onclick = () => call('google_view', ev.doc_kind || 'doc', ev.id || ev.title);
      if (!ev.url) $('.dc-open', el).remove();
    },
    proposal(p) {
      $$('.proposal-card:not(.done)').forEach((c) => c.classList.add('done'));
      const el = this.card('proposal-card', `
        <div class="mc-row mc-headline"><span class="mail-kicker">NEW PROTOCOL</span><span class="mail-state">AWAITING YOUR APPROVAL</span></div>
        <div class="pp-head">${icon(p.icon || 'bolt', 'pp-ic')}<div><div class="pp-name"></div><div class="pp-sub"></div></div></div>
        <ol class="pp-steps">${(p.steps || []).map(() => '<li><span></span><em></em></li>').join('')}</ol>
        <div class="pp-trig"></div>
        <div class="mail-actions">
          <button class="btn primary sm pp-save"><span>SAVE</span></button>
          <button class="btn ghost sm pp-edit">EDIT</button>
          <button class="btn ghost sm pp-cancel">CANCEL</button>
          <span class="mail-note">Nothing is saved until you approve it. You can also just say “yes”.</span>
        </div>`);
      $('.pp-name', el).textContent = p.name || 'Untitled';
      $('.pp-sub', el).textContent = `${(p.category || 'General').toUpperCase()} · ${(p.steps || []).length} STEP${(p.steps || []).length === 1 ? '' : 'S'}`;
      $$('.pp-steps li', el).forEach((li, i) => {
        const s = p.steps[i];
        $('span', li).textContent = s.text;
        $('em', li).textContent = [s.label, s.condition, s.risk ? 'asks you first' : ''].filter(Boolean).join(' · ');
        li.classList.toggle('risk', !!s.risk);
      });
      const tr = p.triggers || {};
      const how = [`“run ${p.name}”`, ...(p.phrases || []).map((x) => `“${x}”`)];
      if (p.schedule && p.schedule.time) how.push(`at ${p.schedule.time}`);
      if (tr.startup) how.push('when JARVIS starts');
      if (tr.app) how.push(`when ${tr.app} opens`);
      if (tr.hotkey) how.push(tr.hotkey);
      $('.pp-trig', el).textContent = `Start it with: ${how.join(' · ')}`;
      const finish = (label) => { el.classList.add('done'); $('.mail-state', el).textContent = label; $$('button', el).forEach((b) => { b.disabled = true; }); };
      $('.pp-save', el).onclick = async () => {
        const r = await call('protocol_proposal_answer', 'save');
        if (r && r.ok) { finish('SAVED'); toast(r.message || 'Saved.', 'ok', 3500); }
        else toast((r && r.message) || 'Could not save it.', 'error');
      };
      $('.pp-edit', el).onclick = () => { finish('OPENED IN EDITOR'); call('protocol_proposal_answer', 'edit'); };
      $('.pp-cancel', el).onclick = () => { finish('CANCELLED'); call('protocol_proposal_answer', 'cancel'); };
    },
    email(ev) {
      const el = this.card('mail-card', `
        <div class="mc-row mc-headline"><span class="mail-kicker">EMAIL DRAFT</span><span class="mail-state">AWAITING YOUR APPROVAL</span></div>
        <label class="mail-field"><span>TO</span><input class="m-to" type="email" spellcheck="false" placeholder="name@example.com" list="mail-cands-${ev.id}"></label>
        <datalist id="mail-cands-${ev.id}"></datalist>
        <label class="mail-field"><span>SUBJECT</span><input class="m-subject" type="text"></label>
        <textarea class="m-body" rows="6" spellcheck="true"></textarea>
        <div class="mail-actions">
          <button class="btn primary sm m-send"><span>${ev.can_send ? 'SEND' : 'OPEN IN GMAIL'}</span></button>
          ${ev.can_send ? '<button class="btn ghost sm m-gmail">EDIT IN GMAIL</button>' : ''}
          <button class="btn ghost sm m-discard">DISCARD</button>
          <span class="mail-note">${ev.can_send ? 'Nothing is sent until you approve it.' : 'Update the Google script in Settings to send directly.'}</span>
        </div>`);
      el.dataset.id = ev.id;
      $('.m-to', el).value = ev.to || '';
      $('.m-subject', el).value = ev.subject || '';
      const body = $('.m-body', el);
      body.value = ev.body || '';
      const grow = () => { body.style.height = 'auto'; body.style.height = `${Math.min(320, body.scrollHeight + 2)}px`; };
      body.addEventListener('input', grow); requestAnimationFrame(grow);
      const list = $(`#mail-cands-${ev.id}`, el);
      (ev.candidates || []).forEach((c) => { const o = document.createElement('option'); o.value = c.email; o.label = c.name || c.email; list.append(o); });
      if (!ev.to) { $('.m-to', el).classList.add('need'); setTimeout(() => $('.m-to', el).focus(), 300); }
      const fields = () => [ev.id, $('.m-to', el).value.trim(), $('.m-subject', el).value, body.value];
      const busy = (on) => $$('button', el).forEach((b) => { b.disabled = on; });
      $('.m-send', el).onclick = async () => {
        if (ev.can_send && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test($('.m-to', el).value.trim())) {
          $('.m-to', el).classList.add('need'); $('.m-to', el).focus(); toast('Enter the recipient\'s email address.', 'warn'); return;
        }
        busy(true); $('.m-send', el).classList.add('busy');
        const r = await call(ev.can_send ? 'email_send' : 'email_open_gmail', ...fields());
        $('.m-send', el).classList.remove('busy');
        if (!r || !r.ok) { busy(false); toast((r && r.error) || 'The email could not be sent.', 'error', 7000); }
      };
      const gm = $('.m-gmail', el);
      if (gm) gm.onclick = () => call('email_open_gmail', ...fields());
      $('.m-discard', el).onclick = () => call('email_discard', ev.id);
      $('.m-to', el).addEventListener('input', (e) => e.target.classList.remove('need'));
    },
    suggest() {
      if ($('.suggest-card')) return;
      const ideas = [
        'What\'s on my screen?', 'Write a bio on Lionel Messi', 'Make a presentation about the solar system', 'Email Sarah saying I\'m running late',
        'Play Bohemian Rhapsody', 'Create a protocol called Morning', 'What\'s the weather in London?', '¿Qué hora es en Tokio?',
      ];
      const el = this.card('suggest-card', '<div class="sg-kicker">TRY ASKING</div><div class="sg-list"></div>');
      call('google_status').then((g) => {
        if (g && g.configured) return;
        const b = document.createElement('button');
        b.className = 'sg-chip accent'; b.type = 'button'; b.textContent = '★ Link my Google account (docs, slides, email)';
        b.onclick = () => Google.open();
        $('.sg-list', el).prepend(b);
      });
      ideas.forEach((idea) => {
        const b = document.createElement('button');
        b.className = 'sg-chip'; b.type = 'button'; b.textContent = idea;
        b.onclick = () => { const input = $('#cmd'); input.value = idea; input.focus(); input.setSelectionRange(idea.length, idea.length); };
        $('.sg-list', el).append(b);
      });
    },
    emailStatus(ev) {
      const el = $(`.mail-card[data-id="${ev.id}"]`);
      if (!el) return;
      const label = { sent: 'SENT ✓', opened: 'OPENED IN GMAIL', discarded: 'DISCARDED', error: 'NOT SENT' }[ev.status] || ev.status;
      $('.mail-state', el).textContent = label;
      el.classList.remove('sent', 'opened', 'discarded', 'error');
      el.classList.add(ev.status);
      if (ev.status === 'error') { $$('button', el).forEach((b) => { b.disabled = false; }); toast(ev.error || 'The email could not be sent.', 'error', 7000); return; }
      $$('input, textarea, button', el).forEach((n) => { n.disabled = true; });
      if (ev.status === 'sent') call('play_sfx', 'notify');
    },
    start(id) {
      const el = this.make('jarvis pending', esc((S.persona && S.persona.display) || 'J.A.R.V.I.S.'));
      $('.msg-body', el).innerHTML = '<span>ANALYZING</span><span class="scanbar"></span>';
      this.typers.set(id, new Typer(el));
      this.count++; this.updateCount();
    },
    token(id, text) {
      const t = this.typers.get(id);
      if (t) t.push(text);
    },
    end(ev) {
      const t = this.typers.get(ev.id);
      if (!t) return;
      t.done = true;
      if (!t.started) {
        this.typers.delete(ev.id);
        t.msg.remove();
        this.count--; this.updateCount();
        return;
      }
      if (ev.interrupted) t.msg.classList.add('interrupted');
    },
    tick(dt) {
      if (!this.typers.size) return;
      let changed = false;
      for (const [id, t] of this.typers) {
        const r = t.tick(dt);
        if (r !== 'idle') changed = true;
        if (r === 'final') this.typers.delete(id);
      }
      if (changed) this.scroll();
    },
  };

  // ========================================================================= HUD status
  const STATE_TITLE = { IDLE: 'STANDING BY', LISTENING: 'LISTENING', THINKING: 'PROCESSING', SPEAKING: 'RESPONDING' };

  const Hud = {
    setState(state) {
      document.body.classList.remove('state-idle', 'state-listening', 'state-thinking', 'state-speaking');
      document.body.classList.add(`state-${state.toLowerCase()}`);
      Col.tgt = (STATE_RGB[state] || STATE_RGB.IDLE).slice();
      $('#state-title span').textContent = STATE_TITLE[state] || state;
      this.updateSub();
    },
    updateSub() {
      const st = S.state, ph = S.listenPhase, o = S.ollama;
      let sub = '';
      if (st === 'IDLE' && S.paused) {
        sub = 'Not listening · press the mic or type "start listening"';
      } else if (st === 'IDLE') {
        const wake = S.wake && S.wake.active;
        sub = !(o && o.online && o.model) ? 'Neural core offline · limited functionality'
          : wake ? `Awaiting your command, ${title()} · say “${S.wake.phrase || 'Hey Jarvis'}”` : `Awaiting your command, ${title()}`;
      }
      else if (st === 'LISTENING') {
        sub = ph === 'calibrating' ? 'Calibrating to ambient noise…'
          : ph === 'patient' ? `Take your time, ${title()} — I'm still listening`
          : ph === 'capturing' ? (Ptt.holding ? 'Receiving · release to transmit' : 'Receiving audio…')
            : `Go ahead, ${title()} — I'm listening`;
      } else if (st === 'THINKING') {
        sub = ph === 'transcribing' ? 'Decoding speech…' : S.activity ? `${S.activity}…` : `Consulting ${(o && o.model) || 'neural core'}…`;
      }
      else if (st === 'SPEAKING') sub = `Voice matrix · ${voiceShort(S.settings.voice)}`;
      $('#state-sub').textContent = sub;
    },
    chip(id, cls, text) {
      const el = $(id);
      el.classList.remove('ok', 'warn', 'bad');
      if (cls) el.classList.add(cls);
      $(`${id}-val`).textContent = text;
    },
    setDD(id, text, cls) {
      const el = $(id);
      el.textContent = text;
      el.classList.remove('ok', 'bad', 'warn');
      if (cls) el.classList.add(cls);
    },
    updateCore() {
      const o = S.ollama, c = S.core || {};
      if (!o) { this.chip('#chip-core', 'warn', 'LINKING'); return; }
      if (o.online && o.model) this.chip('#chip-core', 'ok', o.model.replace(/:latest$/, '').toUpperCase());
      else if (o.online) this.chip('#chip-core', 'warn', 'NO MODEL');
      else this.chip('#chip-core', 'bad', 'OFFLINE');
      this.setDD('#nc-link', o.online ? 'ONLINE' : 'OFFLINE', o.online ? 'ok' : 'bad');
      this.setDD('#nc-model', o.model || (o.online ? 'none installed' : '—'), o.model ? '' : 'warn');
      this.setDD('#nc-host', (o.host || '').replace(/^https?:\/\//, ''));
      this.setDD('#nc-latency', c.first_token_ms != null ? `${c.first_token_ms} ms` : '—');
      this.setDD('#nc-tps', c.tokens_per_sec != null ? `${c.tokens_per_sec} tok/s` : '—');
      const turns = c.memory_turns || 0;
      this.setDD('#nc-memory', `${turns} exchange${turns === 1 ? '' : 's'}`);
      $('#log-memory').textContent = `memory: ${turns} exchange${turns === 1 ? '' : 's'}`;
      this.updateSub();
    },
    updateAudio() {
      const set = S.settings, mic = S.mic || {}, audio = S.audio || {};
      const voiceOn = set.voice_enabled !== false;
      if (!voiceOn) this.chip('#chip-voice', 'warn', 'MUTED');
      else if (!audio.available) this.chip('#chip-voice', 'bad', 'NO AUDIO');
      else if (!S.voiceOk) this.chip('#chip-voice', 'bad', 'OFFLINE');
      else this.chip('#chip-voice', 'ok', voiceShort(set.voice).toUpperCase());
      const engine = (mic.engine || '').replace(/ \(.*\)/, '').toUpperCase();
      const wake = S.wake || {};
      if (mic.available && S.paused) this.chip('#chip-mic', 'warn', 'PAUSED');
      else if (mic.available && wake.active) this.chip('#chip-mic', 'ok', (wake.phrase || 'Hey Jarvis').toUpperCase());
      else if (mic.available) this.chip('#chip-mic', 'ok', engine || 'READY');
      else this.chip('#chip-mic', 'bad', 'NO DEVICE');
      $('#wake-hint').classList.toggle('hidden', !(mic.available && wake.active));
      const phrases = (wake.phrases && wake.phrases.length ? wake.phrases : [wake.phrase || 'Hey Jarvis']).map((x) => `“${x}”`).join(' or ');
      $('#chip-mic').title = wake.active ? `Listening for ${phrases}${wake.learning ? ` · learning “Hey ${wake.learning.name}” (${Math.round(wake.learning.progress * 100)}%)` : ''}`
        : `Wake word off${wake.reason ? `: ${wake.reason}` : ''}`;
      $('#wake-hint-phrase').textContent = `“${wake.phrase || 'Hey Jarvis'}”`;
      $('#mic-btn').classList.toggle('disabled', !mic.available);
      $('#mic-btn').classList.toggle('paused', !!S.paused);
      $('#mic-btn').title = S.paused ? 'Microphone paused: click to listen again' : 'Click to talk · hold for push-to-talk [Space]';
      if (S.paused) $('#wake-hint').classList.add('hidden');

      this.setDD('#am-voice', `${voiceShort(set.voice)} · ${(set.voice || '').split('-').slice(0, 2).join('-')}`, voiceOn && S.voiceOk ? '' : 'warn');
      this.setDD('#am-output', audio.available ? (audio.output ? 'ONLINE' : 'VIRTUAL (no device)') : 'OFFLINE', audio.output ? 'ok' : 'warn');
      this.setDD('#am-mic', mic.available ? (mic.device || 'default') : (mic.reason || 'not detected'), mic.available ? '' : 'bad');
      this.setDD('#am-stt', mic.engine || '—');
    },
    applySettings() {
      if (this.appliedTheme !== (S.settings.theme || 'arc')) { this.appliedTheme = S.settings.theme || 'arc'; applyTheme(this.appliedTheme); }
      $('#cmd').placeholder = `Type a command, ${title()}…`;
      this.updateAudio();
      this.updateSub();
    },
    clock() {
      const d = new Date();
      $('#clock-time').innerHTML = fmtTime(d).replace(/:/g, '<span class="colon">:</span>');
      $('#clock-date').textContent = d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' }).replace(/,/g, '').toUpperCase();
    },
  };

  // ========================================================================= telemetry
  const Telemetry = {
    hist: { cpu: [], ram: [] }, N: 60, nums: {},
    start() { this.poll(); setInterval(() => this.poll(), 1000); },
    async poll() { const s = await call('get_system_stats'); if (s) this.render(s); },
    gauge(sel, v) {
      const el = $(sel);
      $('.g-val', el).style.strokeDashoffset = (295.3 * (1 - clamp(v, 0, 100) / 100)).toFixed(1);
      el.classList.toggle('hot', v >= 70 && v < 90);
      el.classList.toggle('crit', v >= 90);
      const num = $('.g-num', el), from = this.nums[sel] || 0, t0 = performance.now();
      this.nums[sel] = v;
      const step = (now) => {
        const k = clamp((now - t0) / 700, 0, 1), e = 1 - Math.pow(1 - k, 3);
        num.textContent = Math.round(lerp(from, v, e));
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    rate(kb) { return kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB/s` : `${kb.toFixed(kb < 10 ? 1 : 0)} KB/s`; },
    uptime(s) {
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      const hh = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
      return d ? `${d}d ${hh}` : hh;
    },
    render(s) {
      this.gauge('#g-cpu', s.cpu);
      this.gauge('#g-ram', s.ram);
      const cores = $('#cores'), pc = s.per_core || [];
      if (cores.children.length !== pc.length) cores.innerHTML = pc.map(() => '<i></i>').join('');
      pc.forEach((v, i) => { const b = cores.children[i]; b.style.height = `${Math.max(3, v)}%`; b.classList.toggle('hot', v > 80); });
      $('#st-ram').textContent = `${s.ram_used_gb} / ${s.ram_total_gb} GB`;
      $('#st-disk').textContent = s.disk != null ? `${s.disk}% used` : '—';
      $('#st-net').textContent = `↑ ${this.rate(s.net_up_kbps)}  ↓ ${this.rate(s.net_down_kbps)}`;
      $('#st-proc').textContent = s.processes;
      $('#st-uptime').textContent = this.uptime(s.uptime_s);
      if (s.battery) {
        $('#row-battery').hidden = false;
        $('#st-battery').textContent = `${s.battery.percent}% ${s.battery.plugged ? '· AC' : '· battery'}`;
      }
      for (const k of ['cpu', 'ram']) { this.hist[k].push(s[k]); if (this.hist[k].length > this.N) this.hist[k].shift(); }
      this.spark();
    },
    spark() {
      const c = $('#spark'), dpr = Math.min(2, window.devicePixelRatio || 1), w = c.clientWidth, h = c.clientHeight;
      if (!w || !h) return;
      if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
      const ctx = c.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = `rgba(${themeRGB},0.07)`;
      for (let y = h / 4; y < h; y += h / 4) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
      const draw = (data, rgb) => {
        if (data.length < 2) return;
        const step = w / (this.N - 1), x0 = w - (data.length - 1) * step;
        const pts = data.map((v, i) => [x0 + i * step, h - 3 - (v / 100) * (h - 8)]);
        ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.strokeStyle = `rgba(${rgb},0.95)`; ctx.lineWidth = 1.4; ctx.stroke();
        ctx.lineTo(w, h); ctx.lineTo(x0, h); ctx.closePath();
        const g = ctx.createLinearGradient(0, 0, 0, h);
        g.addColorStop(0, `rgba(${rgb},0.25)`); g.addColorStop(1, `rgba(${rgb},0)`);
        ctx.fillStyle = g; ctx.fill();
      };
      draw(this.hist.ram, '255,182,72');
      draw(this.hist.cpu, themeRGB);
    },
  };

  // ========================================================================= push-to-talk
  const Ptt = {
    pressing: false, holding: false, stopping: false, timer: 0,
    down() {
      if (this.pressing) return;
      this.pressing = true;
      if (S.state === 'LISTENING') { this.stopping = true; call('stop_listening'); return; }
      this.stopping = false;
      call('start_listening');
      this.timer = setTimeout(() => {
        if (!this.pressing) return;
        this.holding = true;
        $('#mic-btn').classList.add('holding');
        call('hold_listening');
        Hud.updateSub();
      }, 420);
    },
    up() {
      if (!this.pressing) return;
      this.pressing = false;
      clearTimeout(this.timer);
      if (this.stopping) return;
      if (this.holding) {
        this.holding = false;
        $('#mic-btn').classList.remove('holding');
        call('stop_listening');
      }
    },
  };

  // ========================================================================= ollama overlay
  const Ollama = {
    visible: false, mode: null, dismissed: null, countdown: 10, timer: 0, pulling: false,
    update(st) {
      const { type, ...status } = st;
      S.ollama = status;
      Hud.updateCore();
      Settings.fillModels();
      const mode = !status.online ? 'offline' : !status.model ? 'nomodel' : 'ok';
      if (mode === 'ok') {
        if (this.visible) {
          this.hide();
          toast(`Neural core online · ${status.model}`, 'ok');
          Chat.system(`Neural core linked: ${status.model}`, 'ok');
        }
        this.dismissed = null;
        return;
      }
      if (!S.booted || this.dismissed === mode) return;
      this.show(mode, status);
    },
    show(mode, st) {
      this.mode = mode;
      const el = $('#ollama');
      $('#ol-host').textContent = st.host || 'http://localhost:11434';
      const model = st.suggested_model || 'llama3.2';
      $('#ol-cmd').textContent = `ollama run ${model}`;
      $('#ol-pull-cmd').textContent = `ollama pull ${model}`;
      $('#ol-model-name').textContent = model;
      $('#ol-start').classList.toggle('hidden', !(mode === 'offline' && st.executable));
      const offline = mode === 'offline';
      $('#ol-kicker').textContent = offline ? 'CRITICAL · AI-02 · LINK FAILURE' : 'WARNING · AI-02 · NO MODEL';
      $('#ol-title').textContent = offline ? 'Neural core offline' : 'Neural model missing';
      $('#ol-lead').classList.toggle('hidden', !offline);
      $('#ol-steps-offline').classList.toggle('hidden', !offline);
      $('#ol-steps-model').classList.toggle('hidden', offline);
      $('#ol-pull').classList.toggle('hidden', offline || this.pulling);
      $('#ol-retry span').textContent = offline ? 'RETRY CONNECTION' : 'RESCAN MODELS';
      $('#ol-foot').classList.toggle('hidden', this.pulling);
      if (!this.visible) {
        el.classList.remove('hidden');
        this.visible = true;
        call('play_sfx', 'error');
      }
      clearInterval(this.timer);
      if (!this.pulling) {
        this.countdown = 10;
        $('#ol-countdown').textContent = this.countdown;
        this.timer = setInterval(() => {
          this.countdown -= 1;
          if (this.countdown <= 0) { this.countdown = 10; if (!this.pulling) this.retry(true); }
          $('#ol-countdown').textContent = this.countdown;
        }, 1000);
      }
    },
    hide() {
      $('#ollama').classList.add('hidden');
      this.visible = false;
      clearInterval(this.timer);
    },
    async retry(silent) {
      const btn = $('#ol-retry');
      btn.classList.add('busy'); btn.disabled = true;
      const st = await call('check_ollama');
      setTimeout(() => { btn.classList.remove('busy'); btn.disabled = false; }, 350);
      if (st) this.update(st);
      if (!silent && st && !st.online) toast('Still no response from Ollama.', 'warn');
    },
    async start() {
      const r = await call('start_ollama');
      if (r && r.started) { toast('Starting Ollama…', 'info'); $('#ol-foot').textContent = 'Waiting for Ollama to come online…'; }
      else toast((r && r.reason) || 'Could not start Ollama.', 'error');
    },
    async pull() {
      const model = (S.ollama && S.ollama.suggested_model) || 'llama3.2';
      const r = await call('pull_model', model);
      if (!r || !r.ok) { toast((r && r.error) || 'Download failed to start.', 'error'); return; }
      this.pulling = true;
      $('#pull-box').classList.remove('hidden');
      $('#ol-pull').classList.add('hidden');
      $('#ol-cancel-pull').classList.remove('hidden');
    },
    progress(ev) {
      $('#pull-box').classList.remove('hidden');
      $('#pull-status').textContent = ev.status || '…';
      if (ev.percent != null) {
        $('#pull-pct').textContent = `${ev.percent.toFixed(0)}%`;
        $('#pull-progress').style.width = `${ev.percent}%`;
      }
      if (ev.total) $('#pull-bytes').textContent = `${(ev.completed / 1e9).toFixed(2)} / ${(ev.total / 1e9).toFixed(2)} GB`;
    },
    pullDone(ev) {
      this.pulling = false;
      $('#ol-cancel-pull').classList.add('hidden');
      if (ev.ok) { toast(`Model ${ev.model} installed.`, 'ok'); $('#pull-status').textContent = 'Installed'; }
      else {
        toast(ev.error || 'Download failed.', 'error');
        $('#pull-box').classList.add('hidden');
        if (this.visible && this.mode === 'nomodel') $('#ol-pull').classList.remove('hidden');
      }
    },
    dismiss() {
      this.dismissed = this.mode;
      this.hide();
      Chat.system('Running without a language model. Start Ollama and use Settings ▸ Neural core to reconnect.', 'error');
    },
  };

  // ========================================================================= settings drawer
  const Settings = {
    el: null,
    init() {
      this.el = $('#settings');
      $$('[data-key]', this.el).forEach((input) => {
        const key = input.dataset.key;
        const live = () => this.output(input);
        input.addEventListener('input', live);
        input.addEventListener('change', () => {
          if (key === 'theme') { this.saveTheme(input.value); return; }
          let value;
          if (input.type === 'checkbox') value = input.checked;
          else if (input.type === 'range') value = parseFloat(input.value);
          else value = input.value;
          this.save({ [key]: value }, key);
        });
      });
      $('#settings-close').onclick = () => this.close();
      $('#set-theme-color').onchange = (e) => this.saveTheme(e.target.value.toUpperCase());
      $('#set-model-refresh').onclick = async () => { const st = await call('check_ollama'); if (st) Ollama.update(st); toast('Model list refreshed.', 'info'); };
      $('#set-voice-preview').onclick = () => call('preview_voice', $('#set-voice').value);
      $('#set-clear-memory').onclick = async () => { await call('clear_memory'); toast('Conversation cleared. Long-term memories are kept.', 'ok'); Chat.system('Conversation cleared (long-term memory is untouched).'); };
    },
    output(input) {
      const map = { 'set-temp': ['#out-temp', (v) => (+v).toFixed(2)], 'set-rate': ['#out-rate', (v) => `${v > 0 ? '+' : ''}${v}%`],
        'set-pitch': ['#out-pitch', (v) => `${v > 0 ? '+' : ''}${v} Hz`], 'set-pause': ['#out-pause', (v) => `${(+v).toFixed(1)} s`],
        'set-sfx': ['#out-sfx', (v) => `${Math.round(v * 100)}%`],
        'set-patience': ['#out-patience', (v) => (+v === 0 ? 'off' : `+${(+v).toFixed(1)} s`)],
        'set-watch': ['#out-watch', (v) => `${(+v).toFixed(1)} s`],
        'set-wake': ['#out-wake', (v) => (v < 0.34 ? 'strict' : v < 0.67 ? 'balanced' : 'sensitive')] };
      const m = map[input.id];
      if (m) $(m[0]).textContent = m[1](input.value);
    },
    async saveTheme(value) {
      if (value === 'custom') value = $('#set-theme-color').value.toUpperCase();
      $('#set-theme-custom-row').classList.toggle('hidden', !/^#/.test(value));
      if (S.settings.persona_theme !== false && S.persona) {  // colours follow the personality: this becomes its colour
        const r = await call('persona_color', S.persona.id, value);
        if (r && r.ok) { Personas.patch(r.persona); toast(`${S.persona.name}'s colours updated.`, 'ok', 2000); }
        return;
      }
      this.save({ theme: value }, 'theme');
    },
    fill() {
      const set = S.settings;
      $$('[data-key]', this.el).forEach((input) => {
        const v = set[input.dataset.key];
        if (v === undefined || input === document.activeElement) return;
        if (input.type === 'checkbox') input.checked = !!v;
        else if (input.tagName !== 'SELECT' || [...input.options].some((o) => o.value === String(v))) input.value = v;
        this.output(input);
      });
      const pick = $('#set-persona');
      if (S.personas && S.personas.length) {
        pick.innerHTML = S.personas.map((p) => `<option value="${esc(p.id)}">${esc(p.name === 'Jarvis' ? 'J.A.R.V.I.S.' : p.name)}: ${esc(p.tagline)}</option>`).join('');
        pick.value = set.persona;
      }
      const custom = /^#/.test(set.theme || '');
      if (custom) { $('[data-key=theme]', this.el).value = 'custom'; $('#set-theme-color').value = set.theme.toLowerCase(); }
      $('#set-theme-custom-row').classList.toggle('hidden', !custom);
      this.fillModels();
    },
    fillModels() {
      const sel = $('#set-model');
      if (!sel) return;
      const models = (S.ollama && S.ollama.models) || [];
      const want = S.settings.model || '';
      const active = S.ollama && S.ollama.model;
      const opts = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`);
      if (want && !models.some((m) => m === want || m === `${want}:latest`)) opts.unshift(`<option value="${esc(want)}">${esc(want)} (not installed)</option>`);
      sel.innerHTML = opts.join('') || '<option value="">No models found</option>';
      sel.value = active && models.includes(active) ? active : want;
    },
    async fillVoices() {
      if (!S.voices) S.voices = (await call('list_voices')) || [];
      const sel = $('#set-voice');
      const list = S.voices.length ? S.voices : [{ id: S.settings.voice, label: S.settings.voice }];
      sel.innerHTML = list.map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join('');
      if (S.settings.voice && !list.some((v) => v.id === S.settings.voice)) {
        sel.insertAdjacentHTML('afterbegin', `<option value="${esc(S.settings.voice)}">${esc(S.settings.voice)}</option>`);
      }
      sel.value = S.settings.voice;
    },
    async save(changes, key) {
      const r = await call('save_settings', changes);
      if (!r) return;
      if (!r.ok) { toast(r.error || 'Invalid setting.', 'error'); this.fill(); return; }
      S.settings = r.settings;
      Hud.applySettings();
      if (key === 'frameless') toast('Window style changes on next launch.', 'info');
      if (key === 'voice') toast(`Voice set to ${voiceShort(r.settings.voice)}.`, 'ok', 2500);
    },
    async browsers() {
      const found = await call('installed_browsers');
      if (!found) return;
      [...$('#set-browser').options].forEach((o) => {
        if (o.value === 'default') return;
        if (!o.dataset.label) o.dataset.label = o.textContent;
        o.textContent = found[o.value] ? o.dataset.label : `${o.dataset.label} (not found)`;
        o.disabled = !found[o.value];
      });
    },
    open() { this.fill(); this.fillVoices(); this.browsers(); Google.refresh(); call('vision_status').then((st) => st && Vision.update(st)); this.el.classList.add('open'); this.el.setAttribute('aria-hidden', 'false'); },
    close() { this.el.classList.remove('open'); this.el.setAttribute('aria-hidden', 'true'); },
    toggle() { this.el.classList.contains('open') ? this.close() : this.open(); },
  };

  // ========================================================================= vision
  const Vision = {
    st: null,
    update(st) {
      this.st = st;
      const watching = !!st.watching;
      const label = watching ? 'WATCHING' : st.pulling ? 'DOWNLOADING' : st.model ? st.model.split(':')[0].toUpperCase().slice(0, 14)
        : st.ocr ? 'TEXT ONLY' : 'OFF';
      Hud.chip('#chip-vision', watching ? 'warn' : st.model ? 'ok' : st.ocr ? 'warn' : '', label);
      $('#chip-vision').classList.toggle('watching', watching);
      $('#watch-strip').classList.toggle('hidden', !watching);
      $('#watch-label').textContent = watching ? (st.watch_label || 'your screen') : '';
      $('#look-btn').classList.toggle('ready', !!(st.model || st.ocr));
      this.fillSettings();
    },
    fillSettings() {
      const st = this.st;
      if (!st || !$('#set-vision-model')) return;
      const sel = $('#set-vision-model');
      const models = st.models || [];
      sel.innerHTML = '';
      const auto = document.createElement('option');
      auto.value = ''; auto.textContent = models.length ? `Automatic (${models[0]})` : 'None installed yet';
      sel.append(auto);
      models.forEach((m) => { const o = document.createElement('option'); o.value = m; o.textContent = m; sel.append(o); });
      sel.value = models.includes(S.settings.vision_model) ? S.settings.vision_model : '';
      const parts = [];
      parts.push(st.model ? `Eyes online: ${st.model} (runs on your GPU through Ollama).` : 'No vision model yet: JARVIS can only read the text on screen.');
      parts.push(st.ocr ? `Text reading: ${st.ocr_engine}.` : `Text reading unavailable${st.ocr_error ? ` (${st.ocr_error})` : ''}.`);
      $('#vision-status').textContent = parts.join(' ');
      $('#vision-status').classList.toggle('ok', !!st.model);
      $('#vision-status').classList.toggle('warn', !st.model);
      const dl = $('#vision-download');
      dl.textContent = st.pulling ? 'DOWNLOADING…' : st.model ? 'ADD QWEN2.5-VL' : 'DOWNLOAD (6 GB)';
      dl.disabled = !!st.pulling;
      dl.classList.toggle('hidden', !!st.model && (st.models || []).some((m) => m.startsWith('qwen2.5vl')));
    },
    progress(ev) {
      $('#vision-pull').classList.remove('hidden');
      const pct = ev.percent != null ? ev.percent : 0;
      $('#vision-pull-bar').style.width = `${pct}%`;
      $('#vision-pull-text').textContent = `${ev.model}: ${ev.status || '…'}${ev.percent != null ? ` · ${Math.round(pct)}%` : ''}`;
      Hud.chip('#chip-vision', 'warn', `DL ${Math.round(pct)}%`);
    },
    done(ev) {
      $('#vision-pull-text').textContent = ev.ok ? `${ev.model} installed.` : (ev.error || 'Download failed.');
      if (ev.ok) { toast('Vision model installed: JARVIS can see.', 'ok', 6000); Chat.system(`Vision model ${ev.model} installed.`, 'ok'); }
      else toast(ev.error || 'Vision download failed.', 'error', 8000);
      setTimeout(() => $('#vision-pull').classList.add('hidden'), 4000);
      call('vision_status').then((st) => st && this.update(st));
    },
    seen(ev) {
      const el = Chat.card('seen-card', `<img alt="What JARVIS saw"><div class="seen-meta"><div class="dc-kicker">SEEN${ev.ocr ? ' · TEXT READ' : ''}</div>
        <div class="seen-title"></div><div class="seen-model"></div></div>`);
      $('img', el).src = ev.image;
      $('.seen-title', el).textContent = ev.title || 'your screen';
      $('.seen-model', el).textContent = ev.model ? `through ${ev.model}` : 'text only (no vision model)';
      $('img', el).onclick = () => Reader.open({ doc_kind: 'image', title: ev.title || 'What JARVIS saw', image: ev.image });
    },
    confirm(ev) {
      const el = Chat.card('act-card', `<div class="mc-headline"><span class="mail-kicker">${ev.reason === 'risky' ? 'PLEASE CONFIRM' : 'IS THIS THE ONE?'}</span>
        <span class="mail-state">WAITING FOR YOU</span></div>${ev.image ? '<img alt="Where JARVIS will act">' : ''}<div class="act-label"></div>
        <div class="mail-actions"><button class="btn primary sm a-yes"><span>DO IT</span></button><button class="btn ghost sm a-no">CANCEL</button>
        <span class="mail-note">Or just say “yes” / “no”.</span></div>`);
      el.dataset.id = ev.id;
      if (ev.image) $('img', el).src = ev.image;
      $('.act-label', el).textContent = `${ev.label}${ev.where ? ` · ${ev.where}` : ''}`;
      const go = async (yes) => {
        $$('button', el).forEach((b) => { b.disabled = true; });
        const r = await call('vision_confirm', ev.id, yes);
        if (!r || !r.ok) { toast((r && r.error) || 'That action has expired.', 'warn'); this.status({ id: ev.id, status: 'expired' }); }
      };
      $('.a-yes', el).onclick = () => go(true);
      $('.a-no', el).onclick = () => go(false);
    },
    status(ev) {
      const el = $(`.act-card[data-id="${ev.id}"]`);
      if (!el) return;
      $('.mail-state', el).textContent = { done: 'DONE ✓', cancelled: 'CANCELLED', failed: 'FAILED', expired: 'EXPIRED' }[ev.status] || ev.status;
      el.classList.add(ev.status);
      $$('button', el).forEach((b) => { b.disabled = true; });
    },
  };

  // ========================================================================= timers
  const Timers = {
    list: [],
    set(timers) {
      const now = Date.now();
      this.list = (timers || []).map((t) => ({ ...t, due: now + t.left * 1000 }));
      this.render();
    },
    fmt(sec) {
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
    },
    render() {
      const strip = $('#timer-strip');
      if (!this.list.length) { strip.textContent = ''; return; }
      const have = new Map($$('.timer-pill', strip).map((el) => [el.dataset.id, el]));
      this.list.forEach((t) => {
        let el = have.get(String(t.id));
        if (!el) {
          el = document.createElement('div');
          el.className = 'timer-pill'; el.dataset.id = t.id;
          el.innerHTML = '<svg viewBox="0 0 24 24"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2M9 3h6"/></svg><b></b><span></span><i></i>';
          $('span', el).textContent = t.label || 'timer';
          strip.append(el);
        }
        have.delete(String(t.id));
      });
      have.forEach((el) => el.remove());
      this.tick();
    },
    tick() {
      const now = Date.now();
      this.list.forEach((t) => {
        const el = $(`.timer-pill[data-id="${t.id}"]`);
        if (!el) return;
        const left = Math.max(0, Math.round((t.due - now) / 1000));
        $('b', el).textContent = this.fmt(left);
        $('i', el).style.width = `${t.total ? Math.max(0, Math.min(100, (left / t.total) * 100)) : 0}%`;
        el.classList.toggle('soon', left <= 10);
      });
    },
  };

  // ========================================================================= document reader
  const Reader = {
    data: null,
    open(ev) {
      this.data = ev;
      const names = { doc: 'GOOGLE DOC', slides: 'GOOGLE SLIDES', sheet: 'GOOGLE SHEET', image: 'WHAT JARVIS SAW' };
      $('#rd-kicker').textContent = names[ev.doc_kind] || 'GOOGLE FILE';
      $('#rd-title').textContent = ev.title || 'Untitled';
      const body = $('#rd-body');
      body.textContent = '';
      body.scrollTop = 0;
      if (ev.doc_kind === 'image') {
        const img = document.createElement('img');
        img.src = ev.image; img.className = 'rd-image';
        body.append(img);
      } else if (ev.doc_kind === 'sheet') {
        const table = document.createElement('table');
        (ev.rows || []).forEach((row, i) => {
          const tr = document.createElement('tr');
          row.forEach((cell) => { const td = document.createElement(i === 0 ? 'th' : 'td'); td.textContent = cell; tr.append(td); });
          table.append(tr);
        });
        body.append(table);
        if (ev.truncated) { const p = document.createElement('p'); p.className = 'rd-note'; p.textContent = 'Showing the first rows only.'; body.append(p); }
        if (!(ev.rows || []).length) body.textContent = 'This sheet is empty.';
      } else {
        const md = String(ev.markdown || '').trim();
        if (md) body.innerHTML = renderMarkdown(md); else body.textContent = 'This file is empty.';
        if (ev.truncated) { const p = document.createElement('p'); p.className = 'rd-note'; p.textContent = 'Showing the beginning of a long document.'; body.append(p); }
      }
      $('#rd-open').style.display = ev.url ? '' : 'none';
      $('#reader').classList.remove('hidden');
      call('play_sfx', 'notify');
    },
    close() { $('#reader').classList.add('hidden'); },
    text() {
      const d = this.data || {};
      return d.doc_kind === 'sheet' ? (d.rows || []).map((r) => r.join('\t')).join('\n') : String(d.markdown || '');
    },
    init() {
      $('#rd-close').onclick = $('#rd-done').onclick = () => this.close();
      $('#rd-open').onclick = () => this.data && call('open_url', this.data.url);
      $('#rd-copy').onclick = async () => {
        try { await navigator.clipboard.writeText(this.text()); toast('Copied.', 'ok', 1800); }
        catch { toast('Could not copy.', 'error'); }
      };
      $('#reader').addEventListener('click', (e) => { if (e.target.id === 'reader') this.close(); });
    },
  };

  // ========================================================================= personalities
  const Personas = {
    el: null,
    apply(p) {
      if (!p) return;
      S.persona = p;
      if (Array.isArray(S.personas)) S.personas = S.personas.map((x) => (x.id === p.id ? { ...x, ...p, active: true } : { ...x, active: false }));
      $('#brand-name').textContent = p.display;
      $('#brand-tag').textContent = p.id === 'jarvis' ? 'Just A Rather Very Intelligent System' : p.tagline;
      document.body.dataset.persona = p.id;
      document.title = p.display;
      if (this.el && !this.el.classList.contains('hidden')) this.render();
    },
    setList(list) {
      if (!Array.isArray(list)) return;
      S.personas = list;
      const active = list.find((p) => p.active);
      if (active) this.apply(active);
      else if (this.el && !this.el.classList.contains('hidden')) this.render();
    },
    swatch(theme) { return `rgb(${themeOf(theme).state.IDLE.join(',')})`; },
    wakeLine(p) {
      const w = p.wake || {};
      const phrase = esc(w.phrase || `Hey ${p.name}`);
      if (p.id === 'jarvis') return `<span class="ok">“${phrase}” · built in</span>`;
      if (w.state === 'learning' || w.state === 'queued') {
        const pct = Math.round((w.progress || 0) * 100);
        return `<span>Learning “${phrase}”… ${pct}%</span><i class="ps-bar"><b style="width:${pct}%"></b></i><small>${esc(w.label || 'Waiting')}</small>`;
      }
      if (w.state === 'ready') {
        const m = w.metrics || {};
        const pct = m.held_out_recall != null ? ` · caught ${Math.round(m.held_out_recall * 100)}% of new voices` : '';
        return `<span class="ok">“${phrase}” ready${w.user_samples ? ' · tuned to your voice' : pct}</span>`;
      }
      if (w.state === 'error') return `<span class="bad" title="${esc(w.error || '')}">Couldn't learn “${phrase}”</span>`;
      return `<span>“${phrase}” not learned yet</span>`;
    },
    render() {
      const grid = $('#ps-grid');
      grid.innerHTML = '';
      (S.personas || []).forEach((p) => {
        const active = S.persona && S.persona.id === p.id;
        const w = p.wake || {};
        const card = document.createElement('div');
        card.className = `ps-item${active ? ' active' : ''}${p.custom ? ' custom' : ''}`;
        card.dataset.id = p.id;
        card.style.setProperty('--ps', this.swatch(p.color || p.theme));
        const learnLabel = w.state === 'ready' ? 'RELEARN' : w.state === 'error' ? 'TRY AGAIN' : 'LEARN NAME';
        card.innerHTML = `<div class="ps-top"><div class="ps-face"><i></i><i></i><i></i></div>
            <label class="ps-color" title="Change ${esc(p.name)}'s colours"><input type="color"><span>COLOUR</span></label></div>
          <div class="ps-name"></div><div class="ps-tag"></div><p class="ps-desc"></p>
          <div class="ps-meta"><span class="ps-voice"></span><span class="ps-addr"></span></div>
          <div class="ps-wake">${this.wakeLine(p)}</div>
          ${p.id === 'jarvis' ? '' : `<div class="ps-wake-acts">
            ${w.state === 'learning' || w.state === 'queued' ? '' : `<button class="ps-link" data-act="learn">${learnLabel}</button>`}
            <button class="ps-link" data-act="teach">TEACH MY VOICE</button></div>`}
          <div class="ps-actions"><button class="btn sm ghost" data-act="hear">HEAR</button>
            ${p.custom ? '<button class="btn sm ghost" data-act="edit">EDIT</button>' : ''}
            <button class="btn sm ${active ? '' : 'primary'}" data-act="pick"${active ? ' disabled' : ''}><span>${active ? 'ACTIVE' : 'SWITCH'}</span></button></div>`;
        $('.ps-name', card).textContent = p.display;
        $('.ps-tag', card).textContent = p.tagline;
        $('.ps-desc', card).textContent = p.description;
        $('.ps-voice', card).textContent = `Voice · ${voiceShort(p.voice)}`;
        $('.ps-addr', card).textContent = `Calls you “${p.address}”`;
        const color = $('.ps-color input', card);
        color.value = toHex(p.color || p.theme);
        color.oninput = () => card.style.setProperty('--ps', color.value);
        color.onchange = async () => {
          const r = await call('persona_color', p.id, color.value);
          if (r && r.ok) { this.patch(r.persona); toast(`${p.name}'s colours updated.`, 'ok', 2000); }
        };
        $('[data-act=hear]', card).onclick = () => call('persona_preview', p.id);
        $('[data-act=pick]', card).onclick = async () => {
          const r = await call('persona_set', p.id);
          if (r && r.ok) { this.apply(r.persona); call('play_sfx', 'activate'); setTimeout(() => this.close(), 450); }
          else toast((r && r.error) || 'Could not switch.', 'error');
        };
        const edit = $('[data-act=edit]', card);
        if (edit) edit.onclick = () => PersonaEditor.open(p);
        const learn = $('[data-act=learn]', card);
        if (learn) learn.onclick = async () => { const r = await call('wake_learn', p.id); if (r && !r.ok) toast(r.error, 'warn'); };
        const teach = $('[data-act=teach]', card);
        if (teach) teach.onclick = () => WakeTeach.open(p);
        grid.append(card);
      });
      const add = document.createElement('button');
      add.className = 'ps-item ps-new';
      add.innerHTML = '<b>+</b><span>CREATE YOUR OWN</span><small>Name, personality, voice and colour</small>';
      add.onclick = () => PersonaEditor.open(null);
      grid.append(add);
      $('#ps-theme').checked = S.settings.persona_theme !== false;
    },
    patch(p) {
      S.personas = (S.personas || []).map((x) => (x.id === p.id ? { ...x, ...p } : x));
      if (S.persona && S.persona.id === p.id) S.persona = { ...S.persona, ...p };
      if (this.el && !this.el.classList.contains('hidden')) this.render();
    },
    wake(ev) {
      const p = (S.personas || []).find((x) => x.name.toLowerCase() === String(ev.name || '').toLowerCase());
      if (!p) return;
      const before = (p.wake || {}).state;
      const { type, ...w } = ev;
      p.wake = w;
      if (this.el && !this.el.classList.contains('hidden')) {
        const line = $(`.ps-item[data-id="${p.id}"] .ps-wake`, this.el);
        if (line && before === w.state && w.state === 'learning') line.innerHTML = this.wakeLine(p);
        else this.render();
      }
      if (before !== 'ready' && w.state === 'ready') toast(`Learned “${w.phrase}”${w.user_samples ? ' with your voice' : ''}. Try saying it!`, 'ok', 5000);
      if (w.state === 'error' && before !== 'error') toast(`Couldn't learn “${w.phrase}”: ${w.error}`, 'warn', 7000);
      WakeTeach.update(p);
    },
    async open() {
      const list = await call('persona_list');
      if (Array.isArray(list)) S.personas = list;
      this.render();
      this.el.classList.remove('hidden');
      call('play_sfx', 'click');
    },
    close() { this.el.classList.add('hidden'); },
    init() {
      this.el = $('#personas');
      $('#ps-close').onclick = () => this.close();
      this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
      $('#ps-theme').onchange = (e) => Settings.save({ persona_theme: e.target.checked }, 'persona_theme');
      $('#btn-persona').onclick = () => this.open();
      $('#brand').onclick = () => S.booted && this.open();
      $('#set-open-personas').onclick = () => { Settings.close(); this.open(); };
    },
  };

  // ========================================================================= create / edit a personality
  const PRESET_COLORS = ['#2AD4FF', '#FF8AAA', '#FF7040', '#48FF96', '#AC7AFF', '#FFD24A', '#4A7BFF', '#FF4A5C', '#E0E6F0'];
  const PERSONA_IDEAS = [
    ['Pirate', 'A swashbuckling pirate captain who speaks in pirate slang, loves treasure hunts and turns every task into an adventure.'],
    ['Coach', 'An energetic fitness coach who motivates me, keeps me accountable and celebrates every small win.'],
    ['Scientist', 'A curious, precise scientist who explains how things work with fun facts and simple experiments.'],
    ['Grandma', 'A warm, wise grandmother who gives gentle advice, tells little stories and always asks if I have eaten.'],
  ];
  const PersonaEditor = {
    el: null, editing: null, color: PRESET_COLORS[0],
    async fillVoices(selected) {
      if (!S.voices) S.voices = (await call('list_voices')) || [];
      const list = S.voices.length ? S.voices : [{ id: 'en-US-AvaNeural', label: 'Ava · en-US · Female', gender: 'Female' }, { id: 'en-GB-RyanNeural', label: 'Ryan · en-GB · Male', gender: 'Male' }];
      $('#pe-voice').innerHTML = list.map((v) => `<option value="${esc(v.id)}" data-gender="${esc(v.gender || '')}">${esc(v.label)}</option>`).join('');
      $('#pe-voice').value = selected && list.some((v) => v.id === selected) ? selected : list[0].id;
    },
    swatches() {
      const box = $('#pe-colors');
      box.innerHTML = '';
      PRESET_COLORS.forEach((c) => {
        const b = document.createElement('button');
        b.type = 'button'; b.className = `pe-sw${c.toUpperCase() === this.color.toUpperCase() ? ' on' : ''}`;
        b.style.background = c; b.title = c;
        b.onclick = () => { this.color = c; this.swatches(); };
        box.append(b);
      });
      const custom = document.createElement('label');
      custom.className = `pe-sw pe-custom${PRESET_COLORS.includes(this.color.toUpperCase()) ? '' : ' on'}`;
      custom.title = 'Any colour';
      custom.innerHTML = '<input type="color">';
      $('input', custom).value = this.color.toLowerCase();
      $('input', custom).onchange = (e) => { this.color = e.target.value.toUpperCase(); this.swatches(); };
      box.append(custom);
    },
    async open(p) {
      this.editing = p;
      $('#pe-title').textContent = p ? `Edit ${p.name}` : 'Create a personality';
      const saved = (p && p.saved) || {};
      $('#pe-name').value = saved.name || '';
      $('#pe-desc').value = saved.description || '';
      $('#pe-address').value = saved.address || '';
      this.color = (saved.color || PRESET_COLORS[Math.floor(Math.random() * PRESET_COLORS.length)]).toUpperCase();
      $('#pe-delete').classList.toggle('hidden', !p);
      $('#pe-error').textContent = '';
      this.count(); this.swatches(); this.wakeName();
      $('#pe-ideas').innerHTML = p ? '' : '<span>Ideas:</span>' + PERSONA_IDEAS.map(([n], i) => `<button type="button" data-i="${i}">${n}</button>`).join('');
      $$('#pe-ideas button').forEach((b) => (b.onclick = () => {
        const [n, d] = PERSONA_IDEAS[+b.dataset.i];
        if (!$('#pe-name').value.trim()) $('#pe-name').value = n;
        $('#pe-desc').value = d; this.count(); this.wakeName();
      }));
      await this.fillVoices(saved.voice);
      Personas.close();
      this.el.classList.remove('hidden');
      setTimeout(() => $('#pe-name').focus(), 50);
    },
    close(back = true) { this.el.classList.add('hidden'); if (back) Personas.open(); },
    count() { $('#pe-count').textContent = `${$('#pe-desc').value.length} / 600`; },
    wakeName() { $('#pe-wake-name').textContent = $('#pe-name').value.trim() || '…'; },
    async save(andSwitch) {
      const opt = $('#pe-voice').selectedOptions[0];
      const data = { id: this.editing ? this.editing.id : '', name: $('#pe-name').value, description: $('#pe-desc').value,
        voice: $('#pe-voice').value, gender: opt ? opt.dataset.gender : '', color: this.color, address: $('#pe-address').value };
      const r = await call('persona_save', data);
      if (!r || !r.ok) { $('#pe-error').textContent = (r && r.error) || 'Could not save.'; $('#pe-error').className = 'field-note warn'; return; }
      toast(this.editing ? `${r.persona.name} updated.` : `${r.persona.name} is ready to talk!`, 'ok');
      if (andSwitch && !r.persona.active) {
        const s = await call('persona_set', r.persona.id);
        if (s && s.ok) Personas.apply(s.persona);
        this.close(false);
        return;
      }
      this.close(true);
    },
    init() {
      this.el = $('#persona-edit');
      $('#pe-close').onclick = () => this.close();
      this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
      $('#pe-desc').oninput = () => this.count();
      $('#pe-name').oninput = () => this.wakeName();
      $('#pe-hear').onclick = () => call('preview_voice', $('#pe-voice').value);
      $('#pe-form').onsubmit = (e) => { e.preventDefault(); this.save(true); };
      $('#pe-save').onclick = () => this.save(false);
      let armed = 0;
      $('#pe-delete').onclick = async () => {
        const b = $('#pe-delete');
        if (!armed) { armed = setTimeout(() => { armed = 0; b.textContent = 'DELETE'; b.classList.remove('armed'); }, 4000); b.textContent = 'CLICK AGAIN TO DELETE'; b.classList.add('armed'); return; }
        clearTimeout(armed); armed = 0; b.textContent = 'DELETE'; b.classList.remove('armed');
        const r = await call('persona_delete', this.editing.id);
        if (r && r.ok) { toast(`${this.editing.name} deleted.`, 'ok'); this.close(true); } else toast((r && r.error) || 'Could not delete.', 'error');
      };
    },
  };

  // ========================================================================= teach the wake word my voice
  const WakeTeach = {
    el: null, persona: null, count: 0, busy: false,
    open(p) {
      this.persona = p;
      this.count = (p.wake && p.wake.recordings) || 0;
      $('#wt-phrase').textContent = `“Hey ${p.name}”`;
      $('#wt-title').textContent = `Teach ${p.name} your voice`;
      this.note('Speak at your usual distance from the microphone.');
      this.paint();
      Personas.close();
      this.el.classList.remove('hidden');
    },
    close(back = true) { this.el.classList.add('hidden'); if (back) Personas.open(); },
    note(text, cls = '') { const n = $('#wt-note'); n.textContent = text; n.className = `field-note ${cls}`; },
    paint() {
      $('#wt-takes').innerHTML = Array.from({ length: 5 }, (_, i) => `<i class="${i < this.count ? 'on' : ''}${i === this.count ? ' next' : ''}"></i>`).join('');
      $('#wt-train').disabled = this.count < 3;
      $('#wt-rec-label').textContent = this.busy ? 'LISTENING…' : this.count >= 5 ? 'RECORD AGAIN' : `RECORD ${Math.min(this.count + 1, 5)} OF ${this.count < 3 ? 3 : 5}`;
      $('#wt-rec').classList.toggle('rec', this.busy);
    },
    update(p) {
      if (!this.persona || this.persona.id !== p.id || this.el.classList.contains('hidden')) return;
      const w = p.wake || {};
      if (w.state === 'learning' || w.state === 'queued') this.note(`Training with your voice… ${Math.round((w.progress || 0) * 100)}%`);
      if (w.state === 'ready' && w.user_samples) this.note(`Done! Say “Hey ${p.name}” to try it.`, 'ok');
    },
    async record() {
      if (this.busy) return;
      this.busy = true; this.paint(); this.note(`Now say “Hey ${this.persona.name}”…`);
      call('play_sfx', 'listen');
      const r = await call('wake_record', this.persona.id);
      this.busy = false;
      if (r && r.ok) { this.count = r.count; this.note(this.count >= 3 ? 'Great. Record a couple more, or train now.' : 'Got it. Again, please.', 'ok'); }
      else this.note((r && r.error) || 'Recording failed.', 'warn');
      this.paint();
    },
    init() {
      this.el = $('#wake-teach');
      $('#wt-close').onclick = $('#wt-done').onclick = () => this.close();
      this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
      $('#wt-rec').onclick = () => this.record();
      $('#wt-reset').onclick = async () => { await call('wake_clear_voice', this.persona.id); this.count = 0; this.paint(); this.note('Cleared. Start again whenever you like.'); };
      $('#wt-train').onclick = async () => {
        const r = await call('wake_train_voice', this.persona.id);
        if (r && r.ok) this.note('Training with your voice… this takes a few minutes.'); else this.note((r && r.error) || 'Could not start.', 'warn');
      };
    },
  };

  // ========================================================================= long-term memory
  const KIND_LABEL = { fact: 'About you', preference: 'Preference', routine: 'Routine', project: 'Project' };
  const SOURCE_LABEL = { said: 'you told me', learned: 'picked up', manual: 'added' };
  const MemoryCore = {
    el: null, data: { memories: [], episodes: [], stats: {} }, tab: 'all', query: '', focusId: null, forgetArmed: 0,
    stats(st) {
      if (!st) return;
      S.memory = st;
      const n = st.total || 0;
      const badge = $('#memory-badge');
      badge.textContent = n > 99 ? '99+' : String(n);
      badge.classList.toggle('hidden', !n);
      $('#btn-memory').title = `Memory Core: ${n} memor${n === 1 ? 'y' : 'ies'} (Ctrl+M)`;
      this.data.stats = st;
      if (this.el && !this.el.classList.contains('hidden')) this.renderStats();
    },
    renderStats() {
      const st = this.data.stats || S.memory || {};
      const cells = [['ABOUT YOU', st.fact], ['PREFERENCES', st.preference], ['ROUTINES', st.routine], ['PROJECTS', st.project], ['CONVERSATIONS', st.episodes]];
      $('#mm-stats').innerHTML = cells.map(([k, v]) => `<div><b>${v || 0}</b><span>${k}</span></div>`).join('')
        + `<div class="mm-recall" title="${st.embed_model ? `Finding memories by meaning with ${esc(st.embed_model)}` : 'Finding memories by their words'}"><b>${st.embed_model ? 'SEMANTIC' : 'KEYWORD'}</b><span>RECALL</span></div>`;
      $('#mm-off').classList.toggle('hidden', st.enabled !== false);
      $('#mm-smart').classList.toggle('hidden', !!st.embed_model || !(S.ollama && S.ollama.online) || !(st.total >= 1));
    },
    async refresh() {
      const d = await call('memory_list');
      if (d) { this.data = d; this.stats(d.stats); }
      this.render();
    },
    matches(text) {
      const q = this.query.trim().toLowerCase();
      const t = String(text).toLowerCase();
      return !q || q.split(/\s+/).every((w) => t.includes(w));
    },
    render() {
      this.renderStats();
      $$('#mm-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === this.tab));
      $('#mm-add').classList.toggle('hidden', this.tab === 'episodes');
      if (this.tab !== 'all' && this.tab !== 'episodes') $('#mm-add-kind').value = this.tab;
      const list = $('#mm-list');
      list.innerHTML = '';
      if (this.tab === 'episodes') {
        const eps = (this.data.episodes || []).filter((e) => this.matches(e.summary));
        if (!eps.length) { list.innerHTML = `<div class="mm-empty">${this.query ? 'No conversations match.' : 'Short summaries of our conversations appear here once a chat winds down.'}</div>`; return; }
        eps.forEach((e) => {
          const row = document.createElement('div');
          row.className = 'mm-item episode';
          const when = new Date(e.ended * 1000);
          row.innerHTML = `<div class="mm-main"><div class="mm-text"></div><div class="mm-badges"><span class="k">${when.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })} · ${fmtTime(when).slice(0, 5)}</span><span>${e.turns} message${e.turns === 1 ? '' : 's'} from you</span></div></div>
            <div class="mm-acts"><button class="mm-btn del" title="Delete this summary">×</button></div>`;
          $('.mm-text', row).textContent = e.summary;
          $('.del', row).onclick = async () => { await call('memory_delete_episode', e.id); this.refresh(); };
          list.append(row);
        });
        return;
      }
      const items = (this.data.memories || []).filter((m) => (this.tab === 'all' || m.kind === this.tab) && this.matches(`${m.text} ${m.key}`));
      if (!items.length) {
        const hints = { all: 'Nothing here yet. Tell me about yourself, say “remember that…”, or add something above.', fact: 'Facts about you: your family, pets, job, where you live…',
          preference: 'What you like and dislike, and how you like things done.', routine: 'Things you do regularly, like “I go to the gym on Mondays at 6 pm”.',
          project: 'What you are working on. I keep track until you tell me it is finished.' };
        list.innerHTML = `<div class="mm-empty">${this.query ? 'No memories match your search.' : hints[this.tab]}</div>`;
        return;
      }
      items.forEach((m) => list.append(this.row(m)));
      if (this.focusId != null) {
        const row = list.querySelector(`[data-id="${this.focusId}"]`);
        if (row) { row.scrollIntoView({ block: 'center' }); this.edit(row, this.data.memories.find((x) => x.id === this.focusId)); }
        this.focusId = null;
      }
    },
    row(m) {
      const done = m.kind === 'project' && m.meta && m.meta.status === 'done';
      const row = document.createElement('div');
      row.className = `mm-item kind-${m.kind}${m.pinned ? ' pinned' : ''}${done ? ' done' : ''}`;
      row.dataset.id = m.id;
      const badges = [`<span class="k">${KIND_LABEL[m.kind] || m.kind}</span>`];
      if (m.when) badges.push(`<span>${esc(m.when)}</span>`);
      if (m.kind === 'project') badges.push(`<span>${done ? 'finished' : 'in progress'}</span>`);
      badges.push(`<span>${SOURCE_LABEL[m.source] || esc(m.source)}</span>`);
      if (m.uses) badges.push(`<span>recalled ${m.uses}×</span>`);
      row.innerHTML = `<div class="mm-main"><div class="mm-text"></div><div class="mm-badges">${badges.join('')}</div></div>
        <div class="mm-acts">
          <button class="mm-btn pin" title="${m.pinned ? 'Unpin' : 'Pin: always keep this in mind'}">${m.pinned ? '★' : '☆'}</button>
          ${m.kind === 'project' ? `<button class="mm-btn done-btn" title="${done ? 'Mark as in progress' : 'Mark as finished'}">✓</button>` : ''}
          <button class="mm-btn edit" title="Edit">✎</button>
          <button class="mm-btn del" title="Forget this">×</button>
        </div>`;
      $('.mm-text', row).textContent = m.text;
      $('.pin', row).onclick = () => this.update(m.id, { pinned: !m.pinned });
      const doneBtn = $('.done-btn', row);
      if (doneBtn) doneBtn.onclick = () => this.update(m.id, { meta: { status: done ? 'active' : 'done' } });
      $('.edit', row).onclick = () => this.edit(row, m);
      $('.mm-text', row).ondblclick = () => this.edit(row, m);
      $('.del', row).onclick = async () => {
        const r = await call('memory_delete', m.id);
        if (r && r.ok && r.memory) {
          this.refresh();
          const t = toast(`Forgot: ${m.text}`, 'info', 6500);
          const b = document.createElement('button');
          b.className = 'toast-act'; b.textContent = 'UNDO';
          b.onclick = async () => { await call('memory_restore', r.memory); t.remove(); this.refresh(); };
          t.append(b);
        }
      };
      return row;
    },
    edit(row, m) {
      if (!row || !m || row.classList.contains('editing')) return;
      row.classList.add('editing');
      const input = document.createElement('textarea');
      input.className = 'mm-edit'; input.value = m.text; input.maxLength = 400; input.rows = 2;
      const kind = document.createElement('select');
      kind.className = 'mm-edit-kind';
      kind.innerHTML = Object.entries(KIND_LABEL).map(([k, v]) => `<option value="${k}"${k === m.kind ? ' selected' : ''}>${v}</option>`).join('');
      $('.mm-text', row).replaceWith(input);
      $('.mm-badges', row).replaceWith(kind);
      input.focus(); input.setSelectionRange(input.value.length, input.value.length);
      const finish = async (save) => {
        if (!row.classList.contains('editing')) return;
        row.classList.remove('editing');
        const text = input.value.trim();
        if (save && text && (text !== m.text || kind.value !== m.kind)) await this.update(m.id, { text, kind: kind.value });
        else this.render();
      };
      input.onkeydown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); finish(true); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
      };
      row.addEventListener('focusout', (e) => { if (!row.contains(e.relatedTarget)) finish(true); });
    },
    async update(id, fields) {
      const r = await call('memory_update', id, fields);
      if (r && !r.ok) toast(r.error || 'Could not update that memory.', 'error');
      this.refresh();
    },
    async open(focusId = null) {
      this.focusId = focusId;
      if (focusId != null) { this.tab = 'all'; this.query = ''; $('#mm-search').value = ''; }
      this.el.classList.remove('hidden');
      call('play_sfx', 'click');
      await this.refresh();
    },
    close() { this.el.classList.add('hidden'); this.disarm(); },
    disarm() { clearTimeout(this.forgetArmed); this.forgetArmed = 0; $('#mm-forget').textContent = 'FORGET EVERYTHING'; $('#mm-forget').classList.remove('armed'); },
    init() {
      this.el = $('#memory');
      $('#mm-close').onclick = $('#mm-done').onclick = () => this.close();
      this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
      $('#btn-memory').onclick = () => this.open();
      $('#set-open-memory').onclick = () => { Settings.close(); this.open(); };
      $$('#mm-tabs button').forEach((b) => (b.onclick = () => { this.tab = b.dataset.tab; this.render(); }));
      $('#mm-search').oninput = (e) => { this.query = e.target.value; this.render(); };
      $('#mm-add').onsubmit = async (e) => {
        e.preventDefault();
        const text = $('#mm-add-text').value.trim();
        if (!text) return;
        const r = await call('memory_add', $('#mm-add-kind').value, text);
        if (r && r.ok) {
          $('#mm-add-text').value = '';
          toast(r.status === 'duplicate' ? 'I already knew that.' : r.status === 'updated' ? 'Memory updated.' : 'Remembered.', 'ok', 2200);
          this.refresh();
        } else toast((r && r.error) || 'Could not save that.', 'error');
      };
      $('#mm-export').onclick = async () => {
        const r = await call('memory_export');
        if (r && r.ok) toast(`Saved to ${r.path}`, 'ok', 7000); else toast((r && r.error) || 'Export failed.', 'error');
      };
      $('#mm-forget').onclick = async () => {
        const b = $('#mm-forget');
        if (!this.forgetArmed) {
          this.forgetArmed = setTimeout(() => this.disarm(), 5000);
          b.textContent = 'CLICK AGAIN TO ERASE ALL'; b.classList.add('armed');
          return;
        }
        this.disarm();
        await call('memory_clear');
        toast('Every memory has been erased.', 'ok');
        this.refresh();
      };
      $('#mm-enable').onclick = async () => { await Settings.save({ memory_enabled: true }, 'memory_enabled'); this.refresh(); };
      $('#mm-smart-get').onclick = async () => {
        const r = await call('memory_install_embeddings');
        if (r && r.ok) { toast('Downloading nomic-embed-text in the background…', 'info'); $('#mm-smart').classList.add('hidden'); }
        else toast((r && r.error) || 'Could not start the download.', 'error');
      };
    },
  };

  // ========================================================================= google docs setup
  const Google = {
    async refresh() {
      const st = await call('google_status');
      if (!st) return;
      $('#g-status').textContent = !st.configured
        ? 'Not connected. Link your Google account (free, about 3 minutes) and JARVIS can write Docs and Slides, fill Sheets and send emails.'
        : st.outdated
          ? 'Connected, but your Google script is an older version. Update it once (about a minute) to unlock Gmail, Sheets and slide editing.'
          : 'Connected. Ask J.A.R.V.I.S. to write Docs and Slides, fill Sheets and send emails.';
      $('#g-status').classList.toggle('ok', st.configured && !st.outdated);
      $('#g-status').classList.toggle('warn', !!(st.configured && st.outdated));
      $('#g-setup').textContent = st.configured ? 'RECONNECT' : 'SET UP';
      $('#g-update').classList.toggle('hidden', !(st.configured && st.outdated));
      $('#g-disconnect').classList.toggle('hidden', !st.configured);
      return st;
    },
    open(update = false) {
      this.updating = update;
      const modal = $('#google');
      modal.classList.toggle('updating', update);
      $('#g-title').textContent = update ? 'Update the Google script' : 'Link Google Docs, Slides & Sheets';
      $('#g-connect span').textContent = update ? 'CHECK UPDATE' : 'CONNECT';
      $('#g-copy').textContent = 'COPY SCRIPT';
      modal.classList.remove('hidden'); $('#g-url').value = '';
    },
    close() { $('#google').classList.add('hidden'); },
    async copy() {
      const script = await call('google_script');
      if (!script) { toast('Could not load the script.', 'error'); return; }
      try { await navigator.clipboard.writeText(script); }
      catch {
        const ta = Object.assign(document.createElement('textarea'), { value: script });
        document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
      }
      $('#g-copy').textContent = 'COPIED ✓';
      toast('Script copied: paste it into Apps Script.', 'ok');
    },
    async connect() {
      const btn = $('#g-connect');
      btn.classList.add('busy'); btn.disabled = true;
      const r = await call('google_connect', this.updating ? '' : $('#g-url').value.trim());
      btn.classList.remove('busy'); btn.disabled = false;
      if (r && r.ok && r.outdated) {
        toast('Google still has the old script: make sure you saved, then Deploy ▸ Manage deployments ▸ Edit ▸ New version.', 'error', 10000);
        this.refresh();
      } else if (r && r.ok) {
        toast(`Google ${this.updating ? 'script updated' : 'linked'}${r.user ? ` · ${r.user}` : ''}.`, 'ok', 6000);
        Chat.system(this.updating ? 'Google bridge updated: Sheets and slide editing unlocked.' : 'Google Docs, Slides & Sheets linked.', 'ok');
        this.close(); this.refresh();
      } else toast((r && r.error) || 'Connection failed.', 'error', 8000);
    },
  };

  // ========================================================================= icons used by protocols and the Automation Center
  const ICON_PATHS = {
    bolt: '<path d="M13 2.5 5 13.5h6l-1 8 8-11h-6Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.5M12 19v2.5M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M2.5 12H5M19 12h2.5M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8"/>',
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>',
    game: '<rect x="2.5" y="7" width="19" height="11" rx="4"/><path d="M7.5 10.5v4M5.5 12.5h4"/><circle cx="16" cy="11.5" r="1"/><circle cx="18" cy="13.8" r="1"/>',
    code: '<path d="m8 7-5 5 5 5M16 7l5 5-5 5M13.5 4.5l-3 15"/>',
    music: '<path d="M9 18V5.5l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 8h4M3 12h4M3 16h4M17 8h4M17 12h4M17 16h4"/>',
    focus: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
    work: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8.5 7V5a1.5 1.5 0 0 1 1.5-1.5h4A1.5 1.5 0 0 1 15.5 5v2M3 12.5h18"/>',
    home: '<path d="M3.5 11 12 4l8.5 7M6 9.5V20h12V9.5M10 20v-5h4v5"/>',
    coffee: '<path d="M4 9h13v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5Z"/><path d="M17 10.5h1.5a2.5 2.5 0 0 1 0 5H17M8 3.5v2.5M12 3.5v2.5"/>',
    rocket: '<path d="M12 2.5c3.5 2.5 5 6.5 4.5 11l-2.5 3h-4l-2.5-3C7 9 8.5 5 12 2.5Z"/><circle cx="12" cy="9.5" r="1.8"/><path d="M8 16.5 5.5 19M16 16.5l2.5 2.5M12 18v3.5"/>',
    party: '<path d="M4 20 9.5 6.5l8 8Z"/><path d="M14 4.5v.01M19.5 9v.01M17 3l-1 2M21 7l-2 1M13.5 8.5c1-1.5 2.5-2 4-1.5"/>',
    book: '<path d="M4 4.5h6a2 2 0 0 1 2 2V20a1.5 1.5 0 0 0-1.5-1.5H4ZM20 4.5h-6a2 2 0 0 0-2 2V20a1.5 1.5 0 0 1 1.5-1.5H20Z"/>',
    heart: '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.4 4.3 4.3 0 0 1 19.5 10C19.5 15.4 12 20 12 20Z"/>',
    shield: '<path d="M12 3 4.5 6v5.5c0 4.5 3.2 8 7.5 9.5 4.3-1.5 7.5-5 7.5-9.5V6Z"/><path d="m8.8 12 2.2 2.2 4.2-4.4"/>',
  };
  const icon = (name, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICON_PATHS[name] || ICON_PATHS.bolt}</svg>`;
  const STATUS_MARK = { ok: '✓', done: '✓', unverified: '~', failed: '✗', skipped: '⤼', not_run: '–', pending: '○', running: '◌', stopped: '■', waiting: '…', interrupted: '■' };
  const ago = (ts) => {
    if (!ts) return '';
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(ts * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  };
  const clock = (sec) => (sec == null ? '' : `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`);

  // ========================================================================= now playing (strip under the reactor)
  const Media = {
    now: null, sessions: [], status: {}, at: 0,
    update(ev) {
      if ('sessions' in ev) this.sessions = ev.sessions || [];
      if (ev.status) this.status = ev.status;
      this.at = Date.now();
      this.set(ev.playing || null);
      Center.media();
    },
    set(np) {
      this.now = np;
      const el = $('#media-strip');
      el.classList.toggle('hidden', !np);
      if (!np) return;
      el.classList.toggle('paused', !np.title || np.status !== 'playing');
      $('#media-app').textContent = (np.label || np.app || 'NOW PLAYING').toUpperCase();
      $('#media-title').textContent = np.title ? (np.artist ? `${np.title} · ${np.artist}` : np.title) : 'paused';
      el.title = np.title ? `${np.title}${np.artist ? ` by ${np.artist}` : ''} (${np.label || np.app}, ${np.status}) — click for all players` : `${np.app}: nothing playing`;
      el.dataset.key = np.key || '';
      this.tick();
    },
    position(s) {
      if (s.position == null) return null;
      const extra = s.status === 'playing' ? (Date.now() - this.at) / 1000 : 0;
      return s.duration ? Math.min(s.duration, s.position + extra) : s.position + extra;
    },
    tick() {
      const np = this.now;
      const bar = $('#media-progress');
      if (!np || !np.duration || np.position == null) { bar.style.width = '0'; } else bar.style.width = `${(100 * this.position(np)) / np.duration}%`;
      Center.progress();
    },
    async control(action, key = '') {
      call('play_sfx', 'click');
      const r = await call('media_control', action, key);
      if (r && !r.ok) toast(r.error === 'nothing' ? 'Nothing to control right now.' : `That player didn't respond (${r.error || 'failed'}).`, 'error');
      else if (r && !r.verified) toast('Sent, but Windows didn\'t confirm the change.', 'info', 2500);
    },
    init() {
      $$('#media-strip [data-media]').forEach((b) => (b.onclick = (e) => { e.stopPropagation(); this.control(b.dataset.media, $('#media-strip').dataset.key || ''); }));
      $('#media-strip').onclick = () => Center.open('media');
      setInterval(() => this.tick(), 1000);
    },
  };

  // ========================================================================= protocols
  const DAY_SHORT = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
  const DAY_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const STEP_IDEAS = ['open Discord', 'play some focus music', 'pause everything', 'turn the music down', 'what\'s the weather today',
    'wait 5 seconds', 'say Good morning, sir', 'switch to Harper', 'set a timer for 25 minutes', 'open github.com'];
  const COND_TYPES = [['', 'Always'], ['app_running', 'Only if an app is open'], ['app_not_running', 'Only if an app isn\'t open'],
    ['time', 'Only between times'], ['weekday', 'Only on certain days'], ['previous', 'Only if the previous step…']];
  const Protocols = {
    el: null, data: { protocols: [], templates: [], icons: [], categories: [] }, running: null, recording: null, proposal: null,
    editing: null, steps: [], days: [0, 1, 2, 3, 4, 5, 6], deleteArmed: 0, view: 'mine', filter: 'All', query: '', iconSel: 'bolt',
    set(d) {
      if (!d) return;
      this.data = { ...this.data, ...d };
      this.running = d.running || null;
      this.recording = d.recording || null;
      const was = this.proposal && this.proposal.stage;
      this.proposal = d.proposal || null;
      if (this.proposal && this.proposal.stage === 'approve' && was !== 'approve') Chat.proposal(this.proposal);
      if (!this.proposal) $$('.proposal-card').forEach((c) => c.classList.add('done'));
      const n = (this.data.protocols || []).length;
      $('#protocols-badge').textContent = String(n);
      $('#protocols-badge').classList.toggle('hidden', !n);
      this.strip();
      if (this.el && !this.el.classList.contains('hidden') && $('#pr-form').classList.contains('hidden')) this.render();
      Center.run();
    },
    progress(ev) {
      if (['running', 'step', 'step_done', 'confirm'].includes(ev.status)) {
        this.running = { ...ev };
      } else {
        this.running = null;
        if (!ev.ephemeral) {
          const label = { done: 'complete', failed: 'stopped: a step failed', stopped: 'stopped', interrupted: 'stopped (you took over)' }[ev.status] || ev.status;
          const bad = (ev.results || []).filter((r) => r.status === 'failed').length;
          toast(`${ev.name} protocol ${label}${ev.status === 'done' && bad ? ` (${bad} step${bad === 1 ? '' : 's'} failed)` : ''}.`, ev.status === 'done' && !bad ? 'ok' : 'info', 4500);
        }
        Center.lastRun = ev;
      }
      this.strip();
      Center.run();
      if (this.el && !this.el.classList.contains('hidden') && $('#pr-form').classList.contains('hidden')) this.render();
    },
    strip() {
      const el = $('#protocol-strip');
      const r = this.running, rec = this.recording;
      el.classList.toggle('hidden', !r && !rec);
      el.classList.toggle('recording', !r && !!rec);
      el.classList.toggle('asking', !!(r && r.confirm));
      $('#protocol-confirm').classList.toggle('hidden', !(r && r.confirm));
      if (r) {
        $('#protocol-kind').textContent = r.ephemeral ? 'RUNNING' : 'PROTOCOL';
        $('#protocol-label').textContent = r.ephemeral ? 'your request' : r.name;
        const n = Math.max(0, r.index);
        $('#protocol-progress').textContent = r.confirm ? `step ${r.confirm.index + 1}: ${r.confirm.text} — go ahead?` : (r.index >= 0 ? `${n + 1}/${r.total} · ${r.step || ''}` : 'starting…');
        $('#protocol-stop').textContent = 'STOP';
        const done = (r.results || []).filter((x) => !['pending', 'running'].includes(x.status)).length;
        $('#protocol-bar').style.width = r.total ? `${(100 * done) / r.total}%` : '0';
      } else if (rec) {
        $('#protocol-kind').textContent = 'RECORDING';
        $('#protocol-label').textContent = rec.name || 'new protocol';
        $('#protocol-progress').textContent = `${rec.steps.length} step${rec.steps.length === 1 ? '' : 's'} · say “done” to save`;
        $('#protocol-stop').textContent = 'CANCEL';
        $('#protocol-bar').style.width = '0';
      }
    },
    renderFilters() {
      const cats = ['All', ...new Set((this.data.protocols || []).map((p) => p.category))];
      $('#pr-filters').innerHTML = cats.map((c) => `<button type="button" role="tab" class="${c === this.filter ? 'on' : ''}" data-cat="${esc(c)}">${esc(c.toUpperCase())}</button>`).join('');
      $$('#pr-filters button').forEach((b) => (b.onclick = () => { this.filter = b.dataset.cat; this.render(); }));
    },
    render() {
      $$('.pr-views button').forEach((b) => b.classList.toggle('on', b.dataset.view === this.view));
      this.renderFilters();
      const list = $('#pr-list');
      list.innerHTML = '';
      const sug = (this.data.suggestions || [])[0];
      $('#pr-suggest').classList.toggle('hidden', !sug || this.view !== 'mine');
      if (sug) {
        $('#pr-suggest').innerHTML = `<b>IDEA</b><span>You often say “${esc(sug.steps[0])}” and then “${esc(sug.steps[1])}”. Make it one protocol?</span><button class="btn sm" type="button">CREATE</button>`;
        $('#pr-suggest button').onclick = () => this.edit(null, { steps: sug.steps.map((t) => ({ text: t })) });
      }
      if (this.view === 'templates') {
        (this.data.templates || []).forEach((t) => {
          const card = document.createElement('div');
          card.className = 'pr-card template';
          card.innerHTML = `<div class="pr-card-head">${icon(t.icon, 'pr-ic')}<div><div class="pr-name"></div><div class="pr-cat">${esc((t.category || '').toUpperCase())}</div></div></div>
            <p class="pr-desc"></p><ol class="pr-steps">${t.steps.slice(0, 5).map(() => '<li></li>').join('')}</ol>
            <div class="pr-card-foot"><span class="pr-phrase"></span><button class="btn sm primary add" type="button"><span>ADD</span></button></div>`;
          $('.pr-name', card).textContent = t.name;
          $('.pr-desc', card).textContent = t.description || '';
          $$('.pr-steps li', card).forEach((li, i) => (li.textContent = t.steps[i]));
          $('.pr-phrase', card).textContent = t.phrases && t.phrases[0] ? `“${t.phrases[0]}”` : '';
          $('.add', card).onclick = async () => {
            const r = await call('protocol_from_template', t.name);
            if (r && r.ok) { toast(`Added ${r.protocol.name}. Edit it to pick your own apps.`, 'ok', 4000); this.view = 'mine'; const d = await call('protocol_list'); if (d) this.set(d); this.render(); }
            else toast((r && r.error) || 'Could not add it.', 'error');
          };
          list.append(card);
        });
        return;
      }
      const q = this.query.trim().toLowerCase();
      const items = (this.data.protocols || []).filter((p) => (this.filter === 'All' || p.category === this.filter)
        && (!q || `${p.name} ${p.description} ${(p.phrases || []).join(' ')} ${p.steps.map((s) => s.text).join(' ')}`.toLowerCase().includes(q)));
      if (!items.length) {
        list.innerHTML = q || this.filter !== 'All'
          ? '<div class="mm-empty">No protocols match.</div>'
          : '<div class="mm-empty">No protocols yet. Press <b>NEW PROTOCOL</b>, pick one from <b>TEMPLATES</b>, or say “create a protocol called Morning: open Spotify, then tell me the weather”.</div>';
        return;
      }
      items.forEach((p) => list.append(this.card(p)));
    },
    card(p) {
      const running = this.running && this.running.id === p.id;
      const last = (p.history || [])[p.history.length - 1];
      const card = document.createElement('div');
      card.className = `pr-card${running ? ' running' : ''}${p.enabled ? '' : ' off'}`;
      const badges = [];
      if (p.when) badges.push(`<span class="sched">⏰ ${esc(p.when)}</span>`);
      if (p.triggers && p.triggers.startup) badges.push('<span>at start-up</span>');
      if (p.triggers && p.triggers.app) badges.push(`<span>when ${esc(p.triggers.app)} opens</span>`);
      if (p.triggers && p.triggers.hotkey) badges.push(`<span class="kbd">${esc(p.triggers.hotkey)}</span>`);
      if (p.steps.some((s) => s.risk)) badges.push('<span class="warn" title="Some steps ask you before running">asks first</span>');
      if (!p.enabled) badges.push('<span>off</span>');
      const result = last ? `<span class="pr-last ${last.status}" title="${esc(new Date(last.at * 1000).toLocaleString())}">${STATUS_MARK[last.status === 'done' ? (last.steps.some((s) => s.status === 'failed') ? 'unverified' : 'ok') : 'failed'] || '•'} ${esc(last.status)} · ${ago(last.at)}</span>` : '<span class="pr-last never">never run</span>';
      card.innerHTML = `<div class="pr-card-head">${icon(p.icon, 'pr-ic')}<div class="pr-head-text"><div class="pr-name"></div><div class="pr-cat">${esc((p.category || '').toUpperCase())} · ${p.steps.length} STEP${p.steps.length === 1 ? '' : 'S'}</div></div>${result}</div>
        <p class="pr-desc"></p>
        <ol class="pr-steps">${p.steps.slice(0, 6).map(() => '<li><span class="t"></span><em></em></li>').join('')}</ol>
        <div class="pr-badges">${badges.join('')}</div>
        <div class="pr-card-foot"><span class="pr-phrase"></span>
          <button class="btn sm ghost hist" type="button" title="Last runs">HISTORY</button>
          <button class="btn sm ghost edit" type="button">EDIT</button>
          <button class="btn sm primary run" type="button"><span>${running ? 'STOP' : 'RUN'}</span></button></div>
        <div class="pr-history hidden"></div>`;
      $('.pr-name', card).textContent = p.name;
      $('.pr-desc', card).textContent = p.description || '';
      $$('.pr-steps li', card).forEach((li, i) => {
        const s = p.steps[i];
        $('.t', li).textContent = s.text;
        $('em', li).textContent = [s.condition, s.risk ? 'asks first' : '', s.fallback ? `else ${s.fallback}` : ''].filter(Boolean).join(' · ');
        li.classList.toggle('now', !!(running && this.running.index === i));
        li.classList.toggle('disabled', s.enabled === false);
      });
      if (p.steps.length > 6) { const li = document.createElement('li'); li.className = 'more'; li.textContent = `…and ${p.steps.length - 6} more`; $('.pr-steps', card).append(li); }
      $('.pr-phrase', card).textContent = `“run ${p.name}”${p.phrases && p.phrases.length ? ` · “${p.phrases[0]}”` : ''}`;
      $('.run', card).onclick = async () => {
        if (running) { call('protocol_stop'); return; }
        const r = await call('protocol_run', p.id);
        if (r && !r.ok) toast(r.error || 'Could not run it.', 'error'); else this.close();
      };
      $('.edit', card).onclick = () => this.edit(p);
      $('.pr-name', card).ondblclick = () => this.edit(p);
      $('.hist', card).onclick = () => {
        const box = $('.pr-history', card);
        if (!box.classList.contains('hidden')) { box.classList.add('hidden'); return; }
        box.innerHTML = (p.history || []).slice().reverse().slice(0, 5).map((h) => `<div class="pr-run"><b class="${esc(h.status)}">${esc(h.status.toUpperCase())}</b><span>${esc(new Date(h.at * 1000).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' }))} · ${esc(h.trigger || '')} · ${h.seconds || 0}s</span>
          <ol>${(h.steps || []).map((s) => `<li class="${esc(s.status)}"><i>${STATUS_MARK[s.status] || '•'}</i>${esc(s.text)}${s.detail && s.status !== 'ok' ? ` <em>${esc(s.detail)}</em>` : ''}</li>`).join('')}</ol></div>`).join('') || '<div class="mm-empty">Not run yet.</div>';
        box.classList.remove('hidden');
      };
      return card;
    },
    // -- the editor
    stepRow(s, i) {
      const li = document.createElement('li');
      li.className = 'pr-step';
      const cond = s.when || {};
      li.innerHTML = `<div class="pr-step-main"><span class="n">${i + 1}</span><input class="st-text" type="text" maxlength="300" aria-label="Step ${i + 1}">
          <span class="st-label"></span>
          <button type="button" class="mm-btn up" title="Move up" aria-label="Move up">↑</button><button type="button" class="mm-btn down" title="Move down" aria-label="Move down">↓</button>
          <button type="button" class="mm-btn opts" title="Options" aria-label="Step options" aria-expanded="false">⚙</button><button type="button" class="mm-btn del" title="Remove" aria-label="Remove step">×</button></div>
        <div class="pr-step-opts hidden">
          <label>Run <select class="st-cond">${COND_TYPES.map(([v, l]) => `<option value="${v}"${(cond.type || '') === v ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
          <input class="st-app" type="text" placeholder="app, e.g. Spotify" value="">
          <span class="st-time"><input class="st-after" type="time"> to <input class="st-before" type="time"></span>
          <span class="st-wd"></span>
          <select class="st-prev"><option value="1">worked</option><option value="0">failed</option></select>
          <label class="chk"><input type="checkbox" class="st-confirm"> Ask me first</label>
          <label class="chk st-approve-l"><input type="checkbox" class="st-approved"> Don't ask (I trust it)</label>
          <label>If it fails <select class="st-coe"><option value="">protocol default</option><option value="0">stop</option><option value="1">carry on</option></select></label>
          <label>Retries <select class="st-retries"><option>0</option><option>1</option><option>2</option><option>3</option></select></label>
          <label>Time limit <input class="st-timeout" type="number" min="1" max="600" step="1"> s</label>
          <label class="grow">Otherwise try <input class="st-fallback" type="text" maxlength="300" placeholder="e.g. open YouTube"></label>
          <label class="chk"><input type="checkbox" class="st-enabled"> On</label>
        </div>`;
      $('.st-text', li).value = s.text || '';
      $('.st-label', li).textContent = s.label ? `${s.label}${s.risk ? ' · asks first' : ''}` : '';
      $('.st-label', li).classList.toggle('risk', !!s.risk);
      $('.st-app', li).value = cond.app || '';
      $('.st-after', li).value = cond.after || '';
      $('.st-before', li).value = cond.before || '';
      $('.st-prev', li).value = cond.ok === false ? '0' : '1';
      $('.st-confirm', li).checked = !!s.confirm;
      $('.st-approved', li).checked = !!s.approved;
      $('.st-approve-l', li).classList.toggle('hidden', !s.risk);
      $('.st-coe', li).value = s.continue_on_error == null ? '' : (s.continue_on_error ? '1' : '0');
      $('.st-retries', li).value = String(s.retries || 0);
      $('.st-timeout', li).value = s.timeout || 90;
      $('.st-fallback', li).value = s.fallback || '';
      $('.st-enabled', li).checked = s.enabled !== false;
      const days = cond.days || [];
      $('.st-wd', li).innerHTML = DAY_SHORT.map((d, k) => `<label title="${DAY_LONG[k]}"><input type="checkbox" value="${k}"${days.includes(k) ? ' checked' : ''}>${d}</label>`).join('');
      const showCond = () => {
        const t = $('.st-cond', li).value;
        $('.st-app', li).classList.toggle('hidden', !t.startsWith('app'));
        $('.st-time', li).classList.toggle('hidden', t !== 'time');
        $('.st-wd', li).classList.toggle('hidden', t !== 'weekday');
        $('.st-prev', li).classList.toggle('hidden', t !== 'previous');
      };
      $('.st-cond', li).onchange = showCond;
      showCond();
      $('.opts', li).onclick = () => { const o = $('.pr-step-opts', li); o.classList.toggle('hidden'); $('.opts', li).setAttribute('aria-expanded', String(!o.classList.contains('hidden'))); };
      if (s.when && s.when.type || s.confirm || s.fallback || s.retries || s.continue_on_error != null) $('.opts', li).classList.add('set');
      $('.del', li).onclick = () => { this.readSteps(); this.steps.splice(i, 1); this.renderSteps(); };
      $('.up', li).onclick = () => { this.readSteps(); if (i > 0) { [this.steps[i - 1], this.steps[i]] = [this.steps[i], this.steps[i - 1]]; this.renderSteps(); } };
      $('.down', li).onclick = () => { this.readSteps(); if (i < this.steps.length - 1) { [this.steps[i + 1], this.steps[i]] = [this.steps[i], this.steps[i + 1]]; this.renderSteps(); } };
      return li;
    },
    readSteps() {
      this.steps = $$('#pr-steps-ed .pr-step').map((li, i) => {
        const prev = this.steps[i] || {};
        const t = $('.st-cond', li).value;
        let when = {};
        if (t === 'app_running' || t === 'app_not_running') when = $('.st-app', li).value.trim() ? { type: t, app: $('.st-app', li).value.trim() } : {};
        else if (t === 'time') when = { type: 'time', ...($('.st-after', li).value ? { after: $('.st-after', li).value } : {}), ...($('.st-before', li).value ? { before: $('.st-before', li).value } : {}) };
        else if (t === 'weekday') when = { type: 'weekday', days: $$('.st-wd input:checked', li).map((x) => Number(x.value)) };
        else if (t === 'previous') when = { type: 'previous', ok: $('.st-prev', li).value === '1' };
        const coe = $('.st-coe', li).value;
        return { ...prev, text: $('.st-text', li).value.trim(), when, confirm: $('.st-confirm', li).checked, approved: $('.st-approved', li).checked,
          continue_on_error: coe === '' ? null : coe === '1', retries: Number($('.st-retries', li).value) || 0,
          timeout: Number($('.st-timeout', li).value) || 90, fallback: $('.st-fallback', li).value.trim(), enabled: $('.st-enabled', li).checked };
      }).filter((s) => s.text);
    },
    renderSteps() {
      const ol = $('#pr-steps-ed');
      ol.innerHTML = '';
      this.steps.forEach((s, i) => ol.append(this.stepRow(s, i)));
      if (!this.steps.length) ol.innerHTML = '<li class="mm-empty">No steps yet. Add one below, e.g. “open Discord”.</li>';
      $('#pr-count').textContent = `${this.steps.length} step${this.steps.length === 1 ? '' : 's'}`;
    },
    addStep(text) {
      text = (text || '').trim();
      if (!text) return;
      this.readSteps();
      this.steps.push({ text });
      this.renderSteps();
      $('#pr-add-step').value = '';
      $('#pr-add-step').focus();
    },
    renderDays() {
      $('#pr-days').innerHTML = DAY_SHORT.map((d, i) => `<button type="button" data-d="${i}" class="${this.days.includes(i) ? 'on' : ''}" title="${DAY_LONG[i]}" aria-pressed="${this.days.includes(i)}">${d}</button>`).join('');
      $$('#pr-days button').forEach((b) => (b.onclick = () => {
        const d = Number(b.dataset.d);
        this.days = this.days.includes(d) ? this.days.filter((x) => x !== d) : [...this.days, d].sort();
        if (!this.days.length) this.days = [d];
        this.renderDays();
      }));
    },
    renderIcons() {
      $('#pr-icon-pick').innerHTML = (this.data.icons || Object.keys(ICON_PATHS)).map((n) => `<button type="button" role="radio" aria-checked="${n === this.iconSel}" class="${n === this.iconSel ? 'on' : ''}" data-icon="${n}" title="${n}">${icon(n)}</button>`).join('');
      $$('#pr-icon-pick button').forEach((b) => (b.onclick = () => { this.iconSel = b.dataset.icon; this.renderIcons(); }));
    },
    schedOn(on) { $('#pr-sched-on').checked = on; $('.pr-sched').classList.toggle('off', !on); },
    edit(p = null, preset = null) {
      this.editing = p;
      const src = p || preset || {};
      $('#pr-browse').classList.add('hidden');
      $('#pr-form').classList.remove('hidden');
      $('#pr-title').textContent = p ? `Edit ${p.name}` : 'New protocol';
      $('#pr-kicker').textContent = p ? 'PROTOCOLS · EDIT' : 'PROTOCOLS · NEW';
      $('#pr-name').value = src.name || '';
      $('#pr-name-echo').textContent = src.name || '…';
      $('#pr-desc').value = src.description || '';
      $('#pr-category').innerHTML = (this.data.categories || ['General']).map((c) => `<option${c === (src.category || 'General') ? ' selected' : ''}>${esc(c)}</option>`).join('');
      this.iconSel = src.icon || 'bolt';
      this.renderIcons();
      this.steps = (src.steps || []).map((s) => (typeof s === 'string' ? { text: s } : { ...s }));
      this.renderSteps();
      $('#pr-phrases').value = (src.phrases || []).join(', ');
      const sch = src.schedule || {};
      this.schedOn(!!sch.time && sch.enabled !== false);
      $('#pr-time').value = sch.time || '07:30';
      this.days = (sch.days && sch.days.length) ? sch.days.slice() : [0, 1, 2, 3, 4, 5, 6];
      this.renderDays();
      const tr = src.triggers || {};
      $('#pr-startup').checked = !!tr.startup;
      $('#pr-app').value = tr.app || '';
      $('#pr-hotkey').value = tr.hotkey || '';
      $('#pr-policy').value = src.on_failure || 'stop';
      $('#pr-enabled').checked = src.enabled !== false;
      $('#pr-error').textContent = '';
      $('#pr-delete').classList.toggle('hidden', !p);
      this.disarm();
      setTimeout(() => (p ? $('#pr-add-step') : $('#pr-name')).focus(), 50);
    },
    back() {
      this.editing = null;
      $('#pr-form').classList.add('hidden');
      $('#pr-browse').classList.remove('hidden');
      $('#pr-title').textContent = 'Protocols';
      $('#pr-kicker').textContent = 'PROTOCOLS · YOUR AUTOMATIONS';
      this.render();
    },
    collect() {
      this.readSteps();
      if ($('#pr-add-step').value.trim()) { this.steps.push({ text: $('#pr-add-step').value.trim() }); $('#pr-add-step').value = ''; }
      const data = { name: $('#pr-name').value.trim(), steps: this.steps.map(({ label, kind, risk, condition, ...s }) => s), description: $('#pr-desc').value.trim(),
        icon: this.iconSel, category: $('#pr-category').value, phrases: $('#pr-phrases').value.split(',').map((x) => x.trim()).filter(Boolean),
        on_failure: $('#pr-policy').value, enabled: $('#pr-enabled').checked,
        triggers: { startup: $('#pr-startup').checked, app: $('#pr-app').value.trim(), hotkey: $('#pr-hotkey').value.trim() },
        schedule: $('#pr-sched-on').checked ? { time: $('#pr-time').value || '07:30', days: this.days, enabled: true } : {} };
      if (this.editing) data.id = this.editing.id;
      return data;
    },
    async save(run = false) {
      const data = this.collect();
      const r = await call('protocol_save', data);
      if (!r || !r.ok) { $('#pr-error').textContent = (r && r.error) || 'Could not save it.'; $('#pr-error').className = 'field-note warn'; this.renderSteps(); return; }
      toast(`Protocol ${r.protocol.name} saved.`, 'ok', 2500);
      if (run) {
        const go = await call('protocol_run', r.protocol.id);
        if (go && !go.ok) toast(go.error || 'Could not run it.', 'error');
        this.close();
        return;
      }
      const fresh = await call('protocol_list');
      if (fresh) this.set(fresh);
      this.back();
    },
    disarm() { clearTimeout(this.deleteArmed); this.deleteArmed = 0; $('#pr-delete').textContent = 'DELETE'; $('#pr-delete').classList.remove('armed'); },
    async remove() {
      if (!this.editing) return;
      if (!this.deleteArmed) { this.deleteArmed = setTimeout(() => this.disarm(), 4000); $('#pr-delete').textContent = 'CLICK AGAIN TO DELETE'; $('#pr-delete').classList.add('armed'); return; }
      this.disarm();
      const r = await call('protocol_delete', this.editing.id);
      if (r && r.ok && r.protocol) {
        const t = toast(`Deleted the ${r.protocol.name} protocol.`, 'info', 6500);
        const b = document.createElement('button');
        b.className = 'toast-act'; b.textContent = 'UNDO';
        b.onclick = async () => { await call('protocol_restore', r.protocol); t.remove(); const d = await call('protocol_list'); if (d) this.set(d); };
        t.append(b);
      }
      const fresh = await call('protocol_list');
      if (fresh) this.set(fresh);
      this.back();
    },
    async open(create = false, preset = null) {
      this.el.classList.remove('hidden');
      call('play_sfx', 'click');
      const d = await call('protocol_list');
      if (d) this.set(d);
      if (create || preset) this.edit(null, preset); else this.back();
    },
    close() { this.el.classList.add('hidden'); this.disarm(); },
    init() {
      this.el = $('#protocols');
      $('#btn-protocols').onclick = () => this.open();
      $('#pr-close').onclick = $('#pr-done').onclick = () => this.close();
      this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
      $('#pr-new').onclick = () => this.edit(null);
      $('#pr-back').onclick = () => this.back();
      $('#pr-form').onsubmit = (e) => { e.preventDefault(); this.save(false); };
      $('#pr-save-run').onclick = () => this.save(true);
      $('#pr-delete').onclick = () => this.remove();
      $('#pr-search').oninput = (e) => { this.query = e.target.value; this.render(); };
      $$('.pr-views button').forEach((b) => (b.onclick = () => { this.view = b.dataset.view; this.render(); }));
      $('#pr-name').oninput = (e) => ($('#pr-name-echo').textContent = e.target.value || '…');
      $('#pr-add-btn').onclick = () => this.addStep($('#pr-add-step').value);
      $('#pr-add-step').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); this.addStep(e.target.value); } };
      $('#pr-sched-on').onchange = (e) => this.schedOn(e.target.checked);
      $('#pr-hotkey').onkeydown = (e) => {
        e.preventDefault();
        if (e.key === 'Backspace' || e.key === 'Delete' || e.key === 'Escape') { e.target.value = ''; return; }
        const mods = [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Win'].filter(Boolean);
        const key = /^[a-z0-9]$/i.test(e.key) ? e.key.toUpperCase() : (/^F([1-9]|1[0-2])$/.test(e.key) ? e.key : '');
        if (key && mods.some((m) => m !== 'Shift')) e.target.value = [...mods, key].join('+');
      };
      $('#pr-ideas').innerHTML = '<span>IDEAS:</span>' + STEP_IDEAS.map(() => '<button type="button"></button>').join('');
      $$('#pr-ideas button').forEach((b, i) => { b.textContent = STEP_IDEAS[i]; b.onclick = () => this.addStep(STEP_IDEAS[i]); });
      $('#protocol-stop').onclick = () => {
        if (this.running) call('protocol_stop');
        else if (this.recording) call('send_text', 'cancel');
      };
      $('#protocol-yes').onclick = () => call('protocol_answer', true);
      $('#protocol-no').onclick = () => call('protocol_answer', false);
      $('#protocol-strip').addEventListener('click', (e) => { if (!e.target.closest('button')) Center.open('run'); });
    },
  };

  // ========================================================================= media & automation center
  const Center = {
    el: null, items: [], health: null, browser: null, lastRun: null, kind: 'all', query: '', unseen: 0, corrections: [],
    open(focus = '') {
      this.el.classList.remove('hidden');
      call('play_sfx', 'click');
      this.unseen = 0; this.badge();
      this.refresh();
      if (focus) setTimeout(() => { const p = $(`.ac-${focus}`, this.el); if (p) p.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, 60);
    },
    close() { this.el.classList.add('hidden'); },
    shown() { return this.el && !this.el.classList.contains('hidden'); },
    async refresh() {
      const [act, br, cor] = await Promise.all([call('activity_list'), call('browser_status'), call('corrections_list')]);
      if (act) { this.items = act.items || []; this.health = act.health || null; }
      if (br) this.browser = br;
      if (cor) this.corrections = cor.items || [];
      this.media(); this.browserPanel(); this.run(); this.healthPanel(); this.correctionsPanel(); this.log();
    },
    add(item) {
      this.items.push(item);
      if (this.items.length > 150) this.items.shift();
      if (!this.shown() && item.kind !== 'command' && item.status === 'failed') { this.unseen++; this.badge(); }
      if (this.shown()) this.log();
    },
    badge() { const b = $('#center-badge'); b.textContent = String(this.unseen); b.classList.toggle('hidden', !this.unseen); },
    media() {
      if (!this.el) return;
      const box = $('#ac-sessions');
      const st = Media.status || {};
      $('#ac-media-src').textContent = st.sessions ? 'Windows media sessions' : (st.keys ? 'media keys + window titles' : '');
      const list = Media.sessions || [];
      if (!list.length) { box.innerHTML = '<div class="mm-empty">Nothing is playing. Say “play some jazz” or “resume what I was listening to”.</div>'; return; }
      box.innerHTML = '';
      list.forEach((s) => {
        const row = document.createElement('div');
        row.className = `ac-session ${s.status}`;
        row.dataset.key = s.key;
        const can = new Set(s.can || []);
        row.innerHTML = `<div class="ac-s-main"><b class="ac-s-app"></b><div class="ac-s-title"></div><div class="ac-s-sub"></div>
            <div class="ac-s-bar"><i></i></div></div>
          <div class="ac-s-ctl">
            <button type="button" class="mm-btn" data-a="previous" title="Previous" ${can.has('previous') || s.source === 'window' ? '' : 'disabled'}>⏮</button>
            <button type="button" class="mm-btn big" data-a="toggle" title="${s.status === 'playing' ? 'Pause' : 'Play'}">${s.status === 'playing' ? '⏸' : '▶'}</button>
            <button type="button" class="mm-btn" data-a="next" title="Next" ${can.has('next') || s.source === 'window' ? '' : 'disabled'}>⏭</button>
          </div>
          ${s.volume != null ? '<label class="ac-s-vol" title="This app\'s own volume">🔊<input type="range" min="0" max="100" step="1"></label>' : ''}`;
        $('.ac-s-app', row).textContent = `${(s.label || s.app).toUpperCase()} · ${s.status.toUpperCase()}${s.muted ? ' · MUTED' : ''}`;
        $('.ac-s-title', row).textContent = s.title || '(no title)';
        $('.ac-s-sub', row).textContent = [s.artist, s.duration ? `${clock(Media.position(s))} / ${clock(s.duration)}` : ''].filter(Boolean).join(' · ');
        $$('[data-a]', row).forEach((b) => (b.onclick = () => Media.control(b.dataset.a, s.key)));
        const vol = $('.ac-s-vol input', row);
        if (vol) { vol.value = Math.round((s.volume || 0) * 100); vol.onchange = () => call('media_volume', s.key, Number(vol.value) / 100); }
        box.append(row);
      });
      this.progress();
    },
    progress() {
      if (!this.shown()) return;
      $$('#ac-sessions .ac-session').forEach((row) => {
        const s = (Media.sessions || []).find((x) => x.key === row.dataset.key);
        if (!s) return;
        const pos = Media.position(s);
        $('.ac-s-bar i', row).style.width = s.duration && pos != null ? `${(100 * pos) / s.duration}%` : '0';
        $('.ac-s-sub', row).textContent = [s.artist, s.duration ? `${clock(pos)} / ${clock(s.duration)}` : ''].filter(Boolean).join(' · ');
      });
    },
    browserPanel() {
      if (!this.el) return;
      const b = this.browser;
      const box = $('#ac-browser');
      if (!b) { box.innerHTML = '<div class="mm-empty">—</div>'; return; }
      const names = { NOT_RUNNING: 'not running', READY: 'ready', PAGE_READY: 'page loaded', PAGE_LOADING: 'loading', AUTHENTICATION_REQUIRED: 'needs you to sign in',
        PROFILE_SELECTION: 'asking which profile', FIRST_RUN_SETUP: 'first-run setup', CONSENT_REQUIRED: 'cookie consent', OFFLINE: 'offline', ERROR: 'error page',
        LAUNCHING: 'starting', NAVIGATING: 'navigating' };
      const bad = ['AUTHENTICATION_REQUIRED', 'PROFILE_SELECTION', 'FIRST_RUN_SETUP', 'CONSENT_REQUIRED', 'OFFLINE', 'ERROR'].includes(b.state);
      box.innerHTML = `<div class="ac-b-state ${bad ? 'bad' : b.state === 'NOT_RUNNING' ? 'off' : 'ok'}"><b></b><span></span></div>
        <div class="ac-b-tab"></div><div class="ac-b-url"></div>
        ${b.waiting ? '<div class="ac-b-wait"></div>' : ''}
        <label class="ac-b-prof">Profile <select></select></label>
        <div class="ac-b-note"></div>`;
      $('.ac-b-state b', box).textContent = (b.browser || 'Browser').toUpperCase();
      $('.ac-b-state span', box).textContent = names[b.state] || b.state.toLowerCase();
      $('.ac-b-tab', box).textContent = b.title || '';
      $('.ac-b-url', box).textContent = b.url || '';
      if (b.waiting) $('.ac-b-wait', box).textContent = `Waiting for you: ${b.waiting.label} (${b.waiting.why}). I'll carry on by myself once it's done.`;
      const sel = $('.ac-b-prof select', box);
      const profs = b.profiles || [];
      sel.innerHTML = `<option value="auto">Automatic${b.profile ? ` (${esc(b.profile.name)}${b.profile.email ? ' · ' + esc(b.profile.email) : ''})` : ''}</option>` +
        profs.map((p) => `<option value="${esc(p.directory)}">${esc(p.name)}${p.email ? ' · ' + esc(p.email) : ''}</option>`).join('');
      sel.value = (S.settings && S.settings.browser_profile) || 'auto';
      if (sel.value === '') sel.value = 'auto';
      sel.onchange = async () => { const r = await call('browser_set_profile', sel.value); if (r) { this.browser = r; S.settings.browser_profile = sel.value; this.browserPanel(); } };
      $('.ac-b-prof', box).classList.toggle('hidden', !profs.length);
      $('.ac-b-note', box).textContent = b.address_bar ? 'Reading the address bar to confirm pages loaded.' : 'Confirming pages from window titles.';
    },
    run() {
      if (!this.el) return;
      const r = Protocols.running || this.lastRun;
      const box = $('#ac-timeline');
      if (!r) { $('#ac-run-name').textContent = ''; box.innerHTML = '<div class="mm-empty">No automation running. Say “run Morning”, or try two things at once: “pause the music and switch to Harper”.</div>'; return; }
      const live = !!Protocols.running;
      $('#ac-run-name').textContent = `${r.ephemeral ? 'your request' : r.name}${live ? ' · running' : ` · ${r.status || ''}`}`;
      const results = r.results || (r.steps || []).map((t) => ({ text: t, status: 'pending' }));
      box.innerHTML = results.map((x, i) => `<div class="ac-step ${esc(x.status)}"><i>${STATUS_MARK[x.status] || '•'}</i><div><b>${i + 1}. ${esc(x.text)}</b>${x.detail && x.status !== 'ok' ? `<em>${esc(x.detail)}</em>` : ''}</div>${x.seconds != null ? `<span>${x.seconds}s</span>` : ''}</div>`).join('')
        + (live && r.confirm ? `<div class="ac-confirm"><span>Step ${r.confirm.index + 1} ${esc(r.confirm.why ? `${r.confirm.why}.` : 'needs your OK.')} Go ahead?</span><button class="btn sm primary" data-yes="1"><span>GO AHEAD</span></button><button class="btn sm ghost" data-yes="0">SKIP</button></div>` : '')
        + (live ? '<button class="btn sm ghost danger ac-stop" type="button">STOP</button>' : '');
      $$('[data-yes]', box).forEach((b) => (b.onclick = () => call('protocol_answer', b.dataset.yes === '1')));
      const stop = $('.ac-stop', box);
      if (stop) stop.onclick = () => call('protocol_stop');
    },
    healthPanel() {
      if (!this.el) return;
      const h = this.health;
      const box = $('#ac-health');
      if (!h) { box.innerHTML = ''; return; }
      const rows = [['Neural core', h.neural_core, h.model || 'offline'], ['Voice', h.voice, ''], ['Microphone', h.microphone, ''], ['Wake word', h.wake_word, ''],
        ['Media sessions', h.media_sessions, h.media_sessions ? 'verified control' : 'media keys only'], ['App volumes', h.app_volumes, h.app_volumes ? '' : ((h.helper_errors || {}).audio || 'unavailable')],
        ['Address bar', h.address_bar, ''], ['Screen reading', h.ocr, ''], ['Google link', h.google, ''], ['Internet tools', h.internet, '']];
      box.innerHTML = rows.map(([k, ok, note]) => `<div class="ac-h ${ok ? 'ok' : 'bad'}" title="${esc(note || '')}"><i></i><b>${esc(k)}</b><span>${esc(ok ? (note && note !== 'offline' ? note : 'OK') : (note ? String(note).slice(0, 40) : 'off'))}</span></div>`).join('');
    },
    correctionsPanel() {
      if (!this.el) return;
      const box = $('#ac-corrections');
      const items = (this.corrections || []).slice().reverse();
      if (!items.length) { box.innerHTML = '<li class="mm-empty">Nothing learnt yet. If I get something wrong, say “that was wrong”, then tell me what you meant: I\'ll remember it.</li>'; return; }
      box.innerHTML = items.map(() => '<li><span class="h"></span><i>→</i><span class="m"></span><em></em><button type="button" class="mm-btn del" title="Forget this correction" aria-label="Forget this correction">×</button></li>').join('');
      $$('li', box).forEach((li, i) => {
        const c = items[i];
        $('.h', li).textContent = `“${c.heard}”`;
        $('.m', li).textContent = `“${c.meant}”`;
        $('em', li).textContent = [c.whole ? 'this exact sentence' : 'wherever it comes up', c.voice_only ? 'speech only' : '', c.uses ? `used ${c.uses}×` : ''].filter(Boolean).join(' · ');
        $('.del', li).onclick = async () => { const r = await call('corrections_delete', c.id); if (r && r.ok) toast('Forgotten.', 'ok', 1800); };
      });
    },
    log() {
      if (!this.el) return;
      const kinds = ['all', 'command', 'media', 'browser', 'app', 'protocol', 'feedback'];
      $('#ac-log-kinds').innerHTML = kinds.map((k) => `<button type="button" class="${k === this.kind ? 'on' : ''}" data-k="${k}">${k.toUpperCase()}</button>`).join('');
      $$('#ac-log-kinds button').forEach((b) => (b.onclick = () => { this.kind = b.dataset.k; this.log(); }));
      const q = this.query.trim().toLowerCase();
      const items = this.items.filter((a) => (this.kind === 'all' || a.kind === this.kind) && (!q || `${a.text} ${a.detail}`.toLowerCase().includes(q))).slice(-80).reverse();
      $('#ac-log').innerHTML = items.length ? items.map((a) => `<li class="${esc(a.status)} k-${esc(a.kind)}"><i title="${esc(a.status)}">${a.kind === 'command' ? '›' : (STATUS_MARK[a.status] || '•')}</i><b></b><span>${esc(fmtTime(new Date(a.at * 1000)).slice(0, 5))}</span><em></em></li>`).join('') : '<li class="mm-empty">Nothing yet.</li>';
      $$('#ac-log li', this.el).forEach((li, i) => { if (!items[i]) return; $('b', li).textContent = items[i].text; $('em', li).textContent = items[i].detail || ''; });
    },
    init() {
      this.el = $('#center');
      $('#btn-center').onclick = () => (this.shown() ? this.close() : this.open());
      $('#ac-close').onclick = () => this.close();
      this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
      $('#ac-log-search').oninput = (e) => { this.query = e.target.value; this.log(); };
      setInterval(() => { if (this.shown()) { call('activity_list').then((a) => { if (a) { this.health = a.health; this.healthPanel(); } }); } }, 8000);
    },
  };

  // ========================================================================= the window (borderless: our own title bar)
  const Win = {
    max: false, full: false,
    set(st) {
      if (!st) return;
      this.max = !!st.maximized; this.full = !!st.fullscreen;
      document.body.classList.toggle('win-max', this.max);
      document.body.classList.toggle('win-full', this.full);
      $('#btn-max').title = this.max ? 'Restore' : 'Maximise';
      $('#btn-max').setAttribute('aria-label', this.max ? 'Restore' : 'Maximise');
      $('#btn-full').title = this.full ? 'Leave full screen (Esc)' : 'Full screen (F11)';
    },
    async toggleMax() { this.set(await call('window_toggle_maximize')); },
    async toggleFull() { this.set(await call('window_toggle_fullscreen')); },
    init() {
      $('#btn-min').onclick = () => call('window_minimize');
      $('#btn-max').onclick = () => this.toggleMax();
      $('#btn-full').onclick = () => this.toggleFull();
      $('#btn-close').onclick = () => { if (window.__winDebug) window.__winDebug.close = (window.__winDebug.close || 0) + 1; call('window_close'); };
      // Dragging: once the mouse moves a few pixels with the button down on the title bar, Windows takes over the move.
      // A plain click still clicks (the J.A.R.V.I.S. badge opens the personalities) and a double-click maximises.
      const NOT_DRAG = 'button, input, select, textarea, a, label, .chip.clickable, [data-no-drag]';
      let down = null;
      const dbg = (window.__winDebug = { down: 0, drag: 0, dbl: 0, grip: 0 });
      document.addEventListener('mousedown', (e) => {
        if (e.target.closest && e.target.closest('.drag-region')) dbg.down++;
        if (e.button !== 0 || this.full || !e.target.closest('.drag-region') || e.target.closest(NOT_DRAG)) { down = null; return; }
        down = { x: e.screenX, y: e.screenY };
      });
      document.addEventListener('mousemove', (e) => {
        if (!down || !(e.buttons & 1)) { down = null; return; }
        if (Math.abs(e.screenX - down.x) + Math.abs(e.screenY - down.y) >= 4) { down = null; dbg.drag++; call('window_drag'); }
      });
      document.addEventListener('mouseup', () => { down = null; });
      document.addEventListener('dblclick', (e) => {
        if (e.target.closest('.drag-region') && !e.target.closest(NOT_DRAG)) { e.preventDefault(); dbg.dbl++; this.toggleMax(); }
      });
      $$('.rz').forEach((g) => g.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        dbg.grip++;
        call('window_resize', g.dataset.edge);
      }));
      call('window_state').then((st) => this.set(st));
      // status chips: show as many whole chips as fit between the badge and the clock, never half of one
      const fit = () => {
        const box = $('#titlebar .chips');
        const chips = $$('.chip', box);
        chips.forEach((c) => c.classList.remove('squeezed'));
        for (let i = chips.length - 1; i >= 0 && box.scrollWidth > box.clientWidth + 1; i--) chips[i].classList.add('squeezed');
      };
      this.fitChips = fit;
      window.addEventListener('resize', () => requestAnimationFrame(fit));
      new MutationObserver(() => requestAnimationFrame(fit)).observe($('#btn-update'), { attributes: true, attributeFilter: ['class'] });
      setTimeout(fit, 200); setTimeout(fit, 2500);
    },
  };

  // ========================================================================= updates
  const Updates = {
    st: null,
    label: { idle: '', checking: 'Checking…', up_to_date: 'Up to date', available: 'New version found', downloading: 'Downloading…',
      verifying: 'Checking the download…', ready: 'Ready to install', installing: 'Installing — restarting in a moment…', installed: 'Installed',
      rolled_back: 'Update rolled back', error: 'Problem', unsupported: 'Updates install into Jarvis.exe (this copy runs from source)' },
    set(st) {
      if (!st) return;
      this.st = { ...(this.st || {}), ...st };
      const u = this.st;
      const rel = u.release || null;
      $('#upd-version').textContent = `Version ${u.current || '…'}`;
      let text = this.label[u.state] || u.state || '';
      if (u.state === 'downloading') text = `Downloading ${rel ? rel.version : ''}… ${Math.round((u.progress || 0) * 100)}%`;
      if (u.state === 'ready' && rel) text = `Version ${rel.version} is ready`;
      if (u.state === 'up_to_date' && u.checked_at) text = `Up to date · checked ${ago(u.checked_at)}`;
      if ((u.state === 'error' || u.state === 'rolled_back') && u.error) text = u.error.charAt(0).toUpperCase() + u.error.slice(1);
      if (u.detail && u.state === 'up_to_date') text = u.detail;
      $('#upd-state').textContent = text;
      $('#upd-state').className = `upd-state ${u.state}`;
      $('#upd-bar').classList.toggle('hidden', u.state !== 'downloading');
      $('#upd-progress').style.width = `${Math.round((u.progress || 0) * 100)}%`;
      const notes = rel && rel.notes ? rel.notes.split('\n').map((l) => l.trim()).filter((l) => /^[-*•]\s/.test(l)).slice(0, 6) : [];
      $('#upd-notes').classList.toggle('hidden', !notes.length || !['ready', 'available', 'downloading', 'verifying'].includes(u.state));
      $('#upd-notes').innerHTML = notes.length ? `<b>WHAT'S NEW</b><ul>${notes.map(() => '<li></li>').join('')}</ul>` : '';
      $$('#upd-notes li').forEach((li, i) => (li.textContent = notes[i].replace(/^[-*•]\s*/, '')));
      $('#upd-install').classList.toggle('hidden', u.state !== 'ready');
      $('#upd-restore').classList.toggle('hidden', !u.has_previous || u.state === 'installing');
      $('#upd-check').disabled = ['checking', 'downloading', 'verifying', 'installing', 'unsupported'].includes(u.state);
      $('#update-settings').classList.toggle('unsupported', u.state === 'unsupported');
      const ready = u.state === 'ready' && rel;
      $('#btn-update').classList.toggle('hidden', !ready && u.state !== 'installing');
      $('#update-btn-text').textContent = u.state === 'installing' ? 'UPDATING…' : 'UPDATE';
      $('#btn-update').title = ready ? `Version ${rel.version} is ready — click to install and restart` : 'Installing the update…';
      if (u.state === 'installing') $('#update-overlay').classList.remove('hidden');
      if (['error', 'rolled_back', 'ready'].includes(u.state)) $('#update-overlay').classList.add('hidden');
    },
    async install(restore = false) {
      const r = await call(restore ? 'update_restore_previous' : 'update_install');
      if (r && !r.ok) toast(r.error || 'Could not install it.', 'error');
      else { $('#update-overlay').classList.remove('hidden'); $('#update-overlay-text').textContent = restore ? 'Going back to the previous version…' : `Installing version ${(this.st.release || {}).version || ''}…`; }
    },
    async check() {
      $('#upd-state').textContent = 'Checking…';
      const r = await call('update_check');
      if (!r) return;
      this.set(r);
      if (!r.ok) toast(r.error || 'Could not check for updates.', 'error');
      else if (!r.available) toast(`You're on the latest version (${r.current}).`, 'ok', 2500);
    },
    init() {
      $('#btn-update').onclick = () => { if (this.st && this.st.state === 'ready') this.install(); };
      $('#upd-install').onclick = () => this.install();
      $('#upd-check').onclick = () => this.check();
      let armed = 0;
      $('#upd-restore').onclick = () => {
        if (!armed) { armed = setTimeout(() => { armed = 0; $('#upd-restore').textContent = 'GO BACK TO PREVIOUS VERSION'; }, 4000); $('#upd-restore').textContent = 'CLICK AGAIN TO GO BACK'; return; }
        clearTimeout(armed); armed = 0; this.install(true);
      };
    },
  };

  // ========================================================================= events from Python
  function handle(ev) {
    switch (ev.type) {
      case 'state':
        S.state = ev.state;
        if (ev.state !== 'LISTENING' && ev.state !== 'THINKING') S.listenPhase = null;
        if (ev.state !== 'THINKING') S.activity = null;
        if (ev.state === 'IDLE') { Feed.mic = null; document.body.style.setProperty('--mic-level', 0); $('.ml-val').style.strokeDashoffset = 232.5; }
        Hud.setState(ev.state);
        break;
      case 'listen_phase': S.listenPhase = ev.phase; Hud.updateSub(); break;
      case 'mic_frame':
        Feed.setMic(ev);
        document.body.style.setProperty('--mic-level', (ev.level || 0) / 100);
        $('.ml-val').style.strokeDashoffset = (232.5 * (1 - Math.min(1, (ev.level || 0) / 70))).toFixed(1);
        break;
      case 'speech_clip': Feed.setClip(ev); break;
      case 'speech_stop': Feed.stop(); break;
      case 'user_message': Chat.user(ev); break;
      case 'assistant_start': Chat.start(ev.id); break;
      case 'assistant_token': Chat.token(ev.id, ev.text); break;
      case 'assistant_end': Chat.end(ev); if (ev.stats) { S.core = ev.stats; Hud.updateCore(); } break;
      case 'system_message': Chat.system(ev.text, ev.level); break;
      case 'wake_status': { const { type, ...w } = ev; S.wake = w; Hud.updateAudio(); Hud.updateSub(); break; }
      case 'wake':
        reactorRef && reactorRef.flare();
        $('#mic-btn').classList.remove('woke'); void $('#mic-btn').offsetWidth; $('#mic-btn').classList.add('woke');
        break;
      case 'tool_activity': S.activity = ev.label; Chat.system(ev.label, 'tool'); Hud.updateSub(); break;
      case 'activity': S.activity = ev.label; Hud.updateSub(); break;
      case 'document': Chat.document(ev); break;
      case 'document_view': Reader.open(ev); break;
      case 'timers': Timers.set(ev.timers); break;
      case 'vision_status': { const { type, ...v } = ev; Vision.update(v); break; }
      case 'vision_look': Vision.seen(ev); break;
      case 'act_confirm': Vision.confirm(ev); break;
      case 'act_status': Vision.status(ev); break;
      case 'file_list': Chat.files(ev); break;
      case 'email_draft': Chat.email(ev); break;
      case 'email_status': Chat.emailStatus(ev); break;
      case 'notice': toast(ev.text, ev.level); break;
      case 'ollama_status': Ollama.update(ev); break;
      case 'core_stats': { const { type, ...c } = ev; S.core = c; Hud.updateCore(); break; }
      case 'pull_progress': if (ev.role === 'vision') Vision.progress(ev); else if (ev.role !== 'embed') Ollama.progress(ev); break;
      case 'pull_done': if (ev.role === 'vision') Vision.done(ev); else if (ev.role === 'embed') { toast(ev.ok ? 'Smarter recall is ready: I can now find memories by meaning.' : `Download failed: ${ev.error}`, ev.ok ? 'ok' : 'error'); } else Ollama.pullDone(ev); break;
      case 'voice_status': S.voiceOk = ev.ok; Hud.updateAudio(); break;
      case 'mic_status': { const { type, ...m } = ev; S.mic = { ...S.mic, ...m }; Hud.updateAudio(); break; }
      case 'settings': { const { type, ...s } = ev; S.settings = s; Hud.applySettings(); if ($('#settings').classList.contains('open')) Settings.fill(); break; }
      case 'persona': { const { type, ...p } = ev; Personas.apply(p); break; }
      case 'listening': S.paused = !!ev.paused; Hud.updateAudio(); Hud.updateSub(); if (ev.paused) Chat.system('Microphone paused. Press the mic button, or type "start listening", to talk again.'); break;
      case 'personas': Personas.setList(ev.personas); break;
      case 'wake_learn': Personas.wake(ev); break;
      case 'memory_learned': Chat.memoryNote(ev); if (!MemoryCore.el.classList.contains('hidden')) MemoryCore.refresh(); break;
      case 'memory_forgotten': Chat.memoryNote(ev, true); if (!MemoryCore.el.classList.contains('hidden')) MemoryCore.refresh(); break;
      case 'memory_changed': MemoryCore.stats(ev.stats); if (!MemoryCore.el.classList.contains('hidden')) MemoryCore.refresh(); break;
      case 'memory_open': MemoryCore.open(); break;
      case 'memory_cleared': MemoryCore.refresh(); break;
      case 'protocols': Protocols.set(ev); break;
      case 'protocol': Protocols.progress(ev); break;
      case 'media': Media.update(ev); break;
      case 'activity_log': Center.add(ev.item); break;
      case 'update': { const { type, ...u } = ev; Updates.set(u); break; }
      case 'window_state': { const { type, ...w } = ev; Win.set(w); break; }
      case 'corrections': Center.corrections = ev.items || []; Center.correctionsPanel(); break;
      case 'browser': { const { type, ...b } = ev; Center.browser = { ...(Center.browser || {}), ...b }; Center.browserPanel(); break; }
      case 'protocol_edit': Protocols.open(false, ev.protocol); break;
      default: break;
    }
  }

  window.JARVIS = {
    receive(events) {
      for (const ev of events) {
        try { handle(ev); } catch (err) { console.error('event', ev && ev.type, err); }
      }
    },
  };

  // ========================================================================= boot
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function bootSequence(p) {
    const log = $('#boot-log'), bar = $('#boot-progress');
    let skip = false;
    const skipper = () => { skip = true; };
    window.addEventListener('keydown', skipper, { once: true });
    $('#boot').addEventListener('click', skipper, { once: true });

    const o = p.ollama || {};
    const lines = [
      ['Initialising holographic interface', 'OK', 'ok'],
      ['Bridging host process · python', 'OK', 'ok'],
      ['Audio subsystem', p.audio.available ? (p.audio.output ? 'OK' : 'VIRTUAL') : 'OFFLINE', p.audio.output ? 'ok' : 'warn'],
      [`Voice matrix · ${voiceShort(p.settings.voice)}`, p.settings.voice_enabled ? 'OK' : 'MUTED', p.settings.voice_enabled ? 'ok' : 'warn'],
      [`Speech recognition · ${p.stt_engine}`, p.mic.available ? 'OK' : 'NO MIC', p.mic.available ? 'ok' : 'warn'],
      [`Neural core @ ${(o.host || '').replace(/^https?:\/\//, '')}`, o.online ? (o.model ? 'ONLINE' : 'NO MODEL') : 'OFFLINE', o.online && o.model ? 'ok' : 'bad'],
      [`Telemetry · ${p.system.cores_logical || '?'} logical cores`, 'OK', 'ok'],
    ];
    log.innerHTML = '';
    const head = document.createElement('div');
    head.textContent = `> J.A.R.V.I.S. build ${p.version} · host ${p.system.hostname}`;
    log.append(head);
    for (let i = 0; i < lines.length; i++) {
      const [label, status, cls] = lines[i];
      const row = document.createElement('div');
      row.textContent = `> ${label} `.padEnd(52, '.') + ' ';
      log.append(row);
      if (!skip) await sleep(140 + Math.random() * 130);
      const tag = document.createElement('span');
      tag.className = cls; tag.textContent = `[ ${status} ]`;
      row.append(tag);
      bar.style.width = `${((i + 1) / lines.length) * 100}%`;
      log.scrollTop = log.scrollHeight;
    }
    const ok = o.online && o.model && p.mic.available && p.audio.available;
    const tail = document.createElement('div');
    tail.innerHTML = `> <span class="${ok ? 'ok' : 'warn'}">${ok ? 'ALL SYSTEMS NOMINAL' : 'SYSTEMS ONLINE · DEGRADED MODE'}</span>`;
    log.append(tail);
    log.scrollTop = log.scrollHeight;
    if (!skip) await sleep(650);
  }

  /** Browser preview only: index.html?mock (or ?ollama=offline|nomodel) or a file:// URL. Never inside the app. */
  const PREVIEW = location.protocol === 'file:' || /[?&](mock|ollama)\b/.test(location.search);

  function bootNotice(text, cls = 'warn') {
    const log = $('#boot-log');
    if (!log) return;
    const row = document.createElement('div');
    row.innerHTML = `> <span class="${cls}"></span>`;
    row.lastChild.textContent = text;
    log.append(row);
    log.scrollTop = log.scrollHeight;
  }

  function waitForApi() {
    return new Promise((resolve) => {
      let settled = false;
      const done = (a) => { if (!settled) { settled = true; resolve(a); } };
      if (PREVIEW) {
        const s = document.createElement('script');
        s.src = 'devmock.js';
        s.onload = () => done(window.createJarvisMock());
        document.head.append(s);
        return;
      }
      if (window.pywebview && window.pywebview.api && window.pywebview.api.ui_ready) return done(window.pywebview.api);
      window.addEventListener('pywebviewready', () => done(window.pywebview.api));
      // pywebview injects its bridge after the page loads; on a slow first start that can take a while.
      setTimeout(() => { if (!settled) bootNotice('Waiting for the J.A.R.V.I.S. core to respond...'); }, 12000);
      setTimeout(() => {
        if (!settled) bootNotice('The core is not responding. Close this window and start J.A.R.V.I.S. again; details are in %APPDATA%\\JARVIS\\jarvis.log', 'bad');
      }, 45000);
    });
  }

  function bindControls() {
    const mic = $('#mic-btn');
    mic.addEventListener('pointerdown', (e) => { e.preventDefault(); mic.setPointerCapture(e.pointerId); Ptt.down(); });
    mic.addEventListener('pointerup', () => Ptt.up());
    mic.addEventListener('pointercancel', () => Ptt.up());
    mic.addEventListener('contextmenu', (e) => e.preventDefault());

    $('#cmd-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('#cmd'), text = input.value.trim();
      if (!text) return;
      input.value = '';
      call('send_text', text);
    });
    $('#stop-btn').onclick = () => call('interrupt');
    $('#look-btn').onclick = () => { call('play_sfx', 'click'); call('vision_look', ''); };
    $('#watch-stop').onclick = () => call('vision_stop_watch');
    $('#chip-vision').onclick = () => { Settings.open(); setTimeout(() => $('#vision-settings').scrollIntoView({ behavior: 'smooth' }), 250); };
    $('#vision-download').onclick = async () => {
      const r = await call('vision_install', 'qwen2.5vl:7b');
      if (r && r.ok) { $('#vision-pull').classList.remove('hidden'); $('#vision-pull-text').textContent = 'Starting download…'; }
      else toast((r && r.error) || 'Could not start the download.', 'error');
    };
    $('#btn-clear-log').onclick = () => Chat.empty();
    $('#btn-settings').onclick = () => { call('play_sfx', 'click'); Settings.toggle(); };
    Win.init();

    $('#boot-close').onclick = () => (api ? call('window_close') : window.close());
    $('#g-setup').onclick = () => Google.open();
    $('#g-update').onclick = () => Google.open(true);
    $('#g-disconnect').onclick = async () => { await call('google_disconnect'); Google.refresh(); toast('Google Docs disconnected.', 'info'); };
    $('#g-copy').onclick = () => Google.copy();
    $('#g-connect').onclick = () => Google.connect();
    $('#g-cancel').onclick = () => Google.close();
    $('#ol-retry').onclick = () => Ollama.retry(false);
    $('#ol-start').onclick = () => Ollama.start();
    $('#ol-pull').onclick = () => Ollama.pull();
    $('#ol-cancel-pull').onclick = () => call('cancel_pull');
    $('#ol-dismiss').onclick = () => Ollama.dismiss();
    $$('[data-open]').forEach((b) => (b.onclick = () => call('open_url', b.dataset.open)));
    $('#log').addEventListener('click', (e) => {
      const a = e.target.closest('a[data-url]');
      if (a) { e.preventDefault(); call('open_url', a.dataset.url); }
    });
    $$('[data-copy]').forEach((b) => (b.onclick = async () => {
      const text = $(b.dataset.copy).textContent;
      try { await navigator.clipboard.writeText(text); }
      catch {
        const ta = Object.assign(document.createElement('textarea'), { value: text });
        document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
      }
      b.textContent = 'COPIED'; setTimeout(() => (b.textContent = 'COPY'), 1500);
    }));

    const typing = () => /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    window.addEventListener('keydown', (e) => {
      if (!S.booted) return;
      const modalOpen = ['#reader', '#google', '#memory', '#personas', '#persona-edit', '#wake-teach', '#protocols', '#center'].some((id) => !$(id).classList.contains('hidden'));
      if (e.code === 'Space' && !typing() && !modalOpen) { e.preventDefault(); if (!e.repeat) Ptt.down(); return; }
      if (e.key === 'Escape') {
        if (Win.full && !modalOpen && !$('#settings').classList.contains('open')) { Win.toggleFull(); return; }
        if (!$('#persona-edit').classList.contains('hidden')) PersonaEditor.close();
        else if (!$('#wake-teach').classList.contains('hidden')) WakeTeach.close();
        else if (!$('#memory').classList.contains('hidden')) MemoryCore.close();
        else if (Center.shown()) Center.close();
        else if (!$('#protocols').classList.contains('hidden')) { if (!$('#pr-form').classList.contains('hidden')) Protocols.back(); else Protocols.close(); }
        else if (!$('#personas').classList.contains('hidden')) Personas.close();
        else if (!$('#reader').classList.contains('hidden')) Reader.close();
        else if (!$('#google').classList.contains('hidden')) Google.close();
        else if ($('#settings').classList.contains('open')) Settings.close();
        else if (document.activeElement === $('#cmd') && $('#cmd').value) $('#cmd').value = '';
        else call('interrupt');
        return;
      }
      if (e.key === '/' && !typing()) { e.preventDefault(); $('#cmd').focus(); return; }
      if (e.key === 'F11') { e.preventDefault(); Win.toggleFull(); return; }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'l') { e.preventDefault(); Chat.empty(); }
      if ((e.ctrlKey || e.metaKey) && e.key === ',') { e.preventDefault(); Settings.toggle(); }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'm') { e.preventDefault(); MemoryCore.el.classList.contains('hidden') ? MemoryCore.open() : MemoryCore.close(); }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'r') { e.preventDefault(); Protocols.el.classList.contains('hidden') ? Protocols.open() : Protocols.close(); }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'j') { e.preventDefault(); Center.shown() ? Center.close() : Center.open(); }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'p') { e.preventDefault(); Personas.el.classList.contains('hidden') ? Personas.open() : Personas.close(); }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'l') { e.preventDefault(); call('vision_look', ''); }
    });
    window.addEventListener('keyup', (e) => { if (e.code === 'Space') Ptt.up(); });
    window.addEventListener('blur', () => Ptt.up());
  }

  async function main() {
    Chat.init();
    Settings.init();
    Reader.init();
    Personas.init();
    PersonaEditor.init();
    WakeTeach.init();
    MemoryCore.init();
    Protocols.init();
    Media.init();
    Center.init();
    Updates.init();
    bindControls();
    Hud.clock();
    setInterval(() => Hud.clock(), 1000);
    setInterval(() => Timers.tick(), 500);

    const reactor = new Reactor($('#reactor'));
    reactorRef = reactor;
    const wave = new Wave($('#wave'));
    let last = performance.now();
    const frame = (now) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      Col.update(dt);
      Feed.update(now, dt);
      reactor.draw(now, dt);
      wave.draw(now);
      Chat.tick(dt);
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);

    api = await waitForApi();
    const p = await call('ui_ready');
    if (!p) {
      bootNotice('The J.A.R.V.I.S. core failed to start. Details are in %APPDATA%\\JARVIS\\jarvis.log', 'bad');
      return;
    }
    S.settings = p.settings; S.mic = { ...p.mic, engine: p.stt_engine }; S.audio = p.audio; S.core = p.core; S.system = p.system;
    S.ollama = p.ollama;
    S.wake = p.wake || null;
    S.paused = !!p.paused;
    document.body.classList.toggle('framed', !(p.window ? p.window.frameless : true));
    $('#st-cpu-name').textContent = p.system.cpu_name;
    $('#st-cpu-name').title = p.system.cpu_name;
    $('#about').textContent = `J.A.R.V.I.S. ${p.version} · ${p.system.os} · runs locally · no API keys`;
    Hud.applySettings(); Hud.updateCore(); Hud.setState(p.state || 'IDLE');
    if (p.vision) Vision.update(p.vision);
    if (p.personas) S.personas = p.personas;
    if (p.persona) Personas.apply(p.persona);
    if (p.memory) MemoryCore.stats(p.memory);
    if (p.protocols) Protocols.set(p.protocols);
    if (p.media) Media.set(p.media);
    if (p.activity) Center.items = p.activity.slice();
    if (p.update) Updates.set(p.update);
    Telemetry.start();

    call('play_sfx', 'boot');
    reactor.powerTarget = 0.35;
    await bootSequence(p);
    $('#boot').classList.add('done');
    document.body.classList.remove('booting');
    reactor.powerTarget = 1;
    S.booted = true;
    setTimeout(() => $('#boot').remove(), 1200);
    Chat.restored(p.restored);
    call('boot_complete');
    setTimeout(() => Chat.suggest(), 5200);
    setTimeout(() => { if (S.ollama) Ollama.update(S.ollama); }, 1500);
    setTimeout(async () => {
      const st = await Google.refresh();
      if (st && st.configured && st.outdated) Chat.system('Your Google script needs a one-time update (it adds renaming documents, plus Gmail, Sheets and slide editing): Settings ▸ Google ▸ UPDATE SCRIPT.', 'warn');
    }, 4000);
  }

  document.addEventListener('DOMContentLoaded', main);
})();
