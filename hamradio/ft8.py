"""
hamradio.ft8 — autonomous FT8 QSO engine (encode via ft8_lib, decode via jt9).

Leverages open-source tooling rather than re-implementing the protocol:
  * ENCODE : kgoba/ft8_lib `gen_ft8` -> standards-compliant 15 s WAV
             (verified to decode in WSJT-X jt9 = real-world interop).
  * DECODE : WSJT-X `jt9 --ft8` on each 15 s RX capture.
  * TX     : play the generated WAV through the IC-7300 USB codec while keyed,
             via the existing hamradio.tx safety gate (master switch + band
             plan + fail-safe un-key). All the same guards as voice/CW TX.

FT8 timing (fixed by the protocol):
  * 15 s cycles aligned to UTC (slot start at seconds % 15 == 0).
  * "even" slot = cycle starting at an even multiple of 15 s past the minute
    (0,30 s); "odd" = (15,45 s). We derive our TX slot from the QSO role:
    - the station calling CQ transmits in one slot parity; the answerer uses
      the opposite parity, locked to whichever slot we first heard them in.
  * TX audio must start right at the slot boundary; jt9 wants ~13.5 s captured.

QSO state machine (answering a CQ), messages exactly per the FT8 standard:
    RX: CQ DX GRID              (their CQ)
    TX: DX MYCALL MYGRID        (Tx2  — answer)
    RX: MYCALL DX <rpt>         (their report)
    TX: DX MYCALL R<rpt>        (Tx4  — roger + our report)
    RX: MYCALL DX RRR|RR73      (their roger)
    TX: DX MYCALL 73            (Tx6  — sign off) -> QSO complete/logged
Calling CQ is the mirror image (we start, they answer).

Safety: nothing transmits unless the tx gate passes (allow_tx + master switch +
in-band). A max-cycles cap and a per-cycle un-key make runaway impossible.
"""
from __future__ import annotations
import os
import re
import time
import shutil
import subprocess
import tempfile
import pathlib
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable

from .rig import Rig, clock_sync
from . import tx as txmod
from . import audio as audiomod
from . import decode as decodemod

FT8_LIB_DIR = pathlib.Path(os.path.expanduser("~/radio/ft8_lib"))
GEN_FT8 = FT8_LIB_DIR / "gen_ft8"
CYCLE = 15.0                # seconds
TX_AUDIO_LEN = 12.64        # seconds of FT8 modulation
RX_CAPTURE = 13.5           # seconds to capture for jt9
DATA_MODE = "PKTUSB"        # codec audio -> transmitter on the IC-7300

# Standard FT8 audio offset we transmit on (Hz within the passband). 1500 is a
# safe mid-passband default; a caller may pick a clear offset from a scan.
DEFAULT_TX_OFFSET = 1500


@dataclass
class QSOLog:
    my_call: str
    my_grid: str
    dx_call: str
    dx_grid: str = ""
    rst_sent: str = ""
    rst_rcvd: str = ""
    band_hz: int = 0
    started: str = ""
    completed: bool = False
    transcript: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


ADIF_LOG = os.path.expanduser("~/radio/logs/kd9nwa.adi")


def _band_name(hz: int) -> str:
    """Map a dial/frequency in Hz to an ADIF band string."""
    mhz = hz / 1e6
    bands = [
        (1.8, 2.0, "160m"), (3.5, 4.0, "80m"), (5.3, 5.4, "60m"),
        (7.0, 7.3, "40m"), (10.1, 10.15, "30m"), (14.0, 14.35, "20m"),
        (18.0, 18.2, "17m"), (21.0, 21.45, "15m"), (24.8, 25.0, "12m"),
        (28.0, 29.7, "10m"), (50.0, 54.0, "6m"), (144.0, 148.0, "2m"),
    ]
    for lo, hi, name in bands:
        if lo <= mhz <= hi:
            return name
    return f"{mhz:.3f}MHz"


def _adif_field(name: str, value: str) -> str:
    value = str(value)
    return f"<{name}:{len(value)}>{value}"


