# FT8 30-slot session API reference

## Finding

The current `/home/stevens/radio/propagation-admission/ft8_beacon.py` is a
correctly bounded **single-beacon transaction**, but each invocation constructs
`Rig`, snapshots CAT state, encodes/tunes/keys, disables TX, restores CAT state,
and emits one final receipt. Calling it 30 times would therefore repeat CAT
initialization and restoration 30 times. It also chooses the next 15-second
boundary internally, which is unsuitable for an externally anchored alternating
TX/RX schedule.

## Minimal API

`ft8_session.py` separates one outer session lifecycle from per-slot execution:

```python
with FT8Session(SessionConfig(run_id, anchor_epoch), dependencies...) as session:
    for slot_index in range(30):
        session.run_slot(slot_index, SlotConfig(...))
```

Contract:

- `open()` snapshots CAT state once.
- `run_slot(0..29, ...)` targets immutable offsets `0, 30, ..., 870` from the
  supplied anchor; late slots are skipped/refused rather than caught up.
- Every attempted slot emits a receipt in `finally`, including its telemetry
  samples, error (if any), and completion flag.
- Telemetry policy matches the source beacon: fail after three missing samples,
  require positive forward power, and reject forward power >20 W, SWR >2, or
  ALC >0.9.
- Per-slot cleanup unkeys, but does **not** restore/reinitialize CAT.
- `close()` unkeys, disables TX, restores the original state once, performs a
  readback, and emits one final closeout receipt.
- The deadline is derived only as `anchor_epoch + 900`; `SessionConfig` rejects
  any other duration and `deadline_epoch` is read-only.

The reference is intentionally dependency-injected and has no station imports,
network calls, service controls, or executable CLI. A production adapter should
wrap the existing `Rig`, `txmod`, `encode_wav`, `pw-play`, and atomic JSON writer,
while retaining the outer caller's collision, TX-enable, and authorization
checks.

## Verification

```bash
cd /home/stevens/radio/hourly-15-45/ft8-scout/reference
pytest -q test_ft8_session.py
```

The fake-clock/fake-rig suite covers one snapshot/restore across all 30 slots,
per-slot success and failure receipts, telemetry, final safe closeout, slot
bounds/no catch-up, and immutable 900-second deadline. No RF is possible in the
tests.
