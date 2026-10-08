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
    link_browser: 'default', theme: params.get('theme') || 'arc', allow_files: true, allow_internet: true, auto_language: true, stt_extra_languages: '', user_name: '', wake_word: true, wake_sensitivity: 0.5, stt_engine: 'auto', stt_language: 'en-US', whisper_model: 'base.en', vosk_model_path: '', pause_threshold: 1.0, patience: 3,
    listen_timeout: 10, max_phrase_seconds: 45, auto_listen: false, user_title: 'sir', frameless: true,
  };
  if (params.has('gold')) settings.google_script_url = 'https://script.google.com/macros/s/preview/exec';
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
      wake: { enabled: true, active: true, phrase: 'Hey Jarvis', reason: null },
    }),
    boot_complete: async () => reply(online && models.length
      ? 'Good evening, sir. All systems are online. How may I help?'
      : "Good evening, sir. I'm afraid my neural core is offline. I've put instructions on screen to bring it online.", true),
    play_sfx: async () => {},
    send_text: async (text) => {
      cancelAll();
      const spanish = /[¿¡ñ]|\b(qué|hola|escribe|biografía)\b/i.test(text);
      emit({ type: 'user_message', id: `u${++seq}`, text, source: 'text', ...(spanish ? { lang: 'es', lang_name: 'Spanish' } : {}) });
      if (/\b(bio|biography|essay|presentation|slides)\b/i.test(text)) {
        const slides = /presentation|slides/i.test(text);
        const topic = (text.match(/(?:on|about|of)\s+(.+)$/i) || [, 'Lionel Messi'])[1];
        setState('THINKING');
        later(300, () => emit({ type: 'tool_activity', tool: 'web_search', label: `Searching the web: ${topic}` }));
        later(1100, () => emit({ type: 'tool_activity', tool: 'compose', label: `Writing ${slides ? 'presentation' : 'biography'}: ${topic}` }));
        [120, 260, 410, 588].forEach((w, i) => later(1400 + i * 500, () => emit({ type: 'activity', label: `Writing · ${w} words` })));
        later(3600, () => emit({ type: 'document', doc_kind: slides ? 'slides' : 'doc', title: slides ? `${topic}: An Overview` : `${topic}: The Little Genius`, url: 'https://docs.google.com/document/d/preview/edit' }));
        later(3700, () => reply(`Done. I've written ${slides ? `an 8-slide presentation on ${topic}` : `a 612-word biography of ${topic}`} and opened it for you.`, true));
        return true;
      }
      const timer = text.match(/timer for (\d+) (second|minute)/i);
      if (timer) {
        const secs = +timer[1] * (/minute/i.test(timer[2]) ? 60 : 1);
        emit({ type: 'timers', timers: [{ id: ++seq, label: '', left: secs, total: secs }] });
        reply(`Timer set for ${timer[1]} ${timer[2]}s, sir.`);
        return true;
      }
      if (/\b(recent|what) (docs|documents|files)\b/i.test(text)) {
        setState('THINKING');
        later(900, () => emit({ type: 'file_list', files: [
          { kind: 'doc', id: 'a', title: 'Lionel Messi: The Little Genius', url: 'https://docs.google.com/document/d/a/edit', updated: new Date(Date.now() - 3.6e6).toISOString() },
          { kind: 'slides', id: 'b', title: 'The Solar System: An Overview', url: 'https://docs.google.com/presentation/d/b/edit', updated: new Date(Date.now() - 8.6e7).toISOString() },
          { kind: 'sheet', id: 'c', title: 'Budget', url: 'https://docs.google.com/spreadsheets/d/c/edit', updated: new Date(Date.now() - 3e5).toISOString() }] }));
        later(1000, () => reply('Here are your 3 most recent files, sir.', true));
        return true;
      }
      if (/\b(show|open) (me )?(my )?(messi )?doc\b/i.test(text)) {
        setState('THINKING');
        later(700, () => this.google_view('doc'));
        later(800, () => reply('Here is Lionel Messi: The Little Genius, sir.', true));
        return true;
      }
      if (/\be-?mail\b/i.test(text)) {
        setState('THINKING');
        later(400, () => emit({ type: 'tool_activity', tool: 'email', label: 'Writing an email to Sarah Connor' }));
        later(1500, () => emit({ type: 'email_draft', id: `m${++seq}`, to: 'sarah.connor@example.com', to_name: 'Sarah Connor', subject: 'Running a little late',
          body: "Hi Sarah,\n\nJust a quick note to say I'm running about ten minutes late for our meeting. Sorry for the delay, I'll be there as soon as I can.\n\nBest regards,\nTony",
          candidates: [{ email: 'sarah.connor@example.com', name: 'Sarah Connor' }, { email: 'slee@school.edu', name: 'Sarah Lee' }], can_send: true }));
        later(1600, () => reply("I've drafted an email to Sarah Connor. Shall I send it?", true));
        return true;
      }
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
    google_status: async () => ({ configured: !!settings.google_script_url, outdated: params.has('gold'), url: settings.google_script_url || '' }),
    google_script: async () => '// J.A.R.V.I.S. bridge script (preview)',
    google_connect: async (url) => (!url && settings.google_script_url ? { ok: true, user: 'tony@example.com' } : /\/exec$/.test(url) ? (settings.google_script_url = url, { ok: true, user: 'tony@example.com' }) : { ok: false, error: 'that doesn\'t look like a web app URL (it should end in /exec)' }),
    google_disconnect: async () => { settings.google_script_url = ''; },
    installed_browsers: async () => ({ chrome: 'Google Chrome', edge: 'Microsoft Edge' }),
    google_view: async (kind) => {
      emit({ type: 'document_view', doc_kind: kind, title: kind === 'sheet' ? 'Budget' : 'Lionel Messi: The Little Genius', url: 'https://docs.google.com/document/d/preview/edit',
        ...(kind === 'sheet' ? { rows: [['Item', 'Cost'], ['Rent', '1200'], ['Food', '300']] }
          : { markdown: '# Lionel Messi\n\n## Early life\n\nLionel Andrés Messi was born on 24 June 1987 in Rosario, Argentina.\n\n## Career\n\n- Barcelona (2004–2021)\n- Paris Saint-Germain (2021–2023)\n- Inter Miami (2023–)' }) });
    },
    email_send: async (id, to) => { setTimeout(() => emit({ type: 'email_status', id, status: 'sent', to }), 600); return { ok: true }; },
    email_open_gmail: async (id) => { emit({ type: 'email_status', id, status: 'opened' }); return { ok: true }; },
    email_discard: async (id) => { emit({ type: 'email_status', id, status: 'discarded' }); },
    preview_voice: async () => reply('Good day, sir. This is how I will sound from now on.', true),
    open_url: async (url) => { window.open(url, '_blank'); return true; },
    window_minimize: async () => {}, window_toggle_maximize: async () => {}, window_toggle_fullscreen: async () => {}, window_close: async () => {},
  };
};
