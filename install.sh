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

# --- Agent (pi) integration: extension + skill + guidance ------------------
PI_AGENT_DIR="$HOME/.pi/agent"

# pi extension (agent radio_* tools)
if [ -d pi-extension ]; then
  mkdir -p "$PI_AGENT_DIR/extensions"
  cp pi-extension/radio.ts "$PI_AGENT_DIR/extensions/"
  echo "==> Installed pi extension radio.ts (agent radio_* tools)"
fi

# pi skill (bring-up checklist, safety, commands, troubleshooting)
if [ -d skill ]; then
  mkdir -p "$PI_AGENT_DIR/skills/kd9nwa-station/reference"
  cp skill/SKILL.md "$PI_AGENT_DIR/skills/kd9nwa-station/"
  cp skill/reference/*.md "$PI_AGENT_DIR/skills/kd9nwa-station/reference/" 2>/dev/null || true
  echo "==> Installed pi skill kd9nwa-station"
fi

# AGENTS.md so any agent on this host immediately knows the station + rules.
# Placed at $HOME and $RADIO_HOME (pi loads AGENTS.md as project/dir context).
if [ -f AGENTS.md ]; then
  cp AGENTS.md "$RADIO_HOME/AGENTS.md"
  [ -f "$HOME/AGENTS.md" ] || cp AGENTS.md "$HOME/AGENTS.md"
  echo "==> Installed AGENTS.md (agent guidance) in $RADIO_HOME and $HOME"
fi

cat <<'PICFG'
==> pi model auth is NOT configured by this script (no secrets in the repo).
    To let a local pi session reach a model, populate ~/.pi/agent/models.json
    with your provider(s) + keys and set a default in ~/.pi/agent/settings.json,
    e.g.  {"model": "argo/argo:claude-opus-4.8"}. Verify with:
      pi -p "run radio_status and report the frequency"
PICFG

echo "==> Done. Try: radio status"
echo "    System deps (apt): hamlib-utils wsjtx fldigi rtl-sdr multimon-ng sox ffmpeg python3-numpy python3-scipy python3-matplotlib"
echo "    FT8 TX encoder: build ft8_lib -> git clone https://github.com/kgoba/ft8_lib $RADIO_HOME/ft8_lib && (cd $_ && make)"
echo "    Location DB:    radio whois-rebuild (after downloading FCC l_amat.zip into $RADIO_HOME/data)"
