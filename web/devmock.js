/* Simulated backend so web/index.html can be previewed in a normal browser.
   Never loaded inside the desktop app (pywebview provides the real API).
   Open web/index.html?mock (served over http) or straight from disk.
   URL options: ?ollama=offline | ?ollama=nomodel */
window.createJarvisMock = function createJarvisMock() {
  'use strict';
  const params = new URLSearchParams(location.search);
  const emit = (ev) => window.JARVIS.receive(Array.isArray(ev) ? ev : [ev]);
  const TAU = Math.PI * 2;
  let state = 'IDLE';
  let seq = 0;
  let timers = [];
  let micTimer = 0;
  let memory = 0;
  const mode = params.get('ollama');
  let models = mode === 'nomodel' ? [] : ['llama3.2:latest', 'mistral:latest'];
  let online = mode !== 'offline';
  const settings = {
    ollama_host: 'http://localhost:11434', model: 'llama3.2', temperature: 0.7, max_history_turns: 12, custom_instructions: '',
    voice: 'en-GB-RyanNeural', speech_rate: 0, speech_pitch: 0, voice_enabled: true, sfx_enabled: true, sfx_volume: 0.45,
    allow_files: true, allow_internet: true, stt_engine: 'auto', stt_language: 'en-US', whisper_model: 'base.en', vosk_model_path: '', pause_threshold: 0.9,
    listen_timeout: 8, max_phrase_seconds: 25, auto_listen: false, user_title: 'sir', frameless: true,
  };
  const later = (ms, fn) => timers.push(setTimeout(fn, ms));
  const cancelAll = () => { timers.forEach(clearTimeout); timers = []; clearInterval(micTimer); };
  const setState = (s) => { const prev = state; state = s; emit({ type: 'state', state: s, prev }); };
  const status = () => ({
    type: 'ollama_status', online, host: settings.ollama_host, models: online ? models : [],
    model: online && models.length ? models[0] : null, error: online ? (models.length ? null : 'No chat model is installed in Ollama.') : 'Ollama is not reachable.',
    executable: null, download_url: 'https://ollama.com/download', suggested_model: 'llama3.2',
  });

  function frames(seconds, fps = 40) {
    const out = [], levels = [];
    for (let i = 0; i < seconds * fps; i++) {
      const t = i / fps;
      const env = Math.max(0, Math.sin(t * TAU * 2.3)) * (0.55 + 0.45 * Math.sin(t * 1.7)) * (t % 1.8 < 1.55 ? 1 : 0.05);
      const centre = 12 + 7 * Math.sin(t * 3.1);
      const f = [];
      for (let k = 0; k < 48; k++) {
        const formant = Math.exp(-((k - centre) ** 2) / 70) + 0.55 * Math.exp(-((k - centre - 14) ** 2) / 40);
        f.push(Math.round(Math.min(100, 100 * env * formant + Math.random() * 8 * env)));
      }
      out.push(f); levels.push(Math.round(100 * env));
    }
    return { frames: out, levels };
  }

  function reply(text, announce = false) {
    const id = `a${++seq}`;
    if (!announce) setState('THINKING');
    later(announce ? 50 : 650, () => {
      emit({ type: 'assistant_start', id });
      const tokens = text.match(/\S+\s*/g) || [];
      tokens.forEach((tok, i) => later(i * 55, () => emit({ type: 'assistant_token', id, text: tok })));
      const dur = Math.max(1.5, text.length / 15);
      later(380, () => { setState('SPEAKING'); emit({ type: 'speech_clip', fps: 40, duration: dur, ...frames(dur) }); });
      later(Math.max(tokens.length * 55, dur * 1000 + 400), () => {
        if (!announce) memory += 1;
        emit({ type: 'assistant_end', id, interrupted: false, error: null,
          stats: { online, model: models[0] || null, host: settings.ollama_host, first_token_ms: 412, tokens_per_sec: 38.6, memory_turns: memory } });
        setState('IDLE');
      });
    });
  }

  const answers = [
    'Certainly, sir. All primary systems are operating within normal parameters, and the local neural core is responding nicely.',
    "I've run the diagnostics. Processor load is modest, memory is comfortable, and there is nothing that requires your attention.",
    'Here is a quick example:\n```python\nprint("Hello from J.A.R.V.I.S.")\n```\nShall I explain it further?',
  ];

  return {
    ui_ready: async () => ({
      version: '1.0.0-preview', settings: { ...settings }, state: 'IDLE', ollama: status(),
      mic: { available: true, device: 'Browser preview microphone', reason: null }, stt_engine: 'Google Web Speech',
      stt_engines: {}, audio: { available: true, output: true, error: null },
      system: { hostname: 'stark-tower', os: 'Windows 11', cpu_name: 'Preview CPU @ 4.20GHz', cores_physical: 8, cores_logical: 16, ram_total_gb: 32, python: '3.12' },
      core: { online, model: models[0] || null, host: settings.ollama_host, first_token_ms: null, tokens_per_sec: null, memory_turns: 0 },
      window: { frameless: true },
    }),
    boot_complete: async () => reply(online && models.length
      ? 'Good evening, sir. All systems are online. How may I help?'
      : "Good evening, sir. I'm afraid my neural core is offline. I've put instructions on screen to bring it online.", true),
    play_sfx: async () => {},
    send_text: async (text) => {
      cancelAll();
      emit({ type: 'user_message', id: `u${++seq}`, text, source: 'text' });
      const pick = /code|python|script/i.test(text) ? 2 : /diagnos|status|system/i.test(text) ? 1 : 0;
      reply(online ? answers[pick] : "I'm afraid my neural core is offline, sir.");
      return true;
    },
    start_listening: async () => {
      if (state === 'LISTENING') { cancelAll(); setState('IDLE'); return 'stopping'; }
      cancelAll();
      setState('LISTENING');
      emit({ type: 'listen_phase', phase: 'calibrating' });
      later(300, () => emit({ type: 'listen_phase', phase: 'waiting' }));
      later(900, () => emit({ type: 'listen_phase', phase: 'capturing' }));
      const f = frames(3, 30);
      let i = 0;
      micTimer = setInterval(() => { emit({ type: 'mic_frame', bands: f.frames[i % f.frames.length], level: f.levels[i % f.frames.length] }); i++; }, 33);
      later(2600, () => {
        clearInterval(micTimer);
        setState('THINKING');
        emit({ type: 'listen_phase', phase: 'transcribing' });
        later(500, () => {
          emit({ type: 'user_message', id: `u${++seq}`, text: 'Run a full system diagnostic', source: 'voice' });
          reply(answers[1]);
        });
      });
      return 'listening';
    },
    hold_listening: async () => {},
    stop_listening: async () => {},
    interrupt: async () => { cancelAll(); emit({ type: 'speech_stop' }); const was = state !== 'IDLE'; setState('IDLE'); return was; },
    clear_memory: async () => { memory = 0; },
    get_system_stats: async () => {
      const t = Date.now() / 1000;
      const cpu = 18 + 14 * Math.sin(t / 7) + Math.random() * 8;
      return {
        cpu: +cpu.toFixed(1), per_core: Array.from({ length: 16 }, (_, k) => Math.round(Math.max(2, cpu + 25 * Math.sin(t / 3 + k)))),
        ram: +(54 + 4 * Math.sin(t / 11)).toFixed(1), ram_used_gb: 17.4, ram_total_gb: 32,
        net_up_kbps: +(12 + Math.random() * 30).toFixed(1), net_down_kbps: +(140 + Math.random() * 400).toFixed(1),
        uptime_s: Math.floor(273000 + t % 1000), processes: 312, disk: 61.5, battery: { percent: 87, plugged: true }, cpu_freq_ghz: 4.2,
      };
    },
    get_core_stats: async () => ({}),
    check_ollama: async () => { const s = status(); setTimeout(() => emit(s), 0); return s; },
    start_ollama: async () => { setTimeout(() => { online = true; emit(status()); }, 2000); return { started: true }; },
    pull_model: async (name) => {
      let pct = 0;
      const tick = setInterval(() => {
        pct += 7;
        emit({ type: 'pull_progress', model: name, status: pct < 100 ? 'pulling dde5aa3fc5ff' : 'success', completed: pct * 2e7, total: 2e9, percent: Math.min(100, pct) });
        if (pct >= 100) { clearInterval(tick); models = ['llama3.2:latest']; emit({ type: 'pull_done', model: name, ok: true }); emit(status()); }
      }, 250);
      return { ok: true };
    },
    cancel_pull: async () => {},
    get_settings: async () => ({ ...settings }),
    save_settings: async (changes) => { Object.assign(settings, changes); return { ok: true, settings: { ...settings } }; },
    list_voices: async () => [
      { id: 'en-GB-RyanNeural', label: 'Ryan · en-GB · Male' }, { id: 'en-GB-ThomasNeural', label: 'Thomas · en-GB · Male' },
      { id: 'en-GB-SoniaNeural', label: 'Sonia · en-GB · Female' }, { id: 'en-US-AndrewNeural', label: 'Andrew · en-US · Male' },
    ],
    preview_voice: async () => reply('Good day, sir. This is how I will sound from now on.', true),
    open_url: async (url) => { window.open(url, '_blank'); return true; },
    window_minimize: async () => {}, window_toggle_maximize: async () => {}, window_toggle_fullscreen: async () => {}, window_close: async () => {},
  };
};
