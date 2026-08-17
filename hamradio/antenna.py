"""
hamradio.antenna — IC-7300 internal antenna tuner control.

The IC-7300 has a built-in automatic antenna tuner. After changing frequency
(especially across bands) the tuner must re-tune to present a good match /
low SWR to the finals, otherwise the rig folds back power (or refuses to make
full power) and the transmitted signal is weak.

The tuner is driven over CI-V (command 0x1C 0x01):
    1C 01 00  -> tuner OFF (bypass)
    1C 01 01  -> tuner ON  (use last match)
    1C 01 02  -> START a tuning cycle (rig keys itself, finds a match, then
                 reports 01 when matched or 00 if it couldn't match)

rigctld owns the serial port, and Hamlib's netrigctl doesn't cleanly expose the
"start tuning" action, so tune() briefly stops the user rigctld service, sends
the raw CI-V, polls until the match settles, then restarts rigctld.

Requires the operator to have authorized TX in spirit — tuning keys the rig for
a second or two at reduced power into the antenna. It is a normal, low-risk part
of changing bands, but we still respect the TX master switch via the caller.
"""
from __future__ import annotations
import subprocess
import time
from typing import Optional

SERIAL_DEV = "/dev/ttyUSB0"
BAUD = 115200
CIV_RIG_ADDR = 0x94   # IC-7300 default CI-V address
CIV_CTRL_ADDR = 0xE0  # this controller

_PRE = b"\xfe\xfe"
_END = b"\xfd"


def _frame(payload: bytes) -> bytes:
    return _PRE + bytes([CIV_RIG_ADDR, CIV_CTRL_ADDR]) + payload + _END


def _rigctld_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "rigctld"],
            capture_output=True, text=True)
        return r.stdout.strip() == "active"
    except OSError:
        return False


def _rigctld(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, "rigctld"],
                   capture_output=True)


class _SerialSession:
    """Open the CI-V serial port directly, stopping rigctld first if needed."""
    def __init__(self):
        self._had_rigctld = False
        self.ser = None

    def __enter__(self):
        import serial  # pyserial
        self._had_rigctld = _rigctld_active()
        if self._had_rigctld:
            _rigctld("stop")
            # wait for the port to be released
            for _ in range(10):
                time.sleep(0.4)
                try:
                    self.ser = serial.Serial(SERIAL_DEV, BAUD, timeout=1.5)
                    break
                except serial.SerialException:
                    continue
        if self.ser is None:
            import serial
            self.ser = serial.Serial(SERIAL_DEV, BAUD, timeout=1.5)
        return self

    def civ(self, payload: bytes, wait: float = 0.4) -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write(_frame(payload))
        time.sleep(wait)
        return self.ser.read(128)

    def __exit__(self, *a):
        try:
            if self.ser:
                self.ser.close()
        except OSError:
            pass
        if self._had_rigctld:
            _rigctld("start")
            time.sleep(3)  # let rigctld re-open the port + settle


def _state_byte(resp: bytes) -> Optional[str]:
    """Extract the tuner-state nibble from a 1C 01 reply frame."""
    h = resp.hex()
    i = h.find("1c01")
    if i < 0:
        return None
    nn = h[i + 4:i + 6]
    return nn if len(nn) == 2 else None


_STATE_NAMES = {"00": "no-match/off", "01": "tuned", "02": "tuning"}


def tuner_state() -> dict:
    """Read the current tuner state without starting a tune cycle."""
    with _SerialSession() as s:
        st = _state_byte(s.civ(b"\x1c\x01"))
    return {"state": st, "meaning": _STATE_NAMES.get(st, "unknown")}


def tune(timeout_s: float = 35.0) -> dict:
    """Run an antenna-tuner cycle on the current frequency and wait for a match.

    Returns the final state. Should be called after any band/frequency change,
    before transmitting for real. Keys the rig briefly (the tuner does this
    itself, at reduced power).
    """
    with _SerialSession() as s:
        before = _state_byte(s.civ(b"\x1c\x01"))
        # ensure tuner is enabled, then start a tuning cycle
        s.civ(b"\x1c\x01\x01")
        start = s.civ(b"\x1c\x01\x02")
        started_ok = start.hex().endswith("fbfd")
        final = before
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            time.sleep(1.0)
            final = _state_byte(s.civ(b"\x1c\x01"))
            if final in ("00", "01"):
                break
    matched = final == "01"
    return {
        "tuned": matched,
        "state": final,
        "meaning": _STATE_NAMES.get(final, "unknown"),
        "started": started_ok,
        "before": before,
        "waited_s": round(time.time() - t0, 1) if 't0' in dir() else None,
    }


def set_tuner(on: bool) -> dict:
    """Enable (use last match) or bypass the tuner without a full tune cycle."""
    with _SerialSession() as s:
        r = s.civ(b"\x1c\x01" + (b"\x01" if on else b"\x00"))
    return {"tuner_on": on, "ok": r.hex().endswith("fbfd")}
