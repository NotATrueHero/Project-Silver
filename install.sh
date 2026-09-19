#!/usr/bin/env sh
# Silver — wake-word voice companion installer
# Usage:  curl -fsSL https://<host>/install.sh | sh
# Targets Arch/PipeWire first, degrades gracefully elsewhere.

set -eu

# ── Tuneables ───────────────────────────────────────────────────────────────
PREFIX="${SILVER_PREFIX:-$HOME/.local/share/silver}"
VENV="$PREFIX/venv"
BIN_DIR="$HOME/.local/bin"
RAW_BASE="${SILVER_RAW_BASE:-https://raw.githubusercontent.com/NotATrueHero/Project-Silver/main}"
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
      sudo pacman -S --needed --noconfirm espeak-ng ffmpeg curl >/dev/null
      ;;
    apt)
      say "installing deps (apt)…"
      sudo apt-get update -qq >/dev/null
      sudo apt-get install -y -qq espeak-ng ffmpeg curl >/dev/null
      ;;
    dnf)
      say "installing deps (dnf)…"
      sudo dnf install -y -q espeak-ng ffmpeg curl >/dev/null
      ;;
    zypper)
      say "installing deps (zypper)…"
      sudo zypper install -y -q espeak-ng ffmpeg curl >/dev/null
      ;;
    *)
      warn "unknown package manager — skipping system deps."
      warn "You'll need: espeak-ng, ffmpeg, curl."
      ;;
  esac
}

command -v curl >/dev/null 2>&1 || install_deps
command -v espeak-ng >/dev/null 2>&1 || install_deps
command -v ffmpeg >/dev/null 2>&1 || install_deps

# ── Python (via uv, pinned 3.12 — kokoro 0.9.x needs <3.13) ─────────────────
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv (manages its own Python 3.12, no system-python headaches)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
  export PATH="$HOME/.local/bin:$PATH"
fi

say "creating venv at $VENV (Python 3.12)…"
mkdir -p "$PREFIX"
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
  : # existing Python 3.12 venv — keep it
else
  say "  (re)creating venv with Python 3.12…"
  rm -rf "$VENV"
  uv venv --python 3.12 "$VENV" || die "failed to create venv"
fi

say "installing Python dependencies (this pulls torch for Kokoro — a few hundred MB, one-time)…"
curl -fsSL "$RAW_BASE/requirements.txt" -o "$PREFIX/requirements.txt"
uv pip install --python "$VENV/bin/python" --quiet -r "$PREFIX/requirements.txt"

# ── Install the daemon ──────────────────────────────────────────────────────
say "installing silverd"
if [ -f "${SILVERD_SOURCE:-}" ]; then
  cp "$SILVERD_SOURCE" "$PREFIX/silverd.py"
else
  curl -fsSL "$SILVERD_URL" -o "$PREFIX/silverd.py" || die "failed to download silverd.py"
fi
chmod +x "$PREFIX/silverd.py"

# Fetch the persona (for agent mode / reference) alongside the daemon.
mkdir -p "$PREFIX/profile"
curl -fsSL "$SOUL_URL" -o "$PREFIX/profile/SOUL.md" 2>/dev/null || true

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

# ── Interactive config (warnings + API key) — skipped if already configured ──
if [ -f "$HOME/.config/silver/config.json" ]; then
  say "config already present — skipping setup (run 'silverd config' to change it)"
else
  "$VENV/bin/python" "$PREFIX/silverd.py" config
fi

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
say "  curl -fsSL $RAW_BASE/uninstall.sh | sh   # uninstall"
echo
warn "⚠  Silver has access to this machine by default (agent mode)."
warn "   She answers to the wake phrase with full tool access. You've been warned."
