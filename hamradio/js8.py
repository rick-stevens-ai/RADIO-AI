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


def ensure_running(wait_s: float = 30.0) -> dict:
    """Start JS8Call headless (Xvfb) if it isn't already running.

    IMPORTANT: probe with pgrep, NOT by opening a TCP socket. JS8Call's API
    wedges if a client connects and disconnects immediately before another
    connects, so we must avoid any throw-away socket before the real one."""
    if is_running():
        return {"started": False, "api": True, "note": "already running"}
    subprocess.Popen(
        ["tmux", "new-session", "-d", "-s", "js8",
         'xvfb-run -a -s "-screen 0 1024x768x16" js8call 2>&1 | tee /tmp/js8call.log'],
    )
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


def send(text: str, *, allow_tx: bool = False, dry_run: bool = False) -> dict:
    """Queue a JS8 message for transmission via JS8Call.

    GATED: JS8Call will key the rig to send this. The caller must pass
    allow_tx=True (which the CLI/agent only does after the TX master switch is
    armed and the band is verified). dry_run reports what *would* be sent.
    """
    if dry_run or not allow_tx:
        return {"queued": False, "dry_run": True, "text": text,
                "note": "TX not armed (need allow_tx + TX master switch)"}
    ensure_running()
    with Js8Client() as c:
        c.send_raw("TX.SEND_MESSAGE", text)
    return {"queued": True, "text": text}
