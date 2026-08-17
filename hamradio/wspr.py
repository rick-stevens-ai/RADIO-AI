"""
hamradio.wspr — WSPR (Weak Signal Propagation Reporter) receive + beacon.

WSPR is a propagation-beacon mode: you send a tiny standardized frame
(callsign + 4-char grid + power in dBm), and receivers worldwide auto-decode it
and upload "spots" to wsprnet.org. It works far below the noise floor (~-28 dB),
so it's the definitive "where is my signal actually going?" tool.

Signal: 4-FSK, 1.4648 baud, ~6 Hz wide, 162 symbols, ~110.6 s of transmission
inside a 2-minute window aligned to EVEN UTC minutes (start at seconds==0 on an
even minute). Dial frequencies below are the standard USB dial freqs; WSPR
activity sits ~1400-1600 Hz above the dial.

RX: capture one aligned 2-minute window and decode with WSJT-X `wsprd`.
TX: (gated) generate the WSPR waveform for `KD9NWA EN51 <dBm>` and play it into
    the transmitter during a 2-minute window. Low power / long unattended runs.

Spots can also be fetched from wsprnet.org to see who heard us (see who_spots).
"""
from __future__ import annotations
import os
import re
import time
import math
import subprocess
import tempfile
import urllib.request
import urllib.parse
from typing import Optional

from . import audio as audiomod

# Standard WSPR USB dial frequencies (Hz)
WSPR_DIAL = {
    "160m": 1836600, "80m": 3568600, "60m": 5287200, "40m": 7038600,
    "30m": 10138700, "20m": 14095600, "17m": 18104600, "15m": 21094600,
    "12m": 24924600, "10m": 28124600, "6m": 50293000, "2m": 144489000,
}
CYCLE = 120.0          # WSPR is a 2-minute mode
TX_SECONDS = 110.6     # actual modulation length
DATA_MODE = "USB"      # WSPR uses USB (audio ~1500 Hz above dial)
WSPRD = "/usr/bin/wsprd"


# --- timing ----------------------------------------------------------------
def seconds_to_window() -> float:
    """Seconds until the next even-minute WSPR window start (UTC)."""
    now = time.time()
    # window starts when (minutes even) and seconds==0 -> every 120s from epoch
    return CYCLE - (now % CYCLE)


def wait_window() -> float:
    """Sleep until the next 2-minute WSPR window boundary; return its epoch."""
    dt = seconds_to_window()
    time.sleep(dt + 0.1)
    return time.time()


# --- receive / decode ------------------------------------------------------
def _parse_wsprd(stdout: str) -> list[dict]:
    """Parse wsprd stdout lines into spot dicts.
    Typical line:  '2358  -24  0.3  14.097091  0  KD9NWA EN51 30  0  1  0'
    Fields: UTC SNR DT FREQ_MHz DRIFT  CALL GRID PWR ...
    """
    spots = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("<"):
            continue
        # match: time snr dt freq drift  then message tail
        # (the "time" field is a 4-char timestamp for real captures, but wsprd
        #  uses the first 4 chars of the filename for ad-hoc files -> accept any)
        m = re.match(
            r"^(\S{1,6})\s+(-?\d+)\s+(-?[\d.]+)\s+([\d.]+)\s+(-?\d+)\s+(.+)$",
            line)
        if not m:
            continue
        utc, snr, dt, freq, drift, tail = m.groups()
        toks = tail.split()
        call = grid = pwr = None
        if toks:
            call = toks[0]
            if len(toks) >= 2 and re.match(r"^[A-R]{2}\d\d$", toks[1]):
                grid = toks[1]
                if len(toks) >= 3 and toks[2].lstrip("-").isdigit():
                    pwr = int(toks[2])
        spots.append({
            "utc": utc, "snr_db": int(snr), "dt_s": float(dt),
            "freq_mhz": float(freq), "drift_hz": int(drift),
            "call": call, "grid": grid, "power_dbm": pwr,
            "message": tail.strip(),
        })
    return spots


