"""
hamradio.messaging — third-party outbound notification / SMS relays.

Background: the classic ham SMS paths are dead — SMSGTE (APRS->SMS) is gone
(domain squatted), and AT&T shut down its email-to-SMS gateways (txt.att.net /
mms.att.net have no MX records as of mid-2025). Email via EMAIL-2 still works and
reaches an inbox, but for a *real text message* to a phone we need a third-party
relay. This module provides pluggable providers reachable from the (internet-
connected) station:

  * textbelt  — real SMS to any number. Needs a paid API key (free tier is
                disabled for US delivery due to abuse). Simple HTTP POST.
  * ntfy      — free push notification to the ntfy.sh app (or self-hosted); no
                account, just a topic name. Great for radio alerts.
  * telegram  — free message via a Telegram bot (needs bot token + chat id).
  * email_sms — email-to-SMS carrier gateway (Verizon vtext.com, T-Mobile
                tmomail.net, etc. — NOT AT&T, which is dead). Routed through the
                existing EMAIL-2 / any SMTP path by the caller.

Secrets are NEVER hardcoded. Keys are read from environment variables or the
file ~/radio/agent/secrets.env (KEY=VALUE lines, git-ignored).

These relays go out over the *internet* directly. To make a message ride the
*radio* first (true RF relay), a JS8 listener decodes a directed command and
then calls one of these — see the js8 relay daemon (planned) / docs/TODO.md.
"""
from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request
from typing import Optional

SECRETS_FILE = os.path.expanduser("~/radio/agent/secrets.env")

# Live carrier email-to-SMS gateways (verified by MX lookup). AT&T is omitted on
# purpose: txt.att.net / mms.att.net have no MX (service discontinued 2025).
CARRIER_SMS_GATEWAYS = {
    "verizon": "vtext.com",
    "verizon_mms": "vzwpix.com",
    "tmobile": "tmomail.net",
    "googlefi": "msg.fi.google.com",
    # "att": DEAD — no MX; do not use.
}


def _load_secret(name: str) -> Optional[str]:
    """Read a secret from the environment, falling back to secrets.env."""
    val = os.environ.get(name)
    if val:
        return val.strip()
    try:
        with open(SECRETS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _post_form(url: str, fields: dict, timeout: float = 20.0) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "hamradio-messaging/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode(errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


# --------------------------------------------------------------------------
# Textbelt — real SMS
# --------------------------------------------------------------------------
def textbelt_quota(key: Optional[str] = None) -> dict:
    key = key or _load_secret("TEXTBELT_KEY") or "textbelt"
    try:
        with urllib.request.urlopen(
                f"https://textbelt.com/quota/{urllib.parse.quote(key)}",
                timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_textbelt(number: str, message: str, *, key: Optional[str] = None,
                  test: bool = False, dry_run: bool = False) -> dict:
    """Send a real SMS via Textbelt.

    key: paid Textbelt key (env TEXTBELT_KEY or secrets.env). Free 'textbelt'
         key is disabled for US delivery. test=True uses 'textbelt_test' which
         validates the round-trip WITHOUT delivering (good for wiring checks).
    """
    num = "".join(ch for ch in number if ch.isdigit() or ch == "+")
    use_key = "textbelt_test" if test else (key or _load_secret("TEXTBELT_KEY"))
    if dry_run:
        return {"provider": "textbelt", "to": num, "message": message,
                "dry_run": True, "would_use_key": "textbelt_test" if test
                else ("<TEXTBELT_KEY>" if use_key else "MISSING")}
    if not use_key:
        return {"provider": "textbelt", "success": False,
                "error": "no TEXTBELT_KEY set (env or ~/radio/agent/secrets.env); "
                         "buy one at textbelt.com, or pass test=True to validate"}
    r = _post_form("https://textbelt.com/text",
                   {"phone": num, "message": message, "key": use_key})
    r.setdefault("provider", "textbelt")
    r["to"] = num
    return r


# --------------------------------------------------------------------------
# ntfy.sh — free push notification (no account)
# --------------------------------------------------------------------------
def send_ntfy(message: str, *, topic: Optional[str] = None,
              title: Optional[str] = None, server: str = "https://ntfy.sh",
              dry_run: bool = False) -> dict:
    """Publish a push notification to an ntfy topic (install the ntfy app and
    subscribe to the same topic). topic from arg / NTFY_TOPIC secret."""
    topic = topic or _load_secret("NTFY_TOPIC")
    if dry_run:
        return {"provider": "ntfy", "topic": topic, "message": message,
                "dry_run": True}
    if not topic:
        return {"provider": "ntfy", "success": False,
                "error": "no topic (arg or NTFY_TOPIC secret)"}
    req = urllib.request.Request(f"{server}/{urllib.parse.quote(topic)}",
                                 data=message.encode(),
                                 headers={"User-Agent": "hamradio/1.0"})
    if title:
        req.add_header("Title", title)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"provider": "ntfy", "success": resp.status == 200,
                    "topic": topic, "status": resp.status}
    except Exception as e:
        return {"provider": "ntfy", "success": False, "error": str(e)}


# --------------------------------------------------------------------------
# Telegram bot
# --------------------------------------------------------------------------
def send_telegram(message: str, *, token: Optional[str] = None,
                  chat_id: Optional[str] = None, dry_run: bool = False) -> dict:
    """Send a message via a Telegram bot (token + chat_id from args or
    TELEGRAM_TOKEN / TELEGRAM_CHAT_ID secrets)."""
    token = token or _load_secret("TELEGRAM_TOKEN")
    chat_id = chat_id or _load_secret("TELEGRAM_CHAT_ID")
    if dry_run:
        return {"provider": "telegram", "chat_id": chat_id, "message": message,
                "dry_run": True, "have_token": bool(token)}
    if not (token and chat_id):
        return {"provider": "telegram", "success": False,
                "error": "need TELEGRAM_TOKEN + TELEGRAM_CHAT_ID"}
    r = _post_form(f"https://api.telegram.org/bot{token}/sendMessage",
                   {"chat_id": chat_id, "text": message})
    r.setdefault("provider", "telegram")
    return r


# --------------------------------------------------------------------------
# Unified entry point
# --------------------------------------------------------------------------
def notify(message: str, *, provider: str = "ntfy", to: Optional[str] = None,
           dry_run: bool = False, **kw) -> dict:
    """Route a notification through the chosen provider.

    provider: textbelt | ntfy | telegram
    to: phone number (textbelt) — ntfy/telegram use their configured target.
    """
    provider = provider.lower()
    if provider == "textbelt":
        if not to:
            return {"success": False, "error": "textbelt needs a phone number (to=)"}
        return send_textbelt(to, message, dry_run=dry_run,
                             test=kw.get("test", False), key=kw.get("key"))
    if provider == "ntfy":
        return send_ntfy(message, topic=to or kw.get("topic"),
                         title=kw.get("title"), dry_run=dry_run)
    if provider == "telegram":
        return send_telegram(message, dry_run=dry_run,
                             token=kw.get("token"), chat_id=to or kw.get("chat_id"))
    return {"success": False, "error": f"unknown provider {provider!r}"}
