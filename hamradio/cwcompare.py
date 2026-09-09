"""Multi-engine CW comparison with conservative consensus and abstention."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import os
import subprocess
import time
from typing import Iterable, Sequence


_ALLOWED_TEXT = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/+=?.& -")


@dataclass
class EngineResult:
    engine: str
    text: str
    returncode: int
    elapsed_s: float
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_text(text: str) -> str:
    cleaned = []
    for char in text.upper().replace("#", " "):
        cleaned.append(char if char in _ALLOWED_TEXT else " ")
    return " ".join("".join(cleaned).split())


def extract_ggmorse_text(stdout: str) -> str:
    lines = []
    active = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("[+] Decoding:"):
            active = True
            continue
        if line.startswith("[+] Done"):
            break
        if active and line and not line.startswith("[+"):
            lines.append(line)
    return normalize_text(" ".join(lines))


def extract_json_text(stdout: str) -> str:
    """Extract a decoder's final JSON object, tolerating diagnostic lines."""
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return normalize_text(str(payload.get("text", "")))
    return ""


def extract_last_line(stdout: str) -> str:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return normalize_text(lines[-1]) if lines else ""


def _valid_call_part(part: str, *, require_digit: bool) -> bool:
    if not part or len(part) > 8 or not all(char.isalnum() for char in part):
        return False
    if require_digit and not any(char.isdigit() for char in part):
        return False
    return any(char.isalpha() for char in part)


def _is_callsign(token: str) -> bool:
    """Structural and allocation check; credibility still requires agreement."""
    if not 4 <= len(token) <= 12 or token.count("/") > 1:
        return False
    base, sep, suffix = token.partition("/")
    if not _valid_call_part(base, require_digit=True):
        return False
    if sep and (not suffix or len(suffix) > 4 or
                not all(char.isalnum() for char in suffix)):
        return False
    digit_positions = [i for i, char in enumerate(base) if char.isdigit()]
    if not digit_positions:
        return False
    last_digit = digit_positions[-1]
    # Calls need a letter after the district digit; rejects 599/S55/AB5.
    if not any(char.isalpha() for char in base[last_digit + 1:]):
        return False
    # Digit-leading allocations (for example 5Z4VJ) carry another district
    # digit; this excludes shorthand signal reports such as 5NN.
    if base[0].isdigit() and len(digit_positions) < 2:
        return False
    try:
        from . import location
        lookup = location.lookup(base)
    except Exception:
        return False
    if not lookup.get("sources") or lookup.get("note") == "unknown callsign/prefix":
        return False
    return True


def _callsigns(text: str) -> set[str]:
    return {token for token in normalize_text(text).split() if _is_callsign(token)}


def _authoritatively_assigned(call: str) -> bool:
    """True only when the complete call exists in an authoritative local set.

    DXCC prefix matches are deliberately insufficient: they identify a country,
    not whether the complete callsign was ever assigned.
    """
    base = call.partition("/")[0]
    try:
        from . import location
        lookup = location.lookup(base)
    except Exception:
        return False
    return "fcc" in lookup.get("sources", ())


def consensus(results: Iterable[EngineResult]) -> dict:
    rows = [r for r in results if r.returncode == 0 and normalize_text(r.text)]
    call_support: dict[str, set[str]] = {}
    for row in rows:
        for call in _callsigns(row.text):
            call_support.setdefault(call, set()).add(row.engine)
    agreed = sorted(call for call, engines in call_support.items()
                    if len(engines) >= 2 and _authoritatively_assigned(call))
    support = max((len(call_support[call]) for call in agreed), default=0)
    candidates = sorted(call for call, engines in call_support.items()
                        if len(engines) >= 2 and call not in agreed)
    return {
        "verdict": "agreement" if agreed else "uncertain",
        "agreed_callsigns": agreed,
        "unverified_candidates": candidates,
        "support": support,
        "call_support": {k: sorted(v) for k, v in sorted(call_support.items())},
        "rule": "agreement requires two engines plus authoritative full-callsign evidence",
    }


def repeated_callsigns(windows: Iterable[dict]) -> dict[str, int]:
    """Count callsign appearances across independent capture windows."""
    counts: dict[str, int] = {}
    for window in windows:
        calls = set(window.get("consensus", {}).get("call_support", {}))
        for call in calls:
            counts[call] = counts.get(call, 0) + 1
    return {call: count for call, count in sorted(counts.items()) if count >= 2}


