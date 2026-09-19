#!/usr/bin/env sh
# Silver — uninstaller
# Usage:  curl -fsSL https://raw.githubusercontent.com/NotATrueHero/Project-Silver/main/uninstall.sh | sh
# Removes the daemon, venv, config, launcher, and systemd unit. Leaves nothing behind.

set -eu

PREFIX="${SILVER_PREFIX:-$HOME/.local/share/silver}"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="${SILVER_CONFIG_DIR:-$HOME/.config/silver}"
UNIT="$HOME/.config/systemd/user/silverd.service"

say() { printf '\033[1;36m[ silver ]\033[0m %s\n' "$*"; }

if command -v systemctl >/dev/null 2>&1 && systemctl --user >/dev/null 2>&1; then
  say "stopping and disabling the service…"
  systemctl --user disable --now silverd.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
else
  say "no systemd user session — skipping service cleanup"
fi

rm -f "$BIN_DIR/silverd"
rm -rf "$PREFIX"
rm -rf "$CONFIG_DIR"

say "Silver uninstalled. (System packages like espeak-ng/ffmpeg were left in place.)"
