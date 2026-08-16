"""
hamradio.generate — CW (Morse) and speech GENERATION / transmission.

Two transmit paths, both routed through the hamradio.tx safety gate
(master switch + band-plan guard + fail-safe un-key):

CW:
  * method "rig"  (default, best): use the IC-7300's built-in keyer via hamlib
    send_morse (CI-V). The rig keys itself with precise timing; we only supply
    the text and WPM. rigctld handles PTT. Cleanest, most accurate CW.
  * method "audio": synthesize a CW sidetone WAV and play it into the USB codec
    while we hold PTT (for radios without CI-V keyer, or to send via data mode).

SPEECH:
  * synthesize text to a WAV (piper neural TTS if available, else espeak-ng),
    then key PTT and play the WAV into the radio's USB codec. Always un-keys.

Everything defaults to REFUSE unless allow_tx=True AND the TX master switch is
armed AND the frequency is in a permitted segment. dry_run=True simulates.
"""
from __future__ import annotations
import os
import time
import shutil
import subprocess
import tempfile
import pathlib
from typing import Optional

import numpy as np

from .rig import Rig, RigError
from . import tx as txmod
from . import audio as audiomod

# ---- TTS config ------------------------------------------------------------
TTS_DIR = pathlib.Path(os.path.expanduser("~/radio/tts"))
PIPER_VOICE_DEFAULT = TTS_DIR / "voices" / "en_US-lessac-medium.onnx"


# ===========================================================================
# CW GENERATION
# ===========================================================================
# Modes that actually route USB-codec audio to the transmitter on the IC-7300.
# (Plain USB/LSB transmit the MIC by default; data modes use USB audio.)
DATA_MODES = ("PKTUSB", "PKTLSB", "RTTY", "RTTYR", "USB-D", "DATA-U")


def send_cw(rig: Rig, text: str, *, wpm: int = 20, method: str = "audio",
            tone_hz: int = 700, allow_tx: bool = False,
            timeout: Optional[int] = None, dry_run: bool = False) -> dict:
    """Transmit `text` as Morse code.

    method="audio" (DEFAULT) -> synthesize a CW sidetone and play it into the
                      USB codec while we hold PTT. Works with the standard USB-
                      audio wiring (verified on this IC-7300). Auto-selects a
                      data mode (PKTUSB) so the audio reaches the transmitter.
    method="rig"   -> IC-7300 CI-V keyer (send_morse). Cleaner CW BUT requires
                      "CW Keying via USB"/BK-IN enabled in the radio menu, else
                      the rig ACKs the command but emits nothing. We now VERIFY
                      forward power and report sent=False if it didn't key.
    """
    text = text.upper().strip()
    if not text:
        return {"error": "empty text"}
    # bound key-down time: estimate from length + generous margin
    est = _cw_seconds(text, wpm)
    to = int(timeout if timeout else min(txmod.TX_HARD_CEILING, max(15, est * 2)))

    if method == "rig":
        return _send_cw_rig(rig, text, wpm, allow_tx, to, dry_run, est)
    elif method == "audio":
        return _send_cw_audio(rig, text, wpm, tone_hz, allow_tx, to, dry_run, est)
    return {"error": f"unknown method {method!r}"}


def _ensure_data_mode(rig: Rig) -> tuple[str, int, bool]:
    """Ensure a data mode is active so USB-codec audio reaches the TX.

    Returns (orig_mode, orig_pb, changed). Caller must restore if changed.
    """
    orig_mode, orig_pb = rig.get_mode()
    if orig_mode.upper() in DATA_MODES:
        return orig_mode, orig_pb, False
    rig.set_mode("PKTUSB")
    time.sleep(0.2)
    return orig_mode, orig_pb, True


def _play_and_measure(rig: Rig, wav: str) -> float:
    """Play a WAV into the radio while sampling forward power; return max seen.

    Confirms RF is actually being emitted (0 => audio not reaching the PA:
    check mode is a data mode + IC-7300 'DATA MOD' source = USB + audio gain).
    """
    import threading
    max_fwd = [0.0]
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            f = rig.get_fwd_power()
            if f is not None:
                max_fwd[0] = max(max_fwd[0], f)
            time.sleep(0.25)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    try:
        audiomod.play_wav(wav)
    finally:
        stop.set()
        t.join(timeout=1)
    return max_fwd[0]


