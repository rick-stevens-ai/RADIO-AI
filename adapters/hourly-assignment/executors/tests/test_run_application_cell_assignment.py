import importlib.util
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from assignment_adapters import ALL_MODES, seal_assignment

SPEC = importlib.util.spec_from_file_location("isolated_appcell", HERE / "run_application_cell.py")
assert SPEC is not None and SPEC.loader is not None
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def assignment(tmp_path, *, band="20m", expires=4_000_000_000):
    payload = {
        "schema": "hourly-15-45-assignment-v1",
        "hour_id": "2026-10-04T22Z",
        "run_id": 42,
        "selected_band": band,
        "expires_epoch": expires,
        "assigned_sdrs": [
            {"endpoint": "one.example:8073", "receiver_id": "rx-one", "site": "EN61"},
            {"endpoint": "two.example:8074", "receiver_id": "rx-two", "site": "FN31"},
        ],
        "source_admission_sha256": "a" * 64,
        "modes": {mode: {"owner": "executor", "checks": ["fresh"]} for mode in ALL_MODES},
    }
    path = tmp_path / "assignment.json"
    path.write_text(json.dumps(seal_assignment(payload)))
    return path


@pytest.mark.parametrize("candidate", ["weft", "pilot", "micro", "chirp"])
def test_each_mode_resolves_own_frequency_inside_selected_band(candidate):
    resolved = app.resolve_frequency("20m", candidate)
    assert 14_000_000 <= resolved < 14_350_000
    assert len({app.resolve_frequency("20m", mode) for mode in app.CONFIG}) == 4
    assert resolved != 14_074_000  # FT8 dial is not reused.


def test_assignment_sdrs_are_the_only_runtime_roster(tmp_path):
    valid = app.load_assignment(assignment(tmp_path), now_epoch=1_000)
    assert app.assigned_roster(valid) == [
        {"candidate_id": "rx-one", "host": "one.example", "port": 8073, "site": "EN61"},
        {"candidate_id": "rx-two", "host": "two.example", "port": 8074, "site": "FN31"},
    ]


def test_frequency_flows_to_capture_clearance_and_remote_script():
    frequency = app.resolve_frequency("17m", "pilot")
    capture = app.recorder_command({"candidate_id": "rx", "host": "host", "port": 8073}, "/tmp/out", frequency)
    assert capture[capture.index("-f") + 1] == str(frequency / 1000)
    assert f"freq {frequency}" in app.local_clearance_command(frequency)
    script = app.remote(app.CONFIG["pilot"], 2_000, frequency)
    assert f"rig.set_freq({frequency})" in script
    assert "14074000" not in script


def test_dry_run_validates_assignment_and_has_no_rf_or_writes(tmp_path, monkeypatch, capsys):
    path = assignment(tmp_path)
    monkeypatch.setattr(app.time, "time", lambda: 1_000)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: pytest.fail("process launched"))
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: pytest.fail("process launched"))
    monkeypatch.setattr(sys, "argv", [
        "run_application_cell.py", "micro", "--cycle", "42", "--slot", "3",
        "--deadline-epoch", "2000", "--assignment", str(path), "--dry-run",
    ])
    before = set(tmp_path.rglob("*"))
    assert app.main() == 0
    after = set(tmp_path.rglob("*"))
    plan = json.loads(capsys.readouterr().out)
    assert before == after
    assert plan["rf_authorized"] is False
    assert plan["rf_performed"] is False
    assert plan["selected_band"] == "20m"
    assert plan["frequency_hz"] == app.resolve_frequency("20m", "micro")
    assert [row["candidate_id"] for row in plan["assigned_sdrs"]] == ["rx-one", "rx-two"]


def test_invalid_or_expired_assignment_fails_before_any_process(tmp_path, monkeypatch):
    path = assignment(tmp_path, expires=900)
    monkeypatch.setattr(app.time, "time", lambda: 1_000)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: pytest.fail("process launched"))
    monkeypatch.setattr(app.subprocess, "Popen", lambda *a, **k: pytest.fail("process launched"))
    monkeypatch.setattr(sys, "argv", [
        "run_application_cell.py", "weft", "--cycle", "42", "--slot", "1",
        "--deadline-epoch", "2000", "--assignment", str(path), "--dry-run",
    ])
    with pytest.raises(SystemExit, match="assignment expired"):
        app.main()


def test_requested_deadline_cannot_outlive_assignment(tmp_path, monkeypatch):
    path = assignment(tmp_path, expires=1500)
    monkeypatch.setattr(app.time, "time", lambda: 1_000)
    monkeypatch.setattr(sys, "argv", [
        "run_application_cell.py", "chirp", "--cycle", "42", "--slot", "4",
        "--deadline-epoch", "1501", "--assignment", str(path), "--dry-run",
    ])
    with pytest.raises(SystemExit, match="deadline exceeds assignment expiry"):
        app.main()
