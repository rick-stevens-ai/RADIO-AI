# The JS8 → email → SMS relay chain

This lets the station text a phone **over the radio**, routing around two dead
gateways: **SMSGTE** (domain squatted) and **AT&T email-to-SMS** (`txt.att.net` /
`mms.att.net` discontinued June 2025). The working path is:

```
radio js8-text <number> "<msg>"   (nuc7, GATED)
   │  builds:  SMS <RELAY_TOKEN> <number> <message>
   ▼  transmits as an EMAIL-2 message to RELAY_WATCH_ADDR
JS8Call RF  ─►  APRS igate  ─►  APRS-IS  ─►  EMAIL-2 (aprsemail2@ae5pl.net)
   ▼
operator mailbox (rick.stevens@mac.com), synced by Mail.app on cherryrd (the Mac)
   ▼  launchd watcher polls the LOCAL mail store every 3 min (no IMAP password)
email_sms_watcher.py  ─►  Textbelt API  ─►  real SMS to the phone
```

## Sending (nuc7)

```bash
radio tx-enable "SMS relay"
radio js8-text 6305551234 "on my way" --allow-tx     # JS8Call must be running
radio unkey; radio tx-disable
```
- Builds `SMS <token> <number> <message>` and sends it as an EMAIL-2 message.
- `--dry-run` shows the exact on-air string without keying.
- Config comes from `~/radio/agent/secrets.env` (git-ignored):
  `RELAY_TOKEN=...`, `RELAY_WATCH_ADDR=rick.stevens@mac.com`, `TEXTBELT_KEY=...`.
- **EMAIL-2 UPPERCASES the subject in transit**, so the delivered SMS is
  uppercase. The watcher matches the token case-insensitively.
- Keep messages short — the APRS/EMAIL-2 message field is length-limited; long
  messages can be dropped silently rather than truncated.

## The watcher (cherryrd, the Mac)

`relay/email_sms_watcher.py`:
- Reads the **local Apple Mail store** (`.emlx` files) that Mail.app already
  syncs for rick.stevens@mac.com — **no IMAP password needed**.
  Live inbox: `~/Library/Mail/V10/49EB0A06-.../INBOX.mbox`.
- Only acts on mail whose `From` is the EMAIL-2 gateway `aprsemail2@ae5pl.net`.
- Parses the (uppercased) subject `<CALL>: SMS <TOKEN> <NUMBER> <MESSAGE>`
  (regex `SUBJECT_RE`), verifies the shared `RELAY_TOKEN`, forwards via Textbelt.
- Safety: shared-token check, dedup via `seen.json`, daily cap
  `RELAY_MAX_PER_DAY` (default 50). Logs each action to `relay.log`.
- Config `~/radio-relay/relay.env` (chmod 600, git-ignored): `RELAY_TOKEN`,
  `TEXTBELT_KEY`, `RELAY_MAX_PER_DAY`.
- Uses `find -mmin` to scan only recent `.emlx` files (fast).
- `.emlx` format = a byte-count first line, then RFC822, then a plist trailer:
  `email.message_from_bytes(raw[raw.find(b"\n")+1:])`.

Run modes:
```bash
python3 email_sms_watcher.py --once --window 1200        # one scan (window in s)
python3 email_sms_watcher.py --interval 180 --window 1800  # daemon, 3-min poll
python3 email_sms_watcher.py --once --dry-run            # parse, don't send
```

## Install the always-on watcher (launchd)

```bash
cp relay/com.kd9nwa.email-sms-watcher.plist ~/Library/LaunchAgents/
# EDIT the plist paths for your user + python (it hardcodes /Users/stevens and
# /usr/local/bin/python3). Then:
launchctl load ~/Library/LaunchAgents/com.kd9nwa.email-sms-watcher.plist
launchctl list | grep email-sms          # confirm loaded (PID, exit 0)
```
The plist sets `RunAtLoad` + `KeepAlive` (restart on death, start on boot) with
`--interval 180 --window 1800`, logging to `~/radio-relay/logs/`.

## Verifying end-to-end

1. Send: `radio js8-text <num> "test123" --allow-tx` (JS8Call running).
2. Confirm RF (codec RUNNING; ~90 s TX).
3. Wait 1–5 min for the igate → APRS-IS → EMAIL-2 → Mail.app hop, then check the
   mailbox on cherryrd for a `From: aprsemail2@ae5pl.net` message whose subject
   contains your number + `TEST123`.
4. Force a watcher scan: `python3 ~/radio-relay/email_sms_watcher.py --once
   --window 1200` → look for `{action:"sent", result:{success:true,...}}`.
5. The phone gets `[ham KD9NWA] TEST123`.

## Other messaging options

- **Direct SMS (internet, no RF):** `radio text <num> "<msg>"` — Textbelt again,
  but bypasses the radio entirely. `radio text-quota` for credit.
- **Email over radio (no SMS):** `radio js8-email <addr> "<msg>" --allow-tx` —
  same JS8→APRS-IS→EMAIL-2 path, delivered as email (confirmed working
  end-to-end).
- **Off-air inbound gateway:** `radio relay` — listen for directed
  `CALL: SMS/TEXT/NOTIFY/PING <...>` commands from OTHER stations and forward
  them (allow-list, dedup, daily cap). Needs a 2nd station to test (half-duplex).

## Known-dead — do not use
- `smsgte.org` (Russian casino now), `smsgte.com` (no DNS).
- AT&T `txt.att.net` / `mms.att.net` (no MX; discontinued 2025).
- Verizon `vtext.com`/`vzwpix.com` and T-Mobile `tmomail.net` still have MX, but
  the Textbelt path is more reliable and carrier-agnostic.