def _cw_seconds(text: str, wpm: int) -> float:
    # PARIS standard: 50 units per "PARIS ". Rough estimate of duration.
    unit = 1.2 / wpm
    return max(1.0, len(text) * 6 * unit)


def _send_cw_rig(rig: Rig, text: str, wpm: int, allow_tx: bool, to: int,
                 dry_run: bool, est: float) -> dict:
    # Guard first (raises TxRefused if not permitted). We reuse the gate's
    # checks but keying is done by the rig's send_morse, so we validate then
    # call send_morse directly (rig asserts/releases PTT itself).
    hz = rig.get_freq()
    txmod._check_guards(hz, allow_tx, to, dry_run=dry_run)
    if dry_run:
        return {"dry_run": True, "method": "rig", "text": text, "wpm": wpm,
                "freq_hz": hz, "est_seconds": round(est, 1)}
    # set keyer speed (WPM) if supported; ignore if not
    try:
        rig._cmd(f"set_level KEYSPD {int(wpm)}")
    except RigError:
        pass
    keyed_ok = False
    max_fwd = 0.0
    try:
        rig._cmd("send_morse " + text)
        # VERIFY the rig actually keyed: sample PTT + forward power briefly.
        deadline = time.time() + min(to, est + 1)
        while time.time() < deadline:
            time.sleep(0.3)
            if rig.get_ptt():
                keyed_ok = True
            fwd = rig.get_fwd_power()
            if fwd is not None:
                max_fwd = max(max_fwd, fwd)
            if max_fwd > 0.01:
                keyed_ok = True
    finally:
        time.sleep(0.2)
        txmod.watchdog_unkey(rig)
    if not keyed_ok:
        return {"method": "rig", "text": text, "wpm": wpm, "freq_hz": hz,
                "sent": False, "fwd_power": max_fwd,
                "error": "send_morse accepted but rig did not key / no RF. "
                         "Enable 'CW Keying via USB' / BK-IN on the IC-7300, "
                         "or use method='audio'."}
    return {"method": "rig", "text": text, "wpm": wpm, "freq_hz": hz,
            "est_seconds": round(est, 1), "fwd_power": max_fwd, "sent": True}


def _send_cw_audio(rig: Rig, text: str, wpm: int, tone_hz: int, allow_tx: bool,
                   to: int, dry_run: bool, est: float) -> dict:
    wav = cw_to_wav(text, wpm=wpm, tone_hz=tone_hz)
    orig_mode = orig_pb = None
    changed = False
    try:
        if not dry_run:
            orig_mode, orig_pb, changed = _ensure_data_mode(rig)
        with txmod.keyed(rig, allow_tx=allow_tx, timeout=to, dry_run=dry_run) as k:
            if k.get("dry_run"):
                return {"dry_run": True, "method": "audio", "text": text,
                        "wpm": wpm, "tone_hz": tone_hz, "wav": wav,
                        "tx_mode": "PKTUSB", "est_seconds": round(est, 1)}
            max_fwd = _play_and_measure(rig, wav)
        return {"method": "audio", "text": text, "wpm": wpm,
                "tone_hz": tone_hz, "freq_hz": k.get("freq_hz"),
                "tx_mode": rig.get_mode()[0], "fwd_power": max_fwd,
                "est_seconds": round(est, 1),
                "sent": max_fwd > 0.01,
                **({} if max_fwd > 0.01 else
                   {"warning": "no forward power detected; check ACL/audio gain"})}
    finally:
        if changed and orig_mode:
            rig.set_mode(orig_mode, orig_pb)
        try:
            os.unlink(wav)
        except OSError:
            pass


# Morse table for the audio synth path
_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---",
    "3": "...--", "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.", ".": ".-.-.-", ",": "--..--", "?": "..--..",
    "/": "-..-.", "=": "-...-", "+": ".-.-.", "-": "-....-", "@": ".--.-.",
    ":": "---...", " ": " ",
}


