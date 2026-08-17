"""
hamradio.decode — CW (Morse) and speech-to-text decoders over RX audio.

CW:    pipe captured audio through multimon-ng's MORSE_CW decoder. multimon-ng
       expects 22.05 kHz mono raw; we transcode with sox. Good for steady
       machine/keyer CW; hand-sent CW is harder (as always).

SPEECH: run whisper.cpp (CPU build under ~/radio/whisper.cpp) on the captured
       WAV. Uses the base.en model by default; small.en is more accurate but
       slower on this i5. Whisper is for SSB/AM voice — it will produce garbage
       on data/CW, so pick the decoder that matches the mode.

Both take either a pre-recorded WAV or a live capture duration.
"""
from __future__ import annotations
import os
import re
import time
import subprocess
import shutil
import tempfile
import pathlib
from typing import Optional

from . import audio

WHISPER_DIR = pathlib.Path(os.path.expanduser("~/radio/whisper.cpp"))
WHISPER_BIN_CANDIDATES = [
    WHISPER_DIR / "build" / "bin" / "whisper-cli",
    WHISPER_DIR / "build" / "bin" / "main",
    WHISPER_DIR / "main",
]
WHISPER_MODEL_DEFAULT = WHISPER_DIR / "models" / "ggml-base.en.bin"


# ---- CW / Morse ------------------------------------------------------------
def detect_cw_tone(wav_path: str, lo_hz: int = 300, hi_hz: int = 1200):
    """Return (tone_hz, snr_ratio) of the dominant audio tone, or (None, 0).

    Used to (a) auto-center a bandpass filter on the CW note and (b) gauge
    whether there's actually a signal worth decoding.
    """
    try:
        import wave
        import numpy as np
        w = wave.open(wav_path)
        sr = w.getframerate()
        d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
        if len(d) < sr // 2:
            return None, 0.0
        spec = np.abs(np.fft.rfft(d * np.hanning(len(d))))
        frq = np.fft.rfftfreq(len(d), 1 / sr)
        band = (frq > lo_hz) & (frq < hi_hz)
        if not band.any():
            return None, 0.0
        tone = float(frq[band][np.argmax(spec[band])])
        snr = float(np.max(spec[band]) / (np.median(spec[band]) + 1))
        return tone, snr
    except Exception:
        return None, 0.0


