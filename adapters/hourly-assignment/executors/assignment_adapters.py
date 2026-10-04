"""Pure sealed-assignment validation and current-executor command planning.

This module performs no file, process, network, timer, radio, or SDR operations.
Generated RF-capable plans are deliberately blocked because the inspected current
executors hard-code their band/roster and cannot yet accept the sealed assignment.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import pathlib
import re
from typing import Any, Mapping

SCHEMA = "hourly-15-45-assignment-v1"
ALL_MODES = (
    "key-cw",
    "audio-cw",
    "bpsk",
    "bfsk",
    "weft",
    "pilot-sc",
    "micro-ofdm",
    "chirp-fountain",
)

_PAYLOAD_FIELDS = {
    "schema",
    "hour_id",
    "run_id",
    "selected_band",
    "expires_epoch",
    "assigned_sdrs",
    "source_admission_sha256",
    "modes",
}
_ASSIGNMENT_FIELDS = _PAYLOAD_FIELDS | {"seal"}
_SDR_FIELDS = {"endpoint", "receiver_id", "site"}
_RESPONSIBILITY_FIELDS = {"owner", "checks"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}Z$")
_BAND_RE = re.compile(r"^(?:80|60|40|30|20|17|15|12|10)m$")

_APPLICATION_EXECUTOR = "/home/stevens/sdr/cwprop-24h-live-20261004-143842/run_application_cell.py"
_TRIARM_EXECUTOR = "/home/stevens/radio/experiments/key10-20260927/tools/run_live_triarm_campaign.py"
_OFFLINE_BPSK_ROOT = "/home/stevens/cw-propagation-inverse-20261003"


class AssignmentError(ValueError):
    """The assignment is malformed, expired, or fails its integrity seal."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the one canonical byte representation covered by the seal."""
    _validate_payload(payload)
    return _canonical_bytes(payload)


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise AssignmentError(f"{label} fields mismatch; missing={missing}, unknown={unknown}")


def _reject_frequency_binding(value: Any, path: str = "assignment") -> None:
    """Reject FT8/dial/lane concepts anywhere, including mode metadata."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if "ft8" in normalized or "dial" in normalized or "lane" in normalized:
                raise AssignmentError(f"{path}.{key} is forbidden: assignment is band-only")
            _reject_frequency_binding(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_frequency_binding(child, f"{path}[{index}]")


def _validate_payload(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping):
        raise AssignmentError("assignment payload must be an object")
    _require_exact_fields(payload, _PAYLOAD_FIELDS, "payload")
    _reject_frequency_binding(payload)
    if payload["schema"] != SCHEMA:
        raise AssignmentError(f"schema must be {SCHEMA}")
    if not isinstance(payload["hour_id"], str) or not _HOUR_RE.fullmatch(payload["hour_id"]):
        raise AssignmentError("hour_id must be UTC YYYY-MM-DDTHHZ")
    if not isinstance(payload["run_id"], (str, int)) or isinstance(payload["run_id"], bool):
        raise AssignmentError("run_id must be a string or integer")
    if isinstance(payload["run_id"], str) and not payload["run_id"].strip():
        raise AssignmentError("run_id must not be empty")
    if not isinstance(payload["selected_band"], str) or not _BAND_RE.fullmatch(payload["selected_band"]):
        raise AssignmentError("selected_band must be one of the routine 80m-10m bands")
    expiry = payload["expires_epoch"]
    if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or expiry <= 0:
        raise AssignmentError("expires_epoch must be a positive epoch number")
    source_hash = payload["source_admission_sha256"]
    if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
        raise AssignmentError("source_admission_sha256 must be lowercase SHA-256")

    sdrs = payload["assigned_sdrs"]
    if not isinstance(sdrs, list) or not sdrs:
        raise AssignmentError("assigned_sdrs must be a non-empty list")
    endpoints: set[str] = set()
    sites: set[str] = set()
    receiver_ids: set[str] = set()
    for index, sdr in enumerate(sdrs):
        if not isinstance(sdr, Mapping):
            raise AssignmentError(f"assigned_sdrs[{index}] must be an object")
        _require_exact_fields(sdr, _SDR_FIELDS, f"assigned_sdrs[{index}]")
        for field in _SDR_FIELDS:
            if not isinstance(sdr[field], str) or not sdr[field].strip():
                raise AssignmentError(f"assigned_sdrs[{index}].{field} must be non-empty")
        if sdr["endpoint"] in endpoints:
            raise AssignmentError("assigned SDR endpoint must be unique")
        if sdr["site"] in sites:
            raise AssignmentError("assigned SDR site must be unique")
        if sdr["receiver_id"] in receiver_ids:
            raise AssignmentError("assigned SDR receiver_id must be unique")
        endpoints.add(sdr["endpoint"])
        sites.add(sdr["site"])
        receiver_ids.add(sdr["receiver_id"])

    modes = payload["modes"]
    if not isinstance(modes, Mapping) or set(modes) != set(ALL_MODES):
        raise AssignmentError(f"modes must contain exactly {list(ALL_MODES)}")
    for mode in ALL_MODES:
        responsibility = modes[mode]
        if not isinstance(responsibility, Mapping):
            raise AssignmentError(f"modes.{mode} must be an object")
        _require_exact_fields(responsibility, _RESPONSIBILITY_FIELDS, f"modes.{mode}")
        if not isinstance(responsibility["owner"], str) or not responsibility["owner"].strip():
            raise AssignmentError(f"modes.{mode}.owner must be non-empty")
        checks = responsibility["checks"]
        if not isinstance(checks, list) or not checks or any(not isinstance(x, str) or not x.strip() for x in checks):
            raise AssignmentError(f"modes.{mode}.checks must be non-empty strings")
        if len(set(checks)) != len(checks):
            raise AssignmentError(f"modes.{mode}.checks must be unique")


def seal_assignment(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a deep-copied assignment with a SHA-256 integrity seal."""
    payload_copy = copy.deepcopy(dict(payload))
    canonical = canonical_payload_bytes(payload_copy)
    payload_copy["seal"] = {
        "algorithm": "sha256",
        "digest": hashlib.sha256(canonical).hexdigest(),
    }
    return payload_copy


