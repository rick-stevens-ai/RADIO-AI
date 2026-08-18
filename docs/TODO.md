# RADIO-AI — to-do / roadmap

## JS8Call
- [x] Basic JS8Call API integration (listen / send / status) — `hamradio/js8.py`
- [x] Diagnose + fix API flakiness (single long-lived socket; pgrep liveness;
      no 2nd rigctld link during listen) — see docs/JS8CALL_NOTES.md
- [x] **SMS via JS8Call → APRS-IS → SMSGTE.** `js8-sms` / `js8-email` built.
      (JS8Call auto-derives the APRS passcode from APJ8CL, so only SpotToAPRS=true
      is required — verified. On-air format matches the source exactly.)
      NOTE: SMSGTE.org is DEAD (domain squatted) and AT&T's txt/mms.att.net
      email-to-SMS gateway was discontinued June 2025 — see the relay chain below
      for the working replacement.
- [x] **Message queue / inbox.** `js8-inbox` (list) + `js8-store <call> <text>` built.
- [ ] Persistent JS8 listener daemon (one socket, publishes decodes to a file /
      the agent) instead of per-command connects — smoother for long monitoring.
- [ ] Auto-reply / directed-message watcher: notify (or auto-ACK) when someone
      directs a message to KD9NWA.

## SMS relay chain (works around dead SMSGTE + AT&T gateways)
- [x] **`radio js8-text <number> <message>`** (nuc7): builds a tokenized EMAIL-2
      command `SMS <token> <number> <message>` and transmits it over JS8 → APRS-IS
      → EMAIL-2 → the operator's mailbox. GATED like all TX. Token +
      RELAY_WATCH_ADDR live in `~/radio/agent/secrets.env` (git-ignored).
- [x] **Mail watcher** `relay/email_sms_watcher.py` (runs on the Mac, cherryrd):
      reads the local Apple Mail store (NO IMAP password), matches
      `From: aprsemail2@ae5pl.net` + shared token, parses the (UPPERCASED) subject
      `<CALL>: SMS <TOKEN> <NUMBER> <MESSAGE>`, forwards via Textbelt (real SMS).
      Config in `~/radio-relay/relay.env` (chmod 600, git-ignored): RELAY_TOKEN,
      TEXTBELT_KEY, RELAY_MAX_PER_DAY. Dedup via seen.json; daily cap.
- [x] **launchd service** `relay/com.kd9nwa.email-sms-watcher.plist`: RunAtLoad +
      KeepAlive, 3-min polling (`--interval 180`). Install:
      `cp relay/com.kd9nwa.email-sms-watcher.plist ~/Library/LaunchAgents/` (edit
      the paths for your user), then `launchctl load ~/Library/LaunchAgents/...`.
- [x] **Proven end-to-end** on multiple numbers: RF → igate → EMAIL-2 → Mail.app
      → watcher → Textbelt → phone (delivered).
- [ ] Optional: reject known-dead gateways (att.net txt/mms, smsgte) in code with
      a helpful message; add `radio_text` / relay agent tools to the pi extension.

## FT8
- [x] Autonomous CQ + answer-a-CQ QSO engine (`ft8-cq`, `ft8-call`).
- [x] **ADIF auto-logging** on QSO completion (`ft8.log_qso_adif` →
      `~/radio/logs/kd9nwa.adi`): CALL/GRID/BAND/FREQ/MODE/RST both ways/UTC.
- [ ] Optional: upload the log to LoTW / QRZ / Clublog (needs credentials).

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
