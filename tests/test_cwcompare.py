import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hamradio.cwcompare import (
    EngineResult,
    consensus,
    extract_ggmorse_text,
    normalize_text,
    run_command_engine,
)


def test_normalize_text_strips_decoder_noise_without_inventing_words():
    assert normalize_text("  cq  de  Kd9nwa ### ") == "CQ DE KD9NWA"


def test_ggmorse_parser_extracts_only_decode_payload():
    raw = """[+] Number of channels: 1
[+] Decoding:

CQ CQ DE KD9NWA K

[+] Done
"""
    assert extract_ggmorse_text(raw) == "CQ CQ DE KD9NWA K"


def test_consensus_confirms_repeated_callsign_across_independent_engines():
    rows = [
        EngineResult("deepcw", "ED WA8ZBT TU 5NN TX WA8ZBT TU AC6ZM", 0, 1.0),
        EngineResult("cwformer", "D WA8ZBT TU 5NN X WA8ZBT U AC6ZM", 0, 2.0),
        EngineResult("dsp", "EE5 WA8ZBT TU 5NN TDT WA8ZBT TU AC6ZM", 0, 0.2),
    ]
    got = consensus(rows)
    assert got["verdict"] == "agreement"
    assert got["agreed_callsigns"] == ["AC6ZM", "WA8ZBT"]
    assert got["support"] == 3


def test_consensus_abstains_on_disagreeing_plausible_garbage():
    rows = [
        EngineResult("deepcw", "SHELF WHILE L4LNINM IAMBIAC", 0, 1.0),
        EngineResult("cwformer", "W9KJ0MHILA 8C", 0, 2.0),
        EngineResult("dsp", "S H E L F IT H I L E L AH", 0, 0.2),
    ]
    got = consensus(rows)
    assert got["verdict"] == "uncertain"
    assert got["agreed_callsigns"] == []


def test_consensus_rejects_single_engine_copy():
    rows = [
        EngineResult("deepcw", "CQ DE W1AW", 0, 1.0),
        EngineResult("cwformer", "", 0, 2.0),
        EngineResult("dsp", "", 0, 0.2),
    ]
    assert consensus(rows)["verdict"] == "uncertain"


def test_command_engine_records_timeout(tmp_path):
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"not-used")
    got = run_command_engine(
        "slow", ["python3", "-c", "import time; time.sleep(2)"], wav, timeout=0.05
    )
    assert got.returncode == 124
    assert got.error == "timeout"


def test_repeated_callsigns_requires_two_windows():
    from hamradio.cwcompare import repeated_callsigns

    windows = [
        {"consensus": {"call_support": {"W1AW": ["deepcw", "cwformer"]}}},
        {"consensus": {"call_support": {"W1AW": ["deepcw"], "K9XYZ": ["dsp"]}}},
        {"consensus": {"call_support": {"K9XYZ": ["deepcw"]}}},
    ]
    assert repeated_callsigns(windows) == {"K9XYZ": 2, "W1AW": 2}


def test_extract_json_text_uses_text_field():
    from hamradio.cwcompare import extract_json_text

    assert extract_json_text('noise\n{"decoder":"morseangel","text":"CQ DE W1AW"}\n') == "CQ DE W1AW"


def test_compare_wav_marks_missing_engine_without_crashing(tmp_path):
    from hamradio.cwcompare import compare_wav

    wav = tmp_path / "x.wav"
    wav.write_bytes(b"not-a-real-wave")
    got = compare_wav(wav, engines=("definitely-missing",))
    assert got["consensus"]["verdict"] == "uncertain"
    assert got["engines"][0]["error"] == "unknown engine"


def test_callsign_extractor_rejects_non_callsign_numeric_fragments():
    from hamradio.cwcompare import _callsigns

    assert _callsigns("S55 AB5 5NN 599") == set()
    assert _callsigns("WA8ZBT AC6ZM 5Z4VJ W1AW/4") == {
        "WA8ZBT", "AC6ZM", "5Z4VJ", "W1AW/4"
    }


def test_production_module_contains_no_regex_import_or_calls():
    source = Path(__file__).resolve().parents[1] / "hamradio" / "cwcompare.py"
    text = source.read_text()
    assert "import re" not in text
    assert "re." not in text


def test_compare_wav_rejects_missing_source_before_launching_engines(tmp_path):
    from hamradio.cwcompare import compare_wav

    missing = tmp_path / "missing.wav"
    try:
        compare_wav(missing, engines=("dsp",))
    except FileNotFoundError as exc:
        assert str(missing) in str(exc)
    else:
        raise AssertionError("missing WAV must fail before engine execution")


def test_is_callsign_rejects_digit_leading_signal_report():
    from hamradio.cwcompare import _is_callsign

    assert not _is_callsign("5NN")
    assert _is_callsign("5Z4VJ")


def test_consensus_rejects_correlated_unallocated_garbage():
    rows = [
        EngineResult("a", "CQ DE H3LLO TEST1A CQ1TEST", 0, 0.1),
        EngineResult("b", "H3LLO TEST1A CQ1TEST", 0, 0.1),
    ]
    assert consensus(rows)["verdict"] == "uncertain"


def test_compare_many_rejects_empty_input():
    from hamradio.cwcompare import compare_many

    try:
        compare_many([])
    except ValueError as exc:
        assert "no WAV" in str(exc)
    else:
        raise AssertionError("empty comparison must fail")


def test_dsp_timeout_is_enforced(monkeypatch, tmp_path):
    from hamradio import cwcompare

    wav = tmp_path / "x.wav"
    wav.write_bytes(b"exists")
    sleeper = tmp_path / "slow-python"
    sleeper.write_text("#!/bin/sh\nsleep 2\n")
    sleeper.chmod(0o755)
    monkeypatch.setenv("CW_DSP_PYTHON", str(sleeper))
    got = cwcompare.compare_wav(wav, engines=("dsp",), timeout=0.05)
    assert got["engines"][0]["returncode"] == 124
    assert got["engines"][0]["error"] == "timeout"
