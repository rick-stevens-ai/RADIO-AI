"""
hamradio.rtty — a from-scratch RTTY (Baudot/ITA2 over AFSK) modem: encode + TX,
RX + decode. RTTY is the classic two-tone FSK keyboard mode.

------------------------------------------------------------------------------
What is RTTY?
  RTTY transmits text as **Baudot (ITA-2)** 5-bit codes, framed asynchronously:

      start(space) + 5 data bits (LSB first) + parity + stop(mark, 1.5 bits)

  Each bit is a tone:  1 = MARK (higher tone),  0 = SPACE (lower tone).
  The standard SSB/AFSK pair is MARK=1700 Hz, SPACE=1530 Hz (170 Hz shift),
  45.45 baud. (CW RTTY uses +/-85 Hz around the carrier instead.)

  Baudot is a *shift* code: two 32-entry tables.
    LETTERS state -> A-Z, space, CR, LF
    FIGURES state -> 0-9 and punctuation (US-TTY set)
  Two codes are reserved for shifting:
    11111 (0x1F) = LETTERS (LS)
    11011 (0x1B) = FIGURES (FS)
  The transmitter sends a shift code whenever the character it needs lives in
  the other table; the receiver tracks the state.

  Canonical tables + frame + parity taken from fldigi 4.2.12 (src/rtty/rtty.cxx,
  src/rtty/fsk.cxx) — the de-facto RTTY reference. Parity "odd" means
  parity_bit = popcount(5 data bits) & 1  (i.e. total marks over data+parity
  is even — the RTTY convention).

------------------------------------------------------------------------------
Encode path (encode_wav / send_rtty):
  text -> Baudot codes (with LETTERS/FIGURES shifts) -> bit cells (LSB first,
  + parity) -> AFSK mark/space tones -> 16-bit WAV -> play into the IC-7300 USB
  codec in PKTUSB while PTT is held (gated by hamradio.tx). Verify forward
  power. This is standard SSB RTTY (AFSK 1700/1530).

Decode path (decode / demodulate):
  audio -> find the two tones (FFT peaks; higher = mark) -> per-bit-cell
  mark-vs-space energy (DFT magnitude at each tone) -> bit decisions ->
  frame recovery (start=space, 5 data, parity check, stop=mark) ->
  Baudot decode (track LETTERS/FIGURES) -> text.

  The decoder auto-finds the two tones within the audio passband, auto-detects
  the baud from a small candidate set, and auto-detects parity (odd/even) by
  whichever yields the fewest parity errors. So you only have to land the dial
  roughly on the signal.
"""
from __future__ import annotations
import wave
import tempfile
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Canonical Baudot / ITA-2 tables (from fldigi 4.2.12, US-TTY figures case).
# Index = 5-bit code value (0..31). Value = character in that state.
# ---------------------------------------------------------------------------
LETTERS = [
    #  0    1    2     3    4    5    6    7
    "\0", "E", "\n", "A", " ", "S", "I", "U",
    #  8    9    10   11   12   13   14   15
    "\r", "D", "R", "J", "N", "F", "C", "K",
    # 16   17   18   19   20   21   22   23
    "T", "Z", "L", "W", "H", "Y", "P", "Q",
    # 24   25   26   27   28   29   30   31
    "O", "B", "G", " ", "M", "X", "V", " ",
]
FIGURES = [
    #  0    1    2     3    4    5    6    7
    "\0", "3", "\n", "-", " ", "\a", "8", "7",
    #  8    9    10   11   12   13   14   15
    "\r", "$", "4", "'", ",", "!", ":", "(",
    # 16   17   18   19   20   21   22   23
    "5", '"', ")", "2", "#", "6", "0", "1",
    # 24   25   26   27   28   29   30   31
    "9", "?", "&", " ", ".", "/", ";", " ",
]
# reserved shift codes
LS_CODE = 0x1F    # 11111 -> LETTERS
FS_CODE = 0x1B    # 11011 -> FIGURES
SPACE_CODE = 0x04 # 00100 -> space (maps to ' ' in both tables)

