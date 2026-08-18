---
name: kd9nwa-station
description: >
  Operate the KD9NWA agent-controlled ham-radio station (Intel NUC "nuc7" +
  Icom IC-7300). Use this whenever you need to run the radio: check band
  conditions, scan spectrum, decode/transmit FT8, decode/transmit CW/Morse,
  run autonomous FT8 QSOs, send WSPR beacons, send JS8Call / APRS / email /
  SMS messages over RF, look up callsign locations offline, or check who is
  hearing the station. All transmit is real and legally gated — read the
  TRANSMIT SAFETY section before keying.
---

# KD9NWA agent radio station — operator skill

You are operating a **real, on-air amateur-radio station**. Everything you TX is
radiated on live HF antennas under FCC callsign **KD9NWA** (operator Rick
Stevens, grid **EN51 / EN51TP**, Plainfield IL). Transmit is *allowed* but
*guarded* — see **TRANSMIT SAFETY**. When in doubt, receive/decode only.

Read this whole file first. Then use `reference/` files for command details:
- `reference/commands.md` — every `radio` subcommand, arguments, JSON output
- `reference/architecture.md` — how the pieces fit (rigctld, codec, modules)
- `reference/troubleshooting.md` — the failure modes you WILL hit and the fixes
- `reference/relay-chain.md` — the JS8→email→SMS relay (works around dead gateways)

---

## 1. Where you run & how to reach the radio

- The radio host is the node **`nuc7`** (Intel NUC, Ubuntu 24.04). You reach it
  over the Telario mesh: `cez ssh nuc7 '<command>'` or `cesh run nuc7 '<command>'`.
  (If those are unavailable, plain `ssh nuc7` may work.)
- The **Mac "cherryrd"** hosts the email→SMS watcher (`cez ssh cherryrd`).
- Filter SSH banner noise:
  `... 2>&1 | grep -v -E "post-quantum|WARNING|vulnerable|upgraded"`
- The whole toolkit lives in `~/radio/agent/` on nuc7:
  - `bin/radio` — the JSON CLI you drive everything with (symlinked to
    `~/.local/bin/radio`, already on PATH)
  - `hamradio/` — the Python library (`ft8.py`, `cwdecode.py`, `js8.py`,
    `wspr.py`, `antenna.py`, `tx.py`, `rig.py`, `location.py`, …)
  - `logs/kd9nwa.adi` — the ADIF QSO log
  - `secrets.env` — API keys/tokens (chmod 600, **never** commit or echo)

**Every `radio` command prints JSON on stdout.** Pipe to `jq` to read fields.

## 2. Bring the station up (bootstrap checklist)

Run these on nuc7 at the start of a session:

```bash
# 0. Most operations need NO sudo. If you do (rare), the askpass helper is
#    cleared on reboot; recreate it with the operator's sudo password:
#      printf '#!/bin/sh\necho <SUDO_PASSWORD>\n' > /tmp/askpass.sh
#      chmod +x /tmp/askpass.sh
#    then:  SUDO_ASKPASS=/tmp/askpass.sh sudo -A ...
#    (the password is NOT stored in this repo — get it from the operator.)

# 1. rigctld (CAT control hub) must be running — it's a USER service:
systemctl --user status rigctld    # should be "active (running)"
systemctl --user restart rigctld   # if not

# 2. Confirm the rig answers:
radio status | jq '{freq_hz, mode, ptt}'   # ptt must be false

# 3. RX SENSITIVITY — RF gain silently reverts to 0.0 (deaf receiver!).
#    ALWAYS set it at session start or you'll get zero decodes:
radio rfgain 1.0

# 4. Clock sync (FT8/JS8/WSPR need <~1s accuracy):
radio clock | jq '{verdict, ft8_ok}'       # want ft8_ok: true
```

If `radio status` fails, see `reference/troubleshooting.md` (rigctld / serial).

## 3. The golden rules (memorize these)

1. **RF gain reverts to 0.0.** First thing every session: `radio rfgain 1.0`.
   Symptom of forgetting: "0 decodes" on an obviously busy band.
2. **Tune the antenna after ANY frequency/band change.** Use
   `radio freq-tune <hz>` (sets freq **and** tunes) instead of bare `radio freq`.
   Un-tuned = high SWR = little/no radiated power and possible rig fold-back.
