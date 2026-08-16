# nuc7 Ham Radio Station — Build & Modernization Plan

**Station:** KD9NWA  ·  **Grid:** EN51TP  ·  **Host:** stevens-nuc7i5bnh (Intel NUC7 i5, Ubuntu 20.04)
**Radio:** Icom **IC-7300** (HF/6m, 100W, built-in USB audio codec + CAT)
  - CAT: Silicon Labs CP2102 USB-UART bridge → `/dev/ttyACM0` (also `/dev/serial/by-id/...CP2102...`)
  - Audio: IC-7300 USB Audio CODEC (appears as a USB sound card when radio is ON)
  - Control model: **flrig** as rig-control hub; WSJT-X/fldigi talk to flrig; PTT via CAT

## Goals
1. OS + security hardening: update all packages, **upgrade OpenSSH**, consider 20.04→22.04→24.04.
2. Modernize the ham software suite (2020 versions → current).
3. Add SDR + decode + logging + agentic-control tooling.
4. Build an **agent-operable control layer**: rigctld + CLI wrappers + MCP/HTTP so an
   agent can query/command the radio safely (with TX guards).
5. Documentation + safe-by-default (no accidental transmit).

## Phase 0 — Snapshot & safety (DONE FIRST)
- [ ] Record current config (WSJT-X.ini, flrig prefs, direwolf confs) into `~/radio/backup/`.
- [ ] Note: radio is OFF now; TX must stay disabled until user confirms antenna/dummy load.

## Phase 1 — OS & SSH modernization
- [ ] `apt update && apt full-upgrade` (Focal current).
- [ ] Upgrade OpenSSH: Focal's is 8.2p1. Options:
      (a) do the release upgrade (brings OpenSSH 8.9 on 22.04 / 9.6 on 24.04) — biggest win.
      (b) stay on 20.04 but pull OpenSSH from a backport/PPA.
      → Recommend **do-release-upgrade to 22.04 then 24.04** (24.04 = OpenSSH 9.6p1, PQ KEX).
