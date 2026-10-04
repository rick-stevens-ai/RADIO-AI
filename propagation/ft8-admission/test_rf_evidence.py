import importlib.util
import math
import pathlib
import struct
import wave

import pytest

HERE = pathlib.Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sweep = load("sweep_ft8")
runner = load("run_hourly")


def make_wav(path, *, seconds=2, rate=12000, channels=1, width=2, tone=None):
    samples = []
    seed = 17
    for i in range(rate * seconds):
        seed = (1103515245 * seed + 12345) & 0x7fffffff
        value = (seed % 201) - 100
        if tone is not None:
            value += 6000 * math.sin(2 * math.pi * tone * i / rate)
        sample = int(value)
        samples.extend([sample] * channels)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        if width == 2:
            wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        else:
            wav.writeframes(bytes((x + 128) & 255 for x in samples))


def test_lane_metric_is_generic_for_selected_ft8_offset(tmp_path):
    wav = tmp_path / "tone.wav"
    make_wav(wav, tone=1800)
    assert sweep.lane_metric(wav, 1800) > 20
    assert sweep.lane_metric(wav, 1200) < 15


@pytest.mark.parametrize("kwargs", [
    {"channels": 2}, {"width": 1}, {"rate": 8000},
])
def test_wav_info_rejects_non_mono_pcm16_12k(tmp_path, kwargs):
    wav = tmp_path / "bad.wav"
    make_wav(wav, **kwargs)
    with pytest.raises(ValueError, match="mono PCM16 12 kHz"):
        sweep.wav_info(wav)


def test_kiwi_filename_provides_sample_zero_utc():
    path=pathlib.Path('20261004T203000Z_14074000_site_usb.wav')
    assert runner.kiwi_capture_start(path)==1791145800.0
    with pytest.raises(ValueError,match='timestamp'):
        runner.kiwi_capture_start(pathlib.Path('capture.wav'))

def test_selects_exact_utc_segment_for_transmitted_slot(tmp_path):
    a=tmp_path/'20261004T203000Z_14074000_site_usb.wav';a.touch()
    b=tmp_path/'20261004T203015Z_14074000_site_usb.wav';b.touch()
    assert runner.select_kiwi_slot([a,b],1791145815.0)==b
    with pytest.raises(ValueError,match='exact Kiwi segment'):
        runner.select_kiwi_slot([a],1791145815.0)


def test_exact_slot_extraction_requires_explicit_timing_and_is_exact_15_seconds(tmp_path):
    wav = tmp_path / "capture.wav"
    make_wav(wav, seconds=30)
    out = tmp_path / "slot.wav"
    meta = {"capture_start_epoch": 1000.0, "sample_rate": 12000}
    runner.extract_tx_slot(wav, out, meta, 1015.0)
    with wave.open(str(out), "rb") as slot:
        assert slot.getnframes() == 15 * 12000
    with pytest.raises(ValueError, match="timing metadata"):
        runner.extract_tx_slot(wav, out, {}, 1015.0)
    with pytest.raises(ValueError, match="not fully contained"):
        runner.extract_tx_slot(wav, out, meta, 1020.0)


def test_jt9_runs_in_an_isolated_working_directory(tmp_path, monkeypatch):
    wav = tmp_path / "capture.wav"
    make_wav(wav)
    seen = {}
    class Result:
        stdout = ""
    def fake_sh(cmd, timeout=0, cwd=None):
        seen.update(cmd=cmd, cwd=cwd)
        return Result()
    monkeypatch.setattr(runner, "sh", fake_sh)
    runner.decode_wav(wav)
    assert pathlib.Path(seen["cwd"]).name.startswith("jt9-isolated-")
    assert pathlib.Path(seen["cmd"][-1]).is_absolute()


def test_clearance_requires_remote_quorum_and_local_receive_only_capture():
    rows = [
        {"available": True, "clear": True, "site_id": "site-a"},
        {"available": True, "clear": True, "site_id": "site-b"},
    ]
    assert runner.pre_tx_clearance(rows, {"available": True, "clear": True}, 2)
    assert not runner.pre_tx_clearance(rows[:1], {"available": True, "clear": True}, 2)
    assert not runner.pre_tx_clearance(rows, {"available": True, "clear": False}, 2)
    assert not runner.pre_tx_clearance(rows, None, 2)


def test_selected_offset_requires_metric_clearance_quorum():
    rows = [
        {"host": "a", "port": 1, "available": True, "lane_metrics_db": {"1200": 20, "1500": 2}},
        {"host": "b", "port": 1, "available": True, "lane_metrics_db": {"1200": 3, "1500": 4}},
        {"host": "c", "port": 1, "available": True, "lane_metrics_db": {"1200": 1, "1500": 18}},
    ]
    offset, clear = runner.choose_clear_offset(rows, [], minimum=2)
    assert offset == 1200
    assert {x["host"] for x in clear} == {"b", "c"}


def test_roster_deduplicates_endpoint_and_site_including_fallback():
    guided = [{"host": "a", "port": 8073, "lat": 1.001, "lon": 2.001, "candidate_id": "a"}]
    fallback = [
        {"host": "a", "port": 8073, "lat": 9, "lon": 9, "candidate_id": "same-endpoint"},
        {"host": "b", "port": 8073, "lat": 1.002, "lon": 2.002, "candidate_id": "same-site"},
        {"host": "c", "port": 8073, "candidate_id": "unknown"},
        {"host": "c", "port": 8074, "candidate_id": "unknown"},
    ]
    roster = runner.merge_unique_sdrs(guided, fallback, 8)
    assert [(x["host"], x["port"]) for x in roster] == [("a", 8073), ("c", 8073)]


def test_local_clearance_uses_remote_gateway(monkeypatch, tmp_path):
    calls=[]
    class Result:
        returncode=0; stdout=''; stderr=''
    def fake_sh(cmd, timeout=0, cwd=None):
        calls.append(cmd)
        if cmd[0]=='scp':
            make_wav(pathlib.Path(cmd[-1]), seconds=15)
        return Result()
    monkeypatch.setattr(runner,'sh',fake_sh)
    monkeypatch.setattr(runner,'sweep_module',lambda: sweep)
    row=runner.local_clearance_capture(tmp_path,14074000,1200)
    assert calls[0][0:3]==['ssh','-n','rpi-gateway']
    remote_script=calls[0][3]
    assert 'cleanup() {' in remote_script
    assert any(c[0]=='scp' for c in calls)
    assert row['receive_only'] is True


def test_admission_needs_two_exact_geographically_distinct_sites_and_no_psk_ttl():
    exact_same_site = [
        {"status": "exact", "host": "a", "port": 1, "lat": 41.000, "lon": -87.000},
        {"status": "exact", "host": "b", "port": 1, "lat": 41.004, "lon": -87.004},
    ]
    decision = runner.admission_result(exact_same_site, reports=[{"receiver": "X"}], now=1000)
    assert decision["status"] == "not-validated"
    assert "valid_until_epoch" not in decision
    distinct = exact_same_site + [
        {"status": "exact", "host": "c", "port": 1, "lat": 35.0, "lon": -80.0},
    ]
    decision = runner.admission_result(distinct, reports=[], now=1000)
    assert decision["status"] == "validated"
    assert decision["exact_site_count"] == 2
    assert decision["valid_until_epoch"] == 4600