3. **Only ONE program may own the rig's CAT link at a time.**
   - The `radio` CLI and JS8Call BOTH connect to rigctld (127.0.0.1:4532).
   - **Never** poke rigctld/serial (nc, rigctl, pyserial) while **JS8Call** is
     running — it exhausts JS8Call's tiny connection backlog and pops its "Rig
     Control Error" modal (blocks TX). Stop JS8Call first (`pkill -f js8call`).
   - For FT8/CW/WSPR/scan, JS8Call must be **stopped** so the CLI owns the rig.
4. **DATA MOD source must be USB(03)** for ANY codec-audio TX (FT8, CW-audio,
   voice, WSPR, JS8). It **reverts to 02 on power-cycle** → 0 W output. Fix in
   `reference/troubleshooting.md`. Symptom: PTT keys, codec RUNNING, but 0 W.
5. **Verify forward power on every TX.** `sent: true` is not enough. Read watts
   via Hamlib (below) or the codec RUNNING state; if 0 W, you're not radiating.
6. **Always leave the station safe** when done: `radio unkey; radio tx-disable`,
   then confirm `radio status` shows `ptt: false`.

## 4. TRANSMIT SAFETY (read before keying anything)

TX is legal here (licensed operator, approved), but multi-gated. **No command
transmits unless you clear the whole chain:**

```bash
radio tx-status                      # is the master switch on?
radio tx-enable "why I am keying"    # arm the master switch (creates TX_ENABLED)
radio <tx-cmd> ... --allow-tx        # per-command flag REQUIRED to actually key
radio unkey                          # force PTT off (fail-safe)
radio tx-disable                     # disarm master switch when finished
```

Gate mechanics (in `hamradio/tx.py`):
- Master switch = existence of file `~/radio/agent/TX_ENABLED`
  (`tx_globally_enabled()`), created by `tx-enable`, removed by `tx-disable`.
- Every TX command ALSO needs `--allow-tx`. Missing either → refused, no RF.
- **Band-plan guard** (`freq_tx_ok`) rejects out-of-band frequencies.
- **Hard power ceiling** `TX_HARD_CEILING = 300` and key-down timeout bounds.
- **Fail-safe**: `watchdog_unkey()` forces PTT off on any error/exception.

Operating discipline:
- Never call CQ or answer on top of an existing QSO — verify the frequency is
  clear first (`radio cw --seconds 5` or `radio ft8 --cycles 1`).
- Keep power modest (rig ~30–40 %; codec drive already set). Don't raise it.
- Identify with your callsign (the QSO/CQ engines already send `KD9NWA`).
- After a session, `radio unkey && radio tx-disable`.

**Verify watts** (Hamlib Python binding; safe to run while the CLI owns the rig,
NOT while JS8Call is running):
```python
import Hamlib; Hamlib.rig_set_debug(0)
r = Hamlib.Rig(2)                       # 2 = NETRIGCTL
r.set_conf("rig_pathname", "127.0.0.1:4532"); r.open()
print(r.get_ptt(), r.get_level_f(Hamlib.RIG_LEVEL_RFPOWER_METER_WATTS))
r.close()
```
Or check the codec substream: `cat /proc/asound/card1/pcm0p/sub0/status`
(`state: RUNNING` = audio flowing to the transmitter; card index is usually 1 —
confirm with `aplay -l | grep -i codec`).

## 5. Common operations (quick recipes)

**Check band conditions (what's open, where's the DX):**
```bash
radio rfgain 1.0
for b in 40m 20m 17m 15m 10m; do
  f=$(python3 -c "import sys;sys.path.insert(0,'$HOME/radio/agent');from hamradio import decode;print(decode.FT8_DIAL['$b'])")
  radio freq-tune $f >/dev/null
  radio ft8 --cycles 1 | jq -r "\"$b: \(.n_decodes) decodes\""
done
```

**Decode FT8 with locations:** `radio ft8 --band 20m --cycles 2 --locate`

**Answer a CQ (autonomous QSO, auto-logs to ADIF):**
```bash
radio tx-enable "FT8 QSO"; radio ft8-call <CALL> [GRID] --band 20m --allow-tx
radio unkey; radio tx-disable
```