- [ ] Reboot, re-verify tailscale + cesello legs come back (they're systemd/cron persistent).

## Phase 2 — Ham suite modernization
Current (Focal) → target:
- hamlib 3.3 → **4.6** (rigctl/rigctld; IC-7300 rig #3073). Build from source or PPA.
- wsjtx 2.5.4 → **2.7.0** (.deb from k1jt / sourceforge).
- js8call 2.2.0 → **2.2.0** is current stable (keep) — verify.
- fldigi 4.1.06 → **4.2.x** (w1hkj PPA or source).
- flrig 1.3.49 → **2.0.x** (w1hkj).
- direwolf (never built) → build **1.7** from the existing clone (or fresh 1.7).
- pat (Winlink) 0.10.0 → **0.16.x** (github releases .deb).
- gridtracker 1.20 → current .deb.

## Phase 3 — New tooling
- SDR: `rtl-sdr`, `gqrx-sdr`, `soapysdr` tools, `cubicsdr` (already have SoapySDR core).
- Decoders/utilities: `multimon-ng`, `qsstv`, `wsprd`/`wspr`, `csdr`.
- Logging: `cqrlog` or `klog`; ADIF tools.
- Satellite: `gpredict`.
- Programming: `chirp` (VHF/UHF HTs) via flatpak/pipx.
- APRS: `xastir` (have it), `direwolf` TNC, `aprs` python libs.
- CAT/util: `grig`, `tqsl` (LoTW).

## Phase 4 — Agentic control layer  (~/radio/agent)
- `rigctld` as a persistent service exposing IC-7300 on localhost:4532.
- Python control lib (Hamlib python bindings or raw rigctld TCP) with **TX safeguards**:
    - read-only by default (freq/mode/S-meter/PTT-state).
    - explicit `--allow-tx` + power/timeout guards for any transmit.
- CLI: `radio status|freq|mode|band|smeter|spots` — agent-friendly, JSON out.
- Optional MCP server or HTTP endpoint so pi/piago agents can call it over the mesh.
- Hook into WSJT-X UDP (port 2237) + PSKReporter for spot awareness.

## Phase 5 — Docs & verification
- `~/radio/README.md`: how everything wires together, how to power-on sequence,
  how the agent controls the rig, safety notes.
- Dry-run everything with radio OFF (rigctld -m 1 dummy) before touching real hardware.

## Safety rules (ALWAYS)
- Never key the transmitter without explicit user go-ahead + confirmed load.
- Default all agent tooling to RX/read-only.
- Respect KD9NWA license privileges & band plan.

## PROGRESS LOG

### Phase 0 — DONE (2026-08-16)
- Configs backed up to ~/radio/backup/pre-upgrade-20260816/
- Failsafe: sshd on ports 22 + 2222; tmux/screen installed.

### Phase 1 — DONE (2026-08-16): OS + SSH modernization
- Ubuntu 20.04.6 -> 22.04.5 -> **24.04.4 LTS** (two clean do-release/full-upgrades)
- Kernel 5.15 -> **6.8.0-136**
- OpenSSH 8.2p1 -> **9.6p1** (OpenSSL 3.0.13); PQ KEX sntrup761x25519 now supported (fixes the original SSH warning)
- Tailscale + all 3 cesello legs + cron auto-recovered across 2 reboots.
- Hurdle fixed: wsjtx/wsjtx-data file conflict on /usr/share/pixmaps/wsjtx_icon.png (force-overwrite).

### Ham suite auto-modernized by the release upgrade:
- hamlib 3.3 -> **4.5.5** (rigctl/rigctld; IC-7300 = rig #3073)
- fldigi 4.1.06 -> **4.2.03**
- flrig 1.3.49 -> **2.0.04**
- wsjtx 2.5.4 -> **2.7.0~rc3**
- xastir 2.1.4 -> **2.2.0**
- pat (Winlink) 0.10 -> **0.15.1** (binary is /usr/bin/pat-winlink on noble)
- js8call still 2.2.0 (current stable)

### NEXT: Phase 2/3 (newer-than-repo + SDR + new tools), Phase 4 (agent control layer)

### Phase 2/3 — DONE (2026-08-16): tools installed
- apt: rtl-sdr, gqrx, soapysdr, multimon-ng, direwolf 1.7, gpredict, sox, ffmpeg,
  python3-hamlib (4.5.5), numpy/scipy/matplotlib, pipx, flatpak, espeak-ng.
- Built whisper.cpp (CPU/AVX2) + ggml-base.en model for speech-to-text.

### Phase 4 — DONE (2026-08-16): agentic control layer  (~/radio/agent)
- rigctld systemd --user service (IC-7300 model 3073, auto-detect CP2102, waits for radio).
- hamradio python lib: rig / tx (gated) / scan / audio / decode.
- `radio` CLI (JSON) on PATH. Verified against dummy rig (model 1).
- pi extension radio.ts -> 11 radio_* tools; piago agent verified driving the rig.
- THREE TARGET CAPABILITIES all validated:
  * GOAL 1 band usage map:  radio scan -> JSON + PNG (tested, plot renders).
  * GOAL 2 Morse decode:    radio cw -> multimon-ng (decoded synthetic "TEST DE KD9NWA").
  * GOAL 3 speech-to-text:  radio speech -> whisper.cpp (decoded synthetic voice in 2.5s).
- TX gated: master switch (TX_ENABLED) + band-plan guard + fail-safe un-key + confirm.

### REMAINING (needs the radio powered ON to finish/tune):
- Verify CP2102 by-id path + CI-V baud against the live rig.
- Confirm PipeWire USB-codec source name; real-signal CW/speech accuracy passes.
- Decide final TX_SEGMENTS to match exact KD9NWA privileges.

### Phase 5 — DONE (2026-08-16): TX generation
- CW generation: rig keyer (send_morse) + audio sidetone synth (click-free).
  Round-trip verified: generated "CQ CQ DE KD9NWA" decodes back correctly.
- Speech generation: piper neural TTS (en_US-lessac-medium) + espeak fallback;
  transmit via USB codec playback while keyed.
- audio.py extended: playback sink/device discovery + play_wav.
- 4 new CLI cmds (send-cw/send-speech/preview-cw/preview-speech) + 4 agent tools.
- Gate refined: dry_run simulates full path (needs master switch) but never keys;
  real keying still needs --allow-tx + master switch + band guard. All verified.

### Phase 6 — DONE (2026-08-16): first power-on verification
- radio-poweron-check: staged diagnostic (CAT->rigctld->telemetry->audio->decode->TX).
  Safe by default; CI-V baud auto-probe; TX test gated behind --tx + YES confirm.
  Verified failure path (radio off -> clean FAIL at step 1) and that all
  underlying CLI ops it calls work against the dummy rig.