def decode_cw(wav_path: Optional[str] = None, seconds: float = 20.0,
              bandpass: bool = True, method: str = "dsp") -> dict:
    """Decode CW from a WAV (or a fresh live capture of `seconds`).

    method="dsp"  -> hamradio.cwdecode, our from-scratch DSP decoder (default).
                     Envelope + AGC + Schmitt-trigger + adaptive dit/dah &
                     gap timing. Far better than multimon-ng on real off-air CW;
                     also reports wpm/confidence.
    method="multimon" -> legacy multimon-ng MORSE_CW path (kept for comparison).
    method="both" -> run both and return each under 'dsp'/'multimon'.
    """
    if wav_path is None:
        wav_path = audio.record_wav(seconds)

    if method in ("dsp", "both"):
        from . import cwdecode
        dsp = cwdecode.decode(wav_path)
        dsp["source"] = wav_path
        if method == "dsp":
            return dsp

    if not shutil.which("multimon-ng"):
        if method == "both":
            return {"dsp": dsp, "multimon": {"error": "multimon-ng not installed"}}
        raise RuntimeError("multimon-ng not installed")

    tone, snr = detect_cw_tone(wav_path)

    # multimon-ng wants 22050 Hz signed 16-bit mono raw on stdin
    raw = tempfile.mktemp(suffix=".raw")
    try:
        sox_cmd = ["sox", wav_path, "-r", "22050", "-e", "signed-integer",
                   "-b", "16", "-c", "1", "-t", "raw", raw]
        if bandpass and tone:
            # narrow band-pass (±120 Hz) centered on the detected note, then a
            # gentle normalize; inserted before the output spec.
            sox_cmd = ["sox", wav_path, "-r", "22050", "-e", "signed-integer",
                       "-b", "16", "-c", "1", "-t", "raw", raw,
                       "bandpass", str(int(tone)), "240", "gain", "-n", "-3"]
        subprocess.run(sox_cmd, check=True, stderr=subprocess.DEVNULL)
        out = subprocess.check_output(
            ["multimon-ng", "-t", "raw", "-a", "MORSE_CW", "-q", raw],
            text=True, stderr=subprocess.DEVNULL)
    finally:
        try:
            os.unlink(raw)
        except OSError:
            pass

    # multimon-ng prints lines like "MORSE_CW: CQ CQ DE KD9NWA"
    decoded = []
    for line in out.splitlines():
        if ":" in line:
            _, _, txt = line.partition(":")
            decoded.append(txt.strip())
        elif line.strip():
            decoded.append(line.strip())
    text = " ".join(decoded).strip()
    mm = {"decoder": "multimon-ng/MORSE_CW", "text": text,
          "raw": out, "source": wav_path,
          "tone_hz": round(tone) if tone else None,
          "snr_ratio": round(snr, 1)}
    if method == "both":
        return {"dsp": dsp, "multimon": mm}
    return {"decoder": "multimon-ng/MORSE_CW", "text": text,
            "raw": out, "source": wav_path,
            "tone_hz": round(tone) if tone else None,
            "snr_ratio": round(snr, 1),
            "signal": "strong" if snr > 40 else "weak" if snr > 12 else "none"}


# ---- FT8 / digital (WSJT-X jt9 decoder) -----------------------------------
# Common dial frequencies (Hz) for FT8 by band (USB).
FT8_DIAL = {
    "160m": 1_840_000, "80m": 3_573_000, "60m": 5_357_000, "40m": 7_074_000,
    "30m": 10_136_000, "20m": 14_074_000, "17m": 18_100_000, "15m": 21_074_000,
    "12m": 24_915_000, "10m": 28_074_000, "6m": 50_313_000, "2m": 144_174_000,
}


def _wait_ft8_boundary(cycle: int = 15) -> None:
    """Sleep until just after the next FT8 cycle boundary (0/15/30/45 s)."""
    now = time.time()
    time.sleep(cycle - (now % cycle) + 0.3)


def decode_ft8(wav_path: Optional[str] = None, seconds: float = 13.5,
               enrich: bool = False,  # annotate callers with location
               align: bool = True) -> dict:
    """Decode FT8 from a WAV (or a fresh live capture) using WSJT-X jt9.

    Live capture aligns to the 15 s FT8 cycle (align=True) then records
    `seconds`. Set the radio to the FT8 dial freq + USB/data first (see
    FT8_DIAL). Returns a list of decoded messages with SNR/DT/freq offset.
    FT8 decodes signals well below the noise floor — the right tool for weak
    digital activity where CW-by-ear fails.
    """
    jt9 = shutil.which("jt9") or "/usr/bin/jt9"
    if not os.path.exists(jt9):
        raise RuntimeError("jt9 (WSJT-X) not found")
    if wav_path is None:
        if align:
            _wait_ft8_boundary()
        wav_path = audio.record_wav(seconds)

    norm = tempfile.mktemp(suffix=".wav")
    try:
        # jt9 wants 12 kHz mono
        subprocess.run(["sox", wav_path, "-r", "12000", "-c", "1", norm],
                       check=True, stderr=subprocess.DEVNULL)
        proc = subprocess.run([jt9, "--ft8", norm], capture_output=True,
                              text=True, timeout=90)
    finally:
        try:
            os.unlink(norm)
        except OSError:
            pass

    decodes = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("<Decode", "EOF")):
            continue
        # format: UTC  SNR  DT  FREQ  ~  MESSAGE
        parts = line.split(None, 5)
        if len(parts) >= 6 and parts[4] == "~":
            try:
                decodes.append({
                    "snr_db": int(parts[1]),
                    "dt_s": float(parts[2]),
                    "freq_offset_hz": int(parts[3]),
                    "message": parts[5].strip(),
                })
            except ValueError:
                decodes.append({"message": line})
    if enrich:
        _enrich_decodes(decodes)
    # pull out stations calling CQ for convenience
    cqs = [d["message"] for d in decodes
           if isinstance(d.get("message"), str) and d["message"].startswith("CQ")]
    return {"decoder": "wsjtx/jt9 FT8", "n_decodes": len(decodes),
            "decodes": decodes, "cq_calls": cqs, "source": wav_path}


