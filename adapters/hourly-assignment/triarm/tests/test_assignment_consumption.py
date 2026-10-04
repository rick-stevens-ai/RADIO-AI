import hashlib
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assignment import AssignmentError, seal_payload, validate_assignment
from carrier_plan import resolve_carrier
from receiver_assignment import assigned_receivers

MODES = ("key-cw", "audio-cw", "bpsk", "bfsk", "weft", "pilot-sc", "micro-ofdm", "chirp-fountain")


def payload(band="20m"):
    return {
        "schema": "hourly-15-45-assignment-v1",
        "hour_id": "2026-10-04T22Z",
        "run_id": 42,
        "selected_band": band,
        "expires_epoch": 4_000_000_000,
        "assigned_sdrs": [
            {"endpoint": "kiwi.example.net:8073", "receiver_id": "sdr-a", "site": "EN61"},
            {"endpoint": "https://kiwi2.example.net:8074/", "receiver_id": "sdr-b", "site": "FN31"},
        ],
        "source_admission_sha256": "a" * 64,
        "modes": {mode: {"owner": "offline" if mode == "bpsk" else "executor", "checks": ["fresh", "legal", "assigned-sdrs"]} for mode in MODES},
    }


def write_assignment(tmp_path, band="20m"):
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(seal_payload(payload(band))))
    return path


def run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / script), *map(str, args)], text=True, capture_output=True)


def test_assignment_validation_rejects_tamper_expiry_and_frequency_binding(tmp_path):
    assignment = seal_payload(payload())
    assignment["selected_band"] = "40m"
    with pytest.raises(AssignmentError, match="seal"):
        validate_assignment(assignment, now_epoch=100)
    expired = seal_payload({**payload(), "expires_epoch": 99})
    with pytest.raises(AssignmentError, match="expired"):
        validate_assignment(expired, now_epoch=100)
    bad = payload()
    bad["dial_hz"] = 14_074_000
    with pytest.raises(AssignmentError, match="fields|forbidden"):
        seal_payload(bad)


def test_assigned_receivers_are_built_only_from_assignment():
    rows = assigned_receivers(seal_payload(payload()))
    assert [row["candidate_id"] for row in rows] == ["sdr-a", "sdr-b"]
    assert [(row["host"], row["port"]) for row in rows] == [("kiwi.example.net", 8073), ("kiwi2.example.net", 8074)]


@pytest.mark.parametrize("band", ["80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m"])
@pytest.mark.parametrize("mode", ["key-cw", "audio-cw", "bfsk"])
def test_every_selected_band_has_a_mode_specific_legal_carrier(band, mode):
    row = resolve_carrier(band, mode)
    assert row["band"] == band
    assert row["mode"] == mode
    assert row["carrier_hz"] > 0
    if mode == "key-cw":
        assert row["radio_mode"] == "CW"
        assert row["sdr_dial_hz"] == row["carrier_hz"] - 1500
    else:
        assert row["radio_mode"] == "USB"
        assert row["audio_offset_hz"] == 1500
        assert row["sdr_dial_hz"] + row["audio_offset_hz"] == row["carrier_hz"]


def test_triarm_dry_run_consumes_assignment_without_rf_or_writes(tmp_path):
    assignment = write_assignment(tmp_path, "30m")
    state = tmp_path / "must-not-exist.json"
    result = run("run_live_triarm_campaign.py", "--assignment", assignment, "--start-triad", 7, "--max-triads", 1, "--state", state, "--dry-run", "--now-epoch", 100)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["rf_performed"] is False
    assert plan["assignment_sha256"] == hashlib.sha256(assignment.read_bytes()).hexdigest()
    assert plan["assigned_receiver_ids"] == ["sdr-a", "sdr-b"]
    assert set(plan["carriers"]) == {"key-cw", "audio-cw", "bfsk"}
    assert all(row["band"] == "30m" for row in plan["carriers"].values())
    assert not state.exists()
    assert not (ROOT / "tri-arm-live").exists()


@pytest.mark.parametrize("script,args", [
    ("run_sync_pair.py", ["--iteration", "1", "--wpm", "12", "--repeat", "2", "--message", "DE KD9NWA TST"]),
    ("run_playback_sync_pair.py", ["audio-cw", "--iteration", "1"]),
    ("run_playback_sync_pair.py", ["bfsk", "--iteration", "1"]),
])
def test_arm_dry_runs_require_and_consume_assignment(tmp_path, script, args):
    assignment = write_assignment(tmp_path, "17m")
    result = run(script, *args, "--assignment", assignment, "--dry-run", "--now-epoch", 100)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["rf_performed"] is False
    assert plan["selected_band"] == "17m"
    assert plan["assigned_receiver_ids"] == ["sdr-a", "sdr-b"]


def test_missing_assignment_and_unknown_receiver_endpoint_fail_closed(tmp_path):
    missing = run("run_live_triarm_campaign.py", "--dry-run")
    assert missing.returncode != 0
    assert "--assignment" in missing.stderr
    broken = payload()
    broken["assigned_sdrs"][0]["endpoint"] = "user:pass@kiwi.example.net:8073"
    with pytest.raises(AssignmentError):
        assigned_receivers(seal_payload(broken))


def test_cli_carrier_override_cannot_escape_selected_band_plan(tmp_path):
    assignment = write_assignment(tmp_path, "20m")
    key = run("run_sync_pair.py", "--assignment", assignment, "--iteration", 1, "--frequency-hz", 18_085_000, "--dry-run", "--now-epoch", 100)
    playback = run("run_playback_sync_pair.py", "bfsk", "--assignment", assignment, "--iteration", 1, "--carrier-hz", 18_095_000, "--dry-run", "--now-epoch", 100)
    assert key.returncode != 0 and "assignment carrier" in key.stderr
    assert playback.returncode != 0 and "assignment carrier" in playback.stderr


def test_bpsk_is_assignment_bound_but_offline_only(tmp_path):
    assignment = write_assignment(tmp_path)
    result = run("run_offline_bpsk.py", "--assignment", assignment, "--dry-run", "--now-epoch", 100)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "bpsk"
    assert plan["offline_only"] is True
    assert plan["rf_capable"] is False
    assert plan["rf_performed"] is False
    text = (ROOT / "run_offline_bpsk.py").read_text()
    for forbidden in ("ssh", "rigctl", "kiwirecorder", "tx-enable", "allow-tx"):
        assert forbidden not in text


def test_aggregate_no_rf_rehearsal_exercises_all_consumers(tmp_path):
    assignment = write_assignment(tmp_path, "12m")
    before = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file())
    result = run("rehearse_no_rf.py", "--assignment", assignment, "--now-epoch", 100)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rf_performed"] is False
    assert report["selected_band"] == "12m"
    assert len(report["commands"]) == 5
    assert all(row["rf_performed"] is False for row in report["commands"])
    after = sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file())
    assert after == before
