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
- **Text input** for silent typing, with Markdown/code rendering in the log.
- **Telemetry**: CPU and RAM gauges, 60 s history, per-core bars, disk, network, processes, uptime, battery.
- **Interface sounds** synthesised at start-up (activation chime, listening blips, processing ticks, error tones) — no audio files shipped.
- **Ollama guidance overlay**: if Ollama isn't reachable you get setup steps, a copyable `ollama run llama3.2`, a *Start Ollama* button when it's installed, and auto-retry. If Ollama runs but has no model, JARVIS can download `llama3.2` for you with a progress bar.
- **Settings drawer**: model, host, creativity, custom instructions, voice + preview, rate/pitch, recognition engine, language, end-of-speech pause, conversation mode (listen again after replying), sounds, form of address.

## Quick start (Windows)

1. Install **Ollama** from <https://ollama.com/download>, then in a terminal: `ollama run llama3.2`
2. Run `Jarvis.exe`.

That's it. Settings and logs live in `%APPDATA%\JARVIS\` (`config.json`, `jarvis.log`).
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
python -m pytest -q                  # 60+ tests; no microphone, speakers or network needed
python -m tests.mock_ollama          # fake Ollama on :11434 for UI work without a model
```

Open `web/index.html` in a browser (serve the folder, e.g. `python -m http.server -d web`) to iterate on the HUD against a simulated backend; add `?ollama=offline` or `?ollama=nomodel` to see the setup overlays.

## Privacy

Conversation text goes only to your local Ollama. Spoken replies are synthesised by Microsoft's Edge voice service, and the default recogniser sends your speech to Google; install faster-whisper or Vosk to keep speech recognition entirely on your machine.
