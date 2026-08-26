# Running the KD9NWA radio stack on a Raspberry Pi (aarch64)

The station was originally on `nuc7` (x86_64 Ubuntu 24.04). This doc covers
running the same stack on a Raspberry Pi (aarch64, Debian 12 bookworm) — e.g.
`rpi-gateway`. **Verified 2026-08-26**: a Pi 4B drove a live IC-7300 and decoded
12 FT8 signals off-air on 20m.

## Quick start
```bash
git clone https://github.com/rick-stevens-ai/RADIO-AI && cd RADIO-AI
./scripts/provision-pi.sh     # OS/toolchain layer (apt, ARM builds, venv, groups, service)
./install.sh                  # pi-agent extension + skill (radio_* tools)
# copy whisper models + piper voice (see below), then log out/in, power on radio:
radio status && radio rfgain 1.0 && radio ft8 --seconds 20
```

## What differs from nuc7 (x86_64 → aarch64)
1. **Compiled decoders must be REBUILT for ARM, not copied.** `whisper.cpp` and
   `ft8_lib` are C/C++; x86 binaries won't run. provision-pi.sh clones + builds
   both (`cmake` / `make`) natively on the Pi (a few minutes each).
2. **wsjtx, js8call, fldigi, multimon-ng, espeak-ng, direwolf, hamlib** are all
   in the bookworm aarch64 repos — plain `apt install`, no build.
3. **Python version.** nuc7 uses 3.12.3; bookworm ships 3.11. `numpy==2.5.2`
   (nuc7's pin) requires Python ≥3.12, so we build 3.12.3 via pyenv for exact
   alignment. (On 3.11 you'd have to pin numpy==2.4.6 — avoid; align instead.)
4. **torch** on ARM is the stock aarch64 CPU wheel (`pip install torch`, no
   `+cpu` suffix). pip may pull `nvidia-*` packages as declared deps — they are
   inert on a Pi (no GPU), harmless.

## Device access (the two gotchas that make it "not work" silently)
1. **Serial (CAT):** `/dev/ttyUSB0` is `root:dialout`. The user MUST be in the
   `dialout` group or rigctld can't open it. provision-pi.sh adds it — **log out
   and back in** for group changes to take effect.
2. **Audio (USB codec):** the IC-7300's Burr-Brown/TI USB codec shows as ALSA
   `card 3: CODEC` / PipeWire `alsa_input.usb-Burr-Brown...`. The user must be in
   the `audio` group; querying audio from a session without it shows "no
   soundcards found" even though the kernel sees the card. `radio audio-devices`
   confirms the right source (`plughw:3,0`).

## rigctld service
The repo's `systemd/rigctld.service` runs `bin/rigctld-ic7300`, which auto-detects
the CP2102 CAT device via `/dev/serial/by-id/*CP2102*` and waits for the radio to
power on. It's a **user** service on the Pi (`systemctl --user`); enable linger
(`loginctl enable-linger`) so it survives logout/reboot. Listens on 127.0.0.1:4532
where WSJT-X / fldigi / the agent all share it.

## Models / voices (not in git — large binaries)
- Whisper: `ggml-base.en` (148M), `ggml-small` (488M), `ggml-medium` (1.5G) →
  `~/radio/whisper.cpp/models/`. rsync from an existing station or download from
  HuggingFace (ggerganov/whisper.cpp).
- piper voice: `en_US-lessac-medium.onnx` (+ .json) → `~/radio/tts/voices/`.

## Session start (same as nuc7)
1. `radio status` — confirm CAT link (freq/mode/S-meter).
2. `radio rfgain 1.0` — RF gain reverts to 0 (= deaf) on power-cycle.
3. `radio freq-tune <hz>` after any QSY (antenna tuner).
4. One CAT owner at a time (don't run FT8/scan while JS8Call holds the rig).
5. TX is gated: `radio tx-enable "<reason>"` → cmd `--allow-tx` → `radio unkey`
   → `radio tx-disable`. Verify forward power; never leave the rig keyed.

## Verified result (rpi-gateway, 2026-08-26)
```
radio status  -> 14074000 Hz USB 20m, S-meter live, PTT false
radio ft8 --seconds 20 -> n_decodes: 12  (e.g. "SP9SOS VA5KEN R-22",
   "OE8RPK K1EDR -25", "AA6XM KG5EIU EM13") — real off-air DX.
```
