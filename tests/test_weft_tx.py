import hashlib
import json
import struct
import wave
from contextlib import contextmanager

import pytest

from hamradio import weft


class FakeRig:
    def __init__(self, freq=14_100_000, mode="USB", power=1.0):
        self.freq = freq
        self.mode = mode
        self.power = power
        self.commands = []

    def get_freq(self):
        return self.freq

    def get_mode(self):
        return self.mode, 2400

    def set_mode(self, mode, pb=None):
        self.commands.append(("mode", mode, pb))
        self.mode = mode

    def _cmd(self, command):
        self.commands.append(("cmd", command))


def package(tmp_path, *, rate=12000, channels=1, width=2, seconds=1.0,
            sample=1000, manifest_updates=None):
    wav = tmp_path / "frame.wav"
    frames = int(rate * seconds)
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        if width == 2:
            raw = struct.pack("<h", sample) * frames * channels
        else:
            raw = bytes([128]) * frames * channels
        out.writeframes(raw)
    manifest = {
        "schema": "radio-ai-weft-tx-v1",
        "wav_sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
        "callsign": "KD9NWA",
        "dial_hz": 14_100_000,
        "audio_offset_hz": 1500,
        "duration_s": seconds,
        "profile": "WEFT-400",
        "occupied_bandwidth_hz": 390,
        "bandwidth_measurement": "99pct-power",
    }
    manifest.update(manifest_updates or {})
    manifest_path = tmp_path / "frame.json"
    manifest_path.write_text(json.dumps(manifest))
    return wav, manifest_path


def test_dry_run_validates_everything_without_mode_change_or_keying(tmp_path, monkeypatch):
    wav, manifest = package(tmp_path)
    rig = FakeRig()
    keyed = []
    monkeypatch.setattr(weft.txmod, "tx_globally_enabled", lambda: False)
    monkeypatch.setattr(weft.txmod, "_check_guards", lambda *a, **k: None)
    monkeypatch.setattr(weft.txmod, "keyed", lambda *a, **k: keyed.append(True))
    result = weft.send(rig, wav, manifest, station_callsign="KD9NWA", dry_run=True)
    assert result["validated"] is True
    assert result["dry_run"] is True
    assert keyed == []
    assert rig.commands == []


@pytest.mark.parametrize("updates, message", [
    ({"wav_sha256": "0" * 64}, "SHA-256"),
    ({"callsign": "W1AW"}, "callsign"),
    ({"audio_offset_hz": 50}, "audio offset"),
    ({"occupied_bandwidth_hz": 0}, "bandwidth"),
    ({"bandwidth_measurement": "estimated"}, "premeasured"),
])
def test_manifest_safety_rejections(tmp_path, updates, message):
    wav, manifest = package(tmp_path, manifest_updates=updates)
    with pytest.raises(weft.WeftRefused, match=message):
        weft.validate_package(wav, manifest, station_callsign="KD9NWA")


def test_malformed_manifest_is_rejected(tmp_path):
    wav, manifest = package(tmp_path)
    manifest.write_text("{broken")
    with pytest.raises(weft.WeftRefused, match="manifest"):
        weft.validate_package(wav, manifest, station_callsign="KD9NWA")


@pytest.mark.parametrize("kwargs, updates, message", [
    ({"rate": 8000}, {}, "12000"),
    ({"channels": 2}, {}, "mono"),
    ({"width": 1}, {}, "16-bit"),
    ({"sample": 32767}, {}, "clipping"),
    ({"seconds": 1.0}, {"duration_s": 2.0}, "duration"),
])
def test_wav_safety_rejections(tmp_path, kwargs, updates, message):
    wav, manifest = package(tmp_path, manifest_updates=updates, **kwargs)
    # Preserve a valid hash when only WAV properties are intentionally changed.
    data = json.loads(manifest.read_text())
    data["wav_sha256"] = hashlib.sha256(wav.read_bytes()).hexdigest()
    data.update(updates)
    manifest.write_text(json.dumps(data))
    with pytest.raises(weft.WeftRefused, match=message):
        weft.validate_package(wav, manifest, station_callsign="KD9NWA")


def test_positive_forward_power_succeeds(tmp_path, monkeypatch):
    wav, manifest = package(tmp_path)
    rig = FakeRig()
    monkeypatch.setattr(weft.txmod, "tx_globally_enabled", lambda: True)
    monkeypatch.setattr(weft.generate, "_play_and_measure", lambda *a: 0.001)
    @contextmanager
    def fake_keyed(rig, **kwargs):
        yield {"freq_hz": rig.freq}
    monkeypatch.setattr(weft.txmod, "keyed", fake_keyed)
    result = weft.send(rig, wav, manifest, station_callsign="KD9NWA", allow_tx=True)
    assert result["sent"] is True
    assert result["fwd_power"] == 0.001
    assert rig.mode == "USB"


def test_zero_forward_power_fails_and_restores_mode(tmp_path, monkeypatch):
    wav, manifest = package(tmp_path)
    rig = FakeRig(power=0)
    monkeypatch.setattr(weft.generate, "_play_and_measure", lambda *a: 0.0)
    monkeypatch.setattr(weft.txmod, "tx_globally_enabled", lambda: True)
    @contextmanager
    def fake_keyed(rig, **kwargs):
        rig._cmd("set_ptt 1")
        try:
            yield {"freq_hz": rig.freq}
        finally:
            rig._cmd("set_ptt 0")
    monkeypatch.setattr(weft.txmod, "keyed", fake_keyed)
    with pytest.raises(weft.WeftRefused, match="forward power"):
        weft.send(rig, wav, manifest, station_callsign="KD9NWA", allow_tx=True)
    assert ("cmd", "set_ptt 0") in rig.commands
    assert rig.mode == "USB"


def test_playback_error_unkeys_and_restores_mode(tmp_path, monkeypatch):
    wav, manifest = package(tmp_path)
    rig = FakeRig()
    monkeypatch.setattr(weft.txmod, "tx_globally_enabled", lambda: True)
    @contextmanager
    def fake_keyed(rig, **kwargs):
        rig._cmd("set_ptt 1")
        try:
            yield {"freq_hz": rig.freq}
        finally:
            rig._cmd("set_ptt 0")
    monkeypatch.setattr(weft.txmod, "keyed", fake_keyed)
    monkeypatch.setattr(weft.generate, "_play_and_measure",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("play failed")))
    with pytest.raises(RuntimeError, match="play failed"):
        weft.send(rig, wav, manifest, station_callsign="KD9NWA", allow_tx=True)
    assert rig.mode == "USB"
    assert ("cmd", "set_ptt 0") in rig.commands


def test_real_send_requires_both_tx_gates(tmp_path, monkeypatch):
    wav, manifest = package(tmp_path)
    monkeypatch.setattr(weft.txmod, "tx_globally_enabled", lambda: False)
    with pytest.raises(weft.WeftRefused, match="master switch"):
        weft.send(FakeRig(), wav, manifest, station_callsign="KD9NWA", allow_tx=True)
    monkeypatch.setattr(weft.txmod, "tx_globally_enabled", lambda: True)
    with pytest.raises(weft.WeftRefused, match="--allow-tx"):
        weft.send(FakeRig(), wav, manifest, station_callsign="KD9NWA")
