# J.A.R.V.I.S. — local desktop voice assistant

A holographic, Iron-Man-style desktop assistant that talks back. It runs with **no paid API keys**:

| Part | Engine | Cost / key |
|---|---|---|
| Brain | [Ollama](https://ollama.com) running `llama3.2` (or any local model) | free, runs offline on your PC |
| Voice | Microsoft Edge neural TTS via `edge-tts` — `en-GB-RyanNeural` by default | free, no key (needs internet) |
| Ears | Google Web Speech via `SpeechRecognition` — or **offline** Whisper / Vosk | free, no key |
| HUD | `pywebview` + HTML5 Canvas / SVG / CSS | — |

![J.A.R.V.I.S. HUD](docs/hud.jpg)

![Speech states: idle, listening, thinking, speaking](docs/states.jpg)

## Features

- **Arc-reactor core** on Canvas: rotating tick rings, coils, a 96-bar circular spectrum, shock-wave rings on voice peaks, particles and live HUD read-outs. Colour follows the speech state machine **IDLE → LISTENING → THINKING → SPEAKING** (cyan, aqua, amber, ice blue).
- **Audio-reactive visualisers**: real FFT bands from the microphone while listening and from JARVIS's own voice while speaking (each clip is analysed once in Python and played back in sync in the UI), plus an oscilloscope waveform.
- **Streaming replies**: tokens stream into the comms log with a typewriter effect, and JARVIS starts speaking after the first sentence instead of waiting for the full answer.
- **Push-to-talk + silence detection**: click the mic (or tap <kbd>Space</kbd>) and just talk — capture stops on silence. Hold the button/<kbd>Space</kbd> for classic push-to-talk. Press it while JARVIS is talking to barge in; <kbd>Esc</kbd> interrupts.
- **Opens your files, apps and games**: "open my resume", "open Spotify", "open calculator", "open settings", "open the budget spreadsheet", "play GTA 5", "launch FIFA 26". JARVIS searches your Desktop, Documents, Downloads, Pictures, Music, Videos, OneDrive, every app in the Start menu (including Microsoft Store apps) and your installed games — **Steam** and **Epic** libraries plus anything registered with Windows (EA app, Ubisoft, Battle.net, GOG…). It understands nicknames: *GTA V / gta five* → Grand Theft Auto V, *RDR2*, *CS2*, *COD*, and *FIFA 26* → EA SPORTS FC 26 (FIFA's new name). It also finds programs through their install folders and Windows' App Paths, game shortcuts on your desktop, and dozens of Windows tools ("open device manager", "open windows update", "open command prompt"). It can read text and Word documents to summarise them. For safety it never runs programs you name by path; apps and games open through their shortcuts or launchers.
- **Internet access, free and key-less**: web search (DuckDuckGo, with a Wikipedia fallback), reading web pages and opening sites in your browser. Ask about news, weather, prices or anything recent. Both abilities can be switched off in Settings ▸ Abilities.
- **Writes for you, straight into Google Docs & Slides**: "write a bio on Lionel Messi", "write a 500-word report on electric cars", "make a presentation about the solar system". JARVIS looks the topic up (Wikipedia + the web), writes it with proper headings, lists and bold text, creates the Google Doc or Slides deck and opens it. Follow-ups work too: "add a section about his childhood to it", "make it shorter", "translate it into Spanish", "add a slide about Mars to my Solar System presentation".
- **Google Docs, Slides & Sheets editing**: "add a slide about pricing to my Pitch deck", "delete slide 3", "move the last slide to the front", "make a Google Sheet called Budget with rent 1200 and food 300", "what's in my Budget sheet?".
- **Email**: "email Sarah saying I'll be ten minutes late", "send my boss an email asking for Friday off", "email it to Tom" (shares the doc JARVIS just wrote). JARVIS writes the email, finds the address from your Gmail history, shows the draft in an editable card and asks "Shall I send it?". **Nothing is sent until you say yes or press Send.** Without the Google link, the draft opens in Gmail for you to send.
- **Speaks your language**: talk or type in Spanish, French, German, Italian, Portuguese, Japanese, Chinese, Arabic and 20+ more; JARVIS detects it, answers in it and switches to a native voice. Speech recognition listens for your main language and the others you speak at the same time (Settings ▸ Voice input).
- All Google features work through a tiny script that runs in *your own* Google account (free, no API key): Settings ▸ Google ▸ Set up walks you through the 3-minute, one-time link. The script is `integrations/jarvis_google_bridge.gs`. If you linked an older version, JARVIS tells you and Settings ▸ Google ▸ **Update script** walks you through the one-minute update (your link stays the same).
- **"Hey Jarvis" wake word**: offline and free. Say "Hey Jarvis" and your request in one breath ("Hey Jarvis, what time is it?") or pause after the wake word; JARVIS stops listening by itself when you finish speaking. Say "Hey Jarvis, stop" while it is talking to silence it. This pipeline is tested on recorded speech: wake word → end-of-speech detection → reply → "stop".
- **Text input** for silent typing, with Markdown/code rendering in the log.
- **Telemetry**: CPU and RAM gauges, 60 s history, per-core bars, disk, network, processes, uptime, battery.
- **Interface sounds** synthesised at start-up (activation chime, listening blips, processing ticks, error tones) — no audio files shipped.
- **Ollama guidance overlay**: if Ollama isn't reachable you get setup steps, a copyable `ollama run llama3.2`, a *Start Ollama* button when it's installed, and auto-retry. If Ollama runs but has no model, JARVIS can download `llama3.2` for you with a progress bar.
- **Settings drawer**: model, host, creativity, custom instructions, voice + preview, rate/pitch, recognition engine, language, end-of-speech pause, conversation mode (listen again after replying), sounds, form of address.

## Quick start (Windows)

1. Install **Ollama** from <https://ollama.com/download>, then in a terminal: `ollama run llama3.2`
2. Run `Jarvis.exe`.

That's it. Settings and logs live in `%APPDATA%\JARVIS\` (`config.json`, `jarvis.log`). If J.A.R.V.I.S. ever fails to start it shows an error dialog; the details are in `jarvis.log` (and `%TEMP%\jarvis-crash.txt`).
Because the exe isn't code-signed, Windows SmartScreen may show a warning on first launch: choose *More info → Run anyway*.
The UI needs the Microsoft Edge **WebView2** runtime, which ships with Windows 10/11.

## Run from source

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py                      # --debug opens the web inspector
```

Linux needs PortAudio headers for PyAudio (`sudo apt install portaudio19-dev`) and a pywebview GUI backend (`pip install "pywebview[qt]"`).

Optional offline speech recognition: `pip install -r requirements-optional.txt` (faster-whisper and/or Vosk), then pick the engine in Settings — *Automatic* prefers Whisper, then Vosk, then Google.

## Build `Jarvis.exe`

On a Windows machine with Python 3.10–3.12:

```bat
pip install -r requirements.txt
python build.py
```

`build.py` checks dependencies, runs the unit tests, renders the icon, runs PyInstaller (`--onefile --windowed`, version resource, trimmed SpeechRecognition data) and then **launches the built exe with `--selftest`** to prove it starts. Output: `dist\Jarvis.exe`.

Options: `--install` (pip install first), `--onedir` (folder build, faster start-up), `--console` (keep a console for debugging), `--skip-tests`, `--no-selftest`.

PyInstaller only builds for the OS it runs on, so the Windows exe is also built by GitHub Actions (`.github/workflows/build.yml`) on every push: download it from the run's **Artifacts** (`Jarvis-windows-x64`). Pushing a `v*` tag attaches it to a GitHub release.

## Project layout

```
app.py              entry point, pywebview window, JS⇄Python bridge, --selftest
core/
  assistant.py      orchestrator: turns, interruption, speech pipeline
  state.py          IDLE / LISTENING / THINKING / SPEAKING state machine
  llm.py            Ollama client: detection, streaming chat, memory, model pull, start server
  tts.py            edge-tts on a background asyncio loop, sentence splitter, speech text cleanup
  stt.py            microphone capture with VAD + Google / Whisper / Vosk recognisers
  audio.py          pygame.mixer playback + FFT band analysis for the visualisers
  sfx.py            synthesised interface sounds
  system.py         psutil telemetry
  config.py         validated JSON settings in the user profile
  tools.py          files, apps & games discovery, web search, tool definitions for the model
  compose.py        long-form writing: "write a bio on ...", decks, follow-up edits
  mail.py           email requests, drafting and the confirm-before-send flow
  language.py       offline language detection, voices and recognition languages
  wakeword.py       "Hey Jarvis" detection on onnxruntime + the shared microphone stream
  google_bridge.py  client for the user's own Apps Script (Docs, Slides, Sheets, Gmail)
integrations/       jarvis_google_bridge.gs, the script the user deploys in their Google account
web/
  index.html        HUD markup
  styles.css        holographic styling
  app.js            reactor/waveform renderers, chat, telemetry, overlays, push-to-talk
  devmock.js        simulated backend for previewing the HUD in a normal browser
  fonts/            Orbitron, Rajdhani, Share Tech Mono (SIL OFL), bundled for offline use
assets/             app icon + generator
hooks/              PyInstaller hook override
tests/              pytest suite + mock Ollama server
build.py            automated PyInstaller build
```

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q                  # 100+ tests; no microphone, speakers or network needed
python -m tests.mock_ollama          # fake Ollama on :11434 for UI work without a model
```

Preview the HUD in a normal browser against a simulated backend: `python -m http.server -d web`, then open <http://localhost:8000/index.html?mock>; use `?ollama=offline` or `?ollama=nomodel` to see the setup overlays. (The simulated backend is never used inside the desktop app.)

## Privacy

Conversation text goes only to your local Ollama. Spoken replies are synthesised by Microsoft's Edge voice service, and the default recogniser sends your speech to Google; install faster-whisper or Vosk to keep speech recognition entirely on your machine.

## Credits

The "Hey Jarvis" wake-word models in `assets/wakeword/` come from [openWakeWord](https://github.com/dscripka/openWakeWord) (code Apache-2.0; pretrained models CC BY-NC-SA 4.0, so non-commercial use only).
