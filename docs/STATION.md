# KD9NWA Agentic Radio Station — nuc7 (Intel NUC7 i5)

Agent-operable control layer for an **Icom IC-7300** (HF/6m, 100W).
Call sign **KD9NWA**, grid **EN51TP**.

## Architecture

```
                +----------------------+
   IC-7300  ────┤ CP2102 USB-UART (CAT)├──► /dev/serial/by-id/...CP2102...
   (HF rig)     │ USB Audio CODEC (RX/TX)├─► PipeWire source "...CODEC..."
                +----------┬-----------+
                           │
                 rigctld (systemd --user, model 3073, :4532)  ← single serial owner
                           │  (WSJT-X, fldigi, and the agent all multiplex here)
                           │
      hamradio/ python lib ─┤ rig.py   telemetry + non-TX control
                            │ tx.py    GATED transmit (master switch + band guards)
                            │ scan.py  band occupancy map (JSON + PNG)
                            │ audio.py RX capture from USB codec
                            │ decode.py CW (multimon-ng) + speech (whisper.cpp)
                           │
              `radio` CLI (JSON out)  ← bin/radio, on PATH
                           │
            pi extension radio.ts  → agents (piago) get radio_* tools
```

## Quick start (once the IC-7300 is powered on)

```bash
radio status                 # freq/mode/S-meter/PTT/band (JSON)
radio freq 14074000          # QSY (never transmits)
radio mode USB
radio audio-devices          # confirm the radio's USB codec is visible

# GOAL 1 — band usage map
radio scan --band 20m --step 500 --mode USB --plot ~/radio/scans/20m.png
radio scan --lo 7000000 --hi 7300000 --step 1000 --mode CW

# GOAL 2 — decode Morse (set radio to CW on the signal first)
radio cw --seconds 30

# GOAL 3 — speech-to-text (set radio to USB/LSB on the voice signal first)
radio speech --seconds 30
```

## Transmit safety (gated, per operator request TX is allowed but guarded)

Transmit is possible but every emission passes ALL of these:
1. **Master switch**: `radio tx-enable "reason"` creates `~/radio/agent/TX_ENABLED`.
   `radio tx-disable` removes it. Absent ⇒ hard receive-only.
2. **Explicit intent**: code paths must pass `allow_tx=True`; no accidental default.
3. **Band-plan guard**: frequency must be inside a configured TX segment
   (edit `TX_SEGMENTS` in `hamradio/tx.py` to match exact KD9NWA privileges).
4. **Fail-safe un-key**: a max key-down timeout is always armed and PTT is
   released in a `finally:` even if the caller crashes. `radio unkey` forces off.
5. In interactive agent sessions, `radio_tx_enable` also asks for confirmation.

> You (KD9NWA) are responsible for all emissions. `tx-enable` prints a reminder
> to confirm an antenna or dummy load is connected. Keep TX disabled when unattended.

## Agent control (pi / piago)

The extension `~/.pi/agent/extensions/radio.ts` registers these LLM tools:
`radio_status, radio_set_freq, radio_set_mode, radio_scan_band,
radio_decode_cw, radio_decode_speech, radio_audio_devices, radio_tx_status,
radio_tx_enable, radio_tx_disable, radio_unkey`.

Because nuc7 is on the Telario mesh, agents on any node can drive this radio via
`cez ssh nuc7 'piago -p "..."'` or by running piago on nuc7.

Example (verified working against a dummy rig):
```
piago -p "Scan 20m for activity and tell me the busiest segments, then tune there."
```

## Services / persistence

- `systemctl --user status rigctld` — the IC-7300 control hub. Auto-detects the
  CP2102 serial port and **waits for the radio to power on** (Restart=always,
  linger enabled), so it's live the moment you switch the rig on.
- CW: `multimon-ng`. Speech: `whisper.cpp` (CPU, AVX2) with `ggml-base.en.bin`
  under `~/radio/whisper.cpp` (swap to `small.en` for accuracy at lower speed).

## Files
- `~/radio/agent/hamradio/` — the python library
- `~/radio/agent/bin/radio` — CLI (symlinked to `~/.local/bin/radio`)
- `~/radio/agent/bin/rigctld-ic7300` — auto-detecting rigctld launcher
- `~/.config/systemd/user/rigctld.service`
- `~/.pi/agent/extensions/radio.ts` — agent tools
- `~/radio/scans/` — saved scan JSON + PNG maps
- `~/radio/backup/` — pre-upgrade config backups

