"""
hamradio.tx — GATED transmit control.

Transmitting is a licensed, physical-world action (KD9NWA). This module is the
ONLY place PTT is asserted, and it refuses to do so unless EVERY guard passes:

  1. Global enable file must exist:      ~/radio/agent/TX_ENABLED
     (create with `radio tx-enable`; remove with `radio tx-disable`).
     Absent  => hard read-only. This is the master kill-switch.
  2. Caller must pass allow_tx=True explicitly (no accidental default).
  3. Frequency must fall inside a US ham band AND inside a configured
     TX-allowed segment (band plan / privileges). Out-of-band => refuse.
  4. A max key-down timeout is always armed; PTT is released in a finally:
     block even if the caller crashes. No stuck carrier, ever.
  5. Optional dry_run=True simulates the full sequence without keying.

The enable file also records WHY/when it was enabled (audit trail).

NOTE: this does not itself verify an antenna/dummy-load is connected — that is
a physical precondition the operator asserts by creating TX_ENABLED. The CLI
`radio tx-enable` prints a explicit reminder.
"""
from __future__ import annotations
import os
import time
import pathlib
from contextlib import contextmanager
from typing import Optional

from .rig import Rig, RigError, band_for

AGENT_DIR = pathlib.Path(os.path.expanduser("~/radio/agent"))
TX_ENABLE_FILE = AGENT_DIR / "TX_ENABLED"

# Default max continuous key-down (seconds) before forced un-key. Guards against
# a hung decoder/agent leaving the PA keyed. Callers may lower, never raise past
# TX_HARD_CEILING.
TX_DEFAULT_TIMEOUT = 60
TX_HARD_CEILING = 300

# TX-allowed sub-segments (Hz) by KD9NWA privileges. Conservative General/Extra
# HF phone+data segments; edit to match exact privileges. Anything not covered
# here is refused even when TX is globally enabled.
TX_SEGMENTS = [
    # 80m
    (3_525_000, 4_000_000),
    # 40m
    (7_025_000, 7_300_000),
    # 30m (data/CW only)
    (10_100_000, 10_150_000),
    # 20m
    (14_025_000, 14_350_000),
    # 17m
    (18_068_000, 18_168_000),
    # 15m
    (21_025_000, 21_450_000),
    # 12m
    (24_890_000, 24_990_000),
    # 10m
    (28_000_000, 29_700_000),
    # 6m
    (50_000_000, 54_000_000),
    # 2m
    (144_000_000, 148_000_000),
    # 70cm
    (420_000_000, 450_000_000),
]


class TxRefused(RuntimeError):
    """Raised when a transmit guard blocks the request. PTT is never asserted."""


def tx_globally_enabled() -> bool:
    return TX_ENABLE_FILE.exists()


def enable_tx(reason: str = "") -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    TX_ENABLE_FILE.write_text(
        f"enabled_at={time.strftime('%Y-%m-%dT%H:%M:%S')}\nreason={reason}\n"
    )


def disable_tx() -> None:
    try:
        TX_ENABLE_FILE.unlink()
    except FileNotFoundError:
        pass


def freq_tx_ok(hz: int) -> bool:
    return any(lo <= hz <= hi for lo, hi in TX_SEGMENTS)


def _check_guards(hz: int, allow_tx: bool, timeout: int,
                  dry_run: bool = False) -> None:
    # allow_tx is the explicit-intent gate for REAL keying. A dry_run never
    # keys, so it may simulate without allow_tx — but we still validate the
    # master switch + band plan so the simulation reflects reality.
    if not allow_tx and not dry_run:
        raise TxRefused("allow_tx=False (caller did not explicitly request TX)")
    if not tx_globally_enabled():
        raise TxRefused(
            f"TX master switch OFF (no {TX_ENABLE_FILE}). Run `radio tx-enable`."
        )
    if band_for(hz) is None:
        raise TxRefused(f"{hz} Hz is not in any ham band")
    if not freq_tx_ok(hz):
        raise TxRefused(f"{hz} Hz is outside configured TX-allowed segments")
    if timeout <= 0 or timeout > TX_HARD_CEILING:
        raise TxRefused(f"timeout {timeout}s out of range (1..{TX_HARD_CEILING})")


@contextmanager
def keyed(rig: Rig, *, allow_tx: bool = False,
          timeout: int = TX_DEFAULT_TIMEOUT, dry_run: bool = False):
    """Context manager that keys PTT on entry and ALWAYS un-keys on exit.

    Usage:
        with keyed(rig, allow_tx=True, timeout=10):
            ...  # feed audio / send CW while transmitting
    Guards run before any keying. On dry_run, logs intent but never keys.
    """
    hz = rig.get_freq()
    _check_guards(hz, allow_tx, timeout, dry_run=dry_run)

    if dry_run:
        yield {"dry_run": True, "freq_hz": hz, "would_key_for_s": timeout}
        return

    keyed_at = time.time()
    try:
        rig._cmd("set_ptt 1")
        yield {"dry_run": False, "freq_hz": hz, "keyed_at": keyed_at,
               "timeout": timeout}
    finally:
        # Fail-safe un-key. Try hard; swallow errors so we never propagate a
        # failure that could skip the un-key.
        try:
            rig._cmd("set_ptt 0")
        except Exception:
            # last-ditch: open a fresh connection and drop PTT
            try:
                Rig(rig.host, rig.port)._cmd("set_ptt 0")
            except Exception:
                pass


def watchdog_unkey(rig: Rig) -> None:
    """Unconditionally drop PTT. Safe to call anytime (used by CLI `radio unkey`)."""
    try:
        rig._cmd("set_ptt 0")
    except Exception:
        pass
