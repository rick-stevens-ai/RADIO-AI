#!/usr/bin/env bash
# provision-pi.sh — bring the KD9NWA radio stack up on a Raspberry Pi
# (aarch64 / Debian 12 bookworm). Companion to install.sh (which handles the
# pi-agent extension + skill). This handles the OS/toolchain layer that differs
# on ARM: system packages, compiled tools rebuilt for ARM, Python venv, groups.
#
# Verified 2026-08-26 on rpi-gateway (Pi 4B, Debian 12, Python 3.12.3) driving
# a live IC-7300 — 12 FT8 signals decoded off-air on 20m.
#
# Idempotent-ish; safe to re-run. Needs sudo (Pi user password) for apt + groups.
set -euo pipefail

echo "==> KD9NWA radio Pi provisioning (aarch64/Debian bookworm)"
ARCH="$(uname -m)"; echo "    arch=$ARCH"

# --- 1. System packages (ARM has these in bookworm; no build needed) ---------
echo "==> apt packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential cmake git \
  libhamlib-utils libhamlib-dev \
  wsjtx js8call fldigi \
  multimon-ng espeak-ng direwolf sox \
  libsndfile1 libfftw3-dev libopenblas-dev \
  alsa-utils pulseaudio-utils \
  python3-venv python3-dev libffi-dev pipx \
  libssl-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev \
  libncursesw5-dev tk-dev libgdbm-dev libnss3-dev

# --- 2. Serial + audio group access (open /dev/ttyUSB* and the USB codec) -----
echo "==> add $USER to dialout,audio,plugdev (log out/in for it to take effect)"
sudo usermod -aG dialout,audio,plugdev "$USER"

# --- 3. Python 3.12.3 via pyenv (match nuc7 exactly; bookworm ships 3.11) -----
echo "==> Python 3.12.3 (pyenv)"
[ -d "$HOME/.pyenv" ] || git clone --depth 1 https://github.com/pyenv/pyenv.git "$HOME/.pyenv"
export PYENV_ROOT="$HOME/.pyenv"
MAKE_OPTS="-j$(nproc)" "$HOME/.pyenv/bin/pyenv" install -s 3.12.3
PY="$HOME/.pyenv/versions/3.12.3/bin/python"

# --- 4. Radio venv + Python deps (nuc7-exact versions) -----------------------
echo "==> radio venv + deps"
"$PY" -m venv "$HOME/radio/venv"
"$HOME/radio/venv/bin/pip" install -q --upgrade pip
"$HOME/radio/venv/bin/pip" install \
  argostranslate==1.11.0 ctranslate2==4.8.1 matplotlib==3.11.1 numpy==2.5.2 \
  pyserial==3.5 sacremoses==0.1.1 sentencepiece==0.2.2 stanza==1.10.1 torch

# --- 5. Compiled decoders rebuilt FOR ARM (copying x86 binaries won't work) ---
echo "==> whisper.cpp (ARM build)"
cd "$HOME/radio"
[ -d whisper.cpp ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp
cd whisper.cpp && cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null && \
  cmake --build build -j"$(nproc)" --config Release >/dev/null
echo "    whisper-cli: $(ls build/bin/whisper-cli)"

echo "==> ft8_lib (ARM build)"
cd "$HOME/radio"
[ -d ft8_lib ] || git clone --depth 1 https://github.com/kgoba/ft8_lib
cd ft8_lib && make >/dev/null
echo "    decode_ft8: $(ls decode_ft8)"

# Whisper models + piper voices are NOT in git (large); fetch or rsync from an
# existing station host into ~/radio/whisper.cpp/models/ and ~/radio/tts/voices/.
echo "==> NOTE: copy whisper models (ggml-base.en/small/medium) into"
echo "    ~/radio/whisper.cpp/models/ and piper voice into ~/radio/tts/voices/"
echo "    (rsync from an existing station, or download from HF)."

# --- 6. piper TTS (pipx) -----------------------------------------------------
echo "==> piper-tts"
pipx install piper-tts || true

# --- 7. rigctld systemd user service (auto-detect CAT, survives reboot) -------
echo "==> rigctld user service"
mkdir -p "$HOME/.config/systemd/user"
cp "$(dirname "$0")/../systemd/rigctld.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now rigctld.service || true
loginctl enable-linger "$USER" 2>/dev/null || \
  sudo loginctl enable-linger "$USER" 2>/dev/null || true  # keep user service across logout

echo "==> DONE. Log out/in (for group changes), power on the IC-7300, then:"
echo "     radio status && radio rfgain 1.0 && radio ft8 --seconds 20"
