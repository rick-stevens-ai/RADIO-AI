# RADIO-AI — to-do / roadmap

## JS8Call
- [x] Basic JS8Call API integration (listen / send / status) — `hamradio/js8.py`
- [x] Diagnose + fix API flakiness (single long-lived socket; pgrep liveness;
      no 2nd rigctld link during listen) — see docs/JS8CALL_NOTES.md
- [ ] **SMS via JS8Call → APRS-IS → SMSGTE.** Add `js8-sms <number> <text>` that
      arms TX and sends `@APRSIS CMD :SMSGTE   :@<number> <text>{NN`. Verify
      config `spot_to_aprs=true`, `spot_to_reporting_networks=true`, and a valid
      APRS passcode. Also `js8-email <addr> <text>` via `:EMAIL-2`.
- [ ] **Message queue / inbox.** Wrap `INBOX.GET_MESSAGES` / `INBOX.STORE_MESSAGE`
      as `js8-inbox` (list) and `js8-store <call> <text>` (leave a message for a
      station). Surface directed-message relay (`CALL>CALL text`).
- [ ] Persistent JS8 listener daemon (one socket, publishes decodes to a file /
      the agent) instead of per-command connects — smoother for long monitoring.
- [ ] Auto-reply / directed-message watcher: notify (or auto-ACK) when someone
      directs a message to KD9NWA.

## WSPR (new capability)
- [ ] **WSPR RX decode**: `radio wspr` — tune the WSPR dial for a band, capture a
      2-minute (even-UTC-aligned) window, decode with `wsprd`, return spots
      (call/grid/dBm/SNR/drift/freq) + optional location enrichment.
- [ ] **WSPR beacon TX** (gated): generate the 4-FSK WSPR waveform for
      `KD9NWA EN51 <dBm>` and transmit in the 2-min window. Low power / long
      unattended runs — great propagation beacon.
- [ ] **wsprnet.org**: optionally upload our RX spots, and/or query the wsprnet
      API for where KD9NWA is being heard (like `whohearsme` but for WSPR).
- [ ] WSPR dial table (USB dial freqs): 80m 3.5686, 40m 7.0386, 30m 10.1387,
      20m 14.0956, 17m 18.1046, 15m 21.0946, 10m 28.1246 MHz.

## Nice-to-haves
- [ ] Unify "who hears me" across FT8 (PSKReporter), WSPR (wsprnet), JS8.
- [ ] CW: dictionary/callsign-aware correction already in; consider a longer
      rag-chew capture profile.