def log_qso_adif(log: "QSOLog", *, path: str = ADIF_LOG) -> str:
    """Append a completed QSO to the ADIF log. Returns the log path.
    Only writes if a dx_call is present. Idempotent-ish: callers should only
    log completed QSOs."""
    if not log.dx_call:
        return ""
    freq_mhz = f"{(log.band_hz or 0) / 1e6:.3f}"
    now = time.gmtime()
    date = time.strftime("%Y%m%d", now)
    tm = time.strftime("%H%M%S", now)
    parts = [
        _adif_field("CALL", log.dx_call),
    ]
    if log.dx_grid:
        parts.append(_adif_field("GRIDSQUARE", log.dx_grid))
    parts += [
        _adif_field("BAND", _band_name(log.band_hz or 0)),
        _adif_field("FREQ", freq_mhz),
        _adif_field("MODE", "FT8"),
    ]
    if log.rst_sent:
        parts.append(_adif_field("RST_SENT", log.rst_sent))
    if log.rst_rcvd:
        parts.append(_adif_field("RST_RCVD", log.rst_rcvd))
    parts += [
        _adif_field("QSO_DATE", date),
        _adif_field("TIME_ON", tm),
        _adif_field("STATION_CALLSIGN", log.my_call),
        _adif_field("MY_GRIDSQUARE", log.my_grid),
        "<EOR>",
    ]
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write("".join(parts) + "\n")
    return path