def decode(wav_path: str, dial_mhz: float, deep: bool = True) -> list[dict]:
    """Decode a 2-minute 12000 Hz WSPR WAV with wsprd. Returns spot dicts."""
    args = [WSPRD, "-w", "-f", f"{dial_mhz:.6f}"]  # -w: wideband search
    if deep:
        args.append("-d")
    args.append(wav_path)
    r = subprocess.run(args, capture_output=True, text=True, timeout=120,
                       cwd=tempfile.gettempdir())
    return _parse_wsprd(r.stdout)


def receive(band: str = "20m", *, align: bool = True, set_rig=None,
            enrich: bool = True) -> dict:
    """Capture one aligned 2-minute WSPR window and decode it.

    set_rig(dial_hz, mode): optional callback to tune the radio first.
    enrich: annotate each spot with location (country/state/distance).
    """
    dial = WSPR_DIAL.get(band)
    if not dial:
        return {"error": f"no WSPR dial for band {band}",
                "bands": sorted(WSPR_DIAL)}
    if set_rig:
        set_rig(dial, DATA_MODE)
    if align:
        wait_window()
    wav = tempfile.mktemp(suffix=".wav", prefix="wspr_")
    # WSPR needs the full ~114 s; capture 116 s at 12 kHz mono
    audiomod.record_wav(116, wav)
    # wsprd wants exactly 12000 Hz mono; re-sample if needed
    wav12 = tempfile.mktemp(suffix=".wav", prefix="wspr12k_")
    subprocess.run(["sox", wav, "-r", "12000", "-c", "1", wav12],
                   stderr=subprocess.DEVNULL, check=False)
    src = wav12 if os.path.exists(wav12) else wav
    spots = decode(src, dial / 1e6)
    if enrich:
        _enrich(spots)
    for f in (wav, wav12):
        try:
            os.unlink(f)
        except OSError:
            pass
    dists = [s["distance_km"] for s in spots if s.get("distance_km")]
    return {"engine": "wsprd", "band": band, "dial_hz": dial,
            "n": len(spots), "max_km": max(dists) if dists else None,
            "spots": spots}


def _enrich(spots):
    try:
        from . import location as loc
    except Exception:
        return
    for s in spots:
        if s.get("call"):
            try:
                w = loc.lookup(s["call"], s.get("grid") or "")
                for k in ("country", "us_state", "distance_km", "bearing_deg",
                          "is_dx"):
                    if k in w:
                        s[k] = w[k]
            except Exception:
                pass


# --- transmit (beacon) -----------------------------------------------------
# WSPR symbol/audio constants (WSJT-X): 162 symbols, tone spacing = baud rate.
WSPR_SYMBOLS = 162
WSPR_BAUD = 12000.0 / 8192.0        # ~1.46484 Hz
WSPR_SR = 12000                     # audio sample rate for TX WAV


def encode_symbols(message: str) -> list[int]:
    """Get the 162 WSPR channel symbols (0..3) for a message via wsprsim -c.
    message e.g. 'KD9NWA EN51 30' (call grid dBm)."""
    tmp = tempfile.mktemp(suffix=".c2")
    r = subprocess.run(["/usr/bin/wsprsim", "-c", "-o", tmp, message],
                       capture_output=True, text=True, timeout=30,
                       cwd=tempfile.gettempdir())
    try:
        os.unlink(tmp)
    except OSError:
        pass
    m = re.search(r"Channel symbols:\s*([\d\s]+)", r.stdout)
    if not m:
        raise RuntimeError(f"wsprsim failed: {r.stdout} {r.stderr}")
    syms = [int(x) for x in m.group(1).split()]
    if len(syms) != WSPR_SYMBOLS:
        raise RuntimeError(f"expected {WSPR_SYMBOLS} symbols, got {len(syms)}")
    return syms


