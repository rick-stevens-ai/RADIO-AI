"""Safety-gated adapter for externally generated WEFT transmit WAV packages.

This module does not generate protocol waveforms.  It accepts only a WAV plus a
machine-readable measurement manifest, validates both before touching the rig,
and reuses the station's established TX gate, data-mode, playback, and forward-
power measurement paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import shutil
import subprocess
import threading
import time
import wave
from typing import Any

from . import generate
from . import tx as txmod

SAMPLE_RATE = 12_000
MAX_DURATION_S = 300.0
MIN_AUDIO_HZ = 300.0
MAX_AUDIO_HZ = 2_700.0
POWER_EPSILON_W = 0.0
PROFILE_BANDWIDTH_HZ = {
    "WEFT-200": 200.0,
    "WEFT-400": 400.0,
    "WEFT-500": 500.0,
}
REQUIRED_FIELDS = {
    "schema", "wav_sha256", "callsign", "dial_hz", "audio_offset_hz",
    "duration_s", "profile", "occupied_bandwidth_hz", "bandwidth_measurement",
}


class WeftRefused(txmod.TxRefused):
    """A package or safety precondition prevented WEFT transmission."""


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeftRefused(f"manifest {name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise WeftRefused(f"manifest {name} must be finite")
    return value


def _load_manifest(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WeftRefused(f"invalid manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise WeftRefused("manifest must be a JSON object")
    missing = sorted(REQUIRED_FIELDS - value.keys())
    if missing:
        raise WeftRefused("manifest missing required fields: " + ", ".join(missing))
    if value["schema"] != "radio-ai-weft-tx-v1":
        raise WeftRefused("unsupported manifest schema")
    return value


def validate_package(wav_path: str | pathlib.Path,
                     manifest_path: str | pathlib.Path, *,
                     station_callsign: str) -> dict[str, Any]:
    """Validate immutable waveform bytes and all RF-relevant metadata."""
    wav_path = pathlib.Path(wav_path)
    manifest_path = pathlib.Path(manifest_path)
    if wav_path.suffix.lower() != ".wav":
        raise WeftRefused("waveform must be a .wav file")
    try:
        wav_bytes = wav_path.read_bytes()
    except OSError as exc:
        raise WeftRefused(f"cannot read WAV: {exc}") from exc
    manifest = _load_manifest(manifest_path)

    digest = hashlib.sha256(wav_bytes).hexdigest()
    expected = manifest["wav_sha256"]
    if not isinstance(expected, str) or expected.lower() != digest:
        raise WeftRefused("WAV SHA-256 does not match manifest")

    call = manifest["callsign"]
    station = station_callsign.strip().upper()
    if not isinstance(call, str) or call.strip().upper() != station:
        raise WeftRefused("manifest callsign does not match station identity")
    if not station or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/" for c in station):
        raise WeftRefused("station callsign is invalid")

    profile = manifest["profile"]
    if profile not in PROFILE_BANDWIDTH_HZ:
        raise WeftRefused("manifest profile must be WEFT-200, WEFT-400, or WEFT-500")
    bandwidth = _number(manifest["occupied_bandwidth_hz"], "occupied_bandwidth_hz")
    if bandwidth <= 0 or bandwidth > PROFILE_BANDWIDTH_HZ[profile]:
        raise WeftRefused("measured occupied bandwidth exceeds profile bandwidth")
    measurement = manifest["bandwidth_measurement"]
    max_swr = _number(manifest.get("max_swr", 2.0), "max_swr")
    max_alc = _number(manifest.get("max_alc", 1.0), "max_alc")
    max_forward_power = _number(manifest.get("max_forward_power_w", 10.0), "max_forward_power_w")
    if max_swr < 1.0 or max_alc < 0.0 or max_forward_power <= 0.0:
        raise WeftRefused("manifest SWR/ALC limits are invalid")
    if measurement not in ("99pct-power", "99%-power"):
        raise WeftRefused("premeasured 99%-power bandwidth metadata is required")

    dial_hz = _number(manifest["dial_hz"], "dial_hz")
    if not dial_hz.is_integer():
        raise WeftRefused("manifest dial_hz must be an integer")
    dial_hz = int(dial_hz)
    offset = _number(manifest["audio_offset_hz"], "audio_offset_hz")
    half_bw = bandwidth / 2.0
    if offset - half_bw < MIN_AUDIO_HZ or offset + half_bw > MAX_AUDIO_HZ:
        raise WeftRefused("audio offset plus measured bandwidth is outside 300..2700 Hz")
    if not txmod.freq_tx_ok(dial_hz) or not txmod.freq_tx_ok(int(dial_hz + offset + half_bw)):
        raise WeftRefused("frequency and occupied signal are outside TX-allowed bounds")

    try:
        with wave.open(str(wav_path), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
            compression = wav.getcomptype()
            pcm = wav.readframes(frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise WeftRefused(f"invalid WAV: {exc}") from exc
    if compression != "NONE":
        raise WeftRefused("WAV must contain uncompressed PCM")
    if channels != 1:
        raise WeftRefused("WAV must be mono")
    if width != 2:
        raise WeftRefused("WAV must be 16-bit PCM")
    if rate != SAMPLE_RATE:
        raise WeftRefused("WAV sample rate must be 12000 Hz")
    duration = frames / float(rate)
    declared_duration = _number(manifest["duration_s"], "duration_s")
    if duration <= 0 or duration > MAX_DURATION_S:
        raise WeftRefused(f"WAV duration must be >0 and <= {MAX_DURATION_S:g} seconds")
    if abs(duration - declared_duration) > (1.0 / rate):
        raise WeftRefused("WAV duration does not match manifest duration")
    if len(pcm) != frames * 2:
        raise WeftRefused("WAV PCM data is truncated")
    samples = memoryview(pcm).cast("h")
    peak = max((abs(int(sample)) for sample in samples), default=0)
    if peak >= 32767:
        raise WeftRefused("WAV contains clipping/full-scale samples")

    return {
        "validated": True,
        "wav": str(wav_path),
        "manifest": str(manifest_path),
        "wav_sha256": digest,
        "callsign": station,
        "dial_hz": dial_hz,
        "audio_offset_hz": offset,
        "duration_s": duration,
        "profile": profile,
        "occupied_bandwidth_hz": bandwidth,
        "peak_pcm": peak,
        "max_swr_limit": max_swr,
        "max_alc_limit": max_alc,
        "max_forward_power_w": max_forward_power,
    }


def _start_player(wav: str):
    sink = generate.audiomod.find_playback_sink()
    if sink and shutil.which("pw-play"):
        command = ["pw-play", "--target", sink, wav]
    elif sink and shutil.which("paplay"):
        command = ["paplay", "-d", sink, wav]
    else:
        alsa = generate.audiomod.find_alsa_playback()
        if not alsa:
            raise WeftRefused("no IC-7300 playback device found")
        command = ["aplay", "-D", alsa, wav]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _stop_player(player) -> None:
    if player.poll() is not None:
        return
    player.terminate()
    try:
        player.wait(timeout=1)
    except subprocess.TimeoutExpired:
        player.kill()
        player.wait(timeout=1)


def _play_and_monitor(rig, wav: str, *, max_swr: float,
                      max_alc: float, max_forward: float):
    """Play audio while one CAT reader enforces every RF safety threshold."""
    samples = []
    missing = []
    player = _start_player(wav)
    try:
        while True:
            forward_w = rig.get_fwd_power()
            swr = rig.get_swr()
            alc = rig.get_alc()
            if forward_w is None or swr is None or alc is None:
                missing.append(True)
                _stop_player(player)
                raise WeftRefused("SWR/ALC telemetry unavailable")
            row = {
                "monotonic_s": time.monotonic(),
                "forward_power_w": forward_w,
                "swr": swr,
                "alc": alc,
            }
            samples.append(row)
            if forward_w > max_forward:
                _stop_player(player)
                raise WeftRefused(
                    f"forward power exceeded manifest limit: {forward_w}")
            if swr > max_swr:
                _stop_player(player)
                raise WeftRefused(f"SWR exceeded manifest limit: {swr}")
            if alc > max_alc:
                _stop_player(player)
                raise WeftRefused(f"ALC exceeded manifest limit: {alc}")
            if player.poll() is not None:
                if player.poll() != 0:
                    raise RuntimeError(f"audio player exited {player.poll()}")
                break
            time.sleep(0.25)
    finally:
        _stop_player(player)
    return samples, missing


def send(rig, wav_path: str | pathlib.Path, manifest_path: str | pathlib.Path, *,
         station_callsign: str, allow_tx: bool = False,
         dry_run: bool = False) -> dict[str, Any]:
    """Validate and, only through both gates, play a WEFT waveform once."""
    package = validate_package(wav_path, manifest_path,
                               station_callsign=station_callsign)
    timeout = min(txmod.TX_HARD_CEILING, max(1, math.ceil(package["duration_s"]) + 5))
    if int(rig.get_freq()) != package["dial_hz"]:
        raise WeftRefused("rig frequency does not match manifest dial_hz")
    if dry_run:
        # Validation-only rehearsal: never require or enter the live TX gates.
        # Static frequency/duration checks were already enforced above.
        return {**package, "dry_run": True, "would_key_for_s": timeout}
    if not txmod.tx_globally_enabled():
        raise WeftRefused("TX master switch is off")
    if not allow_tx:
        raise WeftRefused("--allow-tx is required for real transmission")
    txmod._check_guards(package["dial_hz"], allow_tx, timeout, dry_run=False)

    original_mode = original_pb = None
    changed = False
    try:
        original_mode, original_pb, changed = generate._ensure_data_mode(rig)
        with txmod.keyed(rig, allow_tx=True, timeout=timeout) as keyed:
            safety_samples, missing_telemetry = _play_and_monitor(
                rig, str(wav_path),
                max_swr=package["max_swr_limit"],
                max_alc=package["max_alc_limit"],
                max_forward=package["max_forward_power_w"],
            )
        if missing_telemetry or not safety_samples:
            raise WeftRefused("SWR/ALC telemetry unavailable")
        forward_power = max(row["forward_power_w"] for row in safety_samples)
        if forward_power <= POWER_EPSILON_W:
            raise WeftRefused("no positive forward power measured; transmission failed")
        if forward_power > package["max_forward_power_w"]:
            raise WeftRefused(f"forward power exceeded manifest limit: {forward_power}")
        max_swr = max(row["swr"] for row in safety_samples)
        max_alc = max(row["alc"] for row in safety_samples)
        if max_swr > package["max_swr_limit"]:
            raise WeftRefused(f"SWR exceeded manifest limit: {max_swr}")
        if max_alc > package["max_alc_limit"]:
            raise WeftRefused(f"ALC exceeded manifest limit: {max_alc}")
        return {
            **package,
            "dry_run": False,
            "sent": True,
            "freq_hz": keyed.get("freq_hz"),
            "fwd_power": forward_power,
            "max_swr": max_swr,
            "max_alc": max_alc,
            "safety_samples": safety_samples,
            "tx_mode": rig.get_mode()[0],
        }
    finally:
        # keyed() unkeys first; this extra call covers failures before/during
        # context entry and keeps the adapter fail-safe if dependencies change.
        txmod.watchdog_unkey(rig)
        if changed and original_mode:
            rig.set_mode(original_mode, original_pb)
