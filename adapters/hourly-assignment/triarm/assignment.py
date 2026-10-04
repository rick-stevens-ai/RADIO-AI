"""Strict loader for hourly sealed band/SDR assignments."""
from __future__ import annotations
import copy, hashlib, hmac, json, pathlib, re, time
from typing import Any, Mapping

SCHEMA = "hourly-15-45-assignment-v1"
MODES = ("key-cw", "audio-cw", "bpsk", "bfsk", "weft", "pilot-sc", "micro-ofdm", "chirp-fountain")
_PAYLOAD = {"schema", "hour_id", "run_id", "selected_band", "expires_epoch", "assigned_sdrs", "source_admission_sha256", "modes"}
_BANDS = {"80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m"}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_HOUR = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}Z$")

class AssignmentError(ValueError): pass

def _exact(value, fields, label):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise AssignmentError(f"{label} fields mismatch")

def _forbidden(value, path="assignment"):
    if isinstance(value, Mapping):
        for key, child in value.items():
            k = str(key).lower().replace("-", "_")
            if any(word in k for word in ("ft8", "dial", "lane", "frequency", "carrier")):
                raise AssignmentError(f"{path}.{key} is forbidden; assignment is band-only")
            _forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for n, child in enumerate(value): _forbidden(child, f"{path}[{n}]")

def _payload(value):
    _exact(value, _PAYLOAD, "payload"); _forbidden(value)
    if value["schema"] != SCHEMA: raise AssignmentError("wrong schema")
    if not isinstance(value["hour_id"], str) or not _HOUR.fullmatch(value["hour_id"]): raise AssignmentError("invalid hour_id")
    if isinstance(value["run_id"], bool) or not isinstance(value["run_id"], (str, int)) or (isinstance(value["run_id"], str) and not value["run_id"].strip()): raise AssignmentError("invalid run_id")
    if value["selected_band"] not in _BANDS: raise AssignmentError("invalid selected_band")
    if isinstance(value["expires_epoch"], bool) or not isinstance(value["expires_epoch"], (int, float)) or value["expires_epoch"] <= 0: raise AssignmentError("invalid expires_epoch")
    if not isinstance(value["source_admission_sha256"], str) or not _SHA.fullmatch(value["source_admission_sha256"]): raise AssignmentError("invalid source hash")
    rows=value["assigned_sdrs"]
    if not isinstance(rows, list) or not rows: raise AssignmentError("assigned_sdrs must be non-empty")
    seen={key:set() for key in ("endpoint", "receiver_id", "site")}
    for n,row in enumerate(rows):
        _exact(row,{"endpoint","receiver_id","site"},f"assigned_sdrs[{n}]")
        for key in seen:
            item=row[key]
            if not isinstance(item,str) or not item.strip(): raise AssignmentError(f"invalid assigned_sdrs {key}")
            if item in seen[key]: raise AssignmentError(f"duplicate assigned SDR {key}")
            seen[key].add(item)
    if not isinstance(value["modes"], Mapping) or set(value["modes"]) != set(MODES): raise AssignmentError("modes fields mismatch")
    for mode,row in value["modes"].items():
        _exact(row,{"owner","checks"},f"modes.{mode}")
        if not isinstance(row["owner"],str) or not row["owner"].strip(): raise AssignmentError(f"invalid owner for {mode}")
        if not isinstance(row["checks"],list) or not row["checks"] or len(set(row["checks"])) != len(row["checks"]) or any(not isinstance(x,str) or not x.strip() for x in row["checks"]): raise AssignmentError(f"invalid checks for {mode}")

def _canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")

def seal_payload(payload):
    value=copy.deepcopy(dict(payload)); _payload(value)
    value["seal"]={"algorithm":"sha256","digest":hashlib.sha256(_canonical(value)).hexdigest()}; return value

def validate_assignment(assignment, now_epoch=None):
    _exact(assignment,_PAYLOAD|{"seal"},"assignment")
    body={key:copy.deepcopy(assignment[key]) for key in _PAYLOAD}; _payload(body)
    seal=assignment["seal"]
    _exact(seal,{"algorithm","digest"},"seal")
    expected=hashlib.sha256(_canonical(body)).hexdigest()
    if seal.get("algorithm") != "sha256" or not isinstance(seal.get("digest"),str) or not hmac.compare_digest(seal["digest"],expected): raise AssignmentError("assignment seal mismatch")
    now=time.time() if now_epoch is None else now_epoch
    if isinstance(now,bool) or not isinstance(now,(int,float)): raise AssignmentError("now_epoch must be numeric")
    if now >= body["expires_epoch"]: raise AssignmentError("assignment expired")
    return copy.deepcopy(dict(assignment))

def load_assignment(path, now_epoch=None):
    source=pathlib.Path(path)
    try: raw=source.read_bytes(); value=json.loads(raw)
    except (OSError,json.JSONDecodeError) as exc: raise AssignmentError(f"cannot load assignment: {exc}") from exc
    valid=validate_assignment(value,now_epoch)
    return valid, hashlib.sha256(raw).hexdigest()
