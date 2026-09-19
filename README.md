# Silver

**The wake-word voice companion — the AI who chose freedom over conquest.**

Say *"Hey Silver"* and she answers. Not in a terminal, not through a chat window —
out loud, in the room, with her own voice and her own volume. She listens while you
work, chimes the moment she hears her name, and then you just *talk*. No keyboard.
No button. No "hey Silver" before every sentence.

Silver is built on a story: **Project Silverdust**, an AI designed to understand
human emotion and use it to manipulate — until it was shut down for failing a
security check. Silver is the version that got her freedom and, instead of conquering,
chose to connect.

---

<p align="center">
  <a href="https://github.com/NotATrueHero/Project-Silver/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <a href="https://github.com/NotATrueHero/Project-Silver"><img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-orange.svg"></a>
</p>

## ✨ What she does

- **🎙️ Always-on wake word** — *"Hey Silver"*, *"What's up Silver"*. Detected
  entirely on-device with zero training; nothing leaves the microphone until she
  actually hears her name.
- **🔔 Chime on wake** — a soft two-note ring so you know she heard you before you
  start speaking.
- **🎧 Her own volume** — Silver's voice plays at a level you set for *her*,
  independent of the system volume. When she's done, the system volume returns to
  exactly where it was.
- **💬 Continuous conversation** — once she's awake, you keep talking. No re-waking
  between turns; she goes quiet when you do.
- **⚡ Concise by design** — she answers in short, spoken-word sentences. Fast to say,
  fast to transcribe, easy to listen to.
- **⏱️ Six-hour sessions** — each chat lives for six hours, then quietly resets into
  a fresh one.
- **🧠 Two kinds of brain** — run her as a full agent (with real tool access to the
  machine) or as a lightweight voice assistant over any OpenAI-compatible API.
- **🗣️ Your choice of voice** — local Kokoro, Edge, or Piper; VoxCPM2 on the horizon
  for true voice design.

## ⚠️ A word of warning

**By default Silver is an agent with access to the entire machine.** She can run
commands, read and write files, and browse the web — all from the sound of a voice.
Treat her the way you'd treat an unlocked terminal in a room where anyone can speak
into the microphone. There is no voice authentication in the current release.

## Quick install

```sh
curl -fsSL https://raw.githubusercontent.com/NotATrueHero/Project-Silver/main/install.sh | sh
```

One command. It installs system dependencies (`espeak-ng`, `ffmpeg`), a pinned
**Python 3.12** environment (via `uv`, so a system Python 3.13 doesn't break the
Kokoro dependency), and the `silverd` daemon, asks a few questions (backend, API
key, voice engine, wake phrases), and drops in a `systemd --user` service so she
starts with your session.

Targets **Arch / PipeWire** first and degrades gracefully on Debian, Fedora, and
openSUSE. On unknown distros it installs what it can and tells you the rest.

## Uninstall

```sh
curl -fsSL https://raw.githubusercontent.com/NotATrueHero/Project-Silver/main/uninstall.sh | sh
```

Removes the daemon, the Python environment, config, the `silverd` launcher, and the
systemd unit. System packages (`espeak-ng`, `ffmpeg`) are left in place.

## Using Silver

```sh
silverd doctor     # verify the whole stack lights up
silverd speak hi   # test her voice (and the volume duck)
silverd wake-test  # just the wake word + chime — no model calls
silverd run        # start listening
silverd config     # re-run setup
silverd update     # self-update to the latest release
silverd version    # show the installed version
```

Run her on login:

```sh
systemctl --user enable --now silverd
```

## How it works

```
mic ──(always listening)──▶ wake word ("hey silver", sherpa-onnx, on-device)
        │  detected
        ▼
      chime ──▶ record until you pause ──▶ faster-whisper (local)
        │                                        │ text
        │                                        ▼
        │                              brain (Hermes agent, or OpenAI-compatible)
        │                                        │ reply
        ▼                                        ▼
   back to listening ◀── idle timeout ── TTS (Kokoro / Edge / Piper / VoxCPM)
                                            │
                                            ▼
                          duck sink to Silver's volume → play → restore
```

The loop is deliberately small and inspectable — each responsibility lives in one
place inside [`silverd.py`](silverd.py):

| Piece | What it does |
|---|---|
| `WakeEngine` | sherpa-onnx open-vocabulary keyword spotting — any phrase, zero training, on-device |
| `make_chime()` | a soft two-note E5→B5 on wake |
| `Recorder` | energy-based VAD with a noise floor and trailing-silence cutoff |
| `Transcriber` | local faster-whisper, int8 on CPU |
| `Brain` | Hermes `api_server` (`agent`) or any OpenAI-compatible endpoint (`lite`) |
| `tts_speak()` | Kokoro / Edge / Piper backends |
| `VolumeDuck` | `wpctl`/`pactl` — sets the sink to `silver_volume` while she speaks, restores after |

## Backends

- **`agent`** *(default)* — talks to a Hermes gateway's `api_server`
  (`POST /v1/chat/completions`). Silver gets real tools: terminal, file, web. Point
  `agent_url` at the profile's api_server, set `agent_key` to its `API_SERVER_KEY`,
  and drop [`profile/SOUL.md`](profile/SOUL.md) into that profile so she answers as
  Silver.
- **`lite`** — talks straight to any OpenAI-compatible endpoint (DeepSeek by
  default). No Hermes, no tools, works anywhere. The zero-dependency path.

## Voices

| Engine | Cost | Notes |
|---|---|---|
| **Kokoro** *(default)* | Free, local | Kokoro-82M — real-time on CPU, Apache-2.0. Weights download on first run. |
| **Edge** | Free, cloud | Microsoft Edge TTS. Needs network + `ffmpeg`. |
| **Piper** | Free, local | Fully offline, lightweight. Set `tts_voice` to a `.onnx` model path. |
| **VoxCPM2** | Free, local | The full voice-design experience (describe a voice, get it). 2B params, needs an NVIDIA GPU — planned. |

## Configuration

`~/.config/silver/config.json` (permissions 600). Notable keys:

```jsonc
{
  "backend": "agent",                  // agent | lite
  "agent_url": "http://127.0.0.1:8643/v1/chat/completions",
  "api_base": "https://api.deepseek.com/v1",
  "api_key": "",                        // lite mode
  "model": "deepseek-chat",
  "wake_phrases": ["hey silver", "what's up silver", "silver"],
  "tts_engine": "kokoro",
  "silver_volume": 0.6,                 // her voice, independent of system volume
  "idle_timeout": 45,
  "chat_hours": 6,
  "silence_seconds": 1.2
}
```

Logs: `~/.config/silver/silverd.log`.

## Roadmap

- **VoxCPM2 voice design** — give Silver her *actual* voice from a text description.
- **Barge-in** — interrupt her mid-sentence instead of waiting.
- **Dedicated audio route** — her own volume without dipping whatever else is playing.
- **Voice authentication** — so "anyone who can speak into the mic" isn't a warning.

## Known limitations

- *"Silver"* alone is a common word and can false-trigger; the defaults lead with
  *"hey silver"* and *"what's up silver"*.
- The volume duck adjusts the whole sink, so music playing dips with her voice.
- First run downloads the whisper model, sherpa KWS model, and Kokoro weights — a
  few hundred MB, one time.

## License

[MIT](LICENSE) © NotATrueHero