def cw_to_wav(text: str, wpm: int = 20, tone_hz: int = 700, rate: int = 22050,
              out_path: Optional[str] = None) -> str:
    """Render `text` to a CW sidetone WAV (raised-cosine keyed envelope)."""
    import wave
    unit = 1.2 / wpm
    ramp = min(0.005, unit / 4)  # 5ms click-free edges

    def tone(dur):
        n = int(rate * dur)
        t = np.arange(n) / rate
        sig = np.sin(2 * np.pi * tone_hz * t)
        env = np.ones(n)
        r = int(rate * ramp)
        if r > 0 and n > 2 * r:
            edge = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
            env[:r] = edge
            env[-r:] = edge[::-1]
        return sig * env * 0.6

    def sil(dur):
        return np.zeros(int(rate * dur))

    buf = []
    for ch in text.upper():
        code = _MORSE.get(ch)
        if code is None:
            continue
        if code == " ":
            buf.append(sil(unit * 7))
            continue
        for i, sym in enumerate(code):
            buf.append(tone(unit if sym == "." else unit * 3))
            if i < len(code) - 1:
                buf.append(sil(unit))
        buf.append(sil(unit * 3))
    audio = np.concatenate(buf) if buf else sil(0.1)
    audio = (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    if out_path is None:
        out_path = tempfile.mktemp(suffix=".wav", prefix="cw_")
    with wave.open(out_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    return out_path


# ===========================================================================
# SPEECH GENERATION (TTS -> transmit)
# ===========================================================================
def tts_to_wav(text: str, voice: Optional[str] = None,
               out_path: Optional[str] = None) -> dict:
    """Synthesize `text` to a WAV. Prefer piper (neural) else espeak-ng.

    Returns {"wav": path, "engine": ...}.
    """
    if out_path is None:
        out_path = tempfile.mktemp(suffix=".wav", prefix="tts_")
    voice_path = pathlib.Path(voice) if voice else PIPER_VOICE_DEFAULT

    # find piper even when ~/.local/bin isn't on PATH (non-interactive shells)
    piper_bin = shutil.which("piper") or next(
        (str(p) for p in [
            pathlib.Path.home() / ".local/bin/piper",
            pathlib.Path.home() / ".local/share/pipx/venvs/piper-tts/bin/piper",
        ] if p.exists()), None)
    if piper_bin and voice_path.exists():
        p = subprocess.run([piper_bin, "-m", str(voice_path), "-f", out_path],
                           input=text.encode(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        if p.returncode == 0 and os.path.exists(out_path):
            return {"wav": out_path, "engine": f"piper/{voice_path.name}"}

    if shutil.which("espeak-ng"):
        subprocess.run(["espeak-ng", "-w", out_path, text],
                       stderr=subprocess.DEVNULL, check=True)
        return {"wav": out_path, "engine": "espeak-ng"}

    raise RuntimeError("no TTS engine available (piper or espeak-ng)")


def send_speech(rig: Rig, text: str, *, voice: Optional[str] = None,
                allow_tx: bool = False, timeout: Optional[int] = None,
                dry_run: bool = False) -> dict:
    """Synthesize `text` and transmit it as voice through the USB codec.

    Keys PTT via the tx gate, plays the TTS WAV into the radio, always un-keys.
    """
    text = text.strip()
    if not text:
        return {"error": "empty text"}
    synth = tts_to_wav(text, voice=voice)
    wav = synth["wav"]
    dur = audiomod.wav_duration(wav)
    to = int(timeout if timeout else min(txmod.TX_HARD_CEILING, max(15, int(dur) + 5)))
    orig_mode = orig_pb = None
    changed = False
    try:
        if not dry_run:
            orig_mode, orig_pb, changed = _ensure_data_mode(rig)
        with txmod.keyed(rig, allow_tx=allow_tx, timeout=to, dry_run=dry_run) as k:
            if k.get("dry_run"):
                return {"dry_run": True, "text": text, "engine": synth["engine"],
                        "wav": wav, "duration_s": round(dur, 1),
                        "tx_mode": "PKTUSB", "freq_hz": k.get("freq_hz")}
            max_fwd = _play_and_measure(rig, wav)
        return {"text": text, "engine": synth["engine"], "duration_s": round(dur, 1),
                "freq_hz": k.get("freq_hz"), "tx_mode": rig.get_mode()[0],
                "fwd_power": max_fwd, "sent": max_fwd > 0.01,
                **({} if max_fwd > 0.01 else
                   {"warning": "no forward power detected; check data mode + "
                               "IC-7300 audio source/gain"})}
    finally:
        if changed and orig_mode:
            rig.set_mode(orig_mode, orig_pb)
        try:
            os.unlink(wav)
        except OSError:
            pass