def default_engine_commands(wpm: int = 25) -> dict[str, tuple[list[str], object]]:
    """Return configured external engines. Paths can be overridden by env."""
    root = Path(os.environ.get("CW_DECODER_ROOT", Path.home() / "radio/models/cw-decoders"))
    eval_root = Path(os.environ.get("CW_DL_EVAL_ROOT", root / "onnx"))
    onnx_python = os.environ.get("CW_ONNX_PYTHON", str(root / "venv-onnx/bin/python"))
    ma_python = os.environ.get("CW_MORSEANGEL_PYTHON", str(root / "venv-morseangel/bin/python"))
    return {
        "deepcw": ([onnx_python, str(eval_root / "deepcw/decode_morse.py"),
                    "--model", str(eval_root / "deepcw/model.onnx"),
                    "--metadata", str(eval_root / "deepcw/model.onnx.json"),
                    "--wav", "{wav}"], extract_last_line),
        "cwformer": ([onnx_python, str(eval_root / "cwformer/inference_onnx.py"),
                      "--model", str(eval_root / "cwformer/cwformer_streaming_int8.onnx"),
                      "--input", "{wav}"], extract_last_line),
        "ggmorse": ([str(root / "ggmorse/bin/ggmorse-from-file"), "{wav}"],
                     extract_ggmorse_text),
        "morseangel": ([ma_python, str(root / "morseangel/headless.py"),
                        "--wav", "{wav}", "--model",
                        str(root / "morseangel/models/default.model"),
                        "--wpm", str(wpm)], extract_json_text),
    }


def compare_wav(
    wav_path: Path | str,
    engines: Sequence[str] = ("dsp", "deepcw", "cwformer", "ggmorse", "morseangel"),
    *,
    timeout: float = 90.0,
) -> dict:
    """Run independent decoders on identical WAV bytes and compute consensus."""
    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise FileNotFoundError(f"CW source WAV not found: {wav_path}")
    results: list[EngineResult] = []
    dsp_wpm = 25
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if "dsp" in engines:
        started = time.monotonic()
        dsp_root = os.environ.get("CW_DSP_ROOT", str(Path.home() / "radio/agent"))
        command = [
            os.environ.get("CW_DSP_PYTHON", "python3"), "-c",
            "import json,sys; sys.path.insert(0, sys.argv[2]); "
            "from hamradio import cwdecode; "
            "print(json.dumps(cwdecode.decode(sys.argv[1], smart=False)))",
            str(wav_path), dsp_root,
        ]
        dsp_result = run_command_engine(
            "dsp", command, wav_path, timeout=timeout, parser=extract_json_text
        )
        results.append(dsp_result)
        # External models need a WPM hint only; avoid trusting a timed-out DSP.
        dsp_wpm = 25
    commands = default_engine_commands(dsp_wpm)
    for name in engines:
        if name == "dsp":
            continue
        spec = commands.get(name)
        if not spec:
            results.append(EngineResult(name, "", 127, 0.0, "unknown engine"))
            continue
        command, parser = spec
        if not Path(command[0]).exists():
            results.append(EngineResult(name, "", 127, 0.0,
                                        f"engine executable missing: {command[0]}"))
            continue
        results.append(run_command_engine(name, command, wav_path,
                                          timeout=timeout, parser=parser))
    return {
        "source": str(wav_path),
        "engines": [r.as_dict() for r in results],
        "consensus": consensus(results),
    }


def compare_many(
    wav_paths: Iterable[Path | str],
    engines: Sequence[str] = ("dsp", "deepcw", "cwformer", "ggmorse", "morseangel"),
    *,
    timeout: float = 90.0,
) -> dict:
    paths = [Path(path) for path in wav_paths]
    if not paths:
        raise ValueError("no WAV files provided for comparison")
    windows = [compare_wav(path, engines, timeout=timeout) for path in paths]
    return {
        "windows": windows,
        "window_count": len(windows),
        "repeated_callsigns": repeated_callsigns(windows),
        "agreement_windows": sum(w["consensus"]["verdict"] == "agreement" for w in windows),
    }


def run_command_engine(
    name: str,
    command: Sequence[str],
    wav_path: Path | str,
    *,
    timeout: float = 90.0,
    parser=None,
) -> EngineResult:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [part.replace("{wav}", str(wav_path)) for part in command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return EngineResult(name, "", 124, time.monotonic() - started, "timeout")
    text = parser(proc.stdout) if parser else normalize_text(proc.stdout)
    error = proc.stderr.strip() or None if proc.returncode else None
    return EngineResult(name, text, proc.returncode, time.monotonic() - started, error)