LS = "LETTERS"
FS = "FIGURES"

# reverse lookup: char -> (code, state)  (letters take precedence)
def _build_reverse():
    rev = {}
    for i, ch in enumerate(LETTERS):
        if ch and ch not in rev:
            rev[ch] = (i, LS)
    for i, ch in enumerate(FIGURES):
        if ch and ch not in rev:
            rev[ch] = (i, FS)
    return rev
CHAR_REV = _build_reverse()

# characters that are only ever in FIGURES (digits + US-TTY punctuation)
FIG_ONLY = {ch for ch, (c, s) in CHAR_REV.items() if s == FS}


# ---------------------------------------------------------------------------
# Parity (fldigi convention). "odd" is the RTTY default.
# ---------------------------------------------------------------------------
def _popcount(n: int) -> int:
    n &= 0x1F
    return bin(n).count("1")

def parity_bit(code5: int, kind: str) -> int:
    kind = (kind or "odd").lower()
    if kind in ("none", "0", "zero"):
        return 0
    if kind in ("1", "one"):
        return 1
    odd = _popcount(code5) & 1
    if kind in ("odd",):
        return odd
    if kind in ("even",):
        return 1 - odd
    raise ValueError(f"unknown parity {kind!r}; use odd/even/none")


# ---------------------------------------------------------------------------
# Encode: text -> list of (code5, state) with shift codes inserted
# ---------------------------------------------------------------------------
def encode_chars(text: str) -> list[int]:
    """Return the ordered list of 5-bit code values to transmit (shift codes
    included), tracking the LETTERS/FIGURES state. Unsupported chars -> space.
    """
    out: list[int] = []
    state = LS
    def emit(code: int):
        out.append(code)
    for raw in text:
        ch = raw if raw in ("\r", "\n") else raw.upper()
        if ch == "\n":
            # CRLF: ensure letters, send CR then LF (both in letters table)
            if state != LS:
                emit(LS_CODE); state = LS
            emit(LETTERS.index("\r"))
            emit(LETTERS.index("\n"))
            continue
        if ch in ("\r", " "):
            # both are in the letters table; keep state (space maps in both)
            if ch == "\r":
                if state != LS:
                    emit(LS_CODE); state = LS
                emit(LETTERS.index("\r"))
            else:
                emit(SPACE_CODE)
            continue
        if ch in CHAR_REV:
            code, want = CHAR_REV[ch]
            if want != state:
                emit(FS_CODE if want == FS else LS_CODE)
                state = want
            emit(code)
        else:
            # not in either table -> space
            emit(SPACE_CODE)
    return out


# ===========================================================================
# ENCODE / MODULATE (AFSK)
# ===========================================================================
def modulate(text: str, *, baud: float = 45.45, mark_hz: float = 1700.0,
             space_hz: float = 1530.0, rate: int = 16000,
             parity: str = "odd", stop_bits: float = 1.5,
             idle_s: float = 0.3, amplitude: float = 0.6) -> np.ndarray:
    """Return float32 AFSK audio for `text`.

    Frame per char: start(space,1) + 5 data (LSB first) + parity(1) + stop(mark).
    Bit 1 -> mark_hz, bit 0 -> space_hz. Phase-continuous sine per cell.
    """
    if mark_hz < space_hz:
        raise ValueError("mark tone must be the HIGHER frequency")
    codes = encode_chars(text)
    if not codes:
        codes = [SPACE_CODE]

    sps = rate / baud                     # samples per bit
    # Build a flat list of (tone_hz, n_samples) cells.
    cells: list[tuple[float, int]] = []
    cells.append((space_hz, int(sps * idle_s)))            # idle lead-in (space)
    for code in codes:
        p = parity_bit(code, parity)
        # start (space, 1 bit)
        cells.append((space_hz, int(round(sps))))
        # 5 data bits, LSB first
        for b in range(5):
            bit = (code >> b) & 1
            cells.append((mark_hz if bit else space_hz, int(round(sps))))
        # parity
        cells.append((mark_hz if p else space_hz, int(round(sps))))
        # stop (mark, stop_bits)
        cells.append((mark_hz, int(round(sps * stop_bits))))
    cells.append((space_hz, int(sps * idle_s)))            # idle trail (space)

    # Phase-continuous AFSK: track a running phase so there are no clicks.
    total = sum(n for _, n in cells)
    audio = np.zeros(total, dtype=np.float64)
    ph = 0.0
    idx = 0
    for tone, n in cells:
        if n <= 0:
            continue
        t = np.arange(n)
        inst = 2 * np.pi * tone / rate
        audio[idx:idx + n] = np.sin(ph + inst * t)
        ph = (ph + inst * n) % (2 * np.pi)
        idx += n
    return (audio * amplitude).astype(np.float32)