## Tuning notes for the real radio
- IC-7300 CI-V-over-USB default baud is **115200**; if your radio menu differs,
  set `IC7300_BAUD` for the service or edit the wrapper.
- Confirm the PipeWire source name matches a hint in `audio.py:RADIO_NAME_HINTS`
  once the radio is on (`radio audio-devices` shows it).
- The dummy rig (`rigctld -m 1 -t 4532`) is handy for testing without the radio.

## Generation / transmit (added)

Matching the RX decoders, the station can now GENERATE:

```bash
# CW (Morse) — rig method uses the IC-7300 keyer (best timing)
radio send-cw "CQ CQ DE KD9NWA K" --wpm 20 --allow-tx        # transmits (gated)
radio send-cw "..." --method audio --tone 700 --allow-tx     # sidetone via USB codec
radio send-cw "..." --dry-run                                 # simulate (needs TX armed)
radio preview-cw "TEST" --out /tmp/cw.wav                     # render WAV, never keys

# Speech (neural TTS via piper -> transmit as voice on USB/LSB)
radio send-speech "this is kilo delta nine november whiskey alpha" --allow-tx
radio send-speech "..." --dry-run                             # simulate
radio preview-speech "hello" --out /tmp/v.wav                 # render WAV, never keys
```

TTS: piper `en_US-lessac-medium` under ~/radio/tts/voices (natural voice);
falls back to espeak-ng. CW audio uses a click-free raised-cosine envelope.

Transmit rules (unchanged): real keying needs BOTH `--allow-tx` AND the armed
master switch AND an in-band/permitted frequency; `--dry-run` simulates the full
path (still requires the master switch) without keying. Agent tools
`radio_send_cw` / `radio_send_speech` confirm before real keying;
`radio_preview_cw` / `radio_preview_speech` never key.

## First power-on verification

When you switch the IC-7300 ON, run:

```bash
radio-poweron-check           # full RX-side checks (no transmit)
radio-poweron-check --tx      # also run a gated CW TX test (asks to confirm dummy load)
radio-poweron-check --quick   # skip the live audio decode smoke test
```

It walks the stack in dependency order and prints PASS/WARN/FAIL:
  1. CAT serial (CP2102) present
  2. rigctld hub up + talking to the rig (auto-probes CI-V baud on failure)
  3. RX telemetry: freq/mode/S-meter + non-destructive freq round-trip (restores)
  4. Audio: IC-7300 USB codec as capture (RX) AND playback (TX)
  5. RX decode smoke test: 5s live capture through CW + speech decoders
  6. TX test (only with --tx + typing YES at the dummy-load prompt): dry-run
     then a short keyed CW id, then forces PTT off and disarms the master switch.

Exit code 0 = all critical checks passed. Safe by default: never transmits
without --tx AND explicit confirmation.

## On-air TX findings (verified 2026-08-16, real IC-7300)

- CAT control, PTT, and RF generation all confirmed working (FM carrier test
  showed forward power; audio-CW and voice both showed forward power on the meter
  and scope).
- **CW method="audio" is the DEFAULT and works** with the standard USB-audio
  wiring: it auto-switches to PKTUSB, plays CW tones through the codec, keys PTT,
  verifies forward power, and restores the prior mode.
- **CW method="rig" (send_morse) does NOT work** until the IC-7300 menu has
  "CW Keying via USB" / BK-IN enabled. The tool now VERIFIES forward power and
  honestly returns sent=false with guidance instead of a false positive.
- Voice TTS transmit works via the same codec path (piper natural voice, auto
  PKTUSB, power-verified). To enable native SSB-mic voice or rig CW keyer later,
  adjust the IC-7300 DATA MOD source / CW-USB menu.
- All TX still gated: master switch + --allow-tx + band plan + fail-safe un-key,
  and now + forward-power verification.

## On-air operating findings (verified 2026-08-17, real IC-7300)

- **FT8 QSOs now auto-log to ADIF.** `ft8.call_cq()` / `ft8.run_qso()` write a
  proper ADIF record (`hamradio/ft8.log_qso_adif`) to `~/radio/logs/kd9nwa.adi`
  on completion — CALL, GRIDSQUARE, BAND, FREQ, MODE=FT8, RST_SENT/RCVD, UTC
  date/time, STATION_CALLSIGN, MY_GRIDSQUARE. A QSO counts complete once we have
  sent RR73 (we already received their report to reach that stage). Worked a run
  of contacts calling CQ on 20m plus DX (CO8LY, Cuba) on 17m; all logged.
