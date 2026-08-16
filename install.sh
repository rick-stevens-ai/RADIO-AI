#!/usr/bin/env bash
# RADIO-AI installer — sets up the agentic ham radio control layer.
# Target: Ubuntu 24.04 with an Icom IC-7300 (CAT via CP2102 USB-UART + USB audio codec).
set -euo pipefail

RADIO_HOME="${RADIO_HOME:-$HOME/radio}"
AGENT_DIR="$RADIO_HOME/agent"
BIN_DST="$HOME/.local/bin"

echo "==> Installing RADIO-AI into $AGENT_DIR"
mkdir -p "$AGENT_DIR" "$BIN_DST" "$RADIO_HOME/scans" "$RADIO_HOME/logs" "$RADIO_HOME/data"

# Copy the library + CLI
cp -r hamradio "$AGENT_DIR/"
cp -r bin "$AGENT_DIR/"
cp README.md RADIO_PLAN.md "$AGENT_DIR/" 2>/dev/null || true
chmod +x "$AGENT_DIR"/bin/*

# Put the CLI on PATH
ln -sf "$AGENT_DIR/bin/radio" "$BIN_DST/radio"
ln -sf "$AGENT_DIR/bin/rigctld-ic7300" "$BIN_DST/rigctld-ic7300" 2>/dev/null || true
ln -sf "$AGENT_DIR/bin/radio-poweron-check" "$BIN_DST/radio-poweron-check" 2>/dev/null || true

# systemd --user rigctld service
if [ -d systemd ]; then
  mkdir -p "$HOME/.config/systemd/user"
  cp systemd/rigctld.service "$HOME/.config/systemd/user/"
  echo "==> Enable with: systemctl --user daemon-reload && systemctl --user enable --now rigctld"
fi

# pi extension (agent tools) — optional
if [ -d pi-extension ] && [ -d "$HOME/.pi/agent/extensions" ]; then
  cp pi-extension/radio.ts "$HOME/.pi/agent/extensions/"
  echo "==> Installed pi extension radio.ts (agent radio_* tools)"
fi

echo "==> Done. Try: radio status"
echo "    System deps (apt): hamlib-utils wsjtx fldigi rtl-sdr multimon-ng sox ffmpeg python3-numpy python3-scipy python3-matplotlib"
echo "    FT8 TX encoder: build ft8_lib -> git clone https://github.com/kgoba/ft8_lib $RADIO_HOME/ft8_lib && (cd $_ && make)"
echo "    Location DB:    radio whois-rebuild (after downloading FCC l_amat.zip into $RADIO_HOME/data)"