def encode_wav(message: str, offset_hz: float = 1500.0,
               out_path: Optional[str] = None, full_window: bool = True) -> str:
    """Generate a standards-compliant WSPR transmit WAV (4-FSK, phase-continuous)
    for `message`, with the lowest tone at `offset_hz` in the audio passband.
    Verified decodable by wsprd.

    full_window=True pads to a 1 s lead-in + full 120 s window (correct for a
    file you want wsprd to decode, harmless for real TX since keyed() un-keys
    when audio ends). False = just lead-in + signal (prompt un-key on TX)."""
    import wave
    import numpy as np
    syms = encode_symbols(message)
    sps = int(round(WSPR_SR / WSPR_BAUD))   # samples per symbol
    tone_spacing = WSPR_BAUD                 # Hz between adjacent FSK tones
    phase = 0.0
    out = np.empty(sps * WSPR_SYMBOLS, dtype=np.float64)
    idx = 0
    for s in syms:
        f = offset_hz + s * tone_spacing
        dphi = 2 * math.pi * f / WSPR_SR
        for _ in range(sps):
            out[idx] = math.sin(phase)
            phase += dphi
            idx += 1
    # small raised-cosine ramp at start/end to avoid key clicks
    ramp = int(WSPR_SR * 0.02)
    if ramp > 0 and len(out) > 2 * ramp:
        w = np.sin(np.linspace(0, math.pi / 2, ramp)) ** 2
        out[:ramp] *= w
        out[-ramp:] *= w[::-1]
    # A real WSPR transmission starts ~1 s into the 2-minute window. Prepend the
    # lead-in; optionally pad the tail to the full 120 s window (for decode self-
    # tests). For TX we skip the trailing silence so the rig un-keys promptly.
    lead = np.zeros(int(WSPR_SR * 1.0))
    frame = np.concatenate([lead, out])
    if full_window:
        total = int(WSPR_SR * CYCLE)
        if len(frame) < total:
            frame = np.concatenate([frame, np.zeros(total - len(frame))])
        else:
            frame = frame[:total]
    pcm = (np.clip(frame, -1, 1) * 0.7 * 32767).astype(np.int16)
    path = out_path or tempfile.mktemp(suffix=".wav", prefix="wsprtx_")
    w = wave.open(path, "w")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(WSPR_SR)
    w.writeframes(pcm.tobytes()); w.close()
    return path


def transmit(call: str, grid: str, power_dbm: int, band: str, *,
             offset_hz: float = 1500.0, allow_tx: bool = False,
             dry_run: bool = False, rig=None, align: bool = True) -> dict:
    """Transmit a WSPR beacon 'CALL GRID PWR' in the next 2-minute window (GATED).

    rig: a hamradio.rig.Rig for tuning + keying (required for real TX).
    Uses the existing TX safety gate via hamradio.tx.keyed().
    """
    message = f"{call.upper()} {grid.upper()[:4]} {int(power_dbm)}"
    dial = WSPR_DIAL.get(band)
    if not dial:
        return {"error": f"no WSPR dial for band {band}"}
    wav = encode_wav(message, offset_hz=offset_hz, full_window=False)
    if dry_run or not allow_tx or rig is None:
        try:
            os.unlink(wav)
        except OSError:
            pass
        return {"tx": message, "band": band, "dial_hz": dial,
                "offset_hz": offset_hz, "dry_run": True,
                "note": "TX not armed (need allow_tx + TX master switch + rig)"}
    from . import tx as txmod
    rig.set_freq(dial)
    rig.set_mode(DATA_MODE)
    if align:
        wait_window()
    try:
        with txmod.keyed(rig, allow_tx=allow_tx, timeout=int(CYCLE) + 5) as k:
            if k.get("dry_run"):
                return {"tx": message, "dry_run": True}
            audiomod.play_wav(wav)
        return {"tx": message, "band": band, "dial_hz": dial,
                "offset_hz": offset_hz, "sent": True}
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass


# --- who spots us (wsprnet.org) --------------------------------------------
def who_spots(call: str = "KD9NWA", minutes: int = 60,
              timeout: float = 30.0) -> dict:
    """Query wsprnet.org for recent spots OF `call` (who heard us on WSPR)."""
    # wsprnet's old query API: returns rows; we ask for spots where tx=call.
    url = ("https://www.wsprnet.org/olddb?mode=html&band=all&limit=200"
           f"&findcall={urllib.parse.quote(call)}&findreporter=&sort=date")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hamradio/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode(errors="replace")
    except Exception as e:
        return {"error": str(e), "spots": []}
    # crude table parse: rows contain date, call, freq, snr, ... reporter, ...
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    spots = []
    for r in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S | re.I)]
        if len(cells) >= 8 and call.upper() in " ".join(cells).upper():
            spots.append(cells)
    return {"engine": "wsprnet", "callsign": call.upper(),
            "n": len(spots), "rows": spots[:50]}