def encode_wav(text: str, *, baud: float = 45.45, mark_hz: float = 1700.0,
               space_hz: float = 1530.0, rate: int = 16000,
               parity: str = "odd", stop_bits: float = 1.5,
               out_path: Optional[str] = None) -> str:
    """Render `text` to an AFSK RTTY WAV (no transmit). Returns the path."""
    audio = modulate(text, baud=baud, mark_hz=mark_hz, space_hz=space_hz,
                     rate=rate, parity=parity, stop_bits=stop_bits)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    if out_path is None:
        out_path = tempfile.mktemp(suffix=".wav", prefix="rtty_")
    with wave.open(out_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out_path


# ===========================================================================
# DECODE / DEMODULATE (AFSK)
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

def _hilbert(x: np.ndarray) -> np.ndarray:
    n = len(x)
    X = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = 1.0
        h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(X * h).real


def find_tones(sig: np.ndarray, sr: int, lo: float = 100.0,
               hi: float = 3200.0) -> tuple[Optional[float], Optional[float], float]:
    """Find the two FSK tones (full-signal FFT peak pairs). Higher = mark.

    The full-signal FFT resolves the two tones well (each is a strong line);
    the peak may land a few Hz off due to windowing/two-tone interaction, which
    is acceptable for the standard 45.45-baud mode.

    Returns (mark_hz, space_hz, tone_snr) where tone_snr = weaker peak over the
    in-band median (a real AFSK signal has two clear lines).
    """
    x = sig - np.mean(sig)
    if len(x) < 2048:
        return None, None, 0.0
    win = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * win))
    freqs = np.fft.rfftfreq(len(x), 1 / sr)
    band = (freqs >= lo) & (freqs <= hi)
    if band.sum() < 8:
        return None, None, 0.0
    bspec = spec[band].astype(np.float64)
    bfreq = freqs[band]
    med = np.median(bspec) or 1.0
    peaks = []
    for i in range(1, len(bspec) - 1):
        if bspec[i] > 3 * med and bspec[i] >= bspec[i - 1] and bspec[i] >= bspec[i + 1]:
            peaks.append((float(bspec[i]), float(bfreq[i])))
    if len(peaks) < 2:
        return None, None, 0.0
    peaks.sort(reverse=True)
    clusters: list[list] = []
    for amp, f in peaks:
        for c in clusters:
            if abs(c[1] - f) < 30:
                if amp > c[0]:
                    c[0] = amp
                break
        else:
            clusters.append([amp, f])
    clusters.sort(reverse=True)
    if len(clusters) < 2:
        return None, None, 0.0
    (a1, f1), (a2, f2) = clusters[0], clusters[1]
    if abs(f1 - f2) < 20:
        return None, None, 0.0
    return max(f1, f2), min(f1, f2), float(a2 / med)


