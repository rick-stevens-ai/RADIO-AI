"""
hamradio.audio — capture RX audio from the IC-7300 USB CODEC.

The IC-7300 presents a USB Audio CODEC (playback = mic/data into radio,
capture = received audio out of radio) whenever the radio is powered on. We
capture the RX side for the CW / speech decoders.

We locate the capture device by name ("CODEC"/"IC-7300"/"USB Audio") via
PipeWire/PulseAudio (pactl) and fall back to ALSA (arecord -l). Recording is
done with `arecord` (ALSA) or `pw-cat` (PipeWire) to a WAV at 16 kHz mono,
which is what both whisper.cpp and the CW decoder want.
"""
from __future__ import annotations
import re
import subprocess
import shutil
import tempfile
from typing import Optional

# The IC-7300's USB codec enumerates as a Burr-Brown/TI "USB Audio CODEC".
RADIO_NAME_HINTS = ("IC-7300", "CODEC", "USB Audio", "Burr-Brown",
                    "Texas Instrum", "TI_USB")


def find_capture_source() -> Optional[str]:
    """Return a PulseAudio/PipeWire source (true RX audio FROM the radio).

    IMPORTANT: the codec exposes TWO matching sources -- the real capture
    ('alsa_input...') and a '.monitor' loopback of its playback sink. The
    monitor is what we'd HEAR going INTO the radio (TX audio), NOT the received
    signal. We must pick the real input and explicitly reject '.monitor'.
    """
    if not shutil.which("pactl"):
        return None
    try:
        out = subprocess.check_output(["pactl", "list", "short", "sources"],
                                      text=True, timeout=5)
    except Exception:
        return None
    candidates = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and any(h.lower() in line.lower() for h in RADIO_NAME_HINTS):
            name = cols[1]
            if name.endswith(".monitor"):
                continue                      # skip playback loopback
            candidates.append(name)
    # prefer an explicit alsa_input capture device
    for name in candidates:
        if "alsa_input" in name or ".input" in name:
            return name
    return candidates[0] if candidates else None


def find_playback_sink() -> Optional[str]:
    """Return a PulseAudio/PipeWire sink (audio INTO the radio for TX), or None.

    This is the IC-7300 USB CODEC playback device. Feeding audio here while PTT
    is keyed is how we transmit voice/CW-audio through the radio.
    """
    if not shutil.which("pactl"):
        return None
    try:
        out = subprocess.check_output(["pactl", "list", "short", "sinks"],
                                      text=True, timeout=5)
    except Exception:
        return None
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and any(h.lower() in line.lower() for h in RADIO_NAME_HINTS):
            return cols[1]
    return None


def find_alsa_playback() -> Optional[str]:
    """Return an ALSA playback device string like 'plughw:1,0' for the radio."""
    if not shutil.which("aplay"):
        return None
    try:
        out = subprocess.check_output(["aplay", "-l"], text=True, timeout=5)
    except Exception:
        return None
    for line in out.splitlines():
        if line.startswith("card ") and any(h.lower() in line.lower()
                                            for h in RADIO_NAME_HINTS):
            m = re.search(r"card (\d+):.*device (\d+):", line)
            if m:
                return f"plughw:{m.group(1)},{m.group(2)}"
    return None


def play_wav(wav_path: str) -> None:
    """Play a WAV into the radio's USB codec (TX audio). Blocks until done.

    Prefers the named PipeWire sink; falls back to ALSA. Raises RuntimeError if
    no radio playback device is found (radio off?).
    """
    sink = find_playback_sink()
    if sink and shutil.which("pw-play"):
        subprocess.run(["pw-play", "--target", sink, wav_path],
                       check=True, stderr=subprocess.DEVNULL)
        return
    if sink and shutil.which("paplay"):
        subprocess.run(["paplay", "-d", sink, wav_path],
                       check=True, stderr=subprocess.DEVNULL)
        return
    alsa = find_alsa_playback()
    if alsa:
        subprocess.run(["aplay", "-D", alsa, wav_path],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return
    raise RuntimeError(
        "No IC-7300 audio playback device found (radio off / USB disconnected?)")


def wav_duration(wav_path: str) -> float:
    """Return WAV duration in seconds (via sox soxi), or 0 on error."""
    try:
        out = subprocess.check_output(["soxi", "-D", wav_path], text=True,
                                      timeout=5)
        return float(out.strip())
    except Exception:
        return 0.0


def find_alsa_capture() -> Optional[str]:
    """Return an ALSA capture device string like 'plughw:1,0' for the radio."""
    if not shutil.which("arecord"):
        return None
    try:
        out = subprocess.check_output(["arecord", "-l"], text=True, timeout=5)
    except Exception:
        return None
    # lines look like: card 1: CODEC [USB Audio CODEC], device 0: ...
    for line in out.splitlines():
        if line.startswith("card ") and any(h.lower() in line.lower()
                                            for h in RADIO_NAME_HINTS):
            m = re.search(r"card (\d+):.*device (\d+):", line)
            if m:
                return f"plughw:{m.group(1)},{m.group(2)}"
    return None


def record_wav(seconds: float, out_path: Optional[str] = None,
               rate: int = 16000) -> str:
    """Record `seconds` of radio RX audio to a 16 kHz mono WAV. Returns path.

    Prefers the named PipeWire source; falls back to ALSA plughw. Raises
    RuntimeError with a clear message if no radio audio device is found
    (e.g. radio powered off).
    """
    if out_path is None:
        out_path = tempfile.mktemp(suffix=".wav", prefix="rx_")

    src = find_capture_source()
    if src and shutil.which("pw-record"):
        cmd = ["pw-record", "--target", src, "--rate", str(rate),
               "--channels", "1", "--format", "s16", out_path]
        _run_for(cmd, seconds)
        return out_path
    if src and shutil.which("parecord"):
        cmd = ["parecord", "-d", src, "--rate", str(rate), "--channels", "1",
               "--format", "s16le", out_path]
        _run_for(cmd, seconds)
        return out_path

    alsa = find_alsa_capture()
    if alsa:
        cmd = ["arecord", "-D", alsa, "-f", "S16_LE", "-r", str(rate),
               "-c", "1", "-d", str(int(seconds)), out_path]
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_path

    raise RuntimeError(
        "No IC-7300 audio capture device found. Is the radio powered on and "
        "the USB cable connected? (checked PipeWire sources and `arecord -l`)"
    )


def _run_for(cmd: list[str], seconds: float) -> None:
    """Run a streaming recorder for `seconds` then stop it cleanly."""
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        p.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def list_audio_devices() -> dict:
    """Diagnostic: what audio in/out does the system see (radio present?)."""
    info = {"pulse_source": find_capture_source(),
            "pulse_sink": find_playback_sink(),
            "alsa_capture": find_alsa_capture(),
            "alsa_playback": find_alsa_playback(),
            "sources": [], "sinks": [], "cards": []}
    if shutil.which("pactl"):
        try:
            info["sources"] = subprocess.check_output(
                ["pactl", "list", "short", "sources"], text=True,
                timeout=5).splitlines()
            info["sinks"] = subprocess.check_output(
                ["pactl", "list", "short", "sinks"], text=True,
                timeout=5).splitlines()
        except Exception:
            pass
    if shutil.which("arecord"):
        try:
            info["cards"] = [l for l in subprocess.check_output(
                ["arecord", "-l"], text=True, timeout=5).splitlines()
                if l.startswith("card ")]
        except Exception:
            pass
    return info
