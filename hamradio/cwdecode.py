"""
hamradio.cwdecode — a from-scratch, robust CW (Morse) decoder.

Why not just multimon-ng? On real off-air CW multimon-ng produces garbage the
moment there is fading (QSB), speed variation, or a noisy envelope. This module
does proper DSP:

  1. Goertzel/FFT band-pass around the detected CW note (auto-pitched).
  2. Envelope detection (magnitude) + heavy smoothing at the *keying* rate.
  3. Schmitt-trigger (hysteresis) on/off slicing with an adaptive threshold so
     small ripples don't produce spurious elements.
  4. Timing histogram: cluster key-DOWN runs into dit/dah and key-UP runs into
     element/letter/word gaps using k-means-ish 1-D clustering; this makes the
     decoder self-adapt to the sender's speed (WPM) and even track drift.
  5. Map dit/dah sequences -> characters via the standard Morse table.

Returns the decoded text plus diagnostics (WPM estimate, tone, SNR, confidence)
so callers can judge quality and tune.
"""
from __future__ import annotations
import wave
import statistics
from typing import Optional
import numpy as np

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/", "-.--.": "(",
    "-.--.-": ")", "---...": ":", "-...-": "=", ".-.-.": "+", "-....-": "-",
    ".--.-.": "@", ".-...": "&", "...-.-": "SK", "-.-.-": "KA", "...-.": "SN",
}


def _read_wav_mono(path: str):
    w = wave.open(path)
    sr = w.getframerate()
    n = w.getnframes()
    ch = w.getnchannels()
    raw = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
    w.close()
    if ch == 2:
        raw = raw.reshape(-1, 2).mean(axis=1)
    return raw, sr


def detect_tone(sig: np.ndarray, sr: int, lo: int = 300, hi: int = 1200):
    """Dominant audio-tone frequency + a crude SNR ratio (peak / median)."""
    spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    freqs = np.fft.rfftfreq(len(sig), 1 / sr)
    band = (freqs >= lo) & (freqs <= hi)
    if not band.any():
        return None, 0.0
    idx = np.argmax(spec[band])
    tone = float(freqs[band][idx])
    peak = spec[band][idx]
    med = np.median(spec[band]) or 1.0
    return tone, float(peak / med)


def _envelope(sig: np.ndarray, sr: int, tone: float, bw: int = 120,
              smooth_ms: float = 8.0) -> np.ndarray:
    """Band-pass around `tone`, take magnitude, smooth at the keying rate."""
    F = np.fft.rfft(sig)
    freqs = np.fft.rfftfreq(len(sig), 1 / sr)
    mask = (freqs > tone - bw) & (freqs < tone + bw)
    Ff = np.zeros_like(F)
    Ff[mask] = F[mask]
    bp = np.fft.irfft(Ff, n=len(sig))
    env = np.abs(bp)
    win = max(1, int(sr * smooth_ms / 1000.0))
    kernel = np.ones(win) / win
    env = np.convolve(env, kernel, "same")
    return env


