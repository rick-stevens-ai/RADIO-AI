"""
hamradio.js8 — drive JS8Call (keyboard-to-keyboard weak-signal messaging).

JS8Call is built on the FT8 waveform but is a *conversational* mode: free-text
messages, directed calls (@CALL), heartbeats, relays, and store-and-forward.
Rather than re-implement its message assembly, we drive the real JS8Call app
through its documented TCP API (JSON lines on 127.0.0.1:2442) — the robust path,
mirroring how we use WSJT-X for FT8.

Setup (already done on this station):
  * JS8Call.ini: MyCall=KD9NWA, MyGrid=EN51TP, TCPEnabled=true, port 2442,
    Rig=Hamlib NET rigctl -> 127.0.0.1:4532 (shares our rigctld), USB codec.
  * Launch headless:  xvfb-run -a js8call   (see ensure_running()).

This module:
  * ensure_running()  — start JS8Call under Xvfb if not already up.
  * Js8Client         — connect to the API; request()/read messages.
  * station info, get/set dial freq + submode (speed).
  * listen()          — collect decoded RX for N seconds (RX.DIRECTED / RX.SPOT
                        / RX.ACTIVITY), returning structured messages.
  * send()            — queue a free-text / directed message for transmission
                        (GATED: JS8Call keys the rig; caller must arm TX).

Submodes / speeds: A=Normal(15s), B=Fast(10s), C=Turbo(6s), E=Slow(30s).
JS8 dial freqs (USB): 80m 3.578, 40m 7.078, 30m 10.130, 20m 14.078,
                      17m 18.104, 15m 21.078, 10m 28.078 MHz.
"""
from __future__ import annotations
import json
import socket
import subprocess
import time
from typing import Optional

API_HOST = "127.0.0.1"
API_PORT = 2442

JS8_DIAL = {
    "80m": 3578000, "40m": 7078000, "30m": 10130000, "20m": 14078000,
    "17m": 18104000, "15m": 21078000, "10m": 28078000,
}
SPEED_NAME = {0: "Slow", 1: "Normal", 2: "Fast", 4: "Turbo"}


def is_running() -> bool:
    try:
        subprocess.check_output(["pgrep", "-f", "js8call"])
        return True
    except subprocess.CalledProcessError:
        return False


