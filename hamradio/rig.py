"""
hamradio.rig — safe control wrapper around rigctld (Hamlib) for the IC-7300.

Design principles:
  * Talk to a persistent rigctld on 127.0.0.1:4532 (NET rigctl protocol).
    rigctld owns the single serial connection to the radio; every client
    (this lib, WSJT-X, fldigi, the agent) multiplexes through it.
  * READ-ONLY by default. Any command that keys the transmitter is gated
    behind an explicit, deliberate call path + guard checks (see tx.py).
  * Fail closed: on any parse/connection error we never leave PTT asserted.

This module only does *control/telemetry* (freq, mode, S-meter, PTT state).
Transmit sequencing lives in hamradio.tx which imports this and adds guards.
"""
from __future__ import annotations
import socket
import time
from dataclasses import dataclass, asdict
from typing import Optional

RIGCTLD_HOST = "127.0.0.1"
RIGCTLD_PORT = 4532
TIMEOUT = 5.0

# IC-7300 hamlib model (rigctl --list -> 3073). Kept here for reference / launchers.
IC7300_MODEL = 3073


class RigError(RuntimeError):
    pass


class Rig:
    """Thin, robust client for rigctld's text protocol.

    We use the 'extended response' separators so replies are unambiguous and
    machine-parseable (rigctld: send commands prefixed with '+' → newline-
    separated 'Key: Value' lines terminated by 'RPRT <code>').
    """

    def __init__(self, host: str = RIGCTLD_HOST, port: int = RIGCTLD_PORT,
                 timeout: float = TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    # ---- low-level ---------------------------------------------------------
    # Map the long command names used throughout this module to the SHORT
    # rigctl tokens that accept the '+' extended-response prefix. (rigctld's
    # extended mode only works with the single-letter command forms; the long
    # 'get_freq' form hangs under '+'.)
    _SHORT = {
        "get_freq": "f", "set_freq": "F",
        "get_mode": "m", "set_mode": "M",
        "get_ptt": "t", "set_ptt": "T",
        "get_level": "l", "set_level": "L",
    }

    def _cmd(self, line: str) -> list[str]:
        """Send one command (extended-response mode) and return reply lines.

        `line` is written in long form (e.g. 'get_freq', 'set_freq 14074000',
        'get_level STRENGTH'); we translate the verb to its short token so the
        '+' extended-response prefix works. Raises RigError on non-zero RPRT.
        Never raises with PTT left on (this path issues no PTT).
        """
        parts = line.split(" ", 1)
        verb = parts[0]
        rest = (" " + parts[1]) if len(parts) > 1 else ""
        token = self._SHORT.get(verb, verb)
        with socket.create_connection((self.host, self.port), self.timeout) as s:
            s.settimeout(self.timeout)
            s.sendall(("+" + token + rest + "\n").encode())
            buf = b""
            deadline = time.time() + self.timeout
            while b"RPRT " not in buf:
                if time.time() > deadline:
                    raise RigError(f"timeout waiting for reply to {line!r}")
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    raise RigError(f"socket timeout on {line!r}")
                if not chunk:
                    break
                buf += chunk
        text = buf.decode(errors="replace")
        lines = text.splitlines()
        rprt = next((l for l in lines if l.startswith("RPRT ")), None)
        if rprt is None:
            raise RigError(f"no RPRT in reply to {line!r}: {text!r}")
        code = int(rprt.split()[1])
        if code != 0:
            raise RigError(f"rigctld error {code} on {line!r}")
        # strip the leading echo line (e.g. "get_freq:") and trailing RPRT
        payload = [l for l in lines if l and not l.startswith("RPRT ")]
        if payload and payload[0].endswith(":"):
            payload = payload[1:]
        return payload

    @staticmethod
    def _kv(lines: list[str]) -> dict:
        out = {}
        for l in lines:
            if ":" in l:
                k, _, v = l.partition(":")
                out[k.strip()] = v.strip()
        return out

    # ---- read-only telemetry ----------------------------------------------
    def get_freq(self) -> int:
        """Dial frequency in Hz."""
        return int(self._kv(self._cmd("get_freq")).get("Frequency", "0"))

    def get_mode(self) -> tuple[str, int]:
        d = self._kv(self._cmd("get_mode"))
        return d.get("Mode", "?"), int(d.get("Passband", "0") or 0)

    def get_ptt(self) -> Optional[bool]:
        # Some rigs/dummy don't support PTT readback (RPRT -11). Treat as unknown.
        try:
            d = self._kv(self._cmd("get_ptt"))
        except RigError:
            return None
        return d.get("PTT", "0") not in ("0", "", "OFF")

    def get_smeter(self) -> Optional[int]:
        """Return S-meter reading in dB relative to S9 (Hamlib STRENGTH level).

        Value is roughly: 0 == S9, negative == below S9 (each S-unit ~6 dB),
        positive == over S9. Returns None if the radio can't report it.
        """
        try:
            raw = self._cmd("get_level STRENGTH")
        except RigError:
            return None
        # reply is a plain numeric line (e.g. '-45'), possibly after an echoed
        # 'STRENGTH' arg line. Take the first signed-integer token we find.
        for l in raw:
            l = l.strip()
            if l.lstrip("-").isdigit():
                return int(l)
        # also handle a 'Key: Value' shape just in case
        for v in self._kv(raw).values():
            try:
                return int(v)
            except ValueError:
                continue
        return None

    def get_fwd_power(self) -> Optional[float]:
        """Forward-power meter reading, 0.0..1.0 of scale, or None if unsupported.

        Used to VERIFY the radio is actually emitting RF during TX (guards
        against 'command accepted but nothing transmitted' false positives, as
        happens with send_morse when CW-over-USB isn't enabled in the menu).
        """
        for level in ("RFPOWER_METER_WATTS", "RFPOWER_METER"):
            try:
                raw = self._cmd(f"get_level {level}")
            except RigError:
                continue
            for l in raw:
                l = l.strip()
                try:
                    return float(l)
                except ValueError:
                    continue
        return None

    def status(self) -> "RigStatus":
        freq = self.get_freq()
        mode, pb = self.get_mode()
        return RigStatus(
            freq_hz=freq,
            mode=mode,
            passband_hz=pb,
            ptt=self.get_ptt(),
            smeter_db=self.get_smeter(),
            band=band_for(freq),
        )

    # ---- control that does NOT transmit (safe: VFO/mode changes) -----------
    def set_freq(self, hz: int) -> None:
        self._cmd(f"set_freq {int(hz)}")

    def set_mode(self, mode: str, passband: int = 0) -> None:
        mode = mode.upper()
        self._cmd(f"set_mode {mode} {int(passband)}")


@dataclass
class RigStatus:
    freq_hz: int
    mode: str
    passband_hz: int
    ptt: bool
    smeter_db: Optional[int]
    band: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


# ---- band plan helper (US) -------------------------------------------------
# (band_name, lo_hz, hi_hz). Coarse HF/6m/2m/70cm segments for labeling scans.
_BANDS = [
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_330_000, 5_410_000),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
    ("2m", 144_000_000, 148_000_000),
    ("70cm", 420_000_000, 450_000_000),
]