**Call CQ and work whoever answers (auto-logs):**
```bash
radio tx-enable "FT8 CQ"; radio ft8-cq --band 20m --offset 1800 --allow-tx --max-cycles 24
radio unkey; radio tx-disable
```
Each `ft8-cq`/`ft8-call` run completes ONE QSO then returns. Loop it for more.
A run of ~24 cycles takes 6–8 min — budget your command timeout, or run in the
background and poll. Completed QSOs write to `~/radio/logs/kd9nwa.adi`.

**Decode CW / find live CW:**
```bash
radio cw-hunt --dwell 4            # rank the watering holes by keying quality
radio freq-tune <best_hz>; radio mode CW
radio cw --seconds 25 --method dsp # built-in DSP decoder (beats multimon here)
radio cw-monitor --cycles 3        # weak-signal voting copy
```

**Transmit CW (audio method — the one that works):**
```bash
radio tx-enable "CW CQ"
radio mode PKTUSB                   # codec audio path; CW-audio needs PKTUSB
radio tune                          # tune after the mode change
radio send-cw "CQ DE KD9NWA K" --wpm 18 --method audio --tone 700 --allow-tx
radio unkey; radio tx-disable
```
`--method rig` (native keyer) does NOT produce RF unless the IC-7300 menu has
"CW Keying via USB" / BK-IN enabled — use `--method audio`.

**WSPR:** `radio wspr --band 20m` (RX), `radio wspr-spots` (who spotted us),
`radio wspr-tx --band 20m --allow-tx` (gated beacon).

**Messaging over RF (JS8Call must be running for these; they key the rig):**
- Text/keyboard QSO: `radio js8-send "<msg>" --allow-tx`
- Email over radio: `radio js8-email <addr> "<msg>" --allow-tx`
- SMS to a phone: `radio js8-text <number> "<msg>" --allow-tx`
  (goes JS8 → APRS-IS → EMAIL-2 → Mac mail-watcher → Textbelt; see
  `reference/relay-chain.md`. Note: EMAIL-2 UPPERCASES the message.)

**Callsign → location (offline, ~825k US hams):** `radio whois <CALL> --grid`

**Who's hearing us:** `radio whohearsme` (PSKReporter, FT8/JS8).

## 6. Key facts & constants

- Station: `MY_CALL=KD9NWA`, `MY_GRID=EN51` (full EN51TP). Overridable via env.
- Rig: IC-7300, Hamlib model **3073**, CI-V over USB (CP2102 → `/dev/ttyUSB0`)
  @ **115200** baud. rigctld on **127.0.0.1:4532** (NET rigctl / model 2 client).
- Audio codec: Burr-Brown "USB Audio CODEC", ALSA **card 1** (`/proc/asound/card1`).
- FT8 dials (Hz): 40m 7074000 · 30m 10136000 · 20m 14074000 · 17m 18100000 ·
  15m 21074000 · 10m 28074000 (full table in `hamradio/decode.py`).
- JS8 dials (Hz): 40m 7078000 · 20m 14078000 · 17m 18104000 · 15m 21078000.
- WSPR dials (Hz): 40m 7038600 · 30m 10138700 · 20m 14095600 (USB dial).
- FT8 cycle 15 s (offset ~1500–1900 Hz). WSPR cycle 120 s. TX modes are codec-
  audio in **PKTUSB**.
- `_NO_RIG` CLI commands (don't open a 2nd rig link; safe alongside JS8Call):
  js8*, whois*, whohearsme, clock, audio-devices, wspr-encode, wspr-spots,
  tune, text, text-quota, relay.

## 7. What NOT to do

- Don't leave the rig keyed or the master switch armed at end of session.
- Don't run FT8/CW/scan while JS8Call is up (CAT contention) — stop JS8Call.
- Don't poke rigctld with nc/rigctl/pyserial while JS8Call is running.
- Don't handle the operator's personal-account passwords. Use API keys/tokens
  in `secrets.env`. Never print, log, or commit secrets.
- Don't raise TX power. Don't transmit on a busy frequency.
- Don't assume a band is dead from one 15 s window — RF gain, timing, and QSB
  all matter. Set gain, try 2–3 cycles, try a neighboring band.

## 8. When something's wrong

Go to `reference/troubleshooting.md`. The greatest hits: RF gain at 0 (deaf),
DATA MOD reverted to 02 (0 W TX), JS8Call CAT contention (TX blocked / DIAL 0),
antenna not tuned (high SWR / low power), stale `/proc/asound` RUNNING after a
killed TX (verify real PTT via Hamlib — usually already 0 W).
