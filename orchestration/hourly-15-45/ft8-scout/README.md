# Persistent FT8 scout controller (build-only)

A dependency-injected controller for exactly one UTC-aligned 900-second scouting
window: 60 anchored 15-second slots alternating 30 TX reservations and 30 RX
slots. Late slots are durably skipped and never caught up. A started window can
never be replayed.

This repository contains **no concrete Rig, transmitter, network, subprocess, or
device adapter** and has not been deployed or used for RF. Live use would require
separately reviewed injected adapters. Each TX is persistently reserved before
invocation and requires a valid invocation receipt plus complete/fresh safe
telemetry immediately before and after. The station snapshot occurs once and is
restored once in unconditional cleanup.

RX callbacks receive `RxEvent` objects and run on a worker pool, allowing public
SDR sweep, PSKReporter, decode, and assignment processing to overlap one another
and subsequent slots without moving anchored slot times.

## Safe dry run

```bash
PYTHONPATH=src python -m ft8_scout_cli --dry-run --now 1800000007
```

Dry-run only constructs JSON in memory/stdout. It does not instantiate a Rig,
write state, access the network, or touch devices.

## Tests

```bash
pytest -q
```
