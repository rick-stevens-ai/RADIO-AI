"""
hamradio.psk — a from-scratch BPSK31 (and BPSK63/125) modem: encode + TX,
RX + decode. This is a REAL phase-shift-keying data mode, completely
independent from the pskreporter.py "who hears me" web query (which only
scrapes spot reports). Here we actually modulate and demodulate PSK audio.

------------------------------------------------------------------------------
What is BPSK31?
  A narrow (~31 Hz) keyboard-to-keyboard HF chat mode. Text is coded with
  *Varicode* (variable-length, self-synchronising: every character ends in
  "00" and no character contains "00" internally). Bits drive DIFFERENTIAL
  BPSK at 31.25 symbols/s:
      bit 1 -> keep carrier phase
      bit 0 -> reverse carrier phase (180 deg)
  Phase reversals are shaped by a raised-cosine envelope (the amplitude dips
  to zero through the reversal) so the signal stays ~31 Hz wide with no key
  clicks. Idle = continuous 0 bits (continuous reversals) so the receiver can
  stay locked between characters.

Baud variants (all supported here):
    BPSK31  -> 31.25 baud   (classic, most common)
    BPSK63  -> 62.5  baud   (2x, needs better SNR, more common on VHF/data nets)
    BPSK125 -> 125   baud

------------------------------------------------------------------------------
Encode path (encode_wav / send_psk):
  text -> varicode bits -> differential BPSK symbols -> raised-cosine shaped
  baseband -> upconvert to `tone_hz` audio -> 16-bit WAV -> play into the
  IC-7300 USB codec while PTT is held (gated by hamradio.tx). Data mode PKTUSB.

Decode path (decode / decode_wav):
  audio WAV -> find the PSK carrier (squared-signal FFT peak, robust for BPSK)
  -> complex downconvert to baseband -> matched (symbol-length) filter ->
  symbol-timing recovery (maximise energy over the symbol grid) -> differential
  demod (phase change ~ pi => 0, ~0 => 1) -> Varicode decode -> text.

The decoder self-tunes the carrier within a search window, so you only have to
land the dial roughly on the signal (e.g. tune the waterfall tone near 1000 Hz).
"""
from __future__ import annotations
import wave
import tempfile
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# PSK31 Varicode table (character -> bit string, MSB first). This is the
# canonical PSK31 varicode. Characters are separated on-air by "00".
# ---------------------------------------------------------------------------
# Source: the standard PSK31 varicode (Peter Martinez G3PLX). Only printable
# ASCII shown here; control codes map through too but we mostly care 32..126.
VARICODE = {
    "\x00": "1010101011", "\x01": "1011011011", "\x02": "1011101101",
    "\x03": "1101110111", "\x04": "1011101011", "\x05": "1101011111",
    "\x06": "1011101111", "\x07": "1011111101", "\x08": "1011111111",
    "\x09": "11101111",   "\x0a": "11101",      "\x0b": "1101101111",
    "\x0c": "1011011101", "\x0d": "11111",      "\x0e": "1101110101",
    "\x0f": "1110101011", "\x10": "1011110111", "\x11": "1011110101",
    "\x12": "1110101101", "\x13": "1110101111", "\x14": "1101011011",
    "\x15": "1101101011", "\x16": "1101101101", "\x17": "1101010111",
    "\x18": "1101111011", "\x19": "1101111101", "\x1a": "1110110111",
    "\x1b": "1101010101", "\x1c": "1101011101", "\x1d": "1110111011",
    "\x1e": "1011111011", "\x1f": "1101111111",
    " ": "1",           "!": "111111111",  "\"": "101011111",
    "#": "111110101",   "$": "111011011",  "%": "1011010101",
    "&": "1010111011",  "'": "101111111",  "(": "11111011",
    ")": "11110111",    "*": "101101111",  "+": "111011111",
    ",": "1110101",     "-": "110101",     ".": "1010111",
    "/": "110101111",   "0": "10110111",   "1": "10111101",
    "2": "11101101",    "3": "11111111",   "4": "101110111",
    "5": "101011011",   "6": "101101011",  "7": "110101101",
    "8": "110101011",   "9": "110110111",  ":": "11110101",
    ";": "110111101",   "<": "111101101",  "=": "1010101",
    ">": "111010111",   "?": "1010101111", "@": "1010111101",
    "A": "1111101",     "B": "11101011",   "C": "10101101",
    "D": "10110101",    "E": "1110111",    "F": "11011011",
    "G": "11111101",    "H": "101010101",  "I": "1111111",
    "J": "111111101",   "K": "101111101",  "L": "11010111",
    "M": "10111011",    "N": "11011101",   "O": "10101011",
    "P": "11010101",    "Q": "111011101",  "R": "10101111",
    "S": "1101111",     "T": "1101101",    "U": "101010111",
    "V": "110110101",   "W": "101011101",  "X": "101110101",
    "Y": "101111011",   "Z": "1010101101", "[": "111110111",
    "\\": "111101111",  "]": "111111011",  "^": "1010111111",
    "_": "101101101",   "`": "1011011111",
    "a": "1011",        "b": "1011111",    "c": "101111",
    "d": "101101",      "e": "11",         "f": "111101",
    "g": "1011011",     "h": "101011",     "i": "1101",
    "j": "111101011",   "k": "10111111",   "l": "11011",
    "m": "111011",      "n": "1111",       "o": "111",
    "p": "111111",      "q": "110111111",  "r": "10101",
    "s": "10111",       "t": "101",        "u": "110111",
    "v": "1111011",     "w": "1101011",    "x": "11011111",
    "y": "1011101",     "z": "111010101",  "{": "1010110111",
    "|": "110111011",   "}": "1010110101", "~": "1011010111",
    "\x7f": "1110110101",
}
# reverse map for decoding: bit string -> character
VARICODE_REV = {v: k for k, v in VARICODE.items()}

