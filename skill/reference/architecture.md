# Station architecture

## Signal chain

```
                 CI-V over USB (CP2102 -> /dev/ttyUSB0 @115200)
   IC-7300  <==============================================>  rigctld
   (HF xcvr)                                              (127.0.0.1:4532,
      ^  |                                                 Hamlib model 3073,
      |  |  RX/TX audio (USB Audio CODEC, Burr-Brown)       NET rigctl server,
      |  v  ALSA card 1 (/proc/asound/card1)                systemd --user svc)
   antenna + ATU                                                 ^
   (internal tuner, CI-V 1C 01)                                  |
                                                          model-2 NETRIGCTL
                                                          clients:
                                                          - radio CLI (hamradio.rig.Rig)
                                                          - JS8Call (own CAT link)
```

**Two independent audio/CAT users, one at a time on CAT:**
- CAT (frequency, mode, PTT, meters) goes through **rigctld** on :4532.
- Digital audio (FT8/CW-audio/voice/WSPR/JS8) goes through the **codec** (card 1).
- The `radio` CLI and JS8Call each open their own rigctld connection.
  rigctld's LISTEN backlog is tiny (~4); extra connections while JS8Call is up
  exhaust it and trip JS8Call's "Rig Control Error". **Run one at a time.**

## Components on nuc7 (`~/radio/agent/`)

- `bin/radio` — argparse CLI, dispatches to `hamradio/`. Prints JSON. Skips
  opening a rig for `_NO_RIG` commands so they're safe alongside JS8Call.
- `bin/rigctld-ic7300` — launches rigctld (model 3073, /dev/ttyUSB0, 115200).
- `bin/radio-poweron-check` — first-power-on verification.
- `hamradio/` library:
  - `rig.py` — `Rig` (NETRIGCTL model-2 client to :4532); `clock_sync()`.
  - `tx.py` — the TX gate: `tx_globally_enabled()`, `enable_tx()`,
    `disable_tx()`, `freq_tx_ok()`, `keyed()`, `watchdog_unkey()`,
    `TX_ENABLE_FILE = ~/radio/agent/TX_ENABLED`, `TX_HARD_CEILING = 300`.
  - `antenna.py` — ATU control via CI-V `1C 01` (00 off / 01 on / 02 start-tune);
    `tune()`, `tuner_state()`, `set_tuner()`. Briefly owns the serial port.
  - `scan.py` — spectrum occupancy + PNG usage maps.
  - `decode.py` — FT8 decode (jt9), `FT8_DIAL` table, RX capture helpers.
  - `ft8.py` — autonomous QSO engine (`call_cq`, `answer_cq`/`run_qso`),
    ft8_lib WAV gen (`encode_wav`), **ADIF logging** (`log_qso_adif`,
    `_band_name`, `_adif_field`; writes `~/radio/logs/kd9nwa.adi`).
  - `cwdecode.py` — from-scratch DSP CW decoder + `hunt()` (watering holes) +
    `monitor()` (voting). `CW_WATERING_HOLES` table.
  - `cwcorrect.py` — CW error correction (re-segment, RST snap, callsign core).
  - `generate.py` — CW/speech WAV synthesis (piper TTS; raised-cosine CW).
  - `js8.py` — JS8Call headless control + messaging (`ensure_running`,
    `Js8Client`, `send`, `send_email`, `send_text`, `format_email`, `inbox`).
  - `wspr.py` — WSPR RX (`wsprd -w`), TX (Python 4-FSK via `wsprsim` symbols),
    `WSPR_DIAL`, `who_spots()` (wsprnet POST op=Update).
  - `location.py` — offline callsign lookup (FCC ULS SQLite + DXCC + Maidenhead).
  - `pskreporter.py` — "who hears me" (FT8/JS8 spots).
  - `messaging.py` — third-party relays (`send_textbelt`, `send_ntfy`,
    `send_telegram`, `notify`); `_load_secret()` from `secrets.env`.
  - `relay.py` — JS8→SMS off-air gateway (parse directed commands, forward).
- `secrets.env` — chmod 600, git-ignored. `TEXTBELT_KEY`, `RELAY_TOKEN`,
  `RELAY_WATCH_ADDR`. Loaded by `_load_secret()` / `_secret()`.
- `logs/kd9nwa.adi` — ADIF QSO log (git-ignored).

## Services (systemd --user)

- `rigctld.service` — the CAT hub. Auto-restarts. `systemctl --user
  status|restart rigctld`. **This must be running for anything CAT.**
- `radio-audio-level.service` — persists the codec drive level (~45 %).
- (JS8Call is launched on demand by `js8.ensure_running()`, not a service.)

## JS8Call headless (when messaging over RF)

`js8.ensure_running()` launches an off-screen JS8Call:
Xvfb `:99` + openbox (WM) + a dbus session + at-spi-bus-launcher
(`QT_ACCESSIBILITY=1`) + js8call. It shares rigctld :4532 and exposes a TCP API
on **:2442**. Headless TX can't be triggered by the TCP API alone — the code
actuates the checkable "Send (<duration>)" button via **AT-SPI** (`doAction(0)`)
after setting the RX/TX/SPOT menu toggles. Verify TX via the codec RUNNING state.
Details in `docs/JS8CALL_NOTES.md`.

## The pi extension

`pi-extension/radio.ts` exposes a subset of commands as agent tools
(`radio_status`, `radio_scan_band`, `radio_decode_ft8`, `radio_decode_cw`,
`radio_ft8_call`, `radio_ft8_cq`, `radio_send_cw`, `radio_whois`,
`radio_who_hears_me`, `radio_clock_sync`, `radio_js8_*`, `radio_tx_*`, …).
Install per `docs/` if you want tool-call access instead of shelling out.

## The email->SMS relay (on cherryrd, the Mac)

`relay/email_sms_watcher.py` + launchd plist. Reads the local Apple Mail store
(no IMAP password), matches EMAIL-2 mail + shared token, forwards via Textbelt.
Full detail in `reference/relay-chain.md`.
