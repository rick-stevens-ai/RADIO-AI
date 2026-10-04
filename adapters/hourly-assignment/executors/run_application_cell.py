#!/usr/bin/env python3
"""One guarded, assignment-bound application-protocol RF cell.

This is an isolated copy of the current CWPROP application executor. Dry-run
validates and resolves the complete assignment, then returns before creating
state, launching processes, contacting radios, or touching SDRs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import time
import wave
from datetime import datetime, timezone

from assignment_adapters import AssignmentError, validate_assignment

BASE = pathlib.Path(__file__).resolve().parent / "runs"
KIWI = "/home/stevens/sdr/kiwiclient/kiwirecorder.py"
CONFIG = {
    "pilot": {"fixture": "pilot-sc-v2-m60-40m-triple", "stem": "pilot-sc-v2-m60", "commit": "20d8addb3569bfb28c5e3082b8560d83c339380a", "timeout": 90},
    "micro": {"fixture": "micro-ofdm-v5-m60-40m-triple", "stem": "micro-ofdm-v5-m60", "commit": "02b930124c632d17a22b8ddbcb0ab2fbef57344f", "timeout": 90},
    "chirp": {"fixture": "chirp-fountain-v4-m60-40m-triple", "stem": "chirp-fountain-v4-m60", "commit": "af9b5b0bf0e84c39de957394d95a494657514698", "timeout": 140},
    "weft": {"fixture": "weft-guarded-mp1-quad40a-8041-40m-fourmode", "stem": "weft-guarded-mp1-quad40a-8041", "commit": "86fbceed6fc61390aa60c28c6273c704f4b6cd78", "timeout": 220},
}

# Application-protocol USB carrier frequencies. Each row is deliberately
# independent of FT8 calling frequencies and gives each protocol its own
# reproducible position within the selected amateur band.
BAND_MODE_FREQUENCIES = {
    "80m": {"weft": 3_580_000, "pilot": 3_581_000, "micro": 3_582_000, "chirp": 3_583_000},
    "60m": {"weft": 5_330_500, "pilot": 5_346_500, "micro": 5_357_000, "chirp": 5_371_500},
    "40m": {"weft": 7_114_500, "pilot": 7_115_500, "micro": 7_116_500, "chirp": 7_117_500},
    "30m": {"weft": 10_140_000, "pilot": 10_141_000, "micro": 10_142_000, "chirp": 10_143_000},
    "20m": {"weft": 14_100_000, "pilot": 14_101_000, "micro": 14_102_000, "chirp": 14_103_000},
    "17m": {"weft": 18_105_000, "pilot": 18_106_000, "micro": 18_107_000, "chirp": 18_108_000},
    "15m": {"weft": 21_100_000, "pilot": 21_101_000, "micro": 21_102_000, "chirp": 21_103_000},
    "12m": {"weft": 24_920_000, "pilot": 24_921_000, "micro": 24_922_000, "chirp": 24_923_000},
    "10m": {"weft": 28_100_000, "pilot": 28_101_000, "micro": 28_102_000, "chirp": 28_103_000},
}


def remote_pythonpath():
    return "/home/stevens/radio/cwprop-adapter"


def remote_fixture_root():
    return "/home/stevens/radio/cwprop-fixtures"


def resolve_frequency(selected_band: str, candidate: str) -> int:
    """Resolve the application mode inside the assigned band (never via FT8)."""
    try:
        return BAND_MODE_FREQUENCIES[selected_band][candidate]
    except KeyError as exc:
        raise AssignmentError(f"no {candidate!r} frequency for selected band {selected_band!r}") from exc


def load_assignment(path: pathlib.Path, *, now_epoch: float) -> dict:
    """Read and validate a sealed assignment before any operational action."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AssignmentError(f"cannot read assignment: {exc}") from exc
    return validate_assignment(raw, now_epoch=now_epoch)


