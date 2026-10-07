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

  const S = {
    state: 'IDLE', listenPhase: null, settings: {}, ollama: null, mic: null, audio: null,
    core: null, voiceOk: true, voices: null, booted: false, system: null,
  };

  let api = null;
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
  function toast(text, level = 'info', ms = 4200) {
    const el = document.createElement('div');
    el.className = `toast ${level}`;
    el.textContent = text;
    $('#toasts').append(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 400); }, ms);
  }

  // ========================================================================= chat log
  function renderMarkdown(src) {
    const inline = (t) => esc(t)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
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
      el.innerHTML = `<div class="msg-meta"><span class="who">${who}</span><time>${fmtTime()}</time>${extra}</div><div class="msg-body"></div>`;
      const stick = this.pinned();
      this.log.append(el);
      while (this.log.children.length > 300) this.log.firstElementChild.remove();
      this.scroll(stick);
      return el;
    },
    user(ev) {
      const el = this.make('user', 'YOU', `<span class="src">${ev.source === 'voice' ? 'VOICE' : 'TEXT'}</span>`);
      $('.msg-body', el).textContent = ev.text;
      this.count++; this.updateCount();
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
    start(id) {
      const el = this.make('jarvis pending', 'J.A.R.V.I.S.');
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
      if (st === 'IDLE') sub = o && o.online && o.model ? `Awaiting your command, ${title()}` : 'Neural core offline · limited functionality';
      else if (st === 'LISTENING') {
        sub = ph === 'calibrating' ? 'Calibrating to ambient noise…'
          : ph === 'capturing' ? (Ptt.holding ? 'Receiving · release to transmit' : 'Receiving audio…')
            : `Go ahead, ${title()} — I'm listening`;
      } else if (st === 'THINKING') sub = ph === 'transcribing' ? 'Decoding speech…' : `Consulting ${(o && o.model) || 'neural core'}…`;
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
      if (mic.available) this.chip('#chip-mic', 'ok', engine || 'READY');
      else this.chip('#chip-mic', 'bad', 'NO DEVICE');
      $('#mic-btn').classList.toggle('disabled', !mic.available);

      this.setDD('#am-voice', `${voiceShort(set.voice)} · ${(set.voice || '').split('-').slice(0, 2).join('-')}`, voiceOn && S.voiceOk ? '' : 'warn');
      this.setDD('#am-output', audio.available ? (audio.output ? 'ONLINE' : 'VIRTUAL (no device)') : 'OFFLINE', audio.output ? 'ok' : 'warn');
      this.setDD('#am-mic', mic.available ? (mic.device || 'default') : (mic.reason || 'not detected'), mic.available ? '' : 'bad');
      this.setDD('#am-stt', mic.engine || '—');
    },
    applySettings() {
      $('#cmd').placeholder = `Type a command, ${title()}…`;
      this.updateAudio();
      this.updateSub();
    },
    clock() {
      const d = new Date();
      $('#clock-time').textContent = fmtTime(d);
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
      ctx.strokeStyle = 'rgba(42,212,255,0.07)';
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
      draw(this.hist.cpu, '42,212,255');
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
          let value;
          if (input.type === 'checkbox') value = input.checked;
          else if (input.type === 'range') value = parseFloat(input.value);
          else value = input.value;
          this.save({ [key]: value }, key);
        });
      });
      $('#settings-close').onclick = () => this.close();
      $('#set-model-refresh').onclick = async () => { const st = await call('check_ollama'); if (st) Ollama.update(st); toast('Model list refreshed.', 'info'); };
      $('#set-voice-preview').onclick = () => call('preview_voice', $('#set-voice').value);
      $('#set-clear-memory').onclick = async () => { await call('clear_memory'); toast('Conversation memory purged.', 'ok'); Chat.system('Conversation memory purged.'); };
    },
    output(input) {
      const map = { 'set-temp': ['#out-temp', (v) => (+v).toFixed(2)], 'set-rate': ['#out-rate', (v) => `${v > 0 ? '+' : ''}${v}%`],
        'set-pitch': ['#out-pitch', (v) => `${v > 0 ? '+' : ''}${v} Hz`], 'set-pause': ['#out-pause', (v) => `${(+v).toFixed(1)} s`],
        'set-sfx': ['#out-sfx', (v) => `${Math.round(v * 100)}%`] };
      const m = map[input.id];
      if (m) $(m[0]).textContent = m[1](input.value);
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
    open() { this.fill(); this.fillVoices(); this.el.classList.add('open'); this.el.setAttribute('aria-hidden', 'false'); },
    close() { this.el.classList.remove('open'); this.el.setAttribute('aria-hidden', 'true'); },
    toggle() { this.el.classList.contains('open') ? this.close() : this.open(); },
  };

  // ========================================================================= events from Python
  function handle(ev) {
    switch (ev.type) {
      case 'state':
        S.state = ev.state;
        if (ev.state !== 'LISTENING' && ev.state !== 'THINKING') S.listenPhase = null;
        if (ev.state === 'IDLE') { Feed.mic = null; document.body.style.setProperty('--mic-level', 0); }
        Hud.setState(ev.state);
        break;
      case 'listen_phase': S.listenPhase = ev.phase; Hud.updateSub(); break;
      case 'mic_frame': Feed.setMic(ev); document.body.style.setProperty('--mic-level', (ev.level || 0) / 100); break;
      case 'speech_clip': Feed.setClip(ev); break;
      case 'speech_stop': Feed.stop(); break;
      case 'user_message': Chat.user(ev); break;
      case 'assistant_start': Chat.start(ev.id); break;
      case 'assistant_token': Chat.token(ev.id, ev.text); break;
      case 'assistant_end': Chat.end(ev); if (ev.stats) { S.core = ev.stats; Hud.updateCore(); } break;
      case 'system_message': Chat.system(ev.text, ev.level); break;
      case 'notice': toast(ev.text, ev.level); break;
      case 'ollama_status': Ollama.update(ev); break;
      case 'core_stats': { const { type, ...c } = ev; S.core = c; Hud.updateCore(); break; }
      case 'pull_progress': Ollama.progress(ev); break;
      case 'pull_done': Ollama.pullDone(ev); break;
      case 'voice_status': S.voiceOk = ev.ok; Hud.updateAudio(); break;
      case 'mic_status': { const { type, ...m } = ev; S.mic = { ...S.mic, ...m }; Hud.updateAudio(); break; }
      case 'settings': { const { type, ...s } = ev; S.settings = s; Hud.applySettings(); break; }
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

  function waitForApi() {
    return new Promise((resolve) => {
      let settled = false;
      const done = (a) => { if (!settled) { settled = true; resolve(a); } };
      if (window.pywebview && window.pywebview.api && window.pywebview.api.ui_ready) return done(window.pywebview.api);
      window.addEventListener('pywebviewready', () => done(window.pywebview.api));
      setTimeout(() => {
        if (settled || window.pywebview) return;
        const s = document.createElement('script');
        s.src = 'devmock.js';
        s.onload = () => done(window.createJarvisMock());
        document.head.append(s);
      }, 1200);
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
    $('#btn-clear-log').onclick = () => Chat.empty();
    $('#btn-settings').onclick = () => { call('play_sfx', 'click'); Settings.toggle(); };
    $('#btn-min').onclick = () => call('window_minimize');
    $('#btn-max').onclick = () => call('window_toggle_maximize');
    $('#btn-close').onclick = () => call('window_close');
    $$('.pywebview-drag-region').forEach((el) => el.addEventListener('dblclick', () => call('window_toggle_maximize')));

    $('#ol-retry').onclick = () => Ollama.retry(false);
    $('#ol-start').onclick = () => Ollama.start();
    $('#ol-pull').onclick = () => Ollama.pull();
    $('#ol-cancel-pull').onclick = () => call('cancel_pull');
    $('#ol-dismiss').onclick = () => Ollama.dismiss();
    $$('[data-open]').forEach((b) => (b.onclick = () => call('open_url', b.dataset.open)));
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
      if (e.code === 'Space' && !typing()) { e.preventDefault(); if (!e.repeat) Ptt.down(); return; }
      if (e.key === 'Escape') {
        if ($('#settings').classList.contains('open')) Settings.close();
        else if (document.activeElement === $('#cmd') && $('#cmd').value) $('#cmd').value = '';
        else call('interrupt');
        return;
      }
      if (e.key === '/' && !typing()) { e.preventDefault(); $('#cmd').focus(); return; }
      if (e.key === 'F11') { e.preventDefault(); call('window_toggle_fullscreen'); return; }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'l') { e.preventDefault(); Chat.empty(); }
      if ((e.ctrlKey || e.metaKey) && e.key === ',') { e.preventDefault(); Settings.toggle(); }
    });
    window.addEventListener('keyup', (e) => { if (e.code === 'Space') Ptt.up(); });
    window.addEventListener('blur', () => Ptt.up());
  }

  async function main() {
    Chat.init();
    Settings.init();
    bindControls();
    Hud.clock();
    setInterval(() => Hud.clock(), 1000);

    const reactor = new Reactor($('#reactor'));
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
    if (!p) { toast('Backend did not respond.', 'error', 10000); return; }
    S.settings = p.settings; S.mic = { ...p.mic, engine: p.stt_engine }; S.audio = p.audio; S.core = p.core; S.system = p.system;
    S.ollama = p.ollama;
    document.body.classList.toggle('framed', !(p.window ? p.window.frameless : true));
    $('#st-cpu-name').textContent = p.system.cpu_name;
    $('#st-cpu-name').title = p.system.cpu_name;
    $('#about').textContent = `J.A.R.V.I.S. ${p.version} · ${p.system.os} · runs locally · no API keys`;
    Hud.applySettings(); Hud.updateCore(); Hud.setState(p.state || 'IDLE');
    Telemetry.start();

    call('play_sfx', 'boot');
    reactor.powerTarget = 0.35;
    await bootSequence(p);
    $('#boot').classList.add('done');
    document.body.classList.remove('booting');
    reactor.powerTarget = 1;
    S.booted = true;
    setTimeout(() => $('#boot').remove(), 1200);
    call('boot_complete');
    setTimeout(() => { if (S.ollama) Ollama.update(S.ollama); }, 1500);
  }

  document.addEventListener('DOMContentLoaded', main);
})();
