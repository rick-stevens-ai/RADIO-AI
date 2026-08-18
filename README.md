# RADIO-AI

**An agent-operable ham radio station.** A control layer that lets an AI agent
(or a human at a JSON CLI) run a real HF station end-to-end: scan the bands,
decode CW / FT8 / voice, identify who's on the air, and — with explicit,
gated authorization — **transmit and complete real FT8 QSOs autonomously**.

Built and proven on-air for **KD9NWA** (grid EN51) with an **Icom IC-7300**,
running on an Intel NUC (Ubuntu 24.04). All logic is plain Python + a thin
CLI + a [pi](https://github.com/earendil-works/pi) agent extension.

> 📻 First fully-autonomous FT8 QSOs logged with this stack: **AI4FR** (FL),
> **CO8LY** (🇨🇺 Cuba), **V31DL** (🇧🇿 Belize), and more — the agent scanned,
> picked a clear frequency, checked the clock, keyed the rig through the safety
> gate, and worked the contact to `73`.

---

## What it can do

| Capability | Command | Agent tool |
|---|---|---|
| Rig telemetry / QSY (never TX) | `radio status` · `radio freq` · `radio mode` | `radio_status`, `radio_set_freq`, `radio_set_mode` |
| **Band scan → usage map** (JSON + PNG) | `radio scan --band 20m --plot out.png` | `radio_scan_band` |
| **Decode FT8** (WSJT-X `jt9`), weak-signal | `radio ft8 --band 20m --locate` | `radio_decode_ft8` |
| **Decode CW / Morse** (built-in DSP decoder) | `radio cw [--method dsp\|multimon\|both]` | `radio_decode_cw` |
| **Weak-signal CW copy** (multi-cycle voting) | `radio cw-monitor` | — |
| **Find live CW** (survey watering holes) | `radio cw-hunt [--copy]` | — |
| **Speech-to-text** (SSB, whisper.cpp) | `radio speech` | `radio_decode_speech` |
| **JS8Call messaging** (listen / send) | `radio js8 [--band]` · `js8-send` · `js8-status` | `radio_js8_listen`, `radio_js8_send`, `radio_js8_status` |
| **Email over ham radio** (JS8 → APRS-IS → EMAIL-2) | `radio js8-email <addr> "<msg>"` | `radio_js8_email` |
| **SMS to a phone over ham radio** (relay chain) | `radio js8-text <number> "<msg>"` | — |
| **Autonomous FT8 QSO** (answer a CQ) | `radio ft8-call <CALL> [GRID]` | `radio_ft8_call` |
| **Call CQ** and work the first answer | `radio ft8-cq` | `radio_ft8_cq` |
| Generate a standards-compliant FT8 WAV | `radio ft8-encode "<msg>"` | — |
| Transmit **CW** / **voice (TTS)** — gated | `radio send-cw` · `radio send-speech` | `radio_send_cw`, `radio_send_speech` |
| **Who's hearing me?** (PSKReporter) | `radio whohearsme` | `radio_who_hears_me` |
| **Callsign → location** (offline) | `radio whois <CALL> [--grid]` | `radio_whois` |
| **Clock / NTP health** (required for FT8) | `radio clock` | `radio_clock_sync` |
| TX safety gate | `radio tx-enable` / `tx-disable` / `unkey` | `radio_tx_*` |

Every command emits **JSON**, so it composes cleanly for agents and scripts.

---

## Highlights

### From-scratch CW decoder (`hamradio/cwdecode.py`)
A real DSP Morse decoder that beats `multimon-ng` on off-air signals:
band-pass around the auto-detected note → envelope detection → **AGC
normalization** (survives QSB/fading) → **Schmitt-trigger** slicing → adaptive
dit/dah and letter/word **timing clustering** (self-tunes to the sender's WPM and
tracks drift) → Morse table. Reports `wpm`, `tone_hz`, `snr_ratio`, and a
`confidence`, and **gates out noise-only captures** (SNR / speed / keying-
regularity checks) so it stays silent instead of emitting garbage. Proven on-air
decoding live CQs and callsigns (e.g. `CQ DE AA8P`). `--method both` runs the
legacy multimon-ng path side-by-side for comparison.

### Intelligent CW correction pass (`hamradio/cwcorrect.py`)
Real ops run letters together and QSB garbles characters; humans copy through it
using *context*. This pass encodes that knowledge (on by default):
- **re-segments** run-together text against a CW vocabulary/prosign set
  (`CQCQDE` → `CQ CQ DE`),
- **snaps RST** reports (`5NN` → `599`),
- **extracts a valid callsign core** from garbled tokens (`HEAA8P` → `AA8P`) and
  **validates/repairs callsigns against the local FCC database** — but only when
  the match is *unambiguous* (a single 1-edit licensed neighbor), so it never
  fabricates,
- **parses the QSO grammar** into structured fields
  (`cq`, `call`, `rst`, `name`, `qth`, `sign_off`).
The raw decode is always preserved; corrections are additive and logged.

### Weak-signal CW copy by voting (`cwdecode.monitor`)
CW isn't slot-timed, so a single capture may land in a gap or mix two stations
at different pitches. `radio cw-monitor` copies over several cycles, decodes each
in short windows (each locking to its own dominant tone to skip gaps and
separate stations), and **votes** on the callsign — preferring an FCC-validated
token. This mirrors how humans copy weak CW by waiting for repeats. Proven
on-air: voted out **AM4Q** (a Spanish contest station, ~6,800 km) from fading,
fragmented copies across 16 windows.

### CW watering-hole hunter (`cwdecode.hunt`)
`radio cw-hunt` tunes through the well-known non-contest CW calling frequencies
and activity centers — QRP calling (7.030/10.106/14.060…), **SKCC** straight-key
haunts (7.055/10.120/14.050…), **FISTS** centers (7.058/10.118/14.058), and
rag-chew segments — and ranks each by *keying quality*: not just tone SNR but the
envelope on/off ratio, which distinguishes clean hand-sent CW from carriers,
data, and noise. `--copy` then parks on the best clean signal and copies it.
Great for finding an actual conversation instead of a contest pile-up.

### JS8Call keyboard-to-keyboard messaging (`hamradio/js8.py`)
JS8Call is a *conversational* weak-signal mode built on the FT8 waveform —
free-text messages, directed calls (`@CALL`), heartbeats, relays, store-and-
forward. Rather than re-implement its message assembly, this drives the real
JS8Call app headless (Xvfb) through its **TCP API** (JSON on :2442), sharing our
`rigctld` for CAT. `radio js8` returns fully-assembled decoded messages with
from/to/SNR (and the directed subset), optionally location-annotated; `js8-send`
transmits (gated by the TX master switch). Proven on-air decoding live 40m JS8
nets — heartbeats and directed greetings across the continent (e.g.
`KF0DRT (MN): KR4FTX HELLO!`).

> **Note on the JS8Call API:** it wedges if a client connects and disconnects in
> quick succession, so this module uses a *single* long-lived socket per call
> and probes liveness with `pgrep` (never a throw-away socket). It also avoids
> opening a second `rigctld` link during a listen, which would starve JS8Call's
> CAT polling and stall its decode loop.

### Autonomous FT8 QSO engine (`hamradio/ft8.py`)
- **Encode:** [`kgoba/ft8_lib`](https://github.com/kgoba/ft8_lib) `gen_ft8` produces a
  real FT8 waveform — verified to decode in WSJT-X's own `jt9` (true interop).
- **Decode:** WSJT-X `jt9 --ft8` reads replies each 15 s cycle.
- **State machine:** correct FT8 message sequencing
  (`answer → R-report → 73`, or `CQ → report → RR73`), UTC 15 s slot alignment,
  even/odd slot-parity locking, and a captures-the-*reply*-slot timing model.
- **Auto-logs** each contact to ADIF.

### Transmit safety (`hamradio/tx.py`)
Transmit is **off by default** and passes through layered guards:
1. a **master switch** (`radio tx-enable "<reason>"`),
2. an **explicit `--allow-tx`** on the command,
3. a **band-plan guard** (segment + hard ceiling),
4. **forward-power verification** (won't falsely report "sent" with no RF), and
5. a **fail-safe un-key** on exit/timeout.

The agent extension additionally asks for interactive confirmation before keying.

### Offline callsign → location (`hamradio/location.py`)
Fully local, no network at query time. Merges:
1. **FCC ULS** amateur database (~825k active US hams → name/city/state/ZIP),
   imported into a compact SQLite,
2. a **DXCC prefix table** (country/entity + coordinates for *any* callsign), and
3. **Maidenhead grid → lat/lon**,
returning distance + bearing from your QTH and a `is_dx` flag. `radio ft8 --locate`
annotates every decode inline so DX pops out at a glance.

### Propagation feedback (`hamradio/pskreporter.py`)
Query [PSKReporter](https://pskreporter.info) for who recently decoded you —
unique receivers, max/avg distance, DXCC entities, per-station SNR/distance/bearing.
A real-world "am I getting out?" check that needs no second radio.

---

## Repository layout

```
hamradio/            core library (importable Python package)
  rig.py             telemetry + non-TX rig control (via rigctld) + clock_sync
  tx.py              GATED transmit (master switch, band guard, fail-safe)
  scan.py            band-occupancy scan → JSON + matplotlib PNG
  audio.py           RX capture from the IC-7300 USB codec
  decode.py          FT8 (jt9) + CW + speech (whisper.cpp), + location enrich
  cwdecode.py        from-scratch DSP CW/Morse decoder (AGC + adaptive timing)
  cwcorrect.py       context-aware CW correction (re-segment, callsign/RST snap, fields)
  js8.py             JS8Call driver via its TCP API (listen / send / status)
  ft8.py             autonomous FT8 QSO engine (ft8_lib encode + jt9 decode)
  generate.py        CW / TTS waveform generation for TX
  pskreporter.py     "who hears me" via PSKReporter
  location.py        offline callsign→location (FCC + DXCC + grid)
bin/
  radio              the JSON CLI (all commands above)
  rigctld-ic7300     auto-detect the CP2102 and launch rigctld (model 3073)
  radio-poweron-check
pi-extension/
  radio.ts           pi agent extension exposing ~23 radio_* tools
systemd/
  rigctld.service    systemd --user unit (single serial owner; WSJT-X/fldigi multiplex)
  js8call.service    systemd --user unit (JS8Call headless under Xvfb, API :2442)
scripts/
  setup-location-db.sh
docs/
  STATION.md         detailed station/architecture doc
  PLAN.md            build plan / capability roadmap
install.sh
```

---

## Install

```bash
git clone https://github.com/rick-stevens-ai/RADIO-AI.git
cd RADIO-AI
./install.sh                       # copies lib+CLI, links `radio` onto PATH, installs services

# System dependencies (Ubuntu):
sudo apt install hamlib-utils wsjtx fldigi rtl-sdr multimon-ng sox ffmpeg \
                 python3-numpy python3-scipy python3-matplotlib

# FT8 transmit encoder:
git clone https://github.com/kgoba/ft8_lib ~/radio/ft8_lib && (cd ~/radio/ft8_lib && make)

# Offline location database (optional but recommended):
./scripts/setup-location-db.sh     # downloads FCC dump, builds ~53 MB SQLite

# Start the rig control daemon (with the IC-7300 powered on):
systemctl --user daemon-reload && systemctl --user enable --now rigctld
radio status
```

See [`docs/STATION.md`](docs/STATION.md) for the full architecture, wiring, and
per-command reference.

---

## ⚠️ Licensing & legal

**Transmitting requires a valid amateur radio license.** You are the control
operator and are responsible for every emission — frequency privileges, band
plan, identification, and power limits. The TX gate defaults to **off**; you
must explicitly arm it. Configure `TX_SEGMENTS` in `hamradio/tx.py` to *your*
license class and country before enabling transmit. Receive/decode features are
unrestricted.

## License

MIT © 2026 Rick Stevens (KD9NWA). See [LICENSE](LICENSE).

FT8 encoding uses [`kgoba/ft8_lib`](https://github.com/kgoba/ft8_lib) (MIT);
decoding uses [WSJT-X](https://wsjt.sourceforge.io/) (`jt9`, GPL) as an external
tool. Callsign data © the U.S. FCC (public domain).
