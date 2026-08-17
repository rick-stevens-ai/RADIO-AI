"""
hamradio.relay — JS8 (over-the-air) -> SMS/notification gateway.

This turns the station into a genuine ham-radio message gateway, replacing the
defunct SMSGTE/APRS-SMS service. It listens to JS8Call decodes off the air, and
when it sees a directed command addressed to us, it forwards the message to a
phone (via Textbelt) or a push/notification provider (ntfy/telegram).

On-air command syntax (a station sends a *directed* JS8 message to our call):

    KD9NWA: SMS <number> <message text>
    KD9NWA: TEXT <number> <message text>      (alias for SMS)
    KD9NWA: NOTIFY <message text>             (-> default ntfy/telegram)
    KD9NWA: PING                              (-> we reply / notify we're alive)

JS8Call decodes that as an RX.DIRECTED with FROM=<sender>, TO=KD9NWA, and the
free text after the directed prefix. We parse the verb + args and forward.

Safety / anti-abuse:
  * Only ACT on messages *directed to our call* (TO == MY_CALL), never on
    broadcast/heartbeat traffic.
  * Optional allow-list of sender callsigns (RELAY_ALLOW in secrets.env, comma
    separated). If unset, any licensed sender is accepted (log everything).
  * De-duplicate on (from, text) so a repeated/relayed frame doesn't double-send.
  * Every forwarded message is prefixed with the sender's callsign so the
    recipient knows it came over ham radio: "[via ham KD9NWA de <SENDER>] ...".
  * A daily send cap (RELAY_MAX_PER_DAY, default 50) guards the SMS credits.

This module does RX + internet forwarding only. It does NOT transmit on the
radio (except an optional JS8 ACK, which is gated like all TX). Delivery uses
hamradio.messaging (Textbelt/ntfy/telegram).
"""
from __future__ import annotations
import os
import re
import time
import json
from typing import Optional

from . import js8
from . import messaging

MY_CALL = os.environ.get("MY_CALL", "KD9NWA").upper()
STATE_DIR = os.path.expanduser("~/radio/relay")
LOG_FILE = os.path.join(STATE_DIR, "relay.log")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")


def _secret(name: str, default: str = "") -> str:
    return messaging._load_secret(name) or default


def _allow_list() -> Optional[set]:
    raw = _secret("RELAY_ALLOW")
    if not raw:
        return None
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


