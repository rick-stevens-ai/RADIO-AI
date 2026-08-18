# Troubleshooting — the failure modes you WILL hit

Ordered by how often they bite. Each has a symptom → cause → fix.

## 1. "Zero decodes on a busy band" — RF gain reverted to 0.0
- **Symptom:** `radio ft8`/`radio cw` return nothing on a band you know is open;
  `radio scan` shows a flat noise floor.
- **Cause:** RF gain silently resets to 0.0 (deaf receiver) after rig/power events.
- **Fix:** `radio rfgain 1.0`. Re-decode. (This roughly triples decode counts.)
  Make it the first thing you do every session.

## 2. "TX says sent but 0 W out" — DATA MOD source reverted to 02
- **Symptom:** TX command returns `sent: true`, PTT keys, codec shows
  `state: RUNNING`, but `RFPOWER_METER_WATTS` reads 0.0 and nobody hears you.
- **Cause:** IC-7300 DATA MOD source item `1A 05 00 66` reverted from USB(03) to
  02 on the last power-cycle → the rig ignores codec audio in data modes.
- **Fix (CI-V, needs the serial port free — stop the user rigctld first):**
  ```bash
  systemctl --user stop rigctld; sleep 1
  python3 - <<'PY'
  import serial, time
  s = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.5)
  def civ(p):
      fr = bytes([0xFE,0xFE,0x94,0xE0]) + bytes(p) + bytes([0xFD])
      s.write(fr); time.sleep(0.15); return s.read(64)
  print("read:", civ([0x1A,0x05,0x00,0x66]).hex())      # want ...66 03
  r = civ([0x1A,0x05,0x00,0x66,0x03])                    # set to USB(03)
  print("set :", r.hex(), "OK" if r.endswith(b"\xfb\xfd") else "FAIL")
  s.close()
  PY
  systemctl --user start rigctld; sleep 2
  systemctl --user is-active rigctld
  ```
  Reply frames end `fb fd` = OK, `fa fd` = NG. Persists until next power-cycle.

## 3. "Low/no radiated power even with DATA MOD ok" — antenna not tuned
- **Symptom:** Forward power much lower than expected, or ALC pinned, after a QSY.
- **Cause:** You changed frequency/band without tuning the ATU (high SWR).
- **Fix:** `radio tune` (current freq), or always QSY with `radio freq-tune <hz>`.
  Expect ~9 s and `{tuned:true}`. Tune again after any **mode** change too
  (PKTUSB vs CW vs USB present different loads to some tuners).

## 4. "JS8Call TX blocked / DIAL shows 0 / Rig Control Error"
- **Symptom:** JS8Call won't key; its dial reads 0; a "Rig Control Error" modal.
- **Cause:** Something else opened a rigctld/serial connection while JS8Call was
  running (nc, rigctl, pyserial, or a `radio` command that opens a Rig), or you
  restarted rigctld (e.g. via `radio tune`) and severed JS8Call's CAT link.
- **Fix / prevention:**
  - Never poke rigctld/serial while JS8Call is up.
  - Only use `_NO_RIG` `radio` commands alongside JS8Call (js8*, whois*,
    whohearsme, clock, audio-devices, wspr-encode/spots, tune*, text*, relay).
    *Even `tune` restarts rigctld — don't run it mid-JS8-session.*
  - **Order matters:** tune the antenna and set the band BEFORE starting
    JS8Call. If CAT dies mid-session, restart JS8Call (`pkill -f js8call` then
    `js8.ensure_running()`) — a fresh session keys reliably.

## 5. "Killed a TX and /proc still says RUNNING"
- **Symptom:** After `pkill`-ing an `ft8-cq`/`send-cw` process, the codec status
  `cat /proc/asound/card1/pcm0p/sub0/status` still says `state: RUNNING`.
- **Cause:** Stale PCM substream state; the feeding process is gone.
- **Fix / verify:** Check the *real* PTT and power via Hamlib — it's almost
  always already safe:
  ```python
  import Hamlib; Hamlib.rig_set_debug(0)
  r=Hamlib.Rig(2); r.set_conf("rig_pathname","127.0.0.1:4532"); r.open()
  print("PTT", r.get_ptt(), "W", r.get_level_f(Hamlib.RIG_LEVEL_RFPOWER_METER_WATTS))
  r.close()
  ```
  If PTT=0 and W=0, you're safe. Run `radio unkey` to be sure.

## 6. "rigctld not answering / radio status fails"
- **Fix:** `systemctl --user restart rigctld`, then `radio status`.
  If still failing: check `/dev/ttyUSB0` exists (`ls -l /dev/ttyUSB0`), the rig
  is powered on, and no other process holds the port (`fuser /dev/ttyUSB0`).
  Baud is 115200; Hamlib model 3073.

## 7. "FT8 decodes but QSO engine never completes / never logs"
- **Cause A:** You wrapped the run in too-short an external `timeout` and killed
  it before it reached RR73. A 24-cycle run is ~6 min. Give it room or run in
  background; the ADIF write happens when `call_cq`/`run_qso` *returns*.
- **Cause B:** The other station drifted off (common in FT8) — not every started
  QSO completes. Try again; the engine is correct.
- **Cause C:** Clock drift. `radio clock` must show `ft8_ok: true`.

## 8. "send-cw --method rig does nothing"
- **Cause:** IC-7300 "CW Keying via USB" / BK-IN not enabled → CI-V `send_morse`
  is accepted but the rig doesn't key. The tool honestly returns `sent:false`.
- **Fix:** Use `--method audio` (works with the standard codec wiring; needs
  PKTUSB + a fresh antenna tune). Or enable CW-USB/BK-IN in the rig menu.

## 9. "CW decode is garbage / 'implausible speed'"
- **Cause:** Multiple overlapping CW signals in the passband, or the note isn't
  centered. The DSP decoder flags this and gates it out (empty text) rather than
  emit noise.
- **Fix:** `radio cw-hunt` to find a *single clean* signal (`cw_like:true`, high
  `snr_ratio`), QSY there, `radio mode CW`, then `radio cw --method dsp`. Use
  `radio cw-monitor` for weak/fading signals (multi-window voting).

## 10. "SMS didn't arrive over the radio relay"
- See `reference/relay-chain.md`. Fast checks: the message wasn't dropped for
  length (APRS/EMAIL-2 field limit — keep it short), an igate actually copied
  the transmission, the mailbox received it, and the launchd watcher is alive
  (`launchctl list | grep email-sms` on cherryrd). SMSGTE and AT&T
  email-to-SMS gateways are DEAD — the relay uses EMAIL-2 + Textbelt.

## Always leave it safe
```bash
radio unkey && radio tx-disable
radio status | jq '{ptt}'          # want {"ptt": false}
```
