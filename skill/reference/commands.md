# `radio` command reference

Every command prints JSON on stdout. Run `radio <cmd> --help` for exact args.
Bracketed types below note the important **output fields** you'll parse with jq.

## Receive / telemetry (no TX, safe anytime the CLI owns the rig)

| Command | Purpose | Key output fields |
|---|---|---|
| `radio status` | rig telemetry | `freq_hz`, `mode`, `ptt` (must be false) |
| `radio smeter` | S-meter | signal level |
| `radio rfgain [0.0..1.0]` | get/set RF gain — **set 1.0 each session** | `rf_gain`, `set` |
| `radio freq [hz]` | get/set dial (does NOT tune) | `freq_hz` |
| `radio mode [USB\|LSB\|CW\|PKTUSB\|FM\|AM]` | get/set mode | `mode` |
| `radio clock` | NTP/clock health (needed for FT8) | `verdict`, `ft8_ok`, `offset_s` |
| `radio audio-devices` | list capture devices | — |
| `radio tx-status` | is master switch armed? | — |

## Antenna tuner — run after ANY frequency change

| Command | Purpose |
|---|---|
| `radio tune` | tune the ATU on the current frequency (~9 s) |
| `radio tune --state` | read tuner state only, don't tune |
| `radio freq-tune <hz>` | **set frequency AND auto-tune** (preferred QSY) |

