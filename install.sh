#!/usr/bin/env sh
# Silver — wake-word voice companion installer
# Usage:  curl -fsSL https://<host>/install.sh | sh
# Targets Arch/PipeWire first, degrades gracefully elsewhere.

set -eu

# ── Tuneables ───────────────────────────────────────────────────────────────
PREFIX="${SILVER_PREFIX:-$HOME/.local/share/silver}"
VENV="$PREFIX/venv"
BIN_DIR="$HOME/.local/bin"
RAW_BASE="${SILVER_RAW_BASE:-https://raw.githubusercontent.com/NotATrueHero/silver/main}"
SILVERD_URL="$RAW_BASE/silverd.py"
SOUL_URL="$RAW_BASE/profile/SOUL.md"

say()  { printf '\033[1;36m[ silver ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ silver ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ silver ]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Detect distro / package manager ─────────────────────────────────────────
detect_pm() {
  if command -v pacman >/dev/null 2>&1; then echo pacman
  elif command -v apt-get >/dev/null 2>&1; then echo apt
  elif command -v dnf >/dev/null 2>&1; then echo dnf
  elif command -v zypper >/dev/null 2>&1; then echo zypper
  else echo unknown; fi
}
PM="$(detect_pm)"

say "Silver installer — distro: ${PM}"

# ── System dependencies ─────────────────────────────────────────────────────
install_deps() {
  case "$PM" in
    pacman)
      say "installing deps (pacman)…"
      sudo pacman -S --needed --noconfirm python python-pip espeak-ng ffmpeg curl >/dev/null
      ;;
    apt)
      say "installing deps (apt)…"
      sudo apt-get update -qq >/dev/null
      sudo apt-get install -y -qq python3 python3-venv python3-pip espeak-ng ffmpeg curl >/dev/null
      ;;
    dnf)
      say "installing deps (dnf)…"
      sudo dnf install -y -q python3 python3-pip espeak-ng ffmpeg curl >/dev/null
      ;;
    zypper)
      say "installing deps (zypper)…"
      sudo zypper install -y -q python3 python3-pip espeak-ng ffmpeg curl >/dev/null
      ;;
    *)
      warn "unknown package manager — skipping system deps."
      warn "You'll need: python3, pip, espeak-ng, ffmpeg."
      ;;
  esac
}

command -v python3 >/dev/null 2>&1 || install_deps
command -v espeak-ng >/dev/null 2>&1 || install_deps
command -v ffmpeg >/dev/null 2>&1 || install_deps

# ── Python venv + dependencies ──────────────────────────────────────────────
say "creating venv at $VENV"
mkdir -p "$PREFIX"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || die "failed to create venv (is python3-venv installed?)"
fi

say "installing Python dependencies (this pulls torch for Kokoro — a few hundred MB, one-time)…"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
  sounddevice numpy faster-whisper sherpa-onnx \
  "kokoro>=0.9.4" soundfile edge-tts

# ── Install the daemon ──────────────────────────────────────────────────────
say "installing silverd"
# Fetch silverd.py from its canonical location (overridable via SILVERD_URL).
if [ -f "${SILVERD_SOURCE:-}" ]; then
  cp "$SILVERD_SOURCE" "$PREFIX/silverd.py"
else
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$SILVERD_URL" -o "$PREFIX/silverd.py" || die "failed to download silverd.py"
  else
    die "curl is required and not found"
  fi
fi
chmod +x "$PREFIX/silverd.py"

# Fetch the persona (for agent mode / reference) alongside the daemon.
mkdir -p "$PREFIX/profile"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$SOUL_URL" -o "$PREFIX/profile/SOUL.md" 2>/dev/null || true
fi

# Launcher on PATH
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/silverd" <<EOF
#!/usr/bin/env sh
exec "$VENV/bin/python" "$PREFIX/silverd.py" "\$@"
EOF
chmod +x "$BIN_DIR/silverd"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "add $BIN_DIR to your PATH (or restart your shell)" ;;
esac

# ── Interactive config (warnings + API key) ─────────────────────────────────
say "configuration:"
"$VENV/bin/python" "$PREFIX/silverd.py" config

# ── systemd user service ────────────────────────────────────────────────────
if command -v systemctl >/dev/null 2>&1 && systemctl --user >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/silverd.service" <<EOF
[Unit]
Description=Silver voice companion
After=pipewire.service wireplumber.service

[Service]
Type=simple
ExecStart=$VENV/bin/python $PREFIX/silverd.py run
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  say "installed $UNIT_DIR/silverd.service (enable with: systemctl --user enable --now silverd)"
else
  warn "no systemd user session — start manually with:  silverd run"
fi

# ── Agent mode: point at Hermes ─────────────────────────────────────────────
if command -v hermes >/dev/null 2>&1; then
  say "Hermes detected — for agent mode (full machine access), run a silver profile gateway"
  say "and set agent_url in ~/.config/silver/config.json to its api_server."
else
  warn "Hermes not found — Silver runs in 'lite' mode (no tools)."
  warn "Install Hermes for agent mode: https://hermes-agent.nousresearch.com/docs"
fi

echo
say "Done. Quick start:"
say "  silverd doctor     # verify the stack"
say "  silverd speak hi   # test her voice"
say "  silverd run        # start listening for the wake word"
echo
warn "⚠  Silver has access to this machine by default (agent mode)."
warn "   She answers to the wake phrase with full tool access. You've been warned."
