# Hourly 15/45 two-lane scheduler (no RF)

A deterministic prototype of one anchored 3600-second cycle. It contains no radio,
network, subprocess, sleep, device, or service-control implementation. Every RF-like
item has `rf_authorized=False`; execution is possible only through a caller-injected
rehearsal callback.

## Contract represented

- `0 <= t < 900`: 60 alternating 15-second FT8 TX/RX slots. The 30 TX
  reservations occur exactly at offsets `0,30,...,870`.
- Scout processing jobs: band sweep, PSKReporter, SDR capture, decode/ranking,
  sealing, and next-cell preparation, represented by overlapping intervals.
- `900 <= t <= 3600`: one exclusive RF-reservation lane containing all eight modes;
  order rotates left by cycle number.
- A separate concurrent processing queue. Only dependencies explicitly marked
  mandatory safety work can block an RF reservation.
- Atomic `fsync` + replace reservation state; an existing reservation is no-replay.
- Validation rejects any job past the hard 3600-second boundary.
- `EventRecorder` captures observed monotonic start/end intervals in concurrent
  rehearsal tests.
- The module CLI emits all 82 observed virtual-time intervals (60 FT8 slots, six
  scout jobs, eight RF cells, eight per-cell jobs) and derived overlap checks.

## Test

```bash
cd /home/stevens/radio/hourly-15-45
pytest -q
PYTHONPATH=src python -m hourly_scheduler > full-hour.json
PYTHONPATH=src python -m hourly_scheduler --mandatory-safety-delay 30 > safety-delay.json
```
