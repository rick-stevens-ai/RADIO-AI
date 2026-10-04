# Hourly 15/45 assignment adapters

Side-effect-free schema validation and command planning for the current eight-mode campaign executors. Nothing in this directory transmits, tunes, starts a timer, writes campaign state, or runs generated commands.

## Contract

`hourly-15-45-assignment-v1` binds one hourly run to:

- `hour_id` and `run_id`;
- one selected amateur band (not a frequency);
- a strict expiry epoch;
- unique assigned SDR endpoints, receiver IDs, and sites;
- the SHA-256 of the source admission artifact;
- an explicit preflight owner/check list for each of the eight modes;
- a canonical-JSON SHA-256 integrity seal.

The Python validator is authoritative for semantic constraints that JSON Schema cannot express conveniently, including unique SDR sites/endpoints, expiry, seal verification, and recursive rejection of any key containing `ft8`, `dial`, or `lane`. Thus an assignment cannot bind a mode to the FT8 scouting dial or lane.

## Current-executor adapter findings

Inspected, without editing:

- `/home/stevens/sdr/cwprop-24h-live-20261004-143842/run_application_cell.py`
- `/home/stevens/radio/experiments/key10-20260927/tools/run_live_triarm_campaign.py`

The application executor supports WEFT, PILOT-SC, MICRO-OFDM, and CHIRP-FOUNTAIN but hard-codes 40 m frequency and a fixed receiver roster. The tri-arm executor supports KEY-CW, AUDIO-CW, and BFSK but chooses from hard-coded 17 m carriers and delegates to existing RF runners. Neither accepts a sealed band/SDR assignment.

`build_command_plans()` therefore emits assignment-bound plans for all seven RF-candidate modes but marks them `dispatch_ready: false`; silently dispatching their old hard-coded commands would violate the assignment. The BPSK plan is always `offline-only-until-qualified`, points only to its existing offline pytest qualification, and has `rf_capable: false`.

Each plan embeds the same selected band, complete SDR roster, expiry, source-admission hash, assignment-seal digest, and relevant mode preflight responsibilities. The plans are data only and are never executed here.

## Test

```bash
cd /home/stevens/radio/hourly-15-45-adapters
python3 -m unittest discover -s tests -v
```