def _now_slot_parity() -> int:
    """0 if the CURRENT 15s slot starts at an even 30s mark (0/30), else 1."""
    t = int(time.time())
    slot = (t // 15)
    return slot % 2


def _slot_index(t: Optional[float] = None) -> int:
    return int((t if t is not None else time.time()) // CYCLE)


def _wait_next_slot(parity: Optional[int] = None) -> float:
    """Sleep until the next 15 s boundary; if parity given, until the next
    slot of that parity. Returns the epoch time of the boundary."""
    while True:
        now = time.time()
        boundary = (int(now // CYCLE) + 1) * CYCLE
        time.sleep(max(0, boundary - now) + 0.05)
        if parity is None or (int(boundary // CYCLE) % 2) == parity:
            return boundary


def _capture_current_slot() -> list[dict]:
    """Capture+decode the slot we are CURRENTLY in (assumes we're at/just past
    a slot boundary). Used to catch a reply that arrives in the slot right after
    our TX — which _wait_next_slot would wrongly skip. Captures the remaining
    slot time (up to RX_CAPTURE)."""
    now = time.time()
    into_slot = now % CYCLE
    remain = CYCLE - into_slot - 0.5   # leave margin before next boundary
    secs = min(RX_CAPTURE, max(4.0, remain))
    return decodemod.decode_ft8(seconds=secs, align=False).get("decodes", [])


def encode_wav(message: str, offset_hz: int = DEFAULT_TX_OFFSET) -> str:
    """Generate a standards-compliant FT8 WAV for `message` at `offset_hz`.

    Uses ft8_lib gen_ft8 (output verified to decode in WSJT-X jt9).
    """
    if not GEN_FT8.exists():
        raise RuntimeError(f"gen_ft8 not found at {GEN_FT8}; build ft8_lib")
    wav = tempfile.mktemp(suffix=".wav", prefix="ft8tx_")
    r = subprocess.run([str(GEN_FT8), message, wav, str(int(offset_hz))],
                       capture_output=True, text=True, timeout=30)
    if not os.path.exists(wav):
        raise RuntimeError(f"gen_ft8 failed: {r.stdout} {r.stderr}")
    return wav


def _tx_message(rig: Rig, message: str, offset_hz: int, allow_tx: bool,
                dry_run: bool) -> dict:
    """Encode `message` and transmit it in the CURRENT slot (must be called
    right at a slot boundary). Returns a small status dict. Gated + fail-safe."""
    wav = encode_wav(message, offset_hz)
    orig_mode = orig_pb = None
    changed = False
    try:
        if not dry_run:
            om, opb = rig.get_mode()
            if om.upper() != DATA_MODE:
                orig_mode, orig_pb, changed = om, opb, True
                rig.set_mode(DATA_MODE)
                time.sleep(0.15)
        # key + play the full 15 s WAV (gen_ft8 pads to 15 s; jt9-decodable)
        with txmod.keyed(rig, allow_tx=allow_tx, timeout=int(CYCLE) + 2,
                         dry_run=dry_run) as k:
            if k.get("dry_run"):
                return {"tx": message, "offset_hz": offset_hz, "dry_run": True}
            audiomod.play_wav(wav)
        return {"tx": message, "offset_hz": offset_hz, "sent": True}
    finally:
        if changed and orig_mode:
            rig.set_mode(orig_mode, orig_pb)
        try:
            os.unlink(wav)
        except OSError:
            pass


def _rx_decode(seconds: float = RX_CAPTURE) -> list[dict]:
    """Capture one slot and return jt9 FT8 decodes (list of dicts)."""
    res = decodemod.decode_ft8(seconds=seconds, align=False)
    return res.get("decodes", [])


# ---- message parsing helpers ----------------------------------------------
def _msg_to(dxcall: str, mycall: str, decodes: list[dict]) -> list[str]:
    """Return messages addressed to us (mycall first token) from dxcall."""
    out = []
    for d in decodes:
        m = d.get("message", "")
        toks = m.split()
        if len(toks) >= 2 and toks[0] == mycall and toks[1] == dxcall:
            out.append(m)
    return out


def _extract_report(msg: str) -> Optional[str]:
    """Pull a signal report token like -12 / +03 / R-12 from a message.
    Note: a leading '-'/'+' is not a \\b boundary after a space, so use
    non-word lookaround instead of \\b (that was a real bug)."""
    m = re.search(r"(?<![\w])R?[-+]\d{2}(?![\w])", msg)
    return m.group(0) if m else None


def _snr_report(decodes: list[dict], dxcall: str) -> str:
    """Build the report WE send them from the SNR we decoded them at."""
    for d in decodes:
        if dxcall in d.get("message", "") and "snr_db" in d:
            snr = max(-30, min(0, int(d["snr_db"])))  # FT8 reports clamp
            return f"{snr:+03d}"
    return "-15"


# ===========================================================================
# QSO ENGINES
# ===========================================================================
def answer_cq(rig: Rig, dxcall: str, dxgrid: str, my_call: str, my_grid: str,
              *, offset_hz: int = DEFAULT_TX_OFFSET, allow_tx: bool = False,
              dry_run: bool = False, max_cycles: int = 12,
              on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Work a station that is calling CQ. We transmit in the slot OPPOSITE to
    the one we hear them in, and run the standard answer sequence to completion.

    Returns a QSOLog dict. Stops after max_cycles regardless (safety cap).
    """
    log = QSOLog(my_call=my_call, my_grid=my_grid, dx_call=dxcall,
                 dx_grid=dxgrid, band_hz=rig.get_freq(),
                 started=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def emit(ev):
        log.transcript.append(ev)
        if on_event:
            on_event(ev)

    clk = clock_sync()
    emit({"clock": clk})
    if not clk.get("ft8_ok", True) and not dry_run:
        return {"error": "clock not synced for FT8", "clock": clk}

    # Determine THEIR slot parity by listening one cycle, then TX on the other.
    emit({"phase": "listen_for_slot"})
    _wait_next_slot()
    heard = _rx_decode()
    their_parity = _now_slot_parity()  # parity of the slot we just captured
    our_parity = 1 - their_parity

    # State: what we still need. Start by answering their CQ.
    stage = "answer"        # answer -> roger -> signoff -> done
    for cycle in range(max_cycles):
        boundary = _wait_next_slot(our_parity)  # our TX slot
        if stage == "answer":
            msg = f"{dxcall} {my_call} {my_grid}"
        elif stage == "roger":
            rpt = _snr_report(log._last_decodes, dxcall) if hasattr(log, "_last_decodes") else "-15"
            log.rst_sent = rpt
            msg = f"{dxcall} {my_call} R{rpt}"
        elif stage == "signoff":
            msg = f"{dxcall} {my_call} 73"
        else:
            break
        r = _tx_message(rig, msg, offset_hz, allow_tx, dry_run)
        emit({"cycle": cycle, "stage": stage, **r})
        # Once we've transmitted our final 73, the QSO is done — stop keying.
        if stage == "signoff":
            stage = "done"
            break
        if dry_run and cycle >= 2:
            # In dry-run we can't really progress the handshake; simulate a pass.
            emit({"note": "dry-run: simulated one full exchange"})
            break

        # Their reply arrives in the slot IMMEDIATELY after our TX. Our TX just
        # consumed ~15 s from our slot boundary, so we are now at the start of
        # that reply slot -> capture the CURRENT slot (do NOT wait a full slot).
        decodes = _capture_current_slot()
        log._last_decodes = decodes  # type: ignore
        mine = _msg_to(dxcall, my_call, decodes)
        emit({"cycle": cycle, "rx": mine})

        for m in mine:
            rep = _extract_report(m)
            if stage == "answer" and rep and not rep.startswith("R"):
                log.rst_rcvd = rep
                stage = "roger"
            elif stage in ("answer", "roger") and ("RR73" in m or "RRR" in m or "73" in m):
                stage = "signoff"
            elif stage == "signoff":
                stage = "done"
        if stage == "done":
            break

    log.completed = stage in ("signoff", "done")
    if log.completed and not dry_run:
        try:
            log_qso_adif(log)
        except Exception:
            pass
    return log.to_dict()


def call_cq(rig: Rig, my_call: str, my_grid: str, *,
            offset_hz: int = DEFAULT_TX_OFFSET, allow_tx: bool = False,
            dry_run: bool = False, max_cycles: int = 20,
            on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Call CQ and complete a QSO with the first station that answers us.

    We transmit CQ on our chosen slot parity; when someone replies
    'MYCALL THEIRCALL GRID', we run the report exchange to 73.
    """
    log = QSOLog(my_call=my_call, my_grid=my_grid, dx_call="",
                 band_hz=rig.get_freq(),
                 started=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def emit(ev):
        log.transcript.append(ev)
        if on_event:
            on_event(ev)

    clk = clock_sync()
    emit({"clock": clk})
    if not clk.get("ft8_ok", True) and not dry_run:
        return {"error": "clock not synced for FT8", "clock": clk}

    our_parity = _now_slot_parity()
    stage = "cq"      # cq -> report -> rr73 -> done
    rr73_tries = 0
    for cycle in range(max_cycles):
        _wait_next_slot(our_parity)
        if stage == "cq":
            msg = f"CQ {my_call} {my_grid}"
        elif stage == "report":
            msg = f"{log.dx_call} {my_call} {log.rst_sent}"
        elif stage == "rr73":
            msg = f"{log.dx_call} {my_call} RR73"
        else:
            break
        r = _tx_message(rig, msg, offset_hz, allow_tx, dry_run)
        emit({"cycle": cycle, "stage": stage, **r})
        if dry_run and cycle >= 1:
            emit({"note": "dry-run: CQ transmitted; not waiting for a real reply"})
            break

        # Answer arrives in the slot right after our CQ TX -> capture NOW.
        decodes = _capture_current_slot()

        if stage == "cq":
            # find someone answering us: 'MYCALL THEIRCALL GRID'
            for d in decodes:
                toks = d.get("message", "").split()
                if len(toks) >= 3 and toks[0] == my_call:
                    log.dx_call = toks[1]
                    log.dx_grid = toks[2] if re.match(r"^[A-R]{2}\d\d$", toks[2]) else ""
                    log.rst_sent = _snr_report(decodes, log.dx_call)
                    stage = "report"
                    emit({"cycle": cycle, "answered_by": log.dx_call})
                    break
        elif stage == "report":
            for m in _msg_to(log.dx_call, my_call, decodes):
                rep = _extract_report(m)
                if rep and rep.startswith("R"):
                    log.rst_rcvd = rep[1:]
                    stage = "rr73"
        elif stage == "rr73":
            rr73_tries += 1
            for m in _msg_to(log.dx_call, my_call, decodes):
                if "73" in m:
                    stage = "done"
            # We've sent RR73 (contact confirmed on our side). If we don't hear
            # their final 73 after a couple tries, stop keying — they've logged
            # us and moved on. The QSO counts as complete.
            if stage != "done" and rr73_tries >= 2:
                stage = "done"
        if stage == "done":
            break

    # A QSO where we sent RR73 counts as complete on our side.
    log.completed = stage in ("rr73", "done") and bool(log.dx_call)
    if log.completed and not dry_run:
        try:
            log_qso_adif(log)
        except Exception:
            pass
    return log.to_dict()
