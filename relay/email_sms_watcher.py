#!/usr/bin/env python3
"""
email_sms_watcher — watch the local Apple Mail store for ham-radio EMAIL-2
messages and relay them to SMS via Textbelt.

Full path this completes:
  JS8 over RF -> APRS igate -> APRS-IS -> EMAIL-2 gateway (aprsemail2@ae5pl.net)
  -> rick.stevens@mac.com  (Apple Mail on CherryRd keeps this synced locally)
  -> THIS watcher reads new .emlx files -> parses a SMS command -> Textbelt SMS

To send a text over the radio you transmit (from the station):
    radio js8-email rick.stevens@mac.com "SMS <TOKEN> <number> <message>"
which arrives as Subject:  <CALL>: SMS <TOKEN> <NUMBER> <MESSAGE>   (uppercased)

Security:
  * Only acts on mail whose From is the EMAIL-2 gateway (aprsemail2@ae5pl.net).
  * Requires a shared secret TOKEN in the command (so a random email can't fire
    a text). Token + Textbelt key live in ~/radio-relay/relay.env (chmod 600).
  * De-duplicates on Message-ID (state in ~/radio-relay/seen.json).
  * Daily cap to protect SMS credits.

No IMAP password needed — reads the local Mail store Apple Mail already syncs.
"""
from __future__ import annotations
import email
import glob
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
RELAY_DIR = HOME / "radio-relay"
ENV_FILE = RELAY_DIR / "relay.env"
SEEN_FILE = RELAY_DIR / "seen.json"
LOG_FILE = RELAY_DIR / "relay.log"

# The Apple Mail account INBOX that receives rick.stevens@mac.com (V10 store).
# A glob so it survives Mail re-indexing to a new container UUID.
INBOX_BASES = [
    str(HOME / "Library/Mail/V10/49EB0A06-4EFA-42EA-959C-EFCB3E634E09/INBOX.mbox"),
]

GATEWAY_FROM = "aprsemail2@ae5pl.net"     # authentic EMAIL-2 sender
# Subject looks like:  KD9NWA: SMS <TOKEN> <NUMBER> <MESSAGE>
SUBJECT_RE = re.compile(
    r"^\s*(?P<call>[A-Z0-9/]+)\s*:\s*SMS\s+(?P<token>\S+)\s+"
    r"(?P<number>\+?\d[\d\-\s().]{5,}\d)\s+(?P<message>.*)$",
    re.I | re.S)


def load_env() -> dict:
    env = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict) -> None:
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    if len(seen) > 1000:
        for k in list(seen)[:-1000]:
            seen.pop(k, None)
    SEEN_FILE.write_text(json.dumps(seen))


def log(entry: dict) -> None:
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def today_sms_count() -> int:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    n = 0
    try:
        for line in LOG_FILE.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") == "sent" and e.get("ts", "").startswith(today):
                n += 1
    except OSError:
        pass
    return n


def parse_emlx(path: str):
    """Return (msgid, from_addr, subject) for an .emlx file, or None."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    nl = raw.find(b"\n")
    try:
        msg = email.message_from_bytes(raw[nl + 1:])
    except Exception:
        return None
    frm = str(msg.get("From") or "")
    subj = str(msg.get("Subject") or "").replace("\n", " ").strip()
    msgid = str(msg.get("Message-ID") or msg.get("Message-Id") or (subj + "|" + frm))
    return msgid, frm, subj


def send_textbelt(number: str, message: str, key: str, dry_run: bool = False) -> dict:
    num = "".join(ch for ch in number if ch.isdigit() or ch == "+")
    if dry_run:
        return {"success": True, "dry_run": True, "to": num, "message": message}
    data = urllib.parse.urlencode(
        {"phone": num, "message": message, "key": key}).encode()
    req = urllib.request.Request("https://textbelt.com/text", data=data,
                                 headers={"User-Agent": "email-sms-watcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"success": False, "error": str(e)}


def scan_once(dry_run: bool = False, mtime_window_s: int = 3600) -> dict:
    env = load_env()
    token = env.get("RELAY_TOKEN")
    key = env.get("TEXTBELT_KEY")
    cap = int(env.get("RELAY_MAX_PER_DAY", "50"))
    if not token:
        return {"error": "no RELAY_TOKEN in relay.env"}
    seen = load_seen()
    now = time.time()
    acted = []
    # Only recently-modified .emlx (fast): use `find -mtime` instead of globbing
    # the whole store each cycle.
    import subprocess
    files = []
    mins = max(1, int(mtime_window_s / 60))
    for base in INBOX_BASES:
        try:
            out = subprocess.run(
                ["find", base, "-name", "*.emlx", "-mmin", f"-{mins}"],
                capture_output=True, text=True, timeout=30)
            files.extend(l for l in out.stdout.splitlines() if l)
        except (subprocess.TimeoutExpired, OSError):
            pass
    for path in files:
        parsed = parse_emlx(path)
        if not parsed:
            continue
        msgid, frm, subj = parsed
        if msgid in seen:
            continue
        m = SUBJECT_RE.match(subj)
        if not m:
            continue  # not an SMS command
        seen[msgid] = int(now)
        # authenticity: must be from the EMAIL-2 gateway
        if GATEWAY_FROM.lower() not in frm.lower():
            log({"action": "rejected", "reason": "bad sender", "from": frm, "subj": subj})
            continue
        # token check (EMAIL-2 UPPERCASES the whole subject, so compare fold-case)
        if m.group("token").strip().upper() != token.strip().upper():
            log({"action": "rejected", "reason": "bad token", "subj": subj})
            continue
        number = "".join(c for c in m.group("number") if c.isdigit() or c == "+")
        message = m.group("message").strip()
        sender_call = m.group("call").upper()
        tagged = f"[ham {sender_call}] {message}"
        if not dry_run and today_sms_count() >= cap:
            log({"action": "rejected", "reason": "daily cap", "subj": subj})
            acted.append({"number": number, "result": {"success": False, "error": "cap"}})
            continue
        if not key and not dry_run:
            log({"action": "failed", "reason": "no TEXTBELT_KEY", "subj": subj})
            acted.append({"number": number, "result": {"success": False, "error": "no key"}})
            continue
        result = send_textbelt(number, tagged, key, dry_run=dry_run)
        log({"action": "sent" if result.get("success") else "failed",
             "call": sender_call, "number": number, "message": message,
             "dry_run": dry_run, "result": result})
        acted.append({"call": sender_call, "number": number,
                      "message": message, "result": result})
    save_seen(seen)
    return {"scanned": len(files), "acted": len(acted), "actions": acted}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--interval", type=int, default=30, help="poll seconds (daemon)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window", type=int, default=3600,
                    help="only consider mail newer than N seconds")
    a = ap.parse_args()
    if a.once:
        print(json.dumps(scan_once(dry_run=a.dry_run, mtime_window_s=a.window), indent=2))
        return
    while True:
        r = scan_once(dry_run=a.dry_run, mtime_window_s=a.window)
        if r.get("acted"):
            print(json.dumps(r), flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