- **RF gain reverts to 0.0** after some rig/power events — with RF gain at 0 the
  receiver is nearly deaf (few/no FT8 decodes). Set `radio rfgain 1.0` at the
  start of any RX session; it roughly tripled decode counts here.
- **DATA MOD source (1A 05 00 66) must be USB(03)** for ANY codec-audio TX
  (FT8, CW-audio, voice). It reverts on power-cycle. Reset via CI-V:
  frame `FE FE 94 E0 1A 05 00 66 03 FD` (reply ends `fb fd` = OK). Do this with
  the user rigctld stopped (`systemctl --user stop rigctld`) so pyserial can own
  `/dev/ttyUSB0`, then restart it. Symptom when wrong: PTT keys, codec RUNNING,
  but 0 W forward.
- **CW copy of hand-sent SKCC** works well with the built-in DSP decoder
  (`radio cw --method dsp`): locks tone + WPM (25 WPM at SNR 200+), cleaner than
  multimon-ng. Character segmentation of hand-sent CW is imperfect but readable.
  `radio cw-hunt` ranks the watering holes by keying quality; `--copy` parks on
  the best clean one.

## Band scanning (verified on-air 2026-08-16)

```bash
radio scan --band 20m --step 2000 --mode USB --plot ~/radio/scans/20m.png
radio scan --band 40m --step 2000 --mode LSB
radio scan --lo 7175000 --hi 7300000 --step 1000 --mode LSB --settle 0.12  # fine
radio rfgain            # check RF gain (0.0..1.0)
radio rfgain 1.0        # open RF gain fully (low value = muted receiver!)
```

GOTCHA found in testing: if scans read a flat noise floor everywhere, the radios

## Band scanning (verified on-air 2026-08-16)

    radio scan --band 20m --step 2000 --mode USB --plot ~/radio/scans/20m.png
    radio scan --band 40m --step 2000 --mode LSB
    radio scan --lo 7175000 --hi 7300000 --step 1000 --mode LSB --settle 0.12  # fine
    radio rfgain            # check RF gain (0.0..1.0)
    radio rfgain 1.0        # open RF gain fully (low value = muted receiver!)

GOTCHA found in testing: if scans read a flat noise floor everywhere, the radio's
RF GAIN was at 0 (receiver muted) -- NOT an antenna fault. "radio rfgain 1.0"
fixes it; the scanner now emits an rx_warning when RF gain is near zero.
Output JSON has: noise_floor_db, threshold_db, active_segments[], points[]; the
--plot PNG shows S-meter vs frequency with active segments shaded.

## FT8 & CW decoding (verified on-air 2026-08-16)

FT8 (WSJT-X jt9 decoder) -- best for weak-signal work, decodes below the noise floor:

    radio ft8 --band 20m               # auto-tune 14.074 USB, align to 15s cycle, decode
    radio ft8 --band 40m --cycles 3    # decode 3 consecutive cycles
    radio ft8 --wav capture.wav        # decode an existing WAV

Returns n_decodes, per-signal snr_db/dt_s/freq_offset_hz/message, and cq_calls[]
(stations calling CQ). First live test decoded 20 stations across 2 cycles incl.
DX (Wales, Cuba, Italy, Poland, Germany, Sweden, Greece) and W1AW/1 (ARRL HQ).
FT8_DIAL has standard dial freqs for 160m-2m.

CW (multimon-ng, now with auto tone-detection + narrow bandpass):

    radio cw --band-independent --seconds 20   # capture + decode (set CW mode first)
    radio cw --wav file.wav

Now reports tone_hz, snr_ratio, and signal=strong|weak|none so you know if a copy
is trustworthy. CW-by-ear decode needs a STRONG clean signal; for weak/crowded
conditions FT8 is far more reliable. (During testing 20m/40m CW were near the
noise floor, so live CW copy was marginal -- expected for the conditions.)

RF GAIN reminder: if decoders/scans read nothing, check `radio rfgain` (0 = muted RX).

## PSKReporter — "who can hear me" (verified 2026-08-16)

    radio whohearsme                    # who decoded KD9NWA in last 15 min
    radio whohearsme --minutes 30 --top 10
    radio whohearsme --call W1AW        # check any callsign

Queries retrieve.pskreporter.info (senderCallsign=CALL). Returns unique receiver
count, max/avg distance, DXCC entities, and per-receiver call/grid/SNR/
distance_km/bearing_deg (grid math from EN51TP). Cached 5 min to respect PSKR
rate limits. Agent tool: radio_who_hears_me.