def api_up(timeout: float = 1.5) -> bool:
    try:
        s = socket.create_connection((API_HOST, API_PORT), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


# Launcher: Xvfb (:99) + a lightweight WM (openbox, so menus/dialogs get focus)
# + a dbus session with the AT-SPI accessibility bus, then JS8Call. Accessibility
# is REQUIRED so _trigger_tx() can actuate the real 'Send' button; the WM is
# needed for reliable dialog handling under Xvfb.
JS8_DISPLAY = ":99"
_LAUNCHER = '''#!/bin/bash
export DISPLAY=%(disp)s
export QT_ACCESSIBILITY=1
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
export NO_AT_BRIDGE=0
pgrep -f "Xvfb %(disp)s" >/dev/null || (Xvfb %(disp)s -screen 0 1200x800x16 >/tmp/xvfb99.log 2>&1 &)
sleep 2
pgrep -x openbox >/dev/null || (openbox >/tmp/openbox.log 2>&1 &)
sleep 1
dbus-run-session -- bash -c "
  /usr/libexec/at-spi-bus-launcher --launch-immediately &
  sleep 2
  js8call 2>&1 | tee /tmp/js8call.log
"
''' % {"disp": JS8_DISPLAY}


def ensure_running(wait_s: float = 40.0) -> dict:
    """Start JS8Call headless if it isn't already running, with a WM + the
    AT-SPI accessibility bus enabled (needed to actually trigger transmit).

    IMPORTANT: probe with pgrep, NOT by opening a TCP socket. JS8Call's API
    wedges if a client connects and disconnects immediately before another
    connects, so we must avoid any throw-away socket before the real one."""
    if is_running():
        return {"started": False, "api": True, "note": "already running"}
    import os, tempfile
    path = os.path.join(tempfile.gettempdir(), "start_js8.sh")
    with open(path, "w") as f:
        f.write(_LAUNCHER)
    os.chmod(path, 0o755)
    subprocess.Popen(["tmux", "new-session", "-d", "-s", "js8", path])
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if api_up():
            time.sleep(3)  # let it finish wiring the rig; also lets the API
                           # settle after our probe before the caller connects
            return {"started": True, "api": True,
                    "waited_s": round(time.time() - t0, 1)}
        time.sleep(1)
    return {"started": True, "api": False, "error": "API did not come up"}


class Js8Client:
    """Minimal JSON-lines client for the JS8Call TCP API."""

    def __init__(self, host: str = API_HOST, port: int = API_PORT,
                 timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def send_raw(self, typ: str, value: str = "", params: Optional[dict] = None):
        msg = {"type": typ, "value": value, "params": params or {}}
        self.sock.sendall((json.dumps(msg) + "\n").encode())

    def _read_lines(self, seconds: float) -> list[dict]:
        """Read all JSON messages arriving within `seconds`."""
        out = []
        end = time.time() + seconds
        self.sock.settimeout(0.5)
        while time.time() < end:
            try:
                d = self.sock.recv(8192)
                if not d:
                    break
                self._buf += d
            except socket.timeout:
                continue
            except (ConnectionResetError, OSError):
                break
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def request(self, typ: str, want: str, value: str = "",
                params: Optional[dict] = None, timeout: float = 6.0):
        """Send a request and return the first reply whose type == `want`.
        Drains interleaved RX events continuously until the matching reply
        arrives or a hard deadline passes (robust when JS8Call is mid-decode
        and flooding the socket with activity events)."""
        try:
            self.send_raw(typ, value, params)
        except OSError:
            return None
        end = time.time() + timeout
        self.sock.settimeout(0.5)
        while time.time() < end:
            # parse anything already buffered first
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("type") == want:
                    return m
            # then read more
            try:
                d = self.sock.recv(8192)
                if not d:
                    break
                self._buf += d
            except socket.timeout:
                continue
            except (ConnectionResetError, OSError):
                break
        return None

    # --- convenience wrappers ------------------------------------------------
    def station(self) -> dict:
        try:
            c = self.request("STATION.GET_CALLSIGN", "STATION.CALLSIGN")
            g = self.request("STATION.GET_GRID", "STATION.GRID")
        except OSError:
            c = g = None
        return {"callsign": c.get("value") if c else None,
                "grid": g.get("value") if g else None}

    def get_freq(self) -> dict:
        m = self.request("RIG.GET_FREQ", "RIG.FREQ")
        p = (m or {}).get("params", {})
        return {"dial_hz": p.get("DIAL"), "offset_hz": p.get("OFFSET"),
                "freq_hz": p.get("FREQ")}

    def set_dial(self, hz: int):
        self.send_raw("RIG.SET_FREQ", "", {"DIAL": int(hz), "OFFSET": 1500})

    def get_speed(self) -> dict:
        m = self.request("MODE.GET_SPEED", "MODE.SPEED")
        sp = (m or {}).get("params", {}).get("SPEED")
        return {"speed": sp, "name": SPEED_NAME.get(sp, "?")}

    def set_speed(self, speed: int):
        self.send_raw("MODE.SET_SPEED", "", {"SPEED": int(speed)})


def _parse_rx(m: dict) -> Optional[dict]:
    """Normalize an inbound API message into a decoded-activity record."""
    t = m.get("type", "")
    if t not in ("RX.DIRECTED", "RX.SPOT", "RX.ACTIVITY", "RX.CALL_ACTIVITY"):
        return None
    p = m.get("params", {})
    rec = {
        "type": t,
        "text": (m.get("value") or "").strip(),
        "from": p.get("FROM"),
        "to": p.get("TO"),
        "snr": p.get("SNR"),
        "freq_hz": p.get("DIAL") or p.get("FREQ"),
        "offset_hz": p.get("OFFSET"),
        "grid": p.get("GRID"),
        "utc": p.get("UTC"),
        "speed": p.get("SPEED"),
    }
    return rec


def listen(seconds: float = 60.0, station_info: bool = True,
           band: Optional[str] = None) -> dict:
    """Collect decoded JS8 activity for `seconds`. Returns structured messages,
    de-duplicated, with the directed (addressed) traffic separated out.

    Uses a dedicated streaming socket so a burst of RX events can't collide with
    request/reply bookkeeping. Station/freq info is fetched on a separate short-
    lived socket first (best-effort)."""
    ensure_running()
    st, freq, speed = {}, {}, {}
    raw = []
    try:
        # One socket for everything: JS8Call's API serializes best with a single
        # client, so we fetch station/freq/speed on the SAME connection we then
        # stream decodes from (a separate short-lived socket can wedge the API).
        with Js8Client(timeout=6.0) as c:
            if band and band in JS8_DIAL:
                c.set_dial(JS8_DIAL[band])
                time.sleep(2.0)  # let JS8Call retune before we read
            if station_info:
                st = c.station()
                freq = c.get_freq()
                speed = c.get_speed()
            try:
                c.send_raw("RX.GET_CALL_ACTIVITY")
            except OSError:
                pass
            raw = c._read_lines(seconds)
    except (OSError, ConnectionResetError):
        pass
    seen = set()
    msgs, directed = [], []
    for m in raw:
        rec = _parse_rx(m)
        if not rec or not (rec["text"] or rec["from"]):
            continue
        key = (rec["type"], rec["from"], rec["text"], rec["utc"])
        if key in seen:
            continue
        seen.add(key)
        msgs.append(rec)
        if rec["type"] == "RX.DIRECTED":
            directed.append(rec)
    return {"engine": "js8call", "station": st, "dial": freq, "speed": speed,
            "seconds": seconds, "n": len(msgs),
            "directed": directed, "messages": msgs}


def _js8_dbus_env() -> Optional[dict]:
    """Return the environment (incl. DBUS_SESSION_BUS_ADDRESS + DISPLAY) of the
    running js8call process, so we can talk to its AT-SPI accessibility bus."""
    import os
    try:
        pid = subprocess.check_output(
            ["pgrep", "-f", "js8call"]).decode().split()[0]
    except (subprocess.CalledProcessError, IndexError):
        return None
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if b"=" in kv:
                    k, v = kv.split(b"=", 1)
                    env[k.decode(errors="replace")] = v.decode(errors="replace")
    except OSError:
        return None
    out = dict(os.environ)
    for k in ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "XAUTHORITY"):
        if env.get(k):
            out[k] = env[k]
    out["QT_ACCESSIBILITY"] = "1"
    return out


_ATSPI_TRIGGER = r'''
import sys
try:
    import pyatspi
except Exception as e:
    print("NO_PYATSPI", e); sys.exit(2)
d = pyatspi.Registry.getDesktop(0)
apps = [a for a in d if (a.name or "") == "JS8Call"]
if not apps:
    print("NO_JS8CALL_IN_A11Y"); sys.exit(3)
app = apps[0]
target = [None]
def walk(n):
    try:
        if target[0] is not None: return
        if (n.name or "").lower().startswith("send ("):
            target[0] = n; return
        for i in range(n.childCount): walk(n.getChildAtIndex(i))
    except Exception:
        pass
walk(app)
if target[0] is None:
    print("NO_SEND_BUTTON"); sys.exit(4)
target[0].queryAction().doAction(0)   # Toggle -> start TX on next frame
print("TX_TRIGGERED", target[0].name)
'''


_ATSPI_ENSURE = r'''
import sys
try:
    import pyatspi
except Exception as e:
    print("NO_PYATSPI", e); sys.exit(2)
d = pyatspi.Registry.getDesktop(0)
apps = [a for a in d if (a.name or "") == "JS8Call"]
if not apps:
    print("NO_JS8CALL_IN_A11Y"); sys.exit(3)
app = apps[0]
# Menu items we may need to check (Toggle sets desired state only if currently wrong).
# We locate menu items by name; their STATE_CHECKED tells us current state.
want = {
    "enable receiver (rx)": True,
    "enable transmitter (tx)": True,
    "enable autoreply (auto)": True,
    "enable reporting (spot)": True,
}
found = {}
def walk(n):
    try:
        nm = (n.name or "").lower()
        if nm in want:
            st = n.getState()
            found[nm] = (n, st.contains(pyatspi.STATE_CHECKED))
        for i in range(n.childCount): walk(n.getChildAtIndex(i))
    except Exception:
        pass
walk(app)
changed = []
for nm, desired in want.items():
    if nm in found:
        node, checked = found[nm]
        if checked != desired:
            try:
                node.queryAction().doAction(0)  # Press -> toggles menu item
                changed.append(nm)
            except Exception as e:
                print("ERR toggling", nm, e)
print("ENSURED", "changed=" + ",".join(changed) if changed else "already_ok",
      "present=" + ",".join(sorted(found)))
'''


def ensure_tx_ready() -> dict:
    """Make sure JS8Call's session toggles allow transmit: Enable Receiver (RX),
    Enable Transmitter (TX), Enable Autoreply, Enable Reporting (SPOT). These are
    runtime UI states (not reliably restored from the .ini), and TX will silently
    do nothing if RX/monitoring is off. Uses AT-SPI menu items."""
    env = _js8_dbus_env()
    if not env:
        return {"ok": False, "note": "js8call process/env not found"}
    try:
        r = subprocess.run(["python3", "-c", _ATSPI_ENSURE], env=env,
                           capture_output=True, text=True, timeout=25)
        return {"ok": r.returncode == 0,
                "detail": (r.stdout + r.stderr).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": "ensure_tx_ready timed out"}


def _trigger_tx() -> dict:
    """Click JS8Call's 'Send' button via the accessibility (AT-SPI) bus.

    JS8Call's TCP API can load message text but, run headless, its
    TX.SEND_MESSAGE path does not reliably fire the transmit toggle (Qt
    setChecked() doesn't drive the on-air keying). The robust trigger is to
    actuate the real 'Send (<duration>)' toggle button, which we reach through
    AT-SPI. Requires JS8Call started with QT_ACCESSIBILITY=1 under a dbus
    session (see ensure_running / systemd unit).
    """
    env = _js8_dbus_env()
    if not env:
        return {"triggered": False, "note": "js8call process/env not found"}
    try:
        r = subprocess.run(["python3", "-c", _ATSPI_TRIGGER], env=env,
                           capture_output=True, text=True, timeout=25)
        out = (r.stdout + r.stderr).strip()
        return {"triggered": r.returncode == 0, "detail": out}
    except subprocess.TimeoutExpired:
        return {"triggered": False, "note": "a11y trigger timed out"}


def send(text: str, *, allow_tx: bool = False, dry_run: bool = False) -> dict:
    """Queue a JS8 message and actually transmit it via JS8Call.

    GATED: JS8Call will key the rig to send this. The caller must pass
    allow_tx=True (which the CLI/agent only does after the TX master switch is
    armed and the band is verified). dry_run reports what *would* be sent.

    Working recipe (verified on-air): load the text with TX.SET_TEXT, then fire
    the real 'Send' toggle over AT-SPI (_trigger_tx). Requires JS8Call to have
    Monitor(RX) ON, Enable Transmitter(TX) ON, and be running with
    accessibility enabled.
    """
    if dry_run or not allow_tx:
        return {"queued": False, "dry_run": True, "text": text,
                "note": "TX not armed (need allow_tx + TX master switch)"}
    ensure_running()
    ready = ensure_tx_ready()   # RX/TX/autoreply/SPOT must be ON for TX to fire
    with Js8Client() as c:
        c.send_raw("TX.SET_TEXT", text)   # load into compose box (persists)
    time.sleep(1.0)
    trig = _trigger_tx()
    return {"queued": True, "text": text, "tx": trig, "ready": ready}


# ---------------------------------------------------------------------------
# SMS / email via APRS-IS (JS8Call gates @APRSIS CMD traffic to APRS-IS itself)
# ---------------------------------------------------------------------------
# When we transmit a directed message to the group @APRSIS with a " CMD " verb,
# JS8Call's spotAprsCmd() forwards the text verbatim as an APRS third-party
# packet: FROMCALL>APJ8CL,qAS,BYCALL:<text>  (see docs/JS8CALL_NOTES.md). The
# text is a normal APRS message: ":ADDRESSEE:body{NN" where ADDRESSEE is padded
# to 9 chars. Well-known APRS message gateways:
#   SMSGTE  -> SMS   (addressee "SMSGTE", body "@<number> <text>")
#   EMAIL-2 -> email (addressee "EMAIL-2", body "<addr> <text>")
_APRS_SEQ = [0]


def _aprs_addr(call: str) -> str:
    """APRS message addressee field is exactly 9 chars, space-padded."""
    return f"{call:<9}"[:9]


def _next_seq() -> str:
    _APRS_SEQ[0] = (_APRS_SEQ[0] + 1) % 100
    return f"{{{_APRS_SEQ[0]:02d}"   # APRS line-number suffix, e.g. {07


def format_sms(number: str, message: str) -> str:
    """Build the on-air JS8 string that relays an SMS via SMSGTE.
    Result e.g.:  @APRSIS CMD :SMSGTE   :@13125551234 hello{01
    """
    num = "".join(ch for ch in number if ch.isdigit())
    body = f"@{num} {message}".strip()
    return f"@APRSIS CMD :{_aprs_addr('SMSGTE')}:{body}{_next_seq()}"


def format_email(address: str, message: str) -> str:
    """Build the on-air JS8 string that relays an email via the EMAIL-2 gateway.
    Result e.g.:  @APRSIS CMD :EMAIL-2  :you@example.com hi{02
    """
    body = f"{address} {message}".strip()
    return f"@APRSIS CMD :{_aprs_addr('EMAIL-2')}:{body}{_next_seq()}"


def _aprs_ready() -> dict:
    """Best-effort check that JS8Call will actually gate to APRS-IS."""
    import os
    ini = os.path.expanduser("~/.config/JS8Call.ini")
    ok = {"spot_to_aprs": None, "note": ""}
    try:
        txt = open(ini).read()
        ok["spot_to_aprs"] = "SpotToAPRS=true" in txt
    except OSError:
        ok["note"] = "could not read JS8Call.ini"
    return ok


def send_sms(number: str, message: str, *, allow_tx: bool = False,
             dry_run: bool = False) -> dict:
    """Send an SMS to a phone number via JS8 -> APRS-IS -> SMSGTE (GATED).

    Note: this transmits an APRS message over the air; delivery depends on the
    SMSGTE gateway and APRS-IS reachability. JS8Call must have SpotToAPRS=true.
    """
    onair = format_sms(number, message)
    ready = _aprs_ready()
    r = send(onair, allow_tx=allow_tx, dry_run=dry_run)
    r.update({"kind": "sms", "to": number, "message": message,
              "onair": onair, "aprs": ready})
    return r


def send_email(address: str, message: str, *, allow_tx: bool = False,
               dry_run: bool = False) -> dict:
    """Send an email via JS8 -> APRS-IS -> EMAIL-2 gateway (GATED)."""
    onair = format_email(address, message)
    ready = _aprs_ready()
    r = send(onair, allow_tx=allow_tx, dry_run=dry_run)
    r.update({"kind": "email", "to": address, "message": message,
              "onair": onair, "aprs": ready})
    return r


# ---------------------------------------------------------------------------
# Inbox / store-and-forward
# ---------------------------------------------------------------------------
def inbox(timeout: float = 6.0) -> dict:
    """Fetch stored inbox messages (INBOX.GET_MESSAGES -> INBOX.MESSAGES)."""
    ensure_running()
    msgs = []
    try:
        with Js8Client(timeout=timeout) as c:
            m = c.request("INBOX.GET_MESSAGES", "INBOX.MESSAGES", timeout=timeout)
            if m:
                p = m.get("params", {})
                msgs = p.get("MESSAGES", []) or []
    except OSError as e:
        return {"error": str(e), "messages": []}
    return {"engine": "js8call", "n": len(msgs), "messages": msgs}


def store(callsign: str, message: str, *, allow_tx: bool = False,
          dry_run: bool = False) -> dict:
    """Leave a directed message for `callsign` (store-and-forward). Sending a
    directed message that the target isn't currently hearing lets relaying
    stations hold it; on our side we transmit 'CALL MSG'. GATED."""
    onair = f"{callsign.upper()} {message}"
    r = send(onair, allow_tx=allow_tx, dry_run=dry_run)
    r.update({"kind": "store", "to": callsign.upper(), "message": message,
              "onair": onair})
    return r