def _log(entry: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_seen() -> dict:
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_seen(seen: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    # keep only the last ~500 keys
    if len(seen) > 500:
        for k in list(seen)[:-500]:
            seen.pop(k, None)
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


def _today_count() -> int:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    n = 0
    try:
        with open(LOG_FILE) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("action") == "forwarded" and e.get("ts", "").startswith(today):
                    n += 1
    except OSError:
        pass
    return n


# --- command parsing -------------------------------------------------------
# strip a leading directed prefix like "KD9NWA: " or "KD9NWA:" JS8Call may or
# may not include the colon/our call in the value; handle both.
_DIRECTED_PREFIX = re.compile(r"^\s*" + re.escape(MY_CALL) + r"\s*[:>]?\s*", re.I)
_VERB = re.compile(r"^(SMS|TEXT|NOTIFY|PING)\b\s*(.*)$", re.I | re.S)
_NUM = re.compile(r"^\+?\d[\d\-\s().]{6,}\d")


def parse_command(rec: dict) -> Optional[dict]:
    """Given a decoded RX.DIRECTED record, return a command dict or None.

    Accepts only messages addressed to our call. Returns:
      {verb, number?, message, sender}
    """
    to = (rec.get("to") or "").upper()
    frm = (rec.get("from") or "").upper()
    text = (rec.get("text") or "").strip()
    if not text:
        return None
    # Must be directed to us (either TO==MY_CALL, or text starts with our call).
    directed = to == MY_CALL or _DIRECTED_PREFIX.match(text)
    if not directed:
        return None
    body = _DIRECTED_PREFIX.sub("", text).strip()
    m = _VERB.match(body)
    if not m:
        return None
    verb = m.group(1).upper()
    rest = m.group(2).strip()
    if verb in ("SMS", "TEXT"):
        nm = _NUM.match(rest)
        if not nm:
            return None
        number = "".join(ch for ch in nm.group(0) if ch.isdigit() or ch == "+")
        message = rest[nm.end():].strip()
        return {"verb": "SMS", "number": number, "message": message, "sender": frm}
    if verb == "NOTIFY":
        return {"verb": "NOTIFY", "message": rest, "sender": frm}
    if verb == "PING":
        return {"verb": "PING", "message": "", "sender": frm}
    return None


def _forward(cmd: dict, *, sms_provider: str = "textbelt",
             notify_provider: str = "ntfy", dry_run: bool = False) -> dict:
    sender = cmd["sender"] or "?"
    tag = f"[ham {MY_CALL} de {sender}] "
    if cmd["verb"] == "SMS":
        body = tag + cmd["message"]
        return messaging.send_textbelt(cmd["number"], body, dry_run=dry_run) \
            if sms_provider == "textbelt" else \
            messaging.notify(body, provider=sms_provider, to=cmd["number"],
                             dry_run=dry_run)
    if cmd["verb"] == "NOTIFY":
        return messaging.notify(tag + cmd["message"], provider=notify_provider,
                                title=f"JS8 de {sender}", dry_run=dry_run)
    if cmd["verb"] == "PING":
        return messaging.notify(f"{tag}PING (station alive)",
                                provider=notify_provider,
                                title="JS8 ping", dry_run=dry_run)
    return {"success": False, "error": "unknown verb"}


def process(records: list, *, sms_provider: str = "textbelt",
            notify_provider: str = "ntfy", dry_run: bool = False) -> dict:
    """Scan decoded records, act on directed SMS/NOTIFY/PING commands to us."""
    allow = _allow_list()
    cap = int(_secret("RELAY_MAX_PER_DAY", "50"))
    seen = _load_seen()
    acted = []
    for rec in records:
        cmd = parse_command(rec)
        if not cmd:
            continue
        key = f"{cmd['sender']}|{rec.get('text','')}"
        if seen.get(key):
            continue
        seen[key] = int(time.time())
        # allow-list check
        if allow is not None and cmd["sender"] not in allow:
            _log({"action": "rejected", "reason": "not in allow-list", **cmd})
            acted.append({**cmd, "result": {"success": False,
                                            "error": "sender not allowed"}})
            continue
        # daily cap (only counts real SMS forwards)
        if cmd["verb"] == "SMS" and not dry_run and _today_count() >= cap:
            _log({"action": "rejected", "reason": "daily cap", **cmd})
            acted.append({**cmd, "result": {"success": False,
                                            "error": "daily cap reached"}})
            continue
        result = _forward(cmd, sms_provider=sms_provider,
                          notify_provider=notify_provider, dry_run=dry_run)
        _log({"action": "forwarded" if result.get("success") else "failed",
              "dry_run": dry_run, "result": result, **cmd})
        acted.append({**cmd, "result": result})
    _save_seen(seen)
    return {"gateway": MY_CALL, "n_records": len(records),
            "n_acted": len(acted), "acted": acted}


def listen_once(seconds: float = 60.0, *, sms_provider: str = "textbelt",
                notify_provider: str = "ntfy", dry_run: bool = False) -> dict:
    """Listen for `seconds`, decode JS8, and forward any directed commands."""
    heard = js8.listen(seconds=seconds)
    records = heard.get("messages", [])
    result = process(records, sms_provider=sms_provider,
                     notify_provider=notify_provider, dry_run=dry_run)
    result["dial_hz"] = (heard.get("dial") or {}).get("dial_hz") \
        if isinstance(heard.get("dial"), dict) else heard.get("dial")
    result["listened_s"] = seconds
    return result


def run(cycles: int = 0, seconds: float = 60.0, **kw) -> dict:
    """Run the gateway for `cycles` listen windows (0 = forever). Returns a
    summary when it stops (only meaningful for finite cycles)."""
    total = {"gateway": MY_CALL, "windows": 0, "forwarded": 0}
    i = 0
    while cycles == 0 or i < cycles:
        r = listen_once(seconds=seconds, **kw)
        total["windows"] += 1
        total["forwarded"] += sum(
            1 for a in r["acted"] if a["result"].get("success"))
        i += 1
    return total