`tune` briefly stops/restarts the user rigctld to own the serial port, then
reconnects — expect a ~10 s blip. Output: `{tuned: true}` on a good match.
**Never `tune` while JS8Call is running** (it severs JS8Call's CAT link).

## Spectrum scan

```bash
radio scan --band 20m --step 500 --plot ~/radio/scans/20m.png
radio scan --lo 7000000 --hi 7300000 --mode CW --json-out /tmp/40cw.json
```
Output: occupancy list (frequency, level, active?) + optional PNG usage map.

## FT8 (WSJT-X jt9 decoder)

```bash
radio ft8 --band 20m --cycles 2 --locate
```
- `--band <b>` auto-QSYs to that FT8 dial first (still tune the antenna).
- `--cycles N` decode N 15 s windows; `--no-align` skip cycle-boundary wait.
- `--wav FILE` decode an existing WAV instead of live capture.
- `--locate` annotate each decode with the caller's location.

Output fields: `n_decodes`, and `decodes[]` each with `snr_db`, `dt_s`,
`freq_offset_hz`, `message` (and location fields with `--locate`).
`message` examples: `CQ N5DRW EM15`, `NR6TT KE4EB RR73`.

## FT8 autonomous QSO (GATED, auto-logs to ADIF on completion)

```bash
radio tx-enable "FT8"
radio ft8-cq   --band 20m --offset 1800 --allow-tx --max-cycles 24   # call CQ
radio ft8-call <CALL> [GRID] --band 20m --offset 1600 --allow-tx     # answer a CQ
radio unkey; radio tx-disable
```
- Each call completes **one** QSO then returns; loop for more.
- State machine: CQ → (someone answers `MYCALL THEIR GRID`) → send report →
  (their `R<report>`) → send RR73 → done. `ft8-call` mirrors it as answerer.
- A QSO counts complete once we've sent RR73 (we already got their report).
- **Auto-logs** to `~/radio/logs/kd9nwa.adi` via `ft8.log_qso_adif()` with
  CALL, GRIDSQUARE, BAND, FREQ, MODE=FT8, RST_SENT/RCVD, UTC date/time.
- A 24-cycle run is ~6 min; a station may answer at any cycle. Budget your
  command timeout accordingly (or run in background and poll). If you must kill
  it, immediately `radio unkey` and verify PTT=0 via Hamlib.
- `radio ft8-encode "<msg>" --offset 1500 --out f.wav` — make a WAV, no TX.

Event stream (stdout, one JSON/line): `{event:{cycle, stage, tx, sent}}`,
`{event:{answered_by:"NF2E"}}`, final `{qso:{dx_call, dx_grid, rst_sent,
rst_rcvd, completed}}`. Parse defensively — non-JSON lines can appear.

## CW / Morse

```bash
radio cw-hunt --dwell 4              # survey watering holes, rank by keying quality
radio cw-hunt --dwell 3 --copy      # + park on the best clean CW and copy it
radio cw --seconds 25 --method dsp  # decode (dsp=built-in, best; multimon; both)
radio cw-monitor --cycles 3 --seconds 20   # weak-signal voting copy
radio preview-cw "<text>" --wpm 20 --tone 700 --out f.wav   # render WAV, no TX
```
- `cw-hunt` output: `best{freq_hz,label,snr_ratio,keying_ratio,cw_like}` +
  ranked `results[]`. Pick one with `cw_like: true` and high `snr_ratio`.
- `cw` output: `text`, `wpm`, `tone_hz`, `snr_ratio`, `confidence`, and a
  `note` (e.g. "implausible speed … likely noise/QRM" → not a clean single sig).
  The DSP decoder gates out noise (emits empty text rather than garbage).
- Hand-sent CW (SKCC) copies with imperfect character segmentation — normal.

## CW / voice transmit (GATED)

```bash
radio tx-enable "CW CQ"
radio mode PKTUSB && radio tune              # audio-CW needs PKTUSB + tune
radio send-cw "CQ DE KD9NWA K" --wpm 18 --method audio --tone 700 --allow-tx
radio send-speech "<text>" --allow-tx        # TTS voice over codec (piper)
radio unkey; radio tx-disable
```
- **Use `--method audio`** (CW tones through the codec). `--method rig` returns
  `sent:false` with guidance unless the IC-7300 has "CW Keying via USB"/BK-IN on.
- Output includes `sent`, `error`. Verify forward power separately.
- `radio preview-speech "<text>" --out f.wav` — render TTS, no TX.

## WSPR

```bash
radio wspr --band 20m                 # RX one aligned 2-min window + decode
radio wspr-spots                      # wsprnet.org: who spotted us
radio wspr-encode --band 20m --out f.wav                      # make beacon WAV
radio wspr-tx --band 20m --power 30 --allow-tx                # GATED beacon (30 dBm=1W)
```
WSPR RX needs `wsprd -w` (wideband) because of our audio offset — handled
internally. `wspr` output: `spots[]` with call, SNR, grid, km, azimuth.

## Messaging over RF (JS8Call must be RUNNING; these key the rig)

```bash
radio js8-status                      # running? station, dial, speed
radio js8                             # listen for messages
radio js8-send "<msg>" --allow-tx     # keyboard-to-keyboard
radio js8-email <addr> "<msg>" --allow-tx      # email via APRS-IS -> EMAIL-2
radio js8-text <number> "<msg>" --allow-tx     # SMS via the relay chain
radio js8-inbox                       # list stored inbox
radio js8-store <call> "<msg>" --allow-tx      # leave a directed message
```
- `js8-text` builds `SMS <token> <number> <message>` and sends it as an EMAIL-2
  message to the operator's mailbox, where the Mac watcher forwards it via
  Textbelt. See `relay-chain.md`. **EMAIL-2 uppercases the message.**
- JS8Call is half-duplex: nuc7 can't decode its own TX (needs a 2nd station).

## Internet-side messaging (no RF; `_NO_RIG`, safe with JS8Call up)

```bash
radio text <number> "<msg>"           # real SMS via Textbelt (uses secrets.env key)
radio text-quota                      # Textbelt credit remaining
```

## Lookups & feedback

```bash
radio whois <CALL> --grid             # offline FCC + DXCC + Maidenhead location
radio whois-rebuild                   # (re)build the FCC ULS SQLite (~825k hams)
radio whohearsme --minutes 15 --top 10   # PSKReporter: who heard KD9NWA (FT8/JS8)
```

## TX gate control

```bash
radio tx-enable "reason string"       # arm master switch (creates TX_ENABLED)
radio tx-disable                      # disarm
radio unkey                           # force PTT off (fail-safe) -> {ptt:false, unkeyed:true}
```