def band_for(hz: int) -> Optional[str]:
    for name, lo, hi in _BANDS:
        if lo <= hz <= hi:
            return name
    return None


def band_edges(name: str) -> Optional[tuple[int, int]]:
    for n, lo, hi in _BANDS:
        if n.lower() == name.lower():
            return lo, hi
    return None


def clock_sync() -> dict:
    """Report NTP/clock discipline health -- FT8/JT modes need the system clock
    within ~1 s of true time (decoding is slot-aligned to 15 s UTC boundaries).
    Returns synced flag, offset, source, and a human verdict."""
    import subprocess as _sp
    info = {"synchronized": None, "ntp_active": None, "offset_s": None,
            "rms_offset_s": None, "source": None, "stratum": None}
    try:
        td = _sp.run(["timedatectl", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in td.splitlines():
            if line.startswith("NTPSynchronized="):
                info["synchronized"] = line.strip().endswith("yes")
            if line.startswith("NTP="):
                info["ntp_active"] = line.strip().endswith("yes")
    except Exception:
        pass
    try:
        ct = _sp.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
        for line in ct.splitlines():
            if line.startswith("Reference ID"):
                info["source"] = line.split(":", 1)[1].strip()
            elif line.startswith("Stratum"):
                info["stratum"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("System time"):
                p = line.split(":", 1)[1].strip().split()
                v = float(p[0]); info["offset_s"] = -v if "slow" in line else v
            elif line.startswith("RMS offset"):
                info["rms_offset_s"] = float(line.split(":", 1)[1].strip().split()[0])
    except Exception:
        pass
    off = info.get("offset_s")
    if off is None:
        info["verdict"] = "unknown"
        info["ft8_ok"] = bool(info.get("synchronized"))
    else:
        a = abs(off)
        info["ft8_ok"] = a < 1.0
        info["verdict"] = ("excellent" if a < 0.1 else "good" if a < 0.5
                           else "marginal" if a < 1.0 else "BAD - fix NTP")
    return info