def validate_assignment(assignment: Mapping[str, Any], now_epoch: float) -> dict[str, Any]:
    """Validate structure, integrity, and freshness; return a defensive copy."""
    if not isinstance(assignment, Mapping):
        raise AssignmentError("assignment must be an object")
    _require_exact_fields(assignment, _ASSIGNMENT_FIELDS, "assignment")
    payload = {field: copy.deepcopy(assignment[field]) for field in _PAYLOAD_FIELDS}
    canonical = canonical_payload_bytes(payload)
    seal = assignment["seal"]
    if not isinstance(seal, Mapping) or set(seal) != {"algorithm", "digest"}:
        raise AssignmentError("seal must contain exactly algorithm and digest")
    expected = hashlib.sha256(canonical).hexdigest()
    if seal.get("algorithm") != "sha256" or not isinstance(seal.get("digest"), str):
        raise AssignmentError("seal must use SHA-256")
    if not hmac.compare_digest(seal["digest"], expected):
        raise AssignmentError("assignment seal mismatch")
    if not isinstance(now_epoch, (int, float)) or isinstance(now_epoch, bool):
        raise AssignmentError("now_epoch must be numeric")
    if now_epoch >= payload["expires_epoch"]:
        raise AssignmentError("assignment expired")
    return copy.deepcopy(dict(assignment))


def _binding(assignment: Mapping[str, Any], consumes_modes: list[str]) -> dict[str, Any]:
    return {
        "hour_id": assignment["hour_id"],
        "run_id": assignment["run_id"],
        "selected_band": assignment["selected_band"],
        "expires_epoch": assignment["expires_epoch"],
        "assigned_sdrs": copy.deepcopy(assignment["assigned_sdrs"]),
        "source_admission_sha256": assignment["source_admission_sha256"],
        "assignment_seal_sha256": assignment["seal"]["digest"],
        "preflight_responsibility": {
            mode: copy.deepcopy(assignment["modes"][mode]) for mode in consumes_modes
        },
    }


def build_command_plans(
    assignment: Mapping[str, Any], *, now_epoch: float, output_root: str
) -> list[dict[str, Any]]:
    """Generate deterministic commands without running or writing anything.

    RF-capable plans are marked ``dispatch_ready=False`` because inspection found
    that both current executors hard-code frequency/roster and expose no sealed
    assignment argument. This prevents a command from pretending to consume the
    assignment while silently using the old 40m/17m constants.
    """
    valid = validate_assignment(assignment, now_epoch)
    if not isinstance(output_root, str) or not pathlib.PurePath(output_root).is_absolute():
        raise AssignmentError("output_root must be an absolute path")
    run_id = str(valid["run_id"])
    common_block = "current executor does not accept sealed band/SDR assignment"
    plans: list[dict[str, Any]] = []
    applications = (
        ("weft", "weft"),
        ("pilot-sc", "pilot"),
        ("micro-ofdm", "micro"),
        ("chirp-fountain", "chirp"),
    )
    for slot, (mode, candidate) in enumerate(applications, 1):
        modes = [mode]
        plans.append(
            {
                "plan_id": f"{valid['hour_id']}/{run_id}/{mode}",
                "executor_kind": "current-application-cell",
                "consumes_modes": modes,
                "execution_status": "rf-candidate-blocked-adapter-gap",
                "rf_capable": True,
                "dispatch_ready": False,
                "block_reason": common_block,
                "argv": [
                    "python3",
                    _APPLICATION_EXECUTOR,
                    candidate,
                    "--cycle",
                    run_id,
                    "--slot",
                    str(slot),
                    "--deadline-epoch",
                    str(valid["expires_epoch"]),
                ],
                "assignment_binding": _binding(valid, modes),
            }
        )
    tri_modes = ["key-cw", "audio-cw", "bfsk"]
    plans.append(
        {
            "plan_id": f"{valid['hour_id']}/{run_id}/key-audio-bfsk",
            "executor_kind": "current-live-triarm",
            "consumes_modes": tri_modes,
            "execution_status": "rf-candidate-blocked-adapter-gap",
            "rf_capable": True,
            "dispatch_ready": False,
            "block_reason": common_block,
            "argv": [
                "python3",
                _TRIARM_EXECUTOR,
                "--start-triad",
                run_id,
                "--max-triads",
                "1",
                "--deadline-epoch",
                str(valid["expires_epoch"]),
                "--state",
                str(pathlib.PurePath(output_root) / f"run-{run_id}" / "triarm-state.json"),
            ],
            "assignment_binding": _binding(valid, tri_modes),
        }
    )
    bpsk_modes = ["bpsk"]
    plans.append(
        {
            "plan_id": f"{valid['hour_id']}/{run_id}/bpsk",
            "executor_kind": "offline-test",
            "consumes_modes": bpsk_modes,
            "execution_status": "offline-only-until-qualified",
            "rf_capable": False,
            "dispatch_ready": True,
            "block_reason": None,
            "cwd": _OFFLINE_BPSK_ROOT,
            "argv": [
                "python3",
                "-m",
                "pytest",
                "-q",
                "tests/test_waveforms.py::test_true_bpsk31_source_survives_channel_as_phase_sensitive_feed",
            ],
            "assignment_binding": _binding(valid, bpsk_modes),
        }
    )
    return plans