def _agc(env: np.ndarray, sr: int) -> np.ndarray:
    """Normalize the envelope by its slow local peak (AGC). After this, a fixed
    threshold works even through QSB, because the ON level is ~1.0 everywhere
    the signal is present. Window is long vs a character but short vs a fade."""
    win = max(1, int(sr * 1.0))
    # slow local peak via dilation-like running max (max-pool over window)
    if win >= len(env):
        return env / (np.max(env) or 1.0)
    # running max using a strided reduction then interpolation (fast)
    step = max(1, win // 16)
    idx = np.arange(0, len(env), step)
    peaks = np.array([np.percentile(env[max(0, i - win // 2):i + win // 2 + 1], 95)
                      for i in idx])
    peaks = np.maximum(peaks, np.max(env) * 0.08)  # floor so silence stays low
    local_peak = np.interp(np.arange(len(env)), idx, peaks)
    return env / local_peak


def _schmitt(env: np.ndarray, sr: int) -> np.ndarray:
    """AGC-normalize, then hysteresis-slice with fixed thresholds. Robust to
    both noise and fading."""
    norm = _agc(env, sr)
    hi_th, lo_th = 0.55, 0.35
    # noise gate: absolute presence check against the raw noise floor
    raw_floor = np.percentile(env, 15)
    raw_peak = np.percentile(env, 97)
    present = env > raw_floor + 0.18 * (raw_peak - raw_floor)
    out = np.zeros(len(env), dtype=bool)
    state = False
    for i in range(len(env)):
        v = norm[i]
        if state:
            if v < lo_th or not present[i]:
                state = False
        else:
            if v > hi_th and present[i]:
                state = True
        out[i] = state
    return out


def _runs(state: np.ndarray, sr: int):
    """List of (is_on, duration_ms) runs, dropping sub-millisecond glitches."""
    runs = []
    if len(state) == 0:
        return runs
    cur = state[0]
    start = 0
    for i in range(1, len(state)):
        if state[i] != cur:
            dur = (i - start) / sr * 1000.0
            runs.append([bool(cur), dur])
            cur = state[i]
            start = i
    runs.append([bool(cur), (len(state) - start) / sr * 1000.0])
    # merge glitches (< 8 ms) into neighbours
    merged = []
    for r in runs:
        if merged and r[1] < 8.0:
            merged[-1][1] += r[1]  # absorb tiny run's time; keep prev state
        else:
            merged.append(r)
    return merged


def _split_threshold(values):
    """1-D two-cluster split (Otsu-ish): return the boundary that best
    separates 'short' from 'long' durations."""
    if len(values) < 2:
        return (values[0] if values else 0) * 1.5
    v = sorted(values)
    best_t, best_score = v[0], -1
    for i in range(1, len(v)):
        t = (v[i - 1] + v[i]) / 2
        a = [x for x in v if x <= t]
        b = [x for x in v if x > t]
        if not a or not b:
            continue
        # maximize between-class separation / within-class spread
        sep = (statistics.mean(b) - statistics.mean(a))
        spread = (statistics.pstdev(a) + statistics.pstdev(b)) or 1.0
        score = sep / spread
        if score > best_score:
            best_score, best_t = score, t
    return best_t


def decode(path: str, tone: Optional[float] = None,
           smart: bool = True) -> dict:
    """Decode CW from a WAV file. Returns text + diagnostics.

    smart=True runs the context-aware correction pass (hamradio.cwcorrect):
    re-segments run-together text, snaps near-miss callsigns (validated against
    the local FCC DB) and RST reports, and extracts QSO fields. The raw decode
    is always preserved under 'text'; corrections appear under 'corrected' /
    'fields' / 'corrections'.
    """
    sig, sr = _read_wav_mono(path)
    if tone is None:
        tone, snr = detect_tone(sig, sr)
    else:
        _, snr = detect_tone(sig, sr)
    if not tone:
        return {"decoder": "hamradio.cwdecode", "text": "", "tone_hz": None,
                "snr_ratio": 0.0, "note": "no tone detected"}

    # --- signal-presence gate: reject noise-only captures ---
    # A real CW note has a clear peak well above the surrounding audio band.
    # If the tone barely stands out, there is nothing to decode (emitting a
    # string of random dits from noise is worse than saying "no signal").
    if snr < 25.0:
        return {"decoder": "hamradio.cwdecode", "text": "", "tone_hz": round(tone),
                "snr_ratio": round(snr, 1), "wpm": None, "confidence": 0.0,
                "note": "no CW signal (tone SNR below threshold)"}

    env = _envelope(sig, sr, tone)
    state = _schmitt(env, sr)
    runs = _runs(state, sr)
    ons = [d for k, d in runs if k]
    offs = [d for k, d in runs if not k]
    if len(ons) < 3:
        return {"decoder": "hamradio.cwdecode", "text": "", "tone_hz": round(tone),
                "snr_ratio": round(snr, 1), "note": "too few elements"}

    # Reject implausible keying speeds (noise slices into tiny fake dits -> huge
    # WPM). Real CW is ~5-50 WPM (dit 24-240 ms).
    med_on = statistics.median(ons)
    # median element < 20 ms (i.e. implied dit -> > ~50 WPM) is almost always
    # noise chopped by the slicer, not real keying.
    if med_on < 20:
        return {"decoder": "hamradio.cwdecode", "text": "", "tone_hz": round(tone),
                "snr_ratio": round(snr, 1), "wpm": None, "confidence": 0.0,
                "note": "element timing implausible (likely noise)"}

    # Keying-regularity check: real CW clusters into two tight groups (dit/dah).
    # Noise sliced by the trigger yields a broad, unimodal duration spread.
    # Bimodality = how well a 2-cluster split separates vs the within-spread.
    _th = _split_threshold(ons)
    grpA = [d for d in ons if d <= _th]
    grpB = [d for d in ons if d > _th]
    if len(grpA) >= 2 and len(grpB) >= 2:
        sep = statistics.mean(grpB) - statistics.mean(grpA)
        spread = (statistics.pstdev(grpA) + statistics.pstdev(grpB)) or 1.0
        bimodality = sep / spread
    else:
        bimodality = 3.0 if ons else 0.0  # all one type (e.g. all dits) is fine
    if bimodality < 1.2:
        return {"decoder": "hamradio.cwdecode", "text": "", "tone_hz": round(tone),
                "snr_ratio": round(snr, 1), "wpm": None, "confidence": 0.0,
                "note": "keying not CW-like (low bimodality; likely noise/QRM)"}

    # --- dit/dah split from key-down runs (adaptive) ---
    dd_th = _split_threshold(ons)
    dits = [d for d in ons if d <= dd_th]
    dahs = [d for d in ons if d > dd_th]
    dit = statistics.median(dits) if dits else min(ons)
    dah = statistics.median(dahs) if dahs else dit * 3
    # unit time: prefer dit; sanity-cross-check with dah/3
    unit = dit if dits else dah / 3.0
    wpm = round(1200.0 / unit) if unit else None

    # Final plausibility gate: real amateur CW is ~5-45 WPM. Anything faster is
    # the slicer dicing noise into pseudo-elements.
    if wpm and wpm > 50:
        return {"decoder": "hamradio.cwdecode", "text": "", "tone_hz": round(tone),
                "snr_ratio": round(snr, 1), "wpm": wpm, "confidence": 0.0,
                "note": f"implausible speed {wpm} WPM (likely noise/QRM)"}

    # --- gap classification ---
    # Standard CW gaps: 1 unit (intra-char), 3 (letter), 7 (word). Use robust
    # fixed boundaries at 2x and 5x the unit. `unit` is re-estimated from the
    # *element gaps* themselves when possible (the most common short gap), which
    # tracks weighting/speed better than the dit length alone.
    short_gaps = [d for d in offs if d < unit * 2.0]
    if len(short_gaps) >= 3:
        gap_unit = statistics.median(short_gaps)
        unit = (unit + gap_unit) / 2.0   # blend key-down and key-up unit est.
        wpm = round(1200.0 / unit) if unit else wpm
    letter_th = unit * 2.0   # below -> element gap; above -> letter/word
    lw_th = unit * 5.0       # above -> word gap

    text = []
    symbol = ""
    for k, d in runs:
        if k:
            symbol += "-" if d > dd_th else "."
        else:
            if d < letter_th:           # element gap within a character
                continue
            elif d < lw_th:             # letter gap -> emit character
                if symbol:
                    text.append(MORSE.get(symbol, "#"))
                symbol = ""
            else:                       # word gap -> emit character + space
                if symbol:
                    text.append(MORSE.get(symbol, "#"))
                text.append(" ")
                symbol = ""
    if symbol:
        text.append(MORSE.get(symbol, "#"))
    decoded = " ".join("".join(text).split())  # collapse multi-spaces

    # confidence: blend decodability (few '#') with keying regularity.
    total_syms = decoded.replace(" ", "")
    bad = total_syms.count("#")
    decodability = 1 - bad / max(1, len(total_syms))
    regularity = min(1.0, bimodality / 3.0)
    conf = round(0.7 * decodability + 0.3 * regularity, 2)

    result = {
        "decoder": "hamradio.cwdecode",
        "text": decoded,
        "tone_hz": round(tone),
        "snr_ratio": round(snr, 1),
        "wpm": wpm,
        "dit_ms": round(dit),
        "elements": len(ons),
        "confidence": conf,
    }
    if smart and decoded:
        try:
            from . import cwcorrect
            fcc = None
            try:
                from .location import fcc_lookup as fcc
            except Exception:
                fcc = None
            result.update(cwcorrect.correct(decoded, fcc_lookup=fcc))
        except Exception as e:
            result["correct_error"] = str(e)
    return result