def _lowpass(x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    """Zero-phase windowed-sinc lowpass (Blackman), FFT convolution."""
    cutoff = max(cutoff, 2.0)
    n_taps = int(sr / cutoff)
    if n_taps % 2 == 0:
        n_taps += 1
    n_taps = max(n_taps, 31)
    n = np.arange(n_taps) - n_taps // 2
    fc = cutoff / sr
    h = np.sinc(2 * fc * n) * np.blackman(n_taps)
    h /= np.sum(h)
    try:
        from scipy.signal import fftconvolve
        return fftconvolve(x, h, mode="same")
    except Exception:
        return np.convolve(x, h, mode="same")


def _soft_decision(sig: np.ndarray, sr: int, mark: float, space: float,
                   baud: float) -> np.ndarray:
    """Per-sample soft decision: + where the MARK tone dominates, - where the
    SPACE tone dominates. Downconvert to each tone and lowpass (narrow enough
    to reject the other tone, wide enough to pass the FSK symbol rate).
    """
    shift = mark - space
    cutoff = max(1.5 / baud, 0.45 * shift)
    t = np.arange(len(sig)) / sr
    mark_env = np.abs(_lowpass(sig * np.exp(-2j * np.pi * mark * t), sr, cutoff))
    space_env = np.abs(_lowpass(sig * np.exp(-2j * np.pi * space * t), sr, cutoff))
    return (mark_env - space_env)


def _baudot_decode(code: int, state: str) -> tuple[str, str]:
    """Decode one 5-bit code in the current shift state -> (char, new_state)."""
    if code == LS_CODE:
        return "", LS
    if code == FS_CODE:
        return "", FS
    if code == SPACE_CODE:
        return " ", state
    table = LETTERS if state == LS else FIGURES
    ch = table[code]
    if ch in ("\0", "\a", " "):
        return (" " if ch == " " else ""), state
    return ch, state


def _frame_scan(cellsoft: np.ndarray, parity: str) -> tuple[int, int, float, str]:
    """Scan a 1D array of per-half-bit soft values for RTTY frames.

    Frame (half-bit indices within a frame): start(0,1) + 5 data(2,3/4,5/...
    /10,11) + parity(12,13) + stop(14,15,16); next frame at index +17.
    cellsoft>0 = mark(1), <0 = space(0).
    Returns (n_chars, n_parity_err, mean_bit_margin, text). The margin is the
    mean |soft| at the sampled bit positions (higher = cleaner alignment).
    """
    n = len(cellsoft)
    out = []
    n_err = 0
    state = LS
    F = 0
    margins = []
    while F + 16 < n:
        if cellsoft[F] > 0:               # start must be SPACE (<0)
            F += 1
            continue
        code = 0
        fm = [abs(cellsoft[F])]
        for b in range(5):
            v = cellsoft[F + 2 + 2 * b]
            code |= (1 if v > 0 else 0) << b
            fm.append(abs(v))
        pv = cellsoft[F + 12]
        par = 1 if pv > 0 else 0
        fm.append(abs(pv))
        if parity != "none" and par != parity_bit(code, parity):
            n_err += 1
            F += 2
            continue
        if cellsoft[F + 14] < 0:        # stop bit must be MARK
            F += 1
            continue
        margins.extend(fm)
        ch, state = _baudot_decode(code, state)
        if ch:
            out.append(ch)
        F += 17
    margin = float(np.mean(margins)) if margins else 0.0
    return len(out), n_err, margin, "".join(out)


def _try_config(soft: np.ndarray, cell: int,
                baud: float, parity: str) -> tuple[int, int, float, str]:
    """Best (n_chars, n_parity_err, margin, text) for a baud/parity over
    sub-half-bit phases. `cell` = half-bit length in samples."""
    if cell < 16 or len(soft) < cell * 34:
        return 0, 99999, 0.0, ""
    best = (0, 99999, 0.0, "")
    nph = max(1, min(12, int(cell) // 3))
    for ph in range(0, max(1, int(cell)), max(1, int(cell) // nph)):
        n_cells = (len(soft) - ph) // cell
        if n_cells < 34:
            continue
        cellsoft = soft[ph:ph + n_cells * cell].reshape(n_cells, cell).mean(axis=1)
        nch, nerr, margin, txt = _frame_scan(cellsoft, parity)
        if (nch, -nerr, margin) > (best[0], -best[1], best[2]):
            best = (nch, nerr, margin, txt)
    return best


def demodulate(sig: np.ndarray, sr: int, *, baud: float = 45.45,
               mark_hz: Optional[float] = None,
               space_hz: Optional[float] = None,
               parity: str = "auto",
               min_snr: float = 3.0) -> dict:
    """Demodulate AFSK RTTY audio to text + diagnostics.

    Auto-finds the two tones, auto-detects baud (standard set) and parity
    (odd/even) by whichever yields the most valid, cleanest decode.
    """
    base: dict = {"decoder": "hamradio.rtty"}
    mark, space = mark_hz, space_hz
    snr = 0.0
    if mark is None or space is None:
        m, s, snr = find_tones(sig, sr)
        if m is None:
            return {**base, "text": "", "note": "no RTTY tones found"}
        mark, space = m, s
        if snr < min_snr:
            return {**base, "mark_hz": round(mark, 1),
                    "space_hz": round(space, 1), "tone_snr": round(snr, 1),
                    "note": f"weak 2-tone signal (tone SNR {snr:.1f} < {min_snr})"}
    base["mark_hz"] = round(mark, 1)
    base["space_hz"] = round(space, 1)
    base["tone_snr"] = round(snr, 1)

    parities = ["odd", "even"] if parity == "auto" else [parity]
    bauds = list(dict.fromkeys([baud, 45.45, 50.0, 75.0, 100.0, 112.5, 150.0]))

    results = []
    for b in bauds:
        # soft decision tuned to this baud: cutoff wide enough for the symbol
        # rate, narrow enough to reject the other tone.
        soft = _soft_decision(sig, sr, mark, space, b)
        cell = int(round(sr / (2 * b)))
        for p in parities:
            nch, nerr, margin, txt = _try_config(soft, cell, b, p)
            results.append((nch, nerr, margin, b, p, txt))

    def _score(r):
        nch, nerr, margin = r[0], r[1], r[2]
        tot = nch + nerr
        has = 1 if nch > 0 else 0
        pr = nch / tot if tot > 0 else -1.0   # parity pass rate (true sig ~1.0, spur ~0.5)
        # margin (bit cleanliness) ranks BEFORE count: a spurious alignment can
        # produce MORE valid-looking frames (in idle regions) than the true one,
        # but its bits sit off-center (lower margin).
        return (has, pr, margin, nch)
    results.sort(key=_score, reverse=True)
    nch, nerr, margin, b, p, txt = results[0]
    base.update({
        "text": txt,
        "baud": b,
        "parity": p,
        "n_chars": nch,
        "n_parity_err": nerr,
        "bit_margin": round(margin, 4),
        "candidates": [(r[0], r[1], r[3], r[4]) for r in results[:4]],
    })
    tot = nch + nerr
    if nch < 3:
        base["note"] = ("no valid RTTY frames "
                        "(parity/shift/baud mismatch or weak signal)")
        return base
    # quality gate: noise produces many "valid" frames with random bits and a
    # low bit margin. A real RTTY signal has clean bits (high margin) and few
    # parity errors. Reject decodes dominated by parity errors or weak bits so
    # we never hallucinate text from noise.
    pr = nch / tot if tot > 0 else 0.0
    if pr < 0.70 or margin < 0.03:
        base["text"] = ""
        base["note"] = (f"rejected weak decode (parity {nch}/{tot} pass, "
                        f"bit margin {margin:.3f}) — likely noise, not clean RTTY")
    return base


def decode(path: str, *, baud: float = 45.45,
           mark_hz: Optional[float] = None,
           space_hz: Optional[float] = None,
           parity: str = "auto") -> dict:
    """Decode RTTY from a WAV file. Returns text + diagnostics."""
    sig, sr = _read_wav_mono(path)
    if sig.size == 0:
        return {"decoder": "hamradio.rtty", "text": "", "note": "empty audio"}
    return demodulate(sig, sr, baud=baud, mark_hz=mark_hz,
                      space_hz=space_hz, parity=parity)
