# Hourly 15/45 Propagation and Eight-Mode Pipeline

This repository is the canonical home for the KD9NWA hourly radio experiment pipeline.

## Hour contract

### Minutes 00–15: FT8 scout and allocator

The station uses the standard FT8 alternating-slot cadence:

- 60 consecutive 15-second slots;
- 30 TX slots and 30 RX slots;
- missed slots are skipped and are never replayed or compressed.

During both TX and RX slots, the processing lane concurrently performs:

- public-SDR band surveying;
- persistent SDR capture;
- PSKReporter polling;
- decode and propagation scoring;
- band and geographically diverse SDR assignment;
- evidence hashing/sealing;
- preparation of the next experimental cell.

### Minutes 15–60: eight-mode experiment

The sealed FT8 assignment supplies the selected **band** and SDR roster. Each mode performs its own fresh mode-specific frequency selection and mandatory clearance inside that band.

The complete research set is:

1. KEY-CW
2. AUDIO-CW
3. BPSK — offline-only until separately qualified for RF
4. BFSK
5. WEFT
6. PILOT-SC
7. MICRO-OFDM
8. CHIRP-FOUNTAIN

Seven RF-capable modes use one exclusive RF lane. BPSK runs concurrently on the processing lane. Optional decoding, scoring, hashing, and analysis never block the next safe RF cell. Only declared safety dependencies may block RF.

## Repository layout

- `orchestration/hourly-15-45/` — scheduler, persistent FT8 scout, processing lane, receiver-session manager, station arbiter, tests, and no-RF rehearsals.
- `propagation/ft8-admission/` — nine-band FT8 sweep, clearance and assignment evidence tooling.
- `adapters/hourly-assignment/` — sealed assignment schema, pure planner, and isolated assignment-aware mode adapters.
- `evidence/hourly-15-45/` — compact no-RF manifests, sample assignment, and observed overlap trace. Large WAV captures are intentionally excluded.
- `systemd/` — deployed service templates; no new live service is enabled by this branch.

## Safety boundary

The architecture and scheduler are verified in structural no-RF mode. Live RF adapters are kept separate and remain disabled until they independently pass:

- explicit authorization and immutable deadline;
- cross-process station ownership;
- fresh mode-specific local and remote occupancy clearance;
- tuner/match, SWR, ALC, power, telemetry freshness, and cooldown gates;
- invocation-unique remote paths and receipts;
- verified restoration and safe closeout;
- independent pre-air review.

## Tests

```bash
# Scheduler, orchestration, locks, persistent processing and receiver sessions
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=orchestration/hourly-15-45/src \
  python3 -m pytest -q orchestration/hourly-15-45/tests

# Persistent FT8 scout
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest -q orchestration/hourly-15-45/ft8-scout

# FT8 propagation/admission
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=propagation/ft8-admission:/home/stevens/radio-ai-remote-audit-ekydz1hf \
  python3 -m pytest -q propagation/ft8-admission

# Assignment schema and isolated adapters
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=adapters/hourly-assignment \
  python3 -m pytest -q adapters/hourly-assignment/tests/test_assignment_adapters.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=adapters/hourly-assignment/executors \
  python3 -m pytest -q adapters/hourly-assignment/executors/tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=adapters/hourly-assignment/triarm \
  python3 -m pytest -q adapters/hourly-assignment/triarm/tests
```

## Operating principle

**RF is the scarce serialized lane.** Keep the station safe and exclusive, but use every TX, RX, and cooldown interval for independent processing. A decoder or evidence writer may lag; it must not idle the RF lane unless its result is explicitly required for the next transmission's safety.