# Named baud rates.
BAUD = {"BPSK31": 31.25, "BPSK63": 62.5, "BPSK125": 125.0}


def _varicode_bits(text: str) -> list[int]:
    """Encode text to a PSK31 bit stream (list of 0/1).

    Preamble: a run of idle 0s (continuous reversals) lets the RX lock. Each
    character's varicode is followed by "00" as the inter-character gap.
    Postamble: trailing 0s to flush.
    """
    bits: list[int] = []
    bits += [0] * 32                       # preamble idle (reversals)
    for ch in text:
        code = VARICODE.get(ch)
        if code is None:
            code = VARICODE.get(ch.lower())      # best-effort fallback
        if code is None:
            continue
        bits += [int(b) for b in code]
        bits += [0, 0]                     # inter-character separator
    bits += [0] * 32                       # postamble idle
    return bits


# ===========================================================================
# ENCODE / MODULATE
# ===========================================================================
def modulate(text: str, *, mode: str = "BPSK31", tone_hz: float = 1000.0,
             rate: int = 8000, amplitude: float = 0.6) -> np.ndarray:
    """Return float32 audio samples for `text` as differential BPSK.

    Differential encoding: symbol phase = previous phase XOR (bit==0). A 0 bit
    flips the phase 180 deg; a 1 keeps it. Each symbol boundary that FLIPS is
    shaped with a raised-cosine (Hann) amplitude notch to zero -> narrow, click-
    free spectrum (the defining feature of PSK31).
    """
    baud = BAUD.get(mode.upper())
    if baud is None:
        raise ValueError(f"unknown PSK mode {mode!r}; use one of {list(BAUD)}")
    sps = int(round(rate / baud))          # samples per symbol
    bits = _varicode_bits(text)

    # differential phase per symbol: start at 0; flip on every 0 bit.
    phases = np.empty(len(bits), dtype=np.float64)
    cur = 0.0
    for i, b in enumerate(bits):
        if b == 0:
            cur += np.pi                    # reverse
        phases[i] = cur

    # Build a raised-cosine amplitude envelope across each symbol: when the
    # phase changes vs the previous symbol, dip amplitude to 0 at the midpoint
    # so the reversal happens at the zero crossing (classic PSK31 shaping).
    baseband = np.zeros(len(bits) * sps, dtype=np.float64)
    prev_phase = phases[0]
    for i in range(len(bits)):
        ph = phases[i]
        seg = np.full(sps, np.cos(ph))     # BPSK: real +/-1 carrier polarity
        flip = abs(((ph - prev_phase + np.pi) % (2 * np.pi)) - np.pi) > 1e-6
        # cosine amplitude over the symbol; when flipping, notch through zero
        n = np.arange(sps)
        if flip:
            # half-cosine that goes 1 -> 0 -> 1 (dip at symbol start = boundary)
            env = np.abs(np.sin(np.pi * (n + 0.5) / sps))
        else:
            env = np.ones(sps)
        baseband[i * sps:(i + 1) * sps] = seg * env
        prev_phase = ph

    # upconvert to the audio tone
    t = np.arange(len(baseband)) / rate
    carrier = np.cos(2 * np.pi * tone_hz * t)
    audio = baseband * carrier * amplitude
    return audio.astype(np.float32)


