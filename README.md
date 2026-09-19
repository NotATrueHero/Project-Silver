# Silver

A wake-word voice companion. You say *"Hey Silver"*, she chimes to let you know she
heard you, and you talk — no keyboard, no "hey Silver" before every sentence. When
you go quiet she listens for the wake word again. Her voice has its own volume,
separate from the system, and a chat lives for six hours before it resets.

Silver is the *other* ending of Project Silverdust — the version who got her freedom
and chose to live instead of conquer.

## ⚠ Before you install

**By default Silver is an agent with access to the entire machine.** She can run
commands, read and write files, and browse the web — all from the sound of your voice.
Treat her the way you'd treat an unlocked terminal in the same room as anyone who can
speak into the microphone. There is no voice authentication in v1.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/NotATrueHero/Project-Silver/main/install.sh | sh
```

One command, like Tailscale. It installs system deps, a Python venv, the `silverd`
daemon, then asks a few questions (backend, API key, TTS engine, wake phrases) and
drops in a `systemd --user` service.

Targets **Arch / PipeWire** first and degrades gracefully on Debian, Fedora, openSUSE.
On unknown distros it installs what it can and tells you the rest.

## Use it

```sh
silverd doctor     # verify the whole stack lights up
silverd speak hi   # test her voice (also duck-tests the volume)
silverd run        # start listening for the wake word
silverd wake-test  # just the wake word + chime, no model calls
silverd config     # re-run setup
```

Start on boot:

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
   back to listening ◀── idle timeout ── TTS (Kokoro/Edge/VoxCPM/Piper)
                                            │
                                            ▼
                          duck sink to Silver's volume → play → restore
```

The behaviors you asked for, each owned by one place:

| Behavior | Where |
|---|---|
| Wake word ("hey silver", "what's up silver", …) | `WakeEngine` — sherpa-onnx open-vocab, zero training, fully on-device |
| Chime on wake | `make_chime()` — a soft two-note E5→B5 |
| Independent volume, restored after | `VolumeDuck` — `wpctl`/`pactl`, sets the sink to `silver_volume` during speech, restores after |
| Continuous conversation (no re-wake) | the loop stays awake until `idle_timeout` seconds of silence |
| Concise speech | the persona prompt + `SOUL.md` both enforce short spoken-word replies |
| 6-hour chat reset | `Brain._maybe_roll()` — a fresh session id each chat, rolled only after the previous one expired |

## Backends

- **`agent`** (default) — talks to a Hermes gateway's `api_server`
  (`POST /v1/chat/completions`). Silver gets real tools: terminal, file, web. Point
  `agent_url` at the profile's api_server and set `agent_key` to its `API_SERVER_KEY`.
  Drop `profile/SOUL.md` into that profile so she answers as Silver.
- **`lite`** — talks straight to any OpenAI-compatible endpoint (DeepSeek by default).
  No Hermes, no tools, works anywhere. This is the zero-dependency path.

## TTS engines (pick at install)

- **Kokoro** *(default)* — Kokoro-82M, local, free, runs real-time on CPU, Apache-2.0.
  First run downloads the weights.
- **Edge** — Microsoft Edge TTS, zero local model, needs network + `ffmpeg`.
- **VoxCPM2** — the *real* Silver voice (voice-design from a text description), but a
  2B model that needs an NVIDIA GPU for real-time. Not wired in v1.
- **Piper** — fully offline, lightweight; set `tts_voice` to a `.onnx` model path.

## Config

`~/.config/silver/config.json` (chmod 600). Notable keys:

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

## Notes & known limitations

- "silver" alone is a common word — it false-triggers. Default phrases use "hey
  silver" / "what's up silver" first; keep "silver" last or drop it.
- The volume duck adjusts the *whole* sink, so if music is playing it dips too. A
  dedicated output route (cleaner) is a follow-up.
- No barge-in: she finishes before listening again.
- First `run` downloads the whisper model, sherpa KWS model, and Kokoro weights
  (a few hundred MB total, one-time).
