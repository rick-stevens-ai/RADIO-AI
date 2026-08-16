"""
hamradio.scan — band-occupancy scanner.

Steps the radio across a frequency range via rigctld, samples the S-meter at
each step, and builds a "what's being used where" map:
  * JSON: list of {freq_hz, smeter_db, active} plus detected active segments.
  * PNG:  S-meter vs frequency plot with an activity threshold line.

This is a CAT/S-meter scan on the REAL radio (RX only — never keys TX). It is
the honest "what can this radio hear right now" view. Resolution is limited by
CAT step latency (~tens of ms/step), so it's for occupancy mapping, not a fast
waterfall. (A future SDR path via RTL-SDR can add a true wideband waterfall.)

Occupancy detection: a bin is "active" if its S-meter exceeds
  noise_floor + margin_db, where noise_floor is the median of the sweep
  (robust to a few strong signals).
"""
from __future__ import annotations
import time
import json
import statistics
from dataclasses import dataclass, asdict
from typing import Optional

from .rig import Rig, band_for, band_edges


@dataclass
class ScanPoint:
    freq_hz: int
    smeter_db: Optional[int]
    active: bool = False


@dataclass
class Segment:
    lo_hz: int
    hi_hz: int
    peak_db: int
    center_hz: int


def scan(rig: Rig, lo_hz: int, hi_hz: int, step_hz: int = 1000,
         mode: Optional[str] = None, settle_s: float = 0.05,
         margin_db: int = 6, progress=None) -> dict:
    """Sweep [lo_hz, hi_hz] in step_hz increments; return an occupancy map.

    mode: if given, set the radio mode once before sweeping (e.g. 'USB','CW',
          'FM'). Bandwidth of the S-meter reading follows the current filter.
    settle_s: dwell after each QSY before reading the S-meter (let AGC settle).
    margin_db: activity threshold above the measured noise floor.
    progress: optional callable(done, total) for UI.
    Restores the original freq/mode when done.
    """
    orig_freq = rig.get_freq()
    orig_mode, orig_pb = rig.get_mode()

    # Sanity: a fully-closed RF gain (or engaged squelch) mutes the receiver so
    # every bin reads the S-meter floor and the map looks falsely empty. Warn.
    rx_warning = None
    try:
        rf = rig._cmd("get_level RF")[-1]
        if float(rf) <= 0.02:
            rx_warning = (f"RF gain is ~{float(rf):.2f} (receiver nearly muted) "
                          f"-- scan may read empty. Set with: radio rfgain 1.0")
    except Exception:
        pass

    if mode:
        rig.set_mode(mode)

    freqs = list(range(int(lo_hz), int(hi_hz) + 1, int(step_hz)))
    points: list[ScanPoint] = []
    try:
        for i, f in enumerate(freqs):
            rig.set_freq(f)
            time.sleep(settle_s)
            s = rig.get_smeter()
            points.append(ScanPoint(freq_hz=f, smeter_db=s))
            if progress:
                progress(i + 1, len(freqs))
    finally:
        # Always restore the operator's original tuning.
        rig.set_freq(orig_freq)
        rig.set_mode(orig_mode, orig_pb)

    vals = [p.smeter_db for p in points if p.smeter_db is not None]
    noise_floor = int(statistics.median(vals)) if vals else -54
    thresh = noise_floor + margin_db
    for p in points:
        p.active = p.smeter_db is not None and p.smeter_db >= thresh

    segments = _group_segments(points, step_hz)
    return {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **({"rx_warning": rx_warning} if rx_warning else {}),
        "lo_hz": lo_hz,
        "hi_hz": hi_hz,
        "step_hz": step_hz,
        "mode": mode or orig_mode,
        "band": band_for(lo_hz),
        "noise_floor_db": noise_floor,
        "threshold_db": thresh,
        "n_points": len(points),
        "n_active": sum(1 for p in points if p.active),
        "points": [asdict(p) for p in points],
        "active_segments": [asdict(s) for s in segments],
    }


def _group_segments(points: list[ScanPoint], step_hz: int) -> list[Segment]:
    """Coalesce runs of adjacent active bins into segments with a peak."""
    segs: list[Segment] = []
    run: list[ScanPoint] = []
    for p in points + [ScanPoint(freq_hz=-1, smeter_db=None, active=False)]:
        if p.active:
            run.append(p)
        elif run:
            peak = max(run, key=lambda x: (x.smeter_db or -999))
            lo = run[0].freq_hz
            hi = run[-1].freq_hz
            segs.append(Segment(lo_hz=lo, hi_hz=hi,
                                peak_db=peak.smeter_db or 0,
                                center_hz=(lo + hi) // 2))
            run = []
    return segs


def scan_band(rig: Rig, band: str, **kw) -> dict:
    edges = band_edges(band)
    if not edges:
        raise ValueError(f"unknown band {band!r}")
    return scan(rig, edges[0], edges[1], **kw)


def plot(result: dict, png_path: str) -> str:
    """Render the scan to a PNG (S-meter vs frequency). Returns the path."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pts = result["points"]
    xs = [p["freq_hz"] / 1e6 for p in pts]
    ys = [p["smeter_db"] if p["smeter_db"] is not None else result["noise_floor_db"]
          for p in pts]
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(xs, ys, lw=0.8, color="#1f77b4")
    ax.axhline(result["threshold_db"], color="red", ls="--", lw=0.8,
               label=f"active threshold ({result['threshold_db']} dB)")
    for seg in result["active_segments"]:
        ax.axvspan(seg["lo_hz"] / 1e6, seg["hi_hz"] / 1e6,
                   color="orange", alpha=0.25)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("S-meter (dB rel S9)")
    band = result.get("band") or ""
    ax.set_title(f"Band occupancy {band} — {result['scanned_at']} "
                 f"({result['n_active']}/{result['n_points']} active)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(png_path, dpi=110)
    plt.close(fig)
    return png_path