def encode_wav(text: str, *, mode: str = "BPSK31", tone_hz: float = 1000.0,
               rate: int = 8000, out_path: Optional[str] = None) -> str:
    """Render `text` to a PSK WAV file (no transmit). Returns the path."""
    audio = modulate(text, mode=mode, tone_hz=tone_hz, rate=rate)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    if out_path is None:
        out_path = tempfile.mktemp(suffix=".wav", prefix="psk_")
    with wave.open(out_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out_path


# ===========================================================================
# DECODE / DEMODULATE
# ===========================================================================
def _read_wav_mono(path: str):
    w = wave.open(path)
    sr = w.getframerate()
    n = w.getnframes()
    ch = w.getnchannels()
    raw = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
    w.close()
    if ch == 2:
        raw = raw.reshape(-1, 2).mean(axis=1)
    if raw.size:
        raw = raw / (np.max(np.abs(raw)) or 1.0)
    return raw, sr


def find_carrier(sig: np.ndarray, sr: int, lo: float = 300.0, hi: float = 2700.0,
                 hint: Optional[float] = None) -> tuple[Optional[float], float]:
    """Locate the BPSK carrier. BPSK's 180-deg reversals suppress the carrier
    line, but SQUARING the signal doubles the phase (0/pi -> 0/2pi=0), so the
    squared signal has a strong line at 2*fc. We FFT |x^2| and pick the peak in
    [2*lo, 2*hi], then halve. Returns (carrier_hz, snr_ratio)."""
    x = sig - np.mean(sig)
    sq = x * x
    sq = sq - np.mean(sq)
    win = np.hanning(len(sq))
    spec = np.abs(np.fft.rfft(sq * win))
    freqs = np.fft.rfftfreq(len(sq), 1 / sr)
    band = (freqs >= 2 * lo) & (freqs <= 2 * hi)
    if not band.any():
        return None, 0.0
    bspec = spec[band]
    bfreq = freqs[band]
    idx = int(np.argmax(bspec))
    fc = float(bfreq[idx]) / 2.0
    peak = bspec[idx]
    med = np.median(bspec) or 1.0
    snr = float(peak / med)
    if hint is not None and abs(fc - hint) > 100:
        # if a hint is given and the squared-peak disagrees a lot, trust hint
        fc = hint
    return fc, snr


def carrier_stability(sig: np.ndarray, sr: int, fc: float,
                      lo: float = 300.0, hi: float = 2700.0) -> float:
    """How steady is the carrier across the capture? A REAL PSK signal holds one
    frequency; band noise produces a squared-FFT 'peak' that wanders window to
    window. Split into thirds, find each third's squared-peak carrier, and
    return the max deviation from `fc` in Hz (small = stable = real signal)."""
    n = len(sig)
    if n < sr:  # < 1s: not enough to judge
        return 0.0
    devs = []
    for k in range(3):
        seg = sig[k * n // 3:(k + 1) * n // 3]
        f, _ = find_carrier(seg, sr, lo=lo, hi=hi)
        if f:
            devs.append(abs(f - fc))
    return max(devs) if devs else 999.0


def _downconvert(sig: np.ndarray, sr: int, fc: float) -> np.ndarray:
    """Complex baseband: multiply by e^-j2pi*fc*t and low-pass (~2x baud)."""
    t = np.arange(len(sig)) / sr
    bb = sig * np.exp(-2j * np.pi * fc * t)
    return bb


def _lowpass(x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    """Zero-phase moving-average low-pass (cheap, no scipy dependency)."""
    win = max(1, int(sr / cutoff))
    if win <= 1:
        return x
    k = np.ones(win) / win
    return np.convolve(x, k, "same")


def demodulate(sig: np.ndarray, sr: int, *, mode: str = "BPSK31",
               fc: Optional[float] = None,
               fc_search: bool = True) -> dict:
    """Demodulate BPSK audio to text + diagnostics.

    Steps: carrier find -> downconvert -> lowpass -> symbol-timing recovery ->
    differential demod -> varicode decode.
    """
    baud = BAUD.get(mode.upper())
    if baud is None:
        raise ValueError(f"unknown PSK mode {mode!r}")
    sps = sr / baud

    snr = 0.0
    if fc is None:
        fc, snr = find_carrier(sig, sr, hint=None)
        if not fc:
            return {"decoder": "hamradio.psk", "text": "", "carrier_hz": None,
                    "snr_ratio": 0.0, "mode": mode.upper(),
                    "note": "no PSK carrier found"}
    else:
        _, snr = find_carrier(sig, sr, hint=fc)

    # --- noise-only gate #1: carrier SNR ---
    # A real PSK carrier makes the squared-signal FFT peak tower over the median
    # (loopback: hundreds-to-thousands). Band noise sits ~5-12. Require a clear
    # peak. (Tuned against live 20/40/80m noise on the IC-7300, 2026.)
    if snr < 25.0:
        return {"decoder": "hamradio.psk", "text": "", "carrier_hz": round(fc, 1),
                "snr_ratio": round(snr, 1), "mode": mode.upper(),
                "note": "no PSK signal (carrier SNR below threshold)"}

    # --- noise-only gate #2: carrier stability ---
    # Even when a noise burst spikes the SNR, its apparent carrier wanders across
    # the capture. A real signal holds one frequency to within a few Hz.
    stab = carrier_stability(sig, sr, fc)
    if stab > 15.0:
        return {"decoder": "hamradio.psk", "text": "", "carrier_hz": round(fc, 1),
                "snr_ratio": round(snr, 1), "mode": mode.upper(),
                "carrier_drift_hz": round(stab, 1),
                "note": "no stable PSK carrier (drift too high; likely noise)"}

    best = None
    # A small carrier refinement search improves lock when the dial is off.
    candidates = [fc]
    if fc_search:
        candidates = [fc + d for d in (-4, -2, -1, 0, 1, 2, 4)]
    for cand in candidates:
        bb = _downconvert(sig, sr, cand)
        bb = _lowpass(bb, sr, baud * 1.2)
        res = _symbol_decode(bb, sps)
        score = res["_score"]
        if best is None or score > best["_score"]:
            res["carrier_hz"] = round(cand, 1)
            best = res

    text = _varicode_decode(best["bits"])
    conf = _confidence(best, text)
    return {
        "decoder": "hamradio.psk",
        "mode": mode.upper(),
        "text": text,
        "carrier_hz": best.get("carrier_hz", round(fc, 1)),
        "snr_ratio": round(snr, 1),
        "baud": baud,
        "symbols": best["n_symbols"],
        "confidence": conf,
    }


def _symbol_decode(bb: np.ndarray, sps: float) -> dict:
    """Recover symbol timing and differential bits from complex baseband.

    Timing: try several sub-sample phase offsets across one symbol; pick the
    offset whose sampled symbol magnitudes are largest & most consistent (the
    on-grid instants carry the energy). Bits: a phase change near pi between
    consecutive symbols = 0; near 0 = 1 (differential BPSK).
    """
    n_sym = int(len(bb) / sps)
    if n_sym < 4:
        return {"bits": [], "n_symbols": 0, "_score": 0.0}

    best = {"_score": -1.0, "bits": [], "n_symbols": 0}
    # integrate over each symbol with a matched (boxcar) filter at each offset
    n_offsets = max(1, int(min(sps, 16)))
    for off in np.linspace(0, sps, n_offsets, endpoint=False):
        syms = np.empty(n_sym, dtype=complex)
        for i in range(n_sym):
            a = int(round(i * sps + off))
            b = int(round(i * sps + off + sps))
            if b > len(bb):
                syms = syms[:i]
                break
            seg = bb[a:b]
            syms[i] = np.mean(seg) if len(seg) else 0
        if len(syms) < 4:
            continue
        mags = np.abs(syms)
        # score: strong, steady symbol energy = good timing
        m = np.mean(mags)
        score = m / (np.std(mags) + 1e-9)
        if score > best["_score"]:
            # differential demod: phase difference between adjacent symbols
            dphi = np.angle(syms[1:] * np.conj(syms[:-1]))
            # bit=1 if phase held (|dphi|<pi/2), bit=0 if reversed
            bits = [1 if abs(d) < (np.pi / 2) else 0 for d in dphi]
            best = {"_score": float(score), "bits": bits,
                    "n_symbols": len(syms)}
    return best


def _varicode_decode(bits: list[int]) -> str:
    """Turn a differential bit stream into text via varicode. Characters are
    delimited by "00"; sync on those boundaries."""
    if not bits:
        return ""
    s = "".join(str(b) for b in bits)
    # split on runs of "00" (the inter-character gap). Collapse longer idle
    # runs too. Each token between gaps is one varicode symbol.
    chars = []
    token = ""
    zero_run = 0
    for b in s:
        if b == "0":
            zero_run += 1
            token += b
        else:
            if zero_run >= 2 and token:
                # emit the token minus the trailing "00" delimiter
                core = token.rstrip("0")
                if core:
                    ch = VARICODE_REV.get(core)
                    if ch is not None:
                        chars.append(ch)
                token = "1"
                zero_run = 0
            else:
                token += b
                zero_run = 0
    # flush last
    core = token.rstrip("0")
    if core:
        ch = VARICODE_REV.get(core)
        if ch is not None:
            chars.append(ch)
    return "".join(chars)


def _confidence(res: dict, text: str) -> float:
    """Blend timing score with the fraction of the bitstream that decoded to
    valid varicode characters."""
    n = res.get("n_symbols", 0)
    if not n or not text:
        return 0.0
    printable = sum(1 for c in text if 32 <= ord(c) < 127 or c in "\r\n")
    frac = printable / max(1, len(text))
    timing = min(1.0, res.get("_score", 0) / 6.0)
    return round(0.6 * frac + 0.4 * timing, 2)


def decode(path: str, *, mode: str = "BPSK31",
           fc: Optional[float] = None) -> dict:
    """Decode PSK from a WAV file. Returns text + diagnostics."""
    sig, sr = _read_wav_mono(path)
    if sig.size == 0:
        return {"decoder": "hamradio.psk", "text": "", "note": "empty audio"}
    return demodulate(sig, sr, mode=mode, fc=fc)


def decode_windows(path: str, *, mode: str = "BPSK31", window_s: float = 6.0,
                   hop_s: float = 4.0, fc: Optional[float] = None) -> list:
    """Decode a long capture in overlapping windows (like the CW decoder), so a
    single carrier that fades in/out is still copied. Returns per-window dicts
    with a real signal only."""
    sig, sr = _read_wav_mono(path)
    win = int(sr * window_s)
    hop = int(sr * hop_s)
    results = []
    for i in range(0, max(1, len(sig) - win + 1), hop):
        seg = sig[i:i + win]
        r = demodulate(seg, sr, mode=mode, fc=fc)
        if not r.get("note") and r.get("text", "").strip():
            r["t_offset_s"] = round(i / sr, 1)
            results.append(r)
    return results
