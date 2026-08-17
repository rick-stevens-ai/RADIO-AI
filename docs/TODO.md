# RADIO-AI — to-do / roadmap

## JS8Call
- [x] Basic JS8Call API integration (listen / send / status) — `hamradio/js8.py`
- [x] Diagnose + fix API flakiness (single long-lived socket; pgrep liveness;
      no 2nd rigctld link during listen) — see docs/JS8CALL_NOTES.md
- [x] **SMS via JS8Call → APRS-IS → SMSGTE.** `js8-sms` / `js8-email` built.
      (JS8Call auto-derives the APRS passcode from APJ8CL, so only SpotToAPRS=true
      is required — verified. On-air format matches the source exactly.)
- [x] **Message queue / inbox.** `js8-inbox` (list) + `js8-store <call> <text>` built.
- [ ] Persistent JS8 listener daemon (one socket, publishes decodes to a file /
      the agent) instead of per-command connects — smoother for long monitoring.
- [ ] Auto-reply / directed-message watcher: notify (or auto-ACK) when someone
      directs a message to KD9NWA.

## WSPR (new capability)
- [x] **WSPR RX decode**: `radio wspr` — aligned 2-min capture -> wsprd -> spots
      with location. Verified on-air (9 spots incl. Ecuador 5108 km).
- [x] **WSPR beacon TX** (gated): `radio wspr-tx` — Python 4-FSK generator (via
      wsprsim symbols), verified decodable by wsprd. Not yet keyed on-air.
- [x] **wsprnet.org query**: `radio wspr-spots` — who has spotted a call (verified).
      (Uploading our own RX spots to wsprnet: still optional/TODO.)
- [ ] WSPR dial table (USB dial freqs): 80m 3.5686, 40m 7.0386, 30m 10.1387,
      20m 14.0956, 17m 18.1046, 15m 21.0946, 10m 28.1246 MHz.

## Nice-to-haves
- [ ] Unify "who hears me" across FT8 (PSKReporter), WSPR (wsprnet), JS8.
- [ ] CW: dictionary/callsign-aware correction already in; consider a longer
      rag-chew capture profile.