def _split_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse host:port or [IPv6]:port without network access."""
    if endpoint.startswith("["):
        close = endpoint.find("]")
        if close < 1 or endpoint[close + 1:close + 2] != ":":
            raise AssignmentError(f"invalid assigned SDR endpoint: {endpoint!r}")
        host, port_text = endpoint[1:close], endpoint[close + 2:]
    else:
        if endpoint.count(":") != 1:
            raise AssignmentError(f"invalid assigned SDR endpoint: {endpoint!r}")
        host, port_text = endpoint.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise AssignmentError(f"invalid assigned SDR endpoint port: {endpoint!r}") from exc
    if not host or not 1 <= port <= 65535:
        raise AssignmentError(f"invalid assigned SDR endpoint: {endpoint!r}")
    return host, port


def assigned_roster(assignment: dict) -> list[dict]:
    """Translate only the sealed assigned SDRs to the recorder row contract."""
    roster = []
    for assigned in assignment["assigned_sdrs"]:
        host, port = _split_endpoint(assigned["endpoint"])
        roster.append({
            "candidate_id": assigned["receiver_id"],
            "host": host,
            "port": port,
            "site": assigned["site"],
        })
    return roster


def local_clearance_command(frequency_hz: int):
    return f"R=/home/stevens/radio/agent/bin/radio;$R freq {frequency_hz};$R mode CW 500;$R cw --seconds 10 --method dsp;$R mode USB 3000;$R tx-disable"


def local_clearance_pass(returncode, stdout):
    return returncode == 0 and "no CW signal" in stdout


def recorder_command(row, output_stem, frequency_hz):
    """Build recorder argv without side effects."""
    return ["python3", KIWI, "-s", row["host"], "-p", str(row["port"]), "-f", str(frequency_hz / 1000), "-m", "usb", "-L", "100", "-H", "3000", "-r", "12000", "--fn", str(output_stem), "--connect-retries", "1", "--busy-retries", "1", "--log", "warn"]


def start(root, row, frequency_hz):
    directory = root / "captures" / row["candidate_id"]
    directory.mkdir(parents=True, exist_ok=True)
    cmd = recorder_command(row, directory / "capture", frequency_hz)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=(directory / "recorder.stderr").open("w"), start_new_session=True)


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


def remote(config, deadline, frequency_hz):
    return f'''#!/usr/bin/env python3
import json,pathlib,time
from hamradio.rig import Rig
from hamradio import tx as txmod
from hamradio import weft
out=pathlib.Path('/tmp/cwprop-24h-result.json');rig=Rig();orig=rig.get_freq();om,op=rig.get_mode();orp=float(next(x for x in rig._cmd('get_level RFPOWER') if x.strip().replace('.','',1).isdigit()));doc={{'schema':'cwprop-24h-cell-v1','fixture':'{config['fixture']}','source_commit':'{config['commit']}','authorization_deadline_epoch':{deadline},'frequency_hz':{frequency_hz},'rfpower_percent':20,'started_epoch':time.time(),'sent':False,'pre_state':{{'freq_hz':orig,'mode':[om,op],'ptt':rig.get_ptt(),'tx_enabled':txmod.tx_globally_enabled(),'rfpower':orp}}}};out.write_text(json.dumps(doc,indent=2)+'\\n')
try:
 if time.time()+300>doc['authorization_deadline_epoch']:raise RuntimeError('authorization deadline reserve')
 rig.set_freq({frequency_hz});rig._cmd('set_level RFPOWER 0.20');txmod.enable_tx('Rick authorized CWPROP eight-mode campaign for 24 hours on 2026-10-04')
 doc.update(weft.send(rig,'{remote_fixture_root()}/{config['stem']}.wav','{remote_fixture_root()}/{config['stem']}.json',station_callsign='KD9NWA',allow_tx=True,dry_run=False));doc['sent']=True
except Exception as e:doc['error']=repr(e)
finally:
 txmod.watchdog_unkey(rig);txmod.disable_tx()
 try:rig._cmd(f'set_level RFPOWER {{orp}}');rig.set_freq(orig);rig.set_mode(om,op)
 except Exception as e:doc['restore_error']=repr(e)
 doc.update({{'finished_epoch':time.time(),'post_state':{{'ptt':rig.get_ptt(),'tx_enabled':txmod.tx_globally_enabled(),'freq_hz':rig.get_freq(),'mode':rig.get_mode()}}}});out.write_text(json.dumps(doc,indent=2)+'\\n')
print(json.dumps(doc));raise SystemExit(0 if doc.get('sent') and not doc.get('restore_error') and not doc['post_state']['ptt'] and not doc['post_state']['tx_enabled'] else 2)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", choices=CONFIG)
    parser.add_argument("--cycle", type=int, required=True)
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--deadline-epoch", type=float, required=True)
    parser.add_argument("--assignment", type=pathlib.Path, required=True)
    parser.add_argument("--min-receivers", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        assignment = load_assignment(args.assignment, now_epoch=time.time())
        if str(assignment["run_id"]) != str(args.cycle):
            raise AssignmentError("cycle does not match assignment run_id")
        if args.deadline_epoch > assignment["expires_epoch"]:
            raise AssignmentError("deadline exceeds assignment expiry")
        roster = assigned_roster(assignment)
        frequency_hz = resolve_frequency(assignment["selected_band"], args.candidate)
    except AssignmentError as exc:
        raise SystemExit(str(exc)) from exc

    config = CONFIG[args.candidate]
    root = BASE / f"cycle-{args.cycle:02d}" / f"app-{args.slot:02d}-{args.candidate}"
    plan = {
        "candidate": args.candidate,
        "cycle": args.cycle,
        "slot": args.slot,
        "deadline_epoch": args.deadline_epoch,
        "fixture": config["fixture"],
        "source_commit": config["commit"],
        "selected_band": assignment["selected_band"],
        "frequency_hz": frequency_hz,
        "assigned_sdrs": roster,
        "assignment_seal_sha256": assignment["seal"]["digest"],
        "source_admission_sha256": assignment["source_admission_sha256"],
        "rf_authorized": not args.dry_run,
        "rf_performed": False,
    }
    # Structural no-RF boundary: nothing above this line creates directories,
    # starts processes, contacts a receiver, tunes a radio, or enables TX.
    if args.dry_run:
        print(json.dumps(plan))
        return 0

    if root.exists():
        raise SystemExit(f"exists: {root}")
    if time.time() + 300 > args.deadline_epoch:
        raise SystemExit("deadline reserve")
    root.mkdir(parents=True)
    (root / "authorization.json").write_text(json.dumps({
        "authority": "Rick Stevens via Telegram",
        "instruction": "Rick authorized validated eight-mode schedule for next 24 hours on 2026-10-04",
        "deadline_epoch": args.deadline_epoch,
        "candidate": args.candidate,
        "cycle": args.cycle,
        "slot": args.slot,
        "selected_band": assignment["selected_band"],
        "frequency_hz": frequency_hz,
        "assignment_seal_sha256": assignment["seal"]["digest"],
    }, indent=2) + "\n")
    script = root / "remote-runner.py"
    script.write_text(remote(config, args.deadline_epoch, frequency_hz))
    compile(script.read_text(), str(script), "exec")
    remote_path = "/tmp/run-cwprop-24h.py"
    subprocess.run(["scp", "-q", str(script), "rpi-gateway:" + remote_path], check=True)
    processes = {row["candidate_id"]: start(root, row, frequency_hz) for row in roster}
    tx = None
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end:
            ready = [row for row in roster if any(path.stat().st_size > 20000 for path in (root / "captures" / row["candidate_id"]).glob("capture*.wav"))]
            if len(ready) >= args.min_receivers:
                break
            time.sleep(.35)
        else:
            raise RuntimeError("receiver quorum failed")
        (root / "receivers-ready.json").write_text(json.dumps({"ready": True, "minimum": args.min_receivers, "utc": datetime.now(timezone.utc).isoformat(), "names": [row["candidate_id"] for row in ready]}, indent=2) + "\n")
        local = subprocess.run(["ssh", "-n", "rpi-gateway", local_clearance_command(frequency_hz)], capture_output=True, text=True, timeout=35)
        (root / "local-clearance.txt").write_text(local.stdout + local.stderr)
        if not local_clearance_pass(local.returncode, local.stdout):
            raise RuntimeError("local occupancy gate")
        tx = subprocess.run(["ssh", "-n", "rpi-gateway", f"PYTHONPATH={remote_pythonpath()} python3 {remote_path}"], capture_output=True, text=True, timeout=config["timeout"])
        (root / "tx.stdout").write_text(tx.stdout)
        (root / "tx.stderr").write_text(tx.stderr)
        (root / "tx-exit.txt").write_text(str(tx.returncode) + "\n")
        time.sleep(8)
    finally:
        for process in processes.values():
            stop(process)
        subprocess.run(["ssh", "-n", "rpi-gateway", "/home/stevens/radio/agent/bin/force-safe-closeout"], timeout=40)

    rows = []
    for receiver in roster:
        files = []
        for path in sorted((root / "captures" / receiver["candidate_id"]).glob("capture*.wav")):
            with wave.open(str(path), "rb") as wav_file:
                duration = wav_file.getnframes() / wav_file.getframerate()
            files.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "duration_s": duration, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        rows.append({"name": receiver["candidate_id"], "site": receiver["site"], "files": files})
    result = {
        **plan,
        "schema": "cwprop-24h-cell-v1",
        "receivers": rows,
        "tx_exit": tx.returncode if tx else None,
        "tx_result": json.loads(tx.stdout) if tx and tx.stdout.strip().startswith("{") else None,
    }
    (root / "cell.json").write_text(json.dumps(result, indent=2) + "\n")
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (root / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root)}\n" for path in files))
    print(json.dumps({"candidate": args.candidate, "cycle": args.cycle, "tx_exit": result["tx_exit"], "root": str(root)}))
    return 0 if tx and tx.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