FIRST RESULT: after our FT8 CQ/answer attempts, 53-54 unique stations decoded
KD9NWA across the US + Canada, max ~2300 km, avg ~1500 km, best +9 dB. Confirms
the station is getting out well even when a specific QSO does not complete.

## Local callsign -> location lookup (verified 2026-08-16)

Fully offline, fast. Three merged data sources:
  1. FCC ULS amateur DB (825k active US hams) -> SQLite ~/radio/data/fcc_amat.sqlite
     name/city/state/ZIP. Rebuild weekly: `radio whois-rebuild`
     (re-download: cd ~/radio/data && curl -O https://data.fcc.gov/download/pub/uls/complete/l_amat.zip && unzip -o l_amat.zip EN.dat HD.dat)
  2. DXCC prefix table (hamradio/location.py) -> country/entity + lat/lon for ANY call worldwide
  3. Maidenhead grid -> lat/lon (refines coords when a grid is known)

    radio whois V31DL --grid EK57     # -> Belize, 2685 km, is_dx:true
    radio whois AI4FR                 # -> John Whitt Jr, Dade City FL
    radio ft8 --band 20m --locate     # each decode annotated with caller location
                                      #   (DX flagged, US state shown, distance/bearing)

Agent tools: radio_whois ; radio_decode_ft8(locate=true).
FCC EN.dat field map (0-idx): [4]=call [7]=name [16]=city [17]=state [18]=zip;
active filter via HD.dat [5]=='A'. Import ~7 s for the whole DB.

## Transmit hardware chain — critical settings (learned the hard way)

For the IC-7300 to make real RF from computer-generated audio (FT8/WSPR/JS8):

1. **DATA MOD source must be USB.** CI-V item `1A 05 00 66` = `03` (USB). If the
   radio is power-cycled and this reverts (seen as `02`), every digital TX keys
   the rig but produces **0 W / 0 ALC** (audio is ignored). Set it via CI-V:
   `FE FE 94 E0 1A 05 00 66 03 FD`. It persists in the rig's memory once set.
   Symptom to watch: `RFPOWER_METER_WATTS` and `ALC` both read 0 during TX.

2. **Codec TX drive level.** The USB codec sink volume sets audio drive → ALC.
   ~45% gives ALC ~0.9 at ~13-20 W. Persisted by the user service
   `radio-audio-level.service` (re-applies on boot; PulseAudio/PipeWire resets it).

3. **Antenna tuner after any band change.** `radio tune` runs the internal tuner
   (CI-V `1C 01 02`, poll `1C 01` until `01`=matched). Without a match the rig
   folds back power / high SWR. `radio freq-tune <hz>` sets freq + tunes.
   NOTE: tuning ~8-9 s; if it times out at state `02` (still tuning), just run it
   again.

### Verifying TX without disturbing rigctld
`cat /proc/asound/card1/pcm0p/sub0/status` → `state: RUNNING` == audio going to
the codec. For actual watts, read `RIG_LEVEL_RFPOWER_METER_WATTS` via the Hamlib
binding (same rigctld the CLI uses). During JS8/FT8 you'll see ~6 s at full watts
then ~4 s at 0 W — that's the normal inter-frame gap, not a fault.

## JS8Call + antenna tuner interaction (IMPORTANT ordering)

`radio tune` (and anything that restarts rigctld) **severs JS8Call's persistent
CAT link** — JS8Call then logs `write_block ... Broken pipe` /
`network_open: failed to connect to 127.0.0.1:4532`, keeps decoding (audio is
independent) but **cannot key PTT**, so a triggered "Send" toggles with no RF.
Fix / correct order:
  1. Set the band + **`radio tune`** FIRST (JS8Call not yet running, or expect to
     restart it).
  2. THEN start JS8Call (`hamradio.js8.ensure_running`) so it opens a fresh CAT
     link. Verify with `RIG.GET_FREQ` → DIAL must be nonzero (0 == CAT dead).
If JS8Call's CAT dies mid-session, just restart JS8Call.

## SMS via SMSGTE needs the country code
`format_sms()` now prefixes a bare 10-digit NANP number with `1`
(`@16303860391`). Without the country code SMSGTE silently drops the message
(email via EMAIL-2 worked without it, SMS did not). Confirmed: EMAIL-2 delivered
end-to-end (RF → APRS igate → APRS-IS → email).
