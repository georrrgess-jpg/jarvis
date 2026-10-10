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
    link_browser: 'default', theme: params.get('theme') || 'arc', vision_model: '', allow_control: true, act_confirm: 'auto', watch_interval: 2,
    vision_exclusions: 'password, 1password, bitwarden, lastpass, keepass, dashlane, bank, banking, paypal', allow_files: true, allow_internet: true, auto_language: true, stt_extra_languages: '', user_name: '', wake_word: true, wake_sensitivity: 0.5, stt_engine: 'auto', stt_language: 'en-US', whisper_model: 'base.en', vosk_model_path: '', pause_threshold: 1.0, patience: 3,
    listen_timeout: 10, max_phrase_seconds: 45, auto_listen: false, user_title: 'sir', frameless: true,
    persona: params.get('persona') || 'jarvis', weather_location: '', temperature_unit: 'auto', persona_theme: true, memory_enabled: true, memory_auto_learn: true, memory_resume: true, routine_reminders: true,
  };
  const PERSONAS = [
    { id: 'jarvis', name: 'Jarvis', display: 'J.A.R.V.I.S.', tagline: 'The impeccable butler', description: 'Calm, precise and quietly witty. Short, polished answers with a dry British sense of humour.', voice: 'en-GB-RyanNeural', gender: 'male', theme: 'arc', address: 'sir' },
    { id: 'harper', name: 'Harper', display: 'HARPER', tagline: 'Your kind, curious companion', description: 'Exceptionally kind, friendly and informative. Loves a real conversation, explains things clearly with examples, remembers what matters to you and cheers you on.', voice: 'en-US-AvaNeural', gender: 'female', theme: 'rose', address: 'Tony' },
    { id: 'friday', name: 'Friday', display: 'F.R.I.D.A.Y.', tagline: 'Quick, upbeat and a little cheeky', description: 'Fast and casual with an Irish lilt. Gets straight to the point, keeps things light and calls you boss.', voice: 'en-IE-EmilyNeural', gender: 'female', theme: 'mark3', address: 'boss' },
    { id: 'sage', name: 'Sage', display: 'SAGE', tagline: 'The patient mentor', description: 'A calm, encouraging tutor who explains step by step, checks you have understood, and loves a good analogy.', voice: 'en-US-AndrewNeural', gender: 'male', theme: 'stealth', address: 'Tony' },
  ];
  const wakeOf = (p) => (p.id === 'jarvis' ? { name: 'Jarvis', phrase: 'Hey Jarvis', state: 'ready', progress: 1, builtin: true }
    : p.id === 'harper' ? { name: p.name, phrase: 'Hey Harper', state: 'ready', progress: 1, metrics: { held_out_recall: 0.94 }, user_samples: 0, recordings: 0 }
      : { name: p.name, phrase: `Hey ${p.name}`, state: 'missing', progress: 0, recordings: 0 });
  PERSONAS.forEach((p) => { p.wake = wakeOf(p); p.color = p.theme; });
  const listPersonas = () => PERSONAS.map((p) => ({ ...p, active: p.id === settings.persona }));
  const personaInfo = () => ({ ...PERSONAS.find((p) => p.id === settings.persona), active: true });
  const fakeLearn = (p) => {
    let k = 0;
    const tick = () => {
      k += 0.1;
      p.wake = { ...p.wake, state: k >= 1 ? 'ready' : 'learning', progress: Math.min(1, k), label: k < 0.3 ? 'Learning what everyday speech sounds like' : k < 0.75 ? `Learning to hear “Hey ${p.name}”` : 'Training the listener', metrics: { held_out_recall: 0.92 } };
      emit({ type: 'wake_learn', ...p.wake });
      if (k < 1) setTimeout(tick, 500);
    };
    tick();
  };
  const now = Date.now() / 1000;
  let memId = 10;
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const mem = (id, kind, text, extra = {}) => ({ id, kind, text, key: '', meta: {}, source: 'said', pinned: false, uses: 0, created: now - id * 86400, updated: now - id * 3600, last_used: 0, when: '', ...extra });
  let memories = params.has('nomemory') ? [] : [
    mem(1, 'fact', 'You have a golden retriever called Max.', { source: 'learned', uses: 3 }),
    mem(2, 'fact', 'Your sister is called Ana and lives in Madrid.', { pinned: true }),
    mem(3, 'preference', 'You love jazz, especially Miles Davis.', { uses: 2 }),
    mem(4, 'preference', 'You like short, to-the-point answers in the morning.', { source: 'manual' }),
    mem(5, 'routine', 'You go to yoga every Tuesday and Thursday at 6 PM.', { meta: { days: [1, 3], time: '18:00' }, when: 'Tue, Thu · 6 PM' }),
    mem(6, 'project', "You're building a website for your mum's bakery.", { meta: { status: 'active' }, source: 'learned' }),
    mem(7, 'project', 'You were learning to play the guitar.', { meta: { status: 'done' } }),
  ];
  let episodes = params.has('nomemory') ? [] : [
    { id: 1, session: 's1', summary: 'The user asked for a bio of Lionel Messi, which was written to Google Docs, then emailed it to Sarah.', started: now - 90000, ended: now - 86000, turns: 6 },
    { id: 2, session: 's0', summary: 'The user talked about planning a trip to Lisbon in May and asked for restaurant ideas.', started: now - 400000, ended: now - 396000, turns: 9 },
  ];
  const memStats = () => {
    const c = { fact: 0, preference: 0, routine: 0, project: 0 };
    memories.forEach((m) => { c[m.kind]++; });
    return { ...c, total: memories.length, turns: 40, episodes: episodes.length, enabled: settings.memory_enabled, auto_learn: settings.memory_auto_learn, embed_model: params.has('semantic') ? 'nomic-embed-text:latest' : '', path: '%APPDATA%\\JARVIS\\memory.db', error: null };
  };
  const memChanged = () => emit({ type: 'memory_changed', stats: memStats() });
  if (params.has('gold')) settings.google_script_url = 'https://script.google.com/macros/s/preview/exec';
  const mockVision = { model: 'qwen2.5vl:7b', models: ['qwen2.5vl:7b'], ocr: true, ocr_engine: 'Windows OCR', watching: false, watch_label: '',
    allow_control: true, pulling: false, suggested: [] };
  const fakeShot = () => {
    const c = document.createElement('canvas'); c.width = 640; c.height = 400;
    const g = c.getContext('2d'); g.fillStyle = '#f4f6f8'; g.fillRect(0, 0, 640, 400); g.fillStyle = '#1a73e8'; g.fillRect(0, 0, 640, 46);
    g.fillStyle = '#fff'; g.font = 'bold 20px sans-serif'; g.fillText('Inbox - Mail', 16, 30); g.fillStyle = '#222'; g.font = '16px sans-serif';
    ['Sarah Connor: Lunch tomorrow?', 'GitHub: Build failed on main', 'Tony: Mark 85 schematics'].forEach((t, i) => g.fillText(t, 24, 90 + i * 40));
    g.fillStyle = '#1a73e8'; g.fillRect(500, 340, 110, 38); g.fillStyle = '#fff'; g.fillText('Send', 535, 365);
    return c.toDataURL('image/jpeg', 0.8);
  };
  window.__jarvisMockLook = () => {
    setState('THINKING');
    later(300, () => emit({ type: 'tool_activity', tool: 'vision', label: 'Looking at Inbox - Mail' }));
    later(700, () => emit({ type: 'vision_look', image: fakeShot(), title: 'Inbox - Mail', model: 'qwen2.5vl:7b', ocr: true }));
    later(900, () => reply("You're in your inbox, sir. There's a failed build notification from GitHub and a lunch invitation from Sarah.", true));
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

  const step = (text, extra = {}) => ({ text, when: {}, confirm: false, approved: false, continue_on_error: null, retries: 0, timeout: 90, fallback: '', enabled: true,
    label: /^open /i.test(text) ? 'Open app / file' : /^say /i.test(text) ? 'Say' : /^wait /i.test(text) ? 'Wait' : /play|pause|music/i.test(text) ? 'Media' : 'Ask JARVIS',
    risk: /^close |send |delete /i.test(text) ? 'closes programs' : '', ...extra });
  const hist = (status, ago, steps) => ({ at: now - ago, status, trigger: 'voice', seconds: 9, steps: steps.map((t, i) => ({ text: t, status: i === 1 && status === 'failed' ? 'failed' : (i === 2 ? 'unverified' : 'ok'), detail: i === 1 && status === 'failed' ? "Spotify didn't open" : '' })) });
  let protocols = [
    { id: 'p1', name: 'Morning', icon: 'sun', category: 'Morning', description: 'Music, weather and email to start the day.', phrases: ['good morning jarvis'], enabled: true, on_failure: 'continue',
      steps: [step('open Spotify'), step('play some focus music'), step("what's the weather today"), step('say Good morning, sir'), step('open Gmail', { when: { type: 'weekday', days: [0, 1, 2, 3, 4] }, condition: 'on weekdays' })],
      schedule: { time: '07:30', days: [0, 1, 2, 3, 4], enabled: true }, triggers: { startup: false, app: '', hotkey: '' }, created: now, last_run: now - 86000,
      history: [hist('done', 86000, ['open Spotify', 'play some focus music', "what's the weather today", 'say Good morning, sir', 'open Gmail'])] },
    { id: 'p2', name: 'Gaming Mode', icon: 'game', category: 'Gaming', description: 'Discord up, music down, Steam open.', phrases: ['game time'], enabled: true, on_failure: 'stop',
      steps: [step('open Discord'), step('turn the music down'), step('open Steam'), step('close Chrome', { confirm: true, risk: 'closes programs' })],
      schedule: {}, triggers: { startup: false, app: '', hotkey: 'Ctrl+Alt+G' }, created: now, last_run: now - 4000,
      history: [hist('failed', 4000, ['open Discord', 'turn the music down', 'open Steam'])] },
    { id: 'p3', name: 'House Party', icon: 'party', category: 'Entertainment', description: '', phrases: [], enabled: false, on_failure: 'stop',
      steps: [step('play party hits on YouTube'), step('set the volume to 80'), step("say Let's get this party started")], schedule: {}, triggers: {}, created: now, last_run: 0, history: [] },
  ];
  const ICONS = ['bolt', 'sun', 'moon', 'game', 'code', 'music', 'film', 'focus', 'work', 'home', 'coffee', 'rocket', 'party', 'book', 'heart', 'shield'];
  const CATS = ['General', 'Morning', 'Work', 'Development', 'Gaming', 'Entertainment', 'Focus', 'Home', 'Evening'];
  const TEMPLATES = [
    { name: 'Development', icon: 'code', category: 'Development', description: 'Editor, terminal and docs, music low.', steps: ['open VS Code', 'open Windows Terminal', 'open github.com', 'play some lo-fi music', 'set the music volume to 30%'], phrases: ['time to code'] },
    { name: 'Focus', icon: 'focus', category: 'Focus', description: 'A 25-minute focus block.', steps: ['pause everything', 'set a timer for 25 minutes', 'say Focus mode on'], phrases: ['focus mode'] },
    { name: 'Wind Down', icon: 'moon', category: 'Evening', description: 'Quiet music, a reminder to sleep.', steps: ['play some calm piano music', 'set the music volume to 25%', 'remind me in 45 minutes to go to bed'], phrases: [] },
  ];
  let proposal = null;
  const protoList = () => ({ protocols: protocols.map((p) => ({ ...p, when: p.schedule && p.schedule.time ? `on weekdays at ${p.schedule.time}` : '' })), running: null, recording: null,
    proposal, templates: TEMPLATES, icons: ICONS, categories: CATS, suggestions: [{ steps: ['open Discord', 'play some focus music'], count: 4 }] });
  const sessions = [
    { key: 'spotify', app: 'Spotify', label: 'Spotify', process: 'spotify', title: 'Bohemian Rhapsody (Remastered 2011)', artist: 'Queen', status: 'playing', position: 72, duration: 354, can: ['play', 'pause', 'next', 'previous'], source: 'session', volume: 0.8, muted: false },
    { key: 'chrome:youtube', app: 'Chrome', label: 'YouTube', process: 'chrome', title: 'Lo-fi beats to study to', artist: 'Lofi Girl', status: 'paused', position: 1260, duration: null, can: ['play', 'pause'], source: 'session', volume: 1, muted: false },
  ];
  const t0 = now;
  const activity = [
    { at: t0 - 400, kind: 'command', text: 'play bohemian rhapsody on spotify', status: 'ok', detail: 'voice' },
    { at: t0 - 398, kind: 'media', text: 'Playing Bohemian Rhapsody on Spotify', status: 'ok', detail: 'Windows reports playing' },
    { at: t0 - 300, kind: 'command', text: 'open gmail', status: 'ok', detail: 'voice' },
    { at: t0 - 296, kind: 'browser', text: 'Gmail: waiting for you to sign in', status: 'waiting', detail: 'accounts.google.com' },
    { at: t0 - 200, kind: 'protocol', text: 'Gaming Mode: open Steam', status: 'failed', detail: "Steam didn't open within 90 s" },
    { at: t0 - 100, kind: 'media', text: 'Paused YouTube', status: 'unverified', detail: 'sent the media key; Windows did not confirm' },
  ];
  const browser = { state: 'AUTHENTICATION_REQUIRED', title: 'Sign in - Google Accounts', url: 'https://accounts.google.com/v3/signin/identifier', browser: 'Chrome',
    profile: { name: 'Tony', email: 'tony@example.com', directory: 'Default' }, waiting: { label: 'Gmail', why: 'Google sign-in' }, history: [], address_bar: true,
    profiles: [{ name: 'Tony', email: 'tony@example.com', directory: 'Default' }, { name: 'Work', email: 'tony@stark.example', directory: 'Profile 1' }] };
  const winState = { maximized: false, fullscreen: false };
  const updateState = { state: 'ready', current: '1.1.38', progress: 1, error: '', checked_at: now - 600, has_previous: true, auto: true,
    release: { version: '1.1.42', notes: '- One-click updates\n- Say “that was wrong” to correct me\n- The window moves, resizes and goes full screen properly' } };
  const health = { neural_core: true, model: 'llama3.2', voice: true, microphone: true, wake_word: true, media_sessions: true, app_volumes: true, address_bar: true, helper_errors: {}, ocr: true, google: false, internet: true };

  return {
    ui_ready: async () => ({
      version: '1.0.0-preview', settings: { ...settings }, state: 'IDLE', ollama: status(),
      mic: { available: true, device: 'Browser preview microphone', reason: null }, stt_engine: 'Google Web Speech',
      stt_engines: {}, audio: { available: true, output: true, error: null },
      system: { hostname: 'stark-tower', os: 'Windows 11', cpu_name: 'Preview CPU @ 4.20GHz', cores_physical: 8, cores_logical: 16, ram_total_gb: 32, python: '3.12' },
      core: { online, model: models[0] || null, host: settings.ollama_host, first_token_ms: null, tokens_per_sec: null, memory_turns: 0 },
      window: { frameless: true },
      wake: { enabled: true, active: true, phrase: settings.persona === 'harper' ? 'Hey Harper' : 'Hey Jarvis', phrases: ['Hey Jarvis'], reason: null }, vision: mockVision,
      persona: personaInfo(), personas: listPersonas(), memory: memStats(), protocols: protoList(),
      media: sessions[0], activity: activity.slice(), update: { ...updateState },
      restored: params.has('restored') ? [{ role: 'user', text: 'Any ideas for the bakery website homepage?', ts: now - 3000 },
        { role: 'assistant', text: 'Lead with a big photo of the bread, the opening hours and a "call to order" button. Want me to sketch a layout?', ts: now - 2990 }] : [],
    }),
    boot_complete: async () => (setTimeout(() => emit({ type: 'media', playing: sessions[0], sessions: sessions.map((x) => ({ ...x })), status: { sessions: true, mixer: true, keys: true } }), 200), reply(online && models.length
      ? 'Good evening, sir. All systems are online. How may I help?'
      : "Good evening, sir. I'm afraid my neural core is offline. I've put instructions on screen to bring it online.", true)),
    play_sfx: async () => {},
    send_text: async (text) => {
      cancelAll();
      const sw = text.match(/(?:switch to|talk to|bring back)\s+(jarvis|harper|friday|sage)/i);
      if (sw) {
        emit({ type: 'user_message', id: `u${++seq}`, text, source: 'text' });
        settings.persona = sw[1].toLowerCase();
        settings.theme = PERSONAS.find((p) => p.id === settings.persona).color;
        settings.voice = PERSONAS.find((p) => p.id === settings.persona).voice;
        emit([{ type: 'persona', ...personaInfo() }, { type: 'settings', ...settings }]);
        reply({ harper: "Hi Tony, Harper here! It's so nice to talk with you. What's on your mind?", jarvis: 'At your service, sir. J.A.R.V.I.S. is back online.',
          friday: 'F.R.I.D.A.Y. here, boss. What are we working on?', sage: 'Hello, Tony. Sage here. What would you like to understand today?' }[settings.persona], true);
        return true;
      }
      if (/^(?:create|make) a protocol/i.test(text)) {
        emit({ type: 'user_message', id: `u${++seq}`, text, source: 'text' });
        proposal = { name: 'Game Night', icon: 'game', category: 'Gaming', description: 'Discord, then the game.', phrases: [], enabled: true, on_failure: 'stop',
          steps: [step('open Discord'), step('turn the music down'), step('open Rocket League'), step('close Chrome', { risk: 'closes programs' })],
          schedule: {}, triggers: { hotkey: 'Ctrl+Alt+N' }, stage: 'approve', question: '' };
        emit({ type: 'protocols', ...protoList() });
        reply("Here's Game Night: open Discord, turn the music down, open Rocket League, then close Chrome, which will ask you first. Shall I save it?", true);
        return true;
      }
      const rem = text.match(/^(?:please )?remember (?:that )?(.+)$/i);
      if (rem) {
        emit({ type: 'user_message', id: `u${++seq}`, text, source: 'text' });
        const said = rem[1].replace(/\bI'm\b/gi, "you're").replace(/\bI\b/g, 'you').replace(/\bmy\b/gi, 'your').replace(/[.!]$/, '');
        const m = mem(++memId, /like|love|hate|prefer/i.test(said) ? 'preference' : 'fact', said[0].toUpperCase() + said.slice(1) + '.');
        memories.unshift(m);
        emit({ type: 'memory_learned', memory: m, status: 'added' }); memChanged();
        reply(settings.persona === 'harper' ? `Got it! I'll remember that ${said}.` : `Very good, sir. I'll remember that ${said}.`, true);
        return true;
      }
      if (/what do you (know|remember) about me/i.test(text)) {
        emit({ type: 'user_message', id: `u${++seq}`, text, source: 'text' });
        emit({ type: 'memory_open' });
        reply("Here's what I know, sir: you have a golden retriever called Max, your sister is called Ana, you love jazz, and you're building a website for your mum's bakery. It's all on screen in the Memory Core.", true);
        return true;
      }
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
      if (/what'?s on my screen|look at/i.test(text)) { window.__jarvisMockLook(); return true; }
      if (/^click send/i.test(text)) {
        setState('THINKING');
        later(500, () => emit({ type: 'act_confirm', id: `act${++seq}`, label: 'click send', where: 'Inbox - Mail', reason: 'risky', image: fakeShot() }));
        later(600, () => reply('Is this the right one, sir? Say yes and I\'ll click send.', true));
        return true;
      }
      if (/tell me when|watch my screen/i.test(text)) {
        mockVision.watching = true; mockVision.watch_label = text.replace(/^.*?when /i, '') || 'anything important';
        emit({ type: 'vision_status', ...mockVision });
        reply(`Very well, sir. I'll keep an eye on it.`);
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
    persona_list: async () => listPersonas(),
    persona_save: async (d) => {
      if (!d.name || !d.name.trim()) return { ok: false, error: 'Give your personality a name (letters only, up to 24 characters).' };
      if ((d.description || '').trim().length < 10) return { ok: false, error: 'Describe the personality in a sentence or two.' };
      const id = d.id || `my-${d.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
      const p = { id, name: d.name.trim(), display: d.name.trim().toUpperCase(), tagline: d.description.split(/[.!?]/)[0].slice(0, 48), description: d.description,
        voice: d.voice, gender: d.gender, theme: d.color, color: d.color, address: d.address || 'Tony', custom: true, saved: { ...d, id } };
      p.wake = { name: p.name, phrase: `Hey ${p.name}`, state: 'queued', progress: 0, recordings: 0 };
      const i = PERSONAS.findIndex((x) => x.id === id);
      if (i >= 0) PERSONAS[i] = p; else PERSONAS.push(p);
      setTimeout(() => { emit({ type: 'personas', personas: listPersonas() }); fakeLearn(p); }, 50);
      return { ok: true, persona: { ...p, active: settings.persona === id } };
    },
    persona_delete: async (id) => { const i = PERSONAS.findIndex((x) => x.id === id); if (i >= 0) PERSONAS.splice(i, 1); if (settings.persona === id) settings.persona = 'jarvis'; setTimeout(() => emit({ type: 'personas', personas: listPersonas() }), 0); return { ok: true }; },
    persona_color: async (id, color) => {
      const p = PERSONAS.find((x) => x.id === id);
      p.color = color || p.theme;
      if (id === settings.persona && settings.persona_theme) { settings.theme = p.color; setTimeout(() => emit({ type: 'settings', ...settings }), 0); }
      return { ok: true, persona: { ...p, active: id === settings.persona } };
    },
    wake_learn: async (id) => { fakeLearn(PERSONAS.find((x) => x.id === id)); return { ok: true }; },
    wake_record: async (id) => { const p = PERSONAS.find((x) => x.id === id); await new Promise((r) => setTimeout(r, 1200)); p.wake.recordings = (p.wake.recordings || 0) + 1; return { ok: true, count: p.wake.recordings, level: 0.6 }; },
    wake_train_voice: async (id) => { const p = PERSONAS.find((x) => x.id === id); p.wake.user_samples = p.wake.recordings; fakeLearn(p); return { ok: true }; },
    wake_clear_voice: async (id) => { PERSONAS.find((x) => x.id === id).wake.recordings = 0; return { ok: true }; },
    persona_set: async (id) => {
      settings.persona = id;
      if (settings.persona_theme) settings.theme = PERSONAS.find((p) => p.id === id).color;
      settings.voice = PERSONAS.find((p) => p.id === id).voice;
      setTimeout(() => emit({ type: 'settings', ...settings }), 0);
      return { ok: true, persona: personaInfo() };
    },
    persona_preview: async () => {},
    protocol_list: async () => protoList(),
    protocol_save: async (d) => {
      const steps = (Array.isArray(d.steps) ? d.steps : String(d.steps || '').split('\n')).map((x) => (typeof x === 'string' ? step(x.trim()) : step(x.text.trim(), x))).filter((x) => x.text);
      if (!String(d.name || '').trim()) return { ok: false, error: 'Give the protocol a name.' };
      if (!steps.length) return { ok: false, error: 'A protocol needs at least one step.' };
      let p = protocols.find((x) => x.id === d.id) || protocols.find((x) => x.name.toLowerCase() === d.name.trim().toLowerCase());
      if (!p) { p = { id: `p${Date.now()}`, created: Date.now() / 1000, last_run: 0 }; protocols.push(p); }
      Object.assign(p, { history: [], icon: 'bolt', category: 'General', description: '', phrases: [], triggers: {}, enabled: true, on_failure: 'stop' }, p.history ? { history: p.history } : {},
        { name: d.name.trim(), steps, schedule: d.schedule || {}, icon: d.icon || 'bolt', category: d.category || 'General', description: d.description || '', phrases: d.phrases || [],
          triggers: d.triggers || {}, enabled: d.enabled !== false, on_failure: d.on_failure || 'stop' });
      setTimeout(() => emit({ type: 'protocols', ...protoList() }), 0);
      return { ok: true, protocol: p };
    },
    protocol_delete: async (id) => { const p = protocols.find((x) => x.id === id); protocols = protocols.filter((x) => x.id !== id); return { ok: !!p, protocol: p || null }; },
    protocol_restore: async (d) => { protocols.push(d); return { ok: true }; },
    protocol_run: async (id) => {
      const p = protocols.find((x) => x.id === id);
      p.last_run = Date.now() / 1000;
      const texts = p.steps.map((x) => x.text);
      const results = texts.map((t) => ({ text: t, status: 'pending' }));
      const base = { type: 'protocol', id, name: p.name, total: texts.length, steps: texts };
      texts.forEach((t, i) => setTimeout(() => {
        results[i].status = 'running';
        emit({ ...base, status: 'step', index: i, step: t, results: results.map((r) => ({ ...r })) });
        setTimeout(() => { results[i].status = i === 2 ? 'unverified' : 'ok'; results[i].seconds = 1.2; }, 1200);
      }, 600 + i * 1600));
      setTimeout(() => {
        emit({ ...base, status: 'done', index: texts.length - 1, results });
        emit({ type: 'activity_log', item: { at: Date.now() / 1000, kind: 'protocol', text: `${p.name} finished`, status: 'ok', detail: `${texts.length} steps` } });
      }, 600 + texts.length * 1600);
      return { ok: true };
    },
    protocol_stop: async () => ({ ok: true }),
    protocol_answer: async () => ({ ok: true }),
    protocol_from_template: async (name) => {
      const t = TEMPLATES.find((x) => x.name === name);
      const p = { ...t, id: `p${Date.now()}`, steps: t.steps.map((x) => step(x)), schedule: {}, triggers: {}, enabled: true, on_failure: 'stop', history: [], created: Date.now() / 1000, last_run: 0 };
      protocols.push(p);
      return { ok: true, protocol: p };
    },
    protocol_proposal_answer: async (action) => {
      const p = proposal;
      proposal = null;
      if (action === 'save' && p) { const { stage, question, ...rest } = p; protocols.push({ ...rest, id: `p${Date.now()}`, history: [], created: Date.now() / 1000, last_run: 0 }); }
      if (action === 'edit' && p) setTimeout(() => emit({ type: 'protocol_edit', protocol: p }), 0);
      setTimeout(() => emit({ type: 'protocols', ...protoList() }), 0);
      return { ok: true, message: action === 'save' ? `Protocol ${p.name} saved, sir.` : '' };
    },
    media_control: async (action, key) => {
      const s = sessions.find((x) => x.key === key) || sessions[0];
      s.status = action === 'pause' || (action === 'toggle' && s.status === 'playing') ? 'paused' : 'playing';
      setTimeout(() => emit({ type: 'media', playing: sessions.find((x) => x.status === 'playing') || sessions[0], sessions: sessions.map((x) => ({ ...x })), status: { sessions: true, mixer: true, keys: true } }), 50);
      return { ok: true, verified: true };
    },
    media_volume: async (key, level) => { const s = sessions.find((x) => x.key === key); if (s) s.volume = level; return { ok: true, volume: level }; },
    corrections_list: async () => ({ items: [{ id: 'c1', heard: 'this cord', meant: 'discord', whole: false, voice_only: true, uses: 3 },
      { id: 'c2', heard: 'play lo fi', meant: 'play lo fi on youtube', whole: true, voice_only: false, uses: 0 }], log: [] }),
    corrections_delete: async () => ({ ok: true }),
    window_state: async () => ({ ...winState }),
    window_toggle_maximize: async () => { winState.maximized = !winState.maximized; return { ...winState }; },
    window_toggle_fullscreen: async () => { winState.fullscreen = !winState.fullscreen; return { ...winState }; },
    window_drag: async () => {},
    window_resize: async () => {},
    window_minimize: async () => {},
    update_status: async () => ({ ...updateState }),
    update_check: async () => ({ ok: true, available: true, ...updateState }),
    update_install: async () => { setTimeout(() => emit({ type: 'update', ...updateState, state: 'installing' }), 100); return { ok: true }; },
    update_restore_previous: async () => ({ ok: true }),
    activity_list: async () => ({ items: activity.slice(), health }),
    browser_status: async () => ({ ...browser }),
    browser_set_profile: async (dir) => { settings.browser_profile = dir; return { ...browser }; },
    memory_list: async () => ({ memories: memories.slice(), episodes: episodes.slice(), stats: memStats() }),
    memory_add: async (kind, text) => { const m = mem(++memId, kind, text.replace(/\bI\b/g, 'You'), { source: 'manual' }); memories.unshift(m); memChanged(); return { ok: true, memory: m, status: 'added' }; },
    memory_update: async (id, fields) => {
      const m = memories.find((x) => x.id === id);
      if (!m) return { ok: false, error: 'That memory no longer exists.' };
      if (fields.meta) fields = { ...fields, meta: { ...m.meta, ...fields.meta } };
      Object.assign(m, fields, { updated: Date.now() / 1000 });
      return { ok: true, memory: m };
    },
    memory_delete: async (id) => { const m = memories.find((x) => x.id === id); memories = memories.filter((x) => x.id !== id); memChanged(); return { ok: true, memory: m || null }; },
    memory_restore: async (m) => { memories.unshift({ ...m, id: ++memId }); memChanged(); return { ok: true, memory: m }; },
    memory_delete_episode: async (id) => { episodes = episodes.filter((e) => e.id !== id); memChanged(); },
    memory_clear: async () => { memories = []; episodes = []; memChanged(); },
    memory_export: async () => ({ ok: true, path: 'C:\\Users\\Tony\\Documents\\JARVIS memory 2026-10-08 1412.json' }),
    memory_install_embeddings: async () => ({ ok: true }),
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
    vision_status: async () => mockVision,
    vision_look: async () => { window.__jarvisMockLook(); return true; },
    vision_install: async () => ({ ok: true }),
    vision_confirm: async (id, yes) => { setTimeout(() => emit({ type: 'act_status', id, status: yes ? 'done' : 'cancelled' }), 300); return { ok: true }; },
    vision_stop_watch: async () => { mockVision.watching = false; emit({ type: 'vision_status', ...mockVision }); },
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
    window_close: async () => {},
  };
};
