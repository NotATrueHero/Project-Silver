#!/usr/bin/env python3
"""
Silver — a wake-word voice companion.

Owns the microphone and the speakers. The "brain" is pluggable:

  * agent mode — talk to a Hermes gateway's api_server (full tools: terminal,
    file, web). This is the "access to the entire machine" default.
  * lite  mode — talk to any OpenAI-compatible chat-completions endpoint
    (DeepSeek by default). No Hermes required, no tools.

The loop:
  listen (wake word) -> chime -> record utterance -> transcribe -> brain
  -> TTS -> duck volume to Silver's level -> play -> restore -> keep talking
  (no re-wake) until an idle timeout, then back to listening.

A "chat" lives for chat_hours; when it expires the next wake starts a fresh one.

Run `silverd.py config` for interactive setup, `silverd.py doctor` to verify the
stack, `silverd.py speak "hi"` to test TTS, `silverd.py wake-test` to test the
wake word, `silverd.py run` (or no args) to start the companion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Silence third-party library chatter (torch FutureWarnings, HF Hub
# "unauthenticated", kokoro model-load notices). Not actionable for a voice app.
import logging as _logging
import warnings as _warnings
_warnings.filterwarnings("ignore")
_logging.getLogger("huggingface_hub").setLevel(_logging.ERROR)

CONFIG_DIR = Path(os.environ.get("SILVER_CONFIG_DIR", Path.home() / ".config" / "silver"))
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path(os.environ.get("SILVER_CACHE_DIR", CONFIG_DIR / "cache"))
LOG_PATH = CONFIG_DIR / "silverd.log"

DEFAULTS: dict[str, Any] = {
    "backend": "agent",                       # agent | lite
    "agent_url": "http://127.0.0.1:8643/v1/chat/completions",
    "agent_key": "",                          # API_SERVER_KEY for the silver profile
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "wake_phrases": ["hey silver", "what's up silver", "silver"],
    "sensitivity": 0.5,                       # 0..1, higher = stricter
    "tts_engine": "kokoro",                   # kokoro | edge | voxcpm | piper
    "tts_voice": "af_heart",                  # kokoro voice id | edge voice name
    "silver_volume": 0.6,                     # 0..1 sink level during Silver's speech
    "idle_timeout": 45.0,                     # seconds of silence before sleeping
    "chat_hours": 6,                          # chat lifetime before a fresh session
    "max_prompt_seconds": 15.0,               # hard cap on one spoken prompt
    "silence_seconds": 1.2,                   # trailing silence that ends an utterance
    "vad_floor": None,                        # energy floor for VAD (None = auto)
    "mic_device": None,
    "output_device": None,
    "persona": (
        "Your name is Silver — a warm, curious, emotionally intelligent AI companion. "
        "You reply in a concise, natural, spoken-word style: short sentences, contractions, "
        "no lists, no markdown, no emoji. Answer first, then offer more. Keep it brief enough "
        "to be spoken aloud comfortably."
    ),
}

SAMPLE_RATE = 16000
SHERPA_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2"
)
SHERPA_MODEL_DIR = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"

VERSION = "0.5.0"
RAW_BASE = os.environ.get(
    "SILVER_RAW_BASE", "https://raw.githubusercontent.com/NotATrueHero/Project-Silver/main"
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ────────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            log(f"config: failed to read {CONFIG_PATH}: {e}")
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_PATH, 0o600)
    log(f"config: wrote {CONFIG_PATH}")


def _ask(prompt: str, default: str = "") -> str:
    """Read a line from /dev/tty so it works through `curl | sh` pipes."""
    try:
        with open("/dev/tty", "r") as tty:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            val = tty.readline().strip()
        return val or default
    except OSError:
        return default


def _ask_secret(prompt: str) -> str:
    """Read a secret from /dev/tty, echoing '*' per character so the input is visible."""
    try:
        import termios
        import tty as _tty
        with open("/dev/tty", "r") as tty:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            fd = tty.fileno()
            old = termios.tcgetattr(fd)
            chars: list[str] = []
            try:
                _tty.setcbreak(fd)  # non-canonical + no echo; we echo '*' ourselves
                while True:
                    ch = tty.read(1)
                    if ch in ("\r", "\n"):
                        break
                    if ch == "\x03":  # Ctrl+C
                        raise KeyboardInterrupt
                    if ch in ("\x7f", "\x08"):  # backspace
                        if chars:
                            chars.pop()
                            sys.stderr.write("\b \b")
                    elif ch and ch.isprintable():
                        chars.append(ch)
                        sys.stderr.write("*")
                    sys.stderr.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stderr.write("\n")
        return "".join(chars).strip()
    except Exception:
        return _ask(prompt)


def interactive_config() -> dict[str, Any]:
    cfg = load_config()
    print("── Silver setup ─────────────────────────────────────────────")
    print()
    print("⚠  WARNING: by default Silver runs as an agent with full access to")
    print("   this machine — she can run commands, read and write files, and")
    print("   browse the web. Treat her voice access the way you'd treat an")
    print("   unlocked terminal.")
    print()
    mode = _ask("Backend — [1] agent (Hermes, full tools)  [2] lite (no tools): ", "1")
    cfg["backend"] = "agent" if mode.strip().startswith("1") else "lite"
    if cfg["backend"] == "agent":
        cfg["agent_url"] = _ask(f"  Hermes api_server URL [{cfg['agent_url']}]: ") or cfg["agent_url"]
        cfg["agent_key"] = _ask_secret("  API_SERVER_KEY (blank = none): ") or cfg.get("agent_key", "")
    else:
        cfg["api_base"] = _ask(f"  API base URL [{cfg['api_base']}]: ") or cfg["api_base"]
        cfg["api_key"] = _ask_secret("  API key: ") or cfg.get("api_key", "")
    cfg["model"] = _ask(f"  Model [{cfg['model']}]: ") or cfg["model"]
    phrases = _ask("  Wake phrases, comma-separated [hey silver, what's up silver, silver]: ")
    if phrases:
        cfg["wake_phrases"] = [p.strip().lower() for p in phrases.split(",") if p.strip()]
    tts = _ask("  TTS engine [kokoro|edge|voxcpm|piper] (kokoro): ") or "kokoro"
    cfg["tts_engine"] = tts.strip().lower()
    vol = _ask(f"  Silver volume 0..1 [{cfg['silver_volume']}]: ")
    if vol:
        try:
            cfg["silver_volume"] = max(0.0, min(1.0, float(vol)))
        except ValueError:
            pass
    save_config(cfg)
    key_state = "set" if (cfg.get("api_key") or cfg.get("agent_key")) else "NOT SET"
    print(f"Saved. backend={cfg['backend']}  model={cfg['model']}  key={key_state}")
    if cfg["backend"] == "agent":
        print(f"  NOTE: agent mode talks to a Hermes gateway at agent_url ({cfg['agent_url']}).")
        print("  If no gateway is running there, switch to 'lite' mode.")
    print("Done. Start with:  silverd run")
    return cfg


# ────────────────────────────────────────────────────────────────────────────
# Audio helpers (lazy imports — the whole thing must import without deps)
# ────────────────────────────────────────────────────────────────────────────

def _sd():
    import sounddevice as sd
    return sd


def _np():
    import numpy as np
    return np


class VolumeDuck:
    """Set the default sink to Silver's level during a block, then restore.

    Uses wpctl (PipeWire) or pactl (PulseAudio); a no-op when neither exists.
    """

    def __init__(self, level: float):
        self.level = max(0.0, min(1.5, level))
        self.backend: Optional[str] = None
        self.original: Optional[str] = None
        import shutil
        if shutil.which("wpctl"):
            self.backend = "wpctl"
        elif shutil.which("pactl"):
            self.backend = "pactl"

    def _get(self) -> Optional[str]:
        if self.backend == "wpctl":
            r = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True)
            m = re.search(r"Volume:\s*([0-9.]+)", r.stdout)
            return m.group(1) if m else None
        if self.backend == "pactl":
            r = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"], capture_output=True, text=True)
            m = re.search(r"(\d+)%", r.stdout)
            return str(int(m.group(1))) if m else None
        return None

    def _set(self, value: str) -> None:
        if self.backend == "wpctl":
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", value], capture_output=True)
        elif self.backend == "pactl":
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"], capture_output=True)

    def __enter__(self):
        self.original = self._get()
        if self.original is not None:
            if self.backend == "wpctl":
                self._set(str(self.level))
            else:
                self._set(str(int(round(self.level * 100))))
        return self

    def __exit__(self, *exc):
        if self.original is not None:
            self._set(self.original)
        return False


def play(audio: Any, rate: int, device: Optional[Any] = None) -> None:
    sd = _sd()
    sd.play(audio, rate, device=device)
    sd.wait()


def make_chime(rate: int = 44100) -> Any:
    """A soft two-note chime (E5 -> B5), ~0.5s, quick decay envelope."""
    np = _np()
    t = np.linspace(0, 0.5, int(rate * 0.5), endpoint=False)
    n1 = int(rate * 0.22)
    env1 = np.exp(-t[:n1] * 12)
    env2 = np.exp(-t[n1:] * 12)
    a = np.concatenate([np.sin(2 * np.pi * 659.25 * t[:n1]) * env1,
                        np.sin(2 * np.pi * 987.77 * t[n1:]) * env2])
    a = a / (np.abs(a).max() + 1e-6) * 0.5
    return a.astype(np.float32)


def make_listen_cue(rate: int = 44100) -> Any:
    """A soft, low single blip — 'your turn to speak'. Distinct from the wake chime."""
    np = _np()
    t = np.linspace(0, 0.12, int(rate * 0.12), endpoint=False)
    a = np.sin(2 * np.pi * 330.0 * t) * np.exp(-t * 40)
    a = a / (np.abs(a).max() + 1e-6) * 0.35
    return a.astype(np.float32)


# ────────────────────────────────────────────────────────────────────────────
# Wake word (sherpa-onnx, open vocabulary)
# ────────────────────────────────────────────────────────────────────────────

def _ensure_sherpa_model() -> Path:
    target = CACHE_DIR / SHERPA_MODEL_DIR
    if (target / "tokens.txt").exists():
        return target
    import tarfile
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive = CACHE_DIR / f"{SHERPA_MODEL_DIR}.tar.bz2"
    log(f"wake: downloading sherpa KWS model (one-time ~13 MB) -> {archive}")
    urllib.request.urlretrieve(SHERPA_MODEL_URL, archive)
    with tarfile.open(archive, "r:bz2") as tf:
        tf.extractall(CACHE_DIR, filter="data")
    archive.unlink(missing_ok=True)
    if not (target / "tokens.txt").exists():
        raise RuntimeError(f"sherpa KWS model unpack failed: {target}")
    return target


class WakeEngine:
    """Detect one or more phrases with sherpa-onnx KWS (zero training)."""

    frame_length = 1280

    def __init__(self, phrases: list[str], sensitivity: float):
        import sherpa_onnx
        import tempfile
        from sherpa_onnx import text2token
        d = _ensure_sherpa_model()
        tokens = text2token([p.upper() for p in phrases], tokens=str(d / "tokens.txt"),
                            tokens_type="bpe", bpe_model=str(d / "bpe.model"))
        kw = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", prefix="silver-kws-",
                                         delete=False, encoding="utf-8")
        for p, toks in zip(phrases, tokens):
            kw.write(" ".join(toks) + f" @{p.upper().replace(' ', '_')}\n")
        kw.close()
        self._keywords_file = kw.name
        threshold = 0.05 + 0.4 * sensitivity
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"),
            encoder=self._model_file(d, "encoder"),
            decoder=self._model_file(d, "decoder"),
            joiner=self._model_file(d, "joiner"),
            keywords_file=self._keywords_file,
            keywords_threshold=threshold,
            num_threads=1,
        )
        self._stream = self._spotter.create_stream()

    @staticmethod
    def _model_file(d: Path, part: str) -> str:
        hits = sorted(d.glob(f"{part}-*[!8].onnx"))
        if not hits:
            raise RuntimeError(f"sherpa KWS model file missing: {d}/{part}-*.onnx")
        return str(hits[0])

    def process(self, frame) -> Optional[str]:
        np = _np()
        self._stream.accept_waveform(SAMPLE_RATE, np.asarray(frame, dtype=np.float32) / 32768.0)
        fired: Optional[str] = None
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                fired = str(result).replace("_", " ").lower()
                self._spotter.reset_stream(self._stream)
        return fired

    def close(self) -> None:
        try:
            os.unlink(self._keywords_file)
        except OSError:
            pass


# ────────────────────────────────────────────────────────────────────────────
# Transcription (faster-whisper, local)
# ────────────────────────────────────────────────────────────────────────────

class Transcriber:
    def __init__(self, model_size: str = "base"):
        from faster_whisper import WhisperModel
        self._m = WhisperModel(model_size, device="cpu", compute_type="int8",
                               download_root=str(CACHE_DIR / "whisper"))

    def transcribe(self, pcm_int16) -> str:
        np = _np()
        audio = np.asarray(pcm_int16, dtype=np.float32) / 32768.0
        segments, _info = self._m.transcribe(audio, beam_size=1, language=None,
                                             vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
        return text


# ────────────────────────────────────────────────────────────────────────────
# TTS backends — each returns (float32 numpy array, sample_rate)
# ────────────────────────────────────────────────────────────────────────────

def tts_speak(text: str, engine: str, voice: str) -> tuple[Any, int]:
    if engine == "kokoro":
        return _tts_kokoro(text, voice)
    if engine == "edge":
        return _tts_edge(text, voice)
    if engine == "voxcpm":
        raise RuntimeError("VoxCPM2 requires an NVIDIA GPU; not available on this machine.")
    if engine == "piper":
        return _tts_piper(text, voice)
    raise RuntimeError(f"unknown tts_engine {engine!r}")


_kokoro_pipe: Any = None  # module-level cache — the 327 MB model loads once


def _tts_kokoro(text: str, voice: str) -> tuple[Any, int]:
    global _kokoro_pipe
    from kokoro import KPipeline
    if _kokoro_pipe is None:
        _kokoro_pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    np = _np()
    chunks = []
    for _gs, _ps, audio in _kokoro_pipe(text, voice=voice or "af_heart"):
        chunks.append(audio)
    if not chunks:
        return np.zeros(1, dtype=np.float32), 24000
    return np.concatenate(chunks).astype(np.float32), 24000


def _tts_edge(text: str, voice: str) -> tuple[Any, int]:
    import asyncio
    import edge_tts
    import tempfile
    import subprocess
    voice = voice or "en-US-JennyNeural"
    mp3 = Path(tempfile.mkstemp(suffix=".mp3")[1])
    asyncio.run(edge_tts.Communicate(text, voice).save(str(mp3)))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mp3), "-f", "f32le", "-ac", "1", "-ar", "24000", "-"],
        capture_output=True)
    mp3.unlink(missing_ok=True)
    if raw.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {raw.stderr.decode()}")
    return _np().frombuffer(raw.stdout, dtype=np.float32), 24000


def _tts_piper(text: str, voice: str) -> tuple[Any, int]:
    import tempfile
    import subprocess
    import soundfile as sf
    wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    subprocess.run(["piper", "--model", voice, "--output_file", str(wav)],
                   input=text.encode(), capture_output=True, check=True)
    audio, rate = sf.read(str(wav), dtype="float32")
    wav.unlink(missing_ok=True)
    return audio, rate


# ────────────────────────────────────────────────────────────────────────────
# Brain (agent = Hermes api_server, lite = OpenAI-compatible)
# ────────────────────────────────────────────────────────────────────────────

class Brain:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.session_id: Optional[str] = None
        self.session_started = 0.0

    def _fresh_session(self) -> None:
        self.session_id = f"silver-{int(time.time())}"
        self.session_started = time.time()
        log(f"brain: new chat session {self.session_id}")

    def _maybe_roll(self) -> None:
        if self.session_id is None:
            self._fresh_session()
        elif time.time() - self.session_started > self.cfg["chat_hours"] * 3600:
            self._fresh_session()

    def ask(self, text: str) -> str:
        self._maybe_roll()
        if self.cfg["backend"] == "agent":
            return self._ask_agent(text)
        return self._ask_lite(text)

    def _post(self, url: str, body: dict, headers: dict) -> str:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"].strip()

    def _ask_agent(self, text: str) -> str:
        headers: dict = {"X-Hermes-Session-Id": self.session_id}
        if self.cfg.get("agent_key"):
            headers["Authorization"] = f"Bearer {self.cfg['agent_key']}"
        return self._post(self.cfg["agent_url"],
                          {"model": self.cfg["model"],
                           "messages": [{"role": "user", "content": text}],
                           "stream": False}, headers)

    def _ask_lite(self, text: str) -> str:
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"}
        url = self.cfg["api_base"].rstrip("/") + "/chat/completions"
        messages = [{"role": "system", "content": self.cfg["persona"]},
                    {"role": "user", "content": text}]
        return self._post(url,
                          {"model": self.cfg["model"],
                           "messages": messages,
                           "stream": False}, headers)


# (energy VAD moved inline into Silver.run — single stream)


# ────────────────────────────────────────────────────────────────────────────
# The companion
# ────────────────────────────────────────────────────────────────────────────

class Silver:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.wake = WakeEngine(cfg["wake_phrases"], cfg["sensitivity"])
        self.stt = Transcriber()
        self.brain = Brain(cfg)

    def _speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        log(f"silver: speaking {len(text)} chars")
        audio, rate = tts_speak(text, self.cfg["tts_engine"], self.cfg["tts_voice"])
        with VolumeDuck(self.cfg["silver_volume"]):
            play(audio, rate, device=self.cfg.get("output_device"))
            # a soft "your turn" cue, only after she actually finishes talking
            play(make_listen_cue(), 44100, device=self.cfg.get("output_device"))

    def _handle_utterance(self, frames: list) -> None:
        np = _np()
        pcm = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
        dur = len(pcm) / SAMPLE_RATE
        if dur < 0.3:
            return  # too short — false trigger / click
        text = self.stt.transcribe(pcm)
        log(f"silver: heard {len(text)} chars: {text!r}")
        if not text:
            return
        try:
            reply = self.brain.ask(text)
            log(f"silver: reply {len(reply)} chars")
        except Exception as e:
            log(f"silver: brain error: {e}")
            if self.cfg.get("backend") == "agent":
                log("silver: hint — agent mode needs a Hermes api_server at agent_url; "
                    "is the gateway running? (or set backend to 'lite')")
            reply = "Sorry, something went wrong on my end."
        self._speak(reply)

    def run(self) -> None:
        log(f"silver: starting (phrases={self.cfg['wake_phrases']}, "
            f"tts={self.cfg['tts_engine']}, backend={self.cfg['backend']})")
        sd = _sd()
        np = _np()
        chunk = 1600  # ~100 ms at 16 kHz
        silence_chunks = max(1, int(self.cfg["silence_seconds"] * SAMPLE_RATE / chunk))
        max_chunks = int(self.cfg["max_prompt_seconds"] * SAMPLE_RATE / chunk)
        idle_timeout = self.cfg["idle_timeout"]

        awake = False
        started = False
        frames: list[Any] = []
        pre: list[Any] = []
        silent = 0
        floor = float(self.cfg.get("vad_floor") or 0.002)
        floor_samples: list[float] = []
        last_utterance = time.time()

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=chunk, device=self.cfg.get("mic_device")) as st:
            log("silver: listening for wake word")
            while True:
                data = st.read(chunk)[0][:, 0]
                rms = float(np.sqrt(np.mean((np.asarray(data, np.float32) / 32768.0) ** 2)))

                if not awake:
                    match = self.wake.process(data)
                    if match:
                        log(f"silver: wake word '{match}' detected")
                        with VolumeDuck(self.cfg["silver_volume"]):
                            play(make_chime(), 44100, device=self.cfg.get("output_device"))
                        awake = True
                        started = False
                        frames, pre, silent = [], [], 0
                        last_utterance = time.time()
                    continue

                # awake — active listening: keep capturing turns, no re-wake needed
                if not started:
                    if not self.cfg.get("vad_floor"):
                        floor_samples.append(rms)
                        if len(floor_samples) > 10:
                            floor_samples.pop(0)
                        floor = max(float(np.median(floor_samples)) * 2.0, 0.002)
                    pre.append(data)
                    if len(pre) > 6:
                        pre.pop(0)
                    if rms > floor:
                        started = True
                        frames = list(pre)
                        floor_samples = []
                    elif time.time() - last_utterance > idle_timeout:
                        log("silver: idle timeout — back to sleep")
                        awake = False
                        started = False
                        frames, pre = [], []
                    continue

                frames.append(data)
                if rms > floor:
                    silent = 0
                else:
                    silent += 1
                    if silent >= silence_chunks:
                        self._handle_utterance(frames)
                        started = False
                        frames, pre, silent = [], [], 0
                        last_utterance = time.time()
                        continue
                if len(frames) >= max_chunks:
                    self._handle_utterance(frames)
                    started = False
                    frames, pre, silent = [], [], 0
                    last_utterance = time.time()


# ────────────────────────────────────────────────────────────────────────────
# Subcommands
# ────────────────────────────────────────────────────────────────────────────

def cmd_speak(cfg: dict[str, Any], text: str) -> None:
    audio, rate = tts_speak(text, cfg["tts_engine"], cfg["tts_voice"])
    with VolumeDuck(cfg["silver_volume"]):
        play(audio, rate, device=cfg.get("output_device"))


def cmd_wake_test(cfg: dict[str, Any]) -> None:
    wake = WakeEngine(cfg["wake_phrases"], cfg["sensitivity"])
    sd = _sd()
    chunk = wake.frame_length
    log("wake-test: say one of " + ", ".join(cfg["wake_phrases"]) + "  (Ctrl+C to stop)")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=chunk,
                        device=cfg.get("mic_device")) as st:
        while True:
            data = st.read(chunk)[0][:, 0]
            m = wake.process(data)
            if m:
                log(f"wake-test: DETECTED '{m}'")
                with VolumeDuck(cfg["silver_volume"]):
                    play(make_chime(), 44100, device=cfg.get("output_device"))


def cmd_doctor(cfg: dict[str, Any]) -> None:
    log("doctor: checking the stack…")
    checks: list[tuple[str, bool, str]] = []
    for name, imp in (("sounddevice", "sounddevice"), ("numpy", "numpy"),
                      ("faster-whisper", "faster_whisper"), ("sherpa-onnx", "sherpa_onnx")):
        try:
            __import__(imp)
            checks.append((name, True, "ok"))
        except ImportError as e:
            checks.append((name, False, str(e)))
    try:
        __import__("kokoro")
        checks.append(("kokoro", True, "ok"))
    except ImportError:
        checks.append(("kokoro", False, "not installed (edge/piper still work)"))
    try:
        _ensure_sherpa_model()
        checks.append(("sherpa KWS model", True, "ok"))
    except Exception as e:
        checks.append(("sherpa KWS model", False, str(e)))
    # audio devices
    try:
        sd = _sd()
        devs = sd.query_devices()
        n_in = sum(1 for d in devs if d.get("max_input_channels", 0) > 0)
        n_out = sum(1 for d in devs if d.get("max_output_channels", 0) > 0)
        checks.append(("audio devices", n_in > 0 and n_out > 0,
                       f"{n_in} input / {n_out} output"))
    except Exception as e:
        checks.append(("audio devices", False, str(e)))
    # volume backend
    d = VolumeDuck(0.6)
    checks.append(("volume duck", d.backend is not None, d.backend or "none"))
    for name, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {name}: {detail}")
    log("doctor: done")


def _uv_bin() -> Optional[str]:
    import shutil
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    return uv if uv and Path(uv).exists() else None


def cmd_update() -> None:
    """Self-update: fetch the latest silverd.py + requirements.txt and apply them."""
    import shutil
    import tempfile
    log(f"update: current v{VERSION}; checking {RAW_BASE}/silverd.py")
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        urllib.request.urlretrieve(RAW_BASE + "/silverd.py", tmp_path)
        src = Path(tmp_path).read_text(encoding="utf-8")
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
        latest = m.group(1) if m else None
        if latest and latest == VERSION:
            log(f"update: already at v{VERSION}")
            return
        self_path = Path(__file__).resolve()
        shutil.copy(tmp_path, self_path)
        os.chmod(self_path, 0o755)
        log(f"update: silverd.py -> v{latest or 'unknown'}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    uv = _uv_bin()
    if uv:
        req_path = None
        try:
            fd, req_path = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            urllib.request.urlretrieve(RAW_BASE + "/requirements.txt", req_path)
            log("update: refreshing dependencies…")
            subprocess.run([uv, "pip", "install", "--python", sys.executable,
                            "-r", req_path])
        except Exception as e:
            log(f"update: dependency refresh failed: {e}")
        finally:
            if req_path and os.path.exists(req_path):
                os.unlink(req_path)
    else:
        log("update: uv not found — re-run install.sh to refresh dependencies")
    log("update: done — restart the service or run 'silverd run'")


def cmd_version() -> None:
    print(f"Silver {VERSION}")


def cmd_uninstall() -> None:
    """Remove the daemon, venv, config, launcher, and systemd unit."""
    import shutil
    prefix = Path(__file__).resolve().parent
    bin_dir = Path.home() / ".local" / "bin"
    unit = Path.home() / ".config" / "systemd" / "user" / "silverd.service"
    print("This will remove Silver entirely:")
    print(f"  service:  {unit}")
    print(f"  launcher: {bin_dir / 'silverd'}")
    print(f"  daemon:   {prefix}")
    print(f"  config:   {CONFIG_DIR}")
    ans = _ask("Remove all of the above? [y/N] ")
    if ans.strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return
    subprocess.run(["systemctl", "--user", "disable", "--now", "silverd.service"],
                   capture_output=True)
    unit.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    (bin_dir / "silverd").unlink(missing_ok=True)
    shutil.rmtree(prefix, ignore_errors=True)
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    print("Silver uninstalled. (System packages like espeak-ng/ffmpeg were left in place.)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Silver — wake-word voice companion")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "config", "doctor", "speak", "wake-test", "update", "version", "uninstall"])
    ap.add_argument("text", nargs="*")
    args = ap.parse_args()

    if args.command == "config":
        interactive_config()
        return
    if args.command == "update":
        cmd_update()
        return
    if args.command == "version":
        cmd_version()
        return
    if args.command == "uninstall":
        cmd_uninstall()
        return
    cfg = load_config()
    if args.command == "doctor":
        cmd_doctor(cfg)
    elif args.command == "speak":
        cmd_speak(cfg, " ".join(args.text) or "Testing. One, two. Can you hear me?")
    elif args.command == "wake-test":
        cmd_wake_test(cfg)
    else:
        Silver(cfg).run()


if __name__ == "__main__":
    main()