def _enrich_decodes(decodes):
    """Annotate each decode with the CALLER's location (country/state/dist).
    The caller in an FT8 msg 'A B GRID' is token B when A is a call/CQ, but the
    station of interest is usually the SENDER: for 'CQ X GRID' it's X; for
    'A B rpt' the transmitter is B. We tag the most informative call + grid."""
    try:
        from . import location as _loc
    except Exception:
        return
    grid_re = re.compile(r"^[A-R]{2}\d\d$")
    for d in decodes:
        m = d.get("message", "")
        toks = m.split() if isinstance(m, str) else []
        call = grid = None
        if len(toks) >= 2 and toks[0] == "CQ":
            call = toks[2] if (len(toks) >= 3 and toks[1] in ("DX", "POTA", "QRP", "TEST")) else toks[1]
            for t in toks:
                if grid_re.match(t):
                    grid = t
        elif len(toks) >= 2:
            call = toks[1]  # transmitter is the 2nd call in 'TO FROM ...'
            for t in toks[2:]:
                if grid_re.match(t):
                    grid = t
        if not call or not re.search(r"\d", call):
            continue
        try:
            info = _loc.lookup(call, grid or "")
            d["loc"] = {k: info[k] for k in
                        ("country", "us_state", "city", "distance_km",
                         "bearing_deg", "is_dx") if k in info}
        except Exception:
            pass


# ---- Speech-to-text (SSB voice) -------------------------------------------
def _whisper_bin() -> Optional[pathlib.Path]:
    for c in WHISPER_BIN_CANDIDATES:
        if c.exists() and os.access(c, os.X_OK):
            return c
    return None


def decode_speech(wav_path: Optional[str] = None, seconds: float = 20.0,
                  model: Optional[str] = None) -> dict:
    """Transcribe SSB/AM voice from a WAV (or fresh capture) via whisper.cpp.

    Returns {"text": ..., "source": wav, "model": ...}.
    """
    wbin = _whisper_bin()
    if not wbin:
        raise RuntimeError(
            "whisper.cpp not built yet (looked in ~/radio/whisper.cpp/build/bin)")
    model_path = pathlib.Path(model) if model else WHISPER_MODEL_DEFAULT
    if not model_path.exists():
        raise RuntimeError(f"whisper model not found: {model_path}")

    if wav_path is None:
        wav_path = audio.record_wav(seconds)

    # whisper.cpp wants 16 kHz mono WAV (our capture already is, but re-norm to
    # be safe against odd sample rates from ALSA fallbacks)
    norm = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(["sox", wav_path, "-r", "16000", "-c", "1", norm],
                       check=True, stderr=subprocess.DEVNULL)
        out = subprocess.check_output(
            [str(wbin), "-m", str(model_path), "-f", norm, "-nt", "-l", "en"],
            text=True, stderr=subprocess.DEVNULL)
    finally:
        try:
            os.unlink(norm)
        except OSError:
            pass

    text = " ".join(l.strip() for l in out.splitlines() if l.strip())
    return {"decoder": f"whisper.cpp/{model_path.name}", "text": text,
            "source": wav_path, "model": str(model_path)}
