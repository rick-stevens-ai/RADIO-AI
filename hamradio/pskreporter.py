"""
hamradio.pskreporter — query PSKReporter for who is hearing us (or anyone).

PSKReporter (pskreporter.info) aggregates reception reports from thousands of
digital-mode receivers worldwide. After we transmit FT8/etc., stations that
decoded us upload spots. Querying senderCallsign=OURCALL tells us exactly who
heard us, where, and at what SNR -- a real-world propagation / "am I getting
out?" check that needs no second radio.

API: https://retrieve.pskreporter.info/query
  senderCallsign=CALL      reports where CALL was the transmitter (heard BY others)
  receiverCallsign=CALL    reports where CALL was the receiver (what CALL heard)
  flowStartSeconds=-N      look back N seconds (negative)
  rronly=1                 reception reports only (compact)
Be polite: PSKReporter asks for >= ~5 min between identical queries. We cache.
"""
from __future__ import annotations
import math
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

QUERY_URL = "https://retrieve.pskreporter.info/query"
_cache: dict = {}
_CACHE_TTL = 300  # seconds (respect PSKReporter rate limits)


def _grid_to_ll(g: str):
    g = (g or "").upper()
    if len(g) < 4 or not g[0].isalpha():
        return None
    try:
        lon = (ord(g[0]) - 65) * 20 - 180
        lat = (ord(g[1]) - 65) * 10 - 90
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        if len(g) >= 6 and g[4].isalpha():
            lon += (ord(g[4]) - 65) * (2 / 24) + 2 / 48
            lat += (ord(g[5]) - 65) * (1 / 24) + 1 / 48
        else:
            lon += 1
            lat += 0.5
        return lat, lon
    except (ValueError, IndexError):
        return None


def _dist_km(a, b) -> Optional[int]:
    if not a or not b:
        return None
    R = 6371.0
    la1, lo1 = map(math.radians, a)
    la2, lo2 = map(math.radians, b)
    d = 2 * R * math.asin(math.sqrt(
        math.sin((la2 - la1) / 2) ** 2 +
        math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    return int(d)


def _bearing(a, b) -> Optional[int]:
    if not a or not b:
        return None
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dl = lo2 - lo1
    x = math.sin(dl) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return int((math.degrees(math.atan2(x, y)) + 360) % 360)


def who_hears(call: str, my_grid: str = "EN51TP", since_s: int = 900,
              timeout: float = 30.0) -> dict:
    """Return the stations that heard `call` in the last `since_s` seconds.

    Result: summary stats + a per-receiver list (call, grid, snr, distance_km,
    bearing_deg, dxcc, age_min), most-recent first, de-duplicated by receiver.
    """
    key = (call.upper(), since_s // 60)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return {**cached[1], "cached": True}

    url = (f"{QUERY_URL}?senderCallsign={call.upper()}"
           f"&flowStartSeconds=-{int(since_s)}&rronly=1")
    req = urllib.request.Request(url, headers={"User-Agent": "hamradio-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml = resp.read()
    root = ET.fromstring(xml)
    reps = [r.attrib for r in root.iter("receptionReport")]

    me = _grid_to_ll(my_grid)
    now = time.time()
    rows = []
    seen = set()
    for r in sorted(reps, key=lambda x: -int(x.get("flowStartSeconds", 0))):
        rx = r.get("receiverCallsign", "")
        if rx in seen:
            continue
        seen.add(rx)
        loc = r.get("receiverLocator", "")
        ll = _grid_to_ll(loc)
        rows.append({
            "receiver": rx,
            "grid": loc,
            "snr_db": _int_or(r.get("sNR")),
            "distance_km": _dist_km(me, ll),
            "bearing_deg": _bearing(me, ll),
            "dxcc": r.get("receiverDXCC", ""),
            "freq_hz": _int_or(r.get("frequency")),
            "mode": r.get("mode", ""),
            "age_min": int((now - int(r.get("flowStartSeconds", 0))) / 60),
        })
    dists = [x["distance_km"] for x in rows if x["distance_km"]]
    dxccs = sorted({x["dxcc"] for x in rows if x["dxcc"]})
    result = {
        "callsign": call.upper(),
        "window_s": since_s,
        "unique_receivers": len(rows),
        "max_km": max(dists) if dists else None,
        "avg_km": sum(dists) // len(dists) if dists else None,
        "dxcc_entities": dxccs,
        "reports": rows,
    }
    _cache[key] = (time.time(), result)
    return result


def _int_or(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default
