# Isolated tri-arm assignment consumers

This directory contains isolated copies/adapters for KEY-CW, AUDIO-CW, BFSK, and offline BPSK. The source/live tools under `experiments/key10-20260927` are not modified.

All entry points require `--assignment`. Validation checks the complete `hourly-15-45-assignment-v1` shape, canonical SHA-256 seal, expiry, unique SDR endpoint/ID/site values, all eight mode responsibilities, and rejects embedded FT8/dial/lane/frequency/carrier bindings. Assignments select a **band only**.

`carrier_plan.py` resolves a mode-specific carrier from the selected band. KEY-CW uses CW; AUDIO-CW/BFSK use USB with a 1500 Hz audio offset. The 60 m entry is constrained to channel 3 (5358.5 kHz center; 5357.0 kHz USB dial). Runtime capture rows are derived only from `assigned_sdrs`; the inherited fixed receiver set is not used.

BPSK is structurally offline-only: `run_offline_bpsk.py` can only run the existing waveform pytest and has no radio/SDR command path.

## No-RF rehearsal

```bash
python3 rehearse_no_rf.py --assignment /absolute/path/assignment.json
```

This invokes every consumer with `--dry-run`; dry-run validates and prints plans before any state directory, capture, network, radio, or playback operation.

## Tests

```bash
python3 -m pytest -q
```
