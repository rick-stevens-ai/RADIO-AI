"""
hamradio.swbc — HF / shortwave BROADCAST index & map (non-ham bands).

This is a structured, queryable index of the international shortwave broadcast
(SWBC) spectrum plus standard-time and selected utility voice stations that the
IC-7300 can receive (0.03-74.8 MHz general-coverage RX). It answers:

  * "What broadcast bands exist and which can THIS station hear?"      -> BANDS
  * "What's likely on the air RIGHT NOW that we could tune?"           -> now_on_air()
  * "What station is on this frequency?"                                -> whats_on(hz)
  * "Show me everything in Spanish / aimed at N.America"                -> find()
  * live: scan a broadcast band and label detected carriers            -> survey()

SCOPE & HONESTY:
  * The band PLAN (meter bands, ITU segments, WWV/CHU/utility) is exact and
    stable -- it's regulatory allocation, not schedule.
  * The STATION SCHEDULE is a *curated* snapshot of large, long-running
    broadcasters (VOA/RFA/RHC/CRI/BBC/RRI/WWCR/etc.) with typical UTC windows,
    languages, targets and transmitter sites. Shortwave schedules change with
    the A/B (summer/winter) seasons; treat times as guidance, verify on-air.
    Authoritative live data: EiBi (eibispace.de), HFCC, short-wave.info.
  * Times are UTC, 24h, as (start, end) minutes-from-midnight; wrap past 24h
    means it runs through midnight.

All frequencies are Hz. "meters" is the traditional SW band name (e.g. 31m).
"""
from __future__ import annotations
import datetime as _dt
from typing import Optional

# Receiver limit of the IC-7300 (general-coverage RX). Anything above is NOT
# tunable on this station.
RX_MAX_HZ = 74_800_000
RX_MIN_HZ = 30_000

# ---------------------------------------------------------------------------
# 1. HF BROADCAST BAND PLAN  (ITU international broadcast segments + time sigs)
#    name, lo_hz, hi_hz, kind, note
# ---------------------------------------------------------------------------
# kind: "swbc" international broadcast band | "time" standard time/freq |
#       "mw" medium-wave AM broadcast | "lw" long-wave | "utility" voice utility
BANDS = [
    ("LW",     148_500,     283_500, "lw",   "Long-wave AM broadcast (Europe/Africa/Asia)"),
    ("MW",     530_000,   1_700_000, "mw",   "Medium-wave AM broadcast band"),
    ("120m",  2_300_000,  2_495_000, "swbc", "Tropical band (regional, night)"),
    ("90m",   3_200_000,  3_400_000, "swbc", "Tropical band (regional, night)"),
    ("75m",   3_900_000,  4_000_000, "swbc", "60m/75m SWBC (shared, region-dependent)"),
    ("60m",   4_750_000,  5_060_000, "swbc", "Tropical band (regional, night)"),
    ("49m",   5_800_000,  6_200_000, "swbc", "Major nighttime intl band"),
    ("41m",   7_200_000,  7_600_000, "swbc", "Major night band (above 40m ham)"),
    ("31m",   9_250_000,  9_990_000, "swbc", "The workhorse band, day & night"),
    ("25m",  11_600_000, 12_200_000, "swbc", "Day/night intl band"),
    ("22m",  13_570_000, 13_870_000, "swbc", "Daytime band"),
    ("19m",  15_100_000, 15_830_000, "swbc", "Strong daytime band"),
    ("16m",  17_480_000, 17_900_000, "swbc", "Daytime, long-haul"),
    ("15m",  18_900_000, 19_020_000, "swbc", "Minor daytime band"),
    ("13m",  21_450_000, 21_850_000, "swbc", "Daytime, high-SFI openings"),
    ("11m",  25_670_000, 26_100_000, "swbc", "Daytime, sporadic-E / high solar"),
]

# Standard time & frequency stations (AM voice + ticks) — reliable test signals.
TIME_STATIONS = [
    # call, hz, site, note
    ("WWV",   2_500_000,  "Ft Collins CO", "Male voice @ :08-:15; 100W"),
    ("WWV",   5_000_000,  "Ft Collins CO", "Male voice announce; 10kW"),
    ("WWV",  10_000_000,  "Ft Collins CO", "Male voice announce; 10kW"),
    ("WWV",  15_000_000,  "Ft Collins CO", "Male voice announce; 10kW (often strongest by day)"),
    ("WWV",  20_000_000,  "Ft Collins CO", "Male voice; 2.5kW"),
    ("WWVH", 15_000_000,  "Kekaha HI",     "Female voice @ :00-:08 (before WWV male)"),
    ("CHU",   3_330_000,  "Ottawa ON",     "Canadian time, English+French, USB-ish (3850 too)"),
    ("CHU",   7_850_000,  "Ottawa ON",     "Canadian time, English+French"),
    ("CHU",  14_670_000,  "Ottawa ON",     "Canadian time, English+French"),
]

# ---------------------------------------------------------------------------
# 2. CURATED STATION / SCHEDULE INDEX
#    Each entry: hz, station, language, target, site, (utc_start, utc_end) 24h,
#    days ("daily"/"Mon-Fri"/...), note. Times UTC. Multiple entries per freq ok.
#    This is a representative snapshot of large, long-running services aimed at
#    or hearable from North America (our QTH: EN51, central US). VERIFY on-air.
# ---------------------------------------------------------------------------
def _t(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[2:])

# (hz, station, lang, target, site, start, end, days, note)
_RAW_SCHEDULE = [
    # ---- Radio Havana Cuba (strong into central US, esp. evenings) ----
    (6_000_000,  "Radio Havana Cuba", "Spanish", "Americas", "Bauta CU", "0000", "0500", "daily", "Big signal at night"),
    (6_100_000,  "Radio Havana Cuba", "English", "N.America", "Bauta CU", "0100", "0600", "daily", "English service"),
    (5_040_000,  "Radio Havana Cuba", "Spanish", "Americas", "Bauta CU", "0000", "0700", "daily", ""),
    (11_760_000, "Radio Havana Cuba", "Spanish", "Americas", "Bauta CU", "1300", "1600", "daily", "Daytime 25m"),
    (15_140_000, "Radio Havana Cuba", "Spanish", "Americas", "Bauta CU", "1400", "1700", "daily", "Daytime 19m"),
    # ---- WWCR (Nashville TN) — English religious/political, easy US catch ----
    (4_840_000,  "WWCR", "English", "N.America", "Nashville TN", "0000", "1200", "daily", "49m night"),
    (7_490_000,  "WWCR", "English", "N.America", "Nashville TN", "0000", "1300", "daily", "41m"),
    (9_350_000,  "WWCR", "English", "N.America", "Nashville TN", "1200", "2400", "daily", "31m day"),
    (12_160_000, "WWCR", "English", "N.America", "Nashville TN", "1300", "2200", "daily", "25m day"),
    (15_825_000, "WWCR", "English", "N.America", "Nashville TN", "1400", "2200", "daily", "19m day, strong"),
    # ---- WRMI Radio Miami Intl — multi-program brokered SW ----
    (5_010_000,  "WRMI", "English", "Americas", "Okeechobee FL", "0000", "1200", "daily", "49m"),
    (9_455_000,  "WRMI", "English", "Americas", "Okeechobee FL", "1100", "2400", "daily", "31m, many programs"),
    (15_770_000, "WRMI", "English", "N.America/Europe", "Okeechobee FL", "1400", "2200", "daily", "19m day"),
    # ---- China Radio International (via relays; strong, multilingual) ----
    (9_570_000,  "China Radio Intl", "English", "N.America", "Kashi/relay", "0000", "0200", "daily", "CRI English NA"),
    (13_740_000, "China Radio Intl", "English", "N.America", "relay", "1400", "1600", "daily", "22m day"),
    (11_885_000, "China Radio Intl", "Chinese", "Global", "China", "1200", "1400", "daily", "Mandarin"),
    # ---- Radio Romania International — reliable English to NA ----
    (9_700_000,  "Radio Romania Intl", "English", "N.America", "Tiganesti RO", "0100", "0200", "daily", "RRI English NA"),
    (11_800_000, "Radio Romania Intl", "English", "N.America", "Galbeni RO", "1800", "1900", "daily", "Evening EU->NA"),
    # ---- BBC World Service (via Ascension/relays; better on E.coast) ----
    (5_875_000,  "BBC World Service", "English", "Africa/Atl", "Ascension", "0300", "0700", "daily", "May be weak central US"),
    (9_915_000,  "BBC World Service", "English", "Africa", "Ascension", "1800", "2200", "daily", ""),
    # ---- Voice of America / RFA / Radio Marti (US intl) ----
    (5_980_000,  "Radio Marti", "Spanish", "Cuba", "Greenville NC/relay", "0000", "0500", "daily", "US->Cuba Spanish"),
    (7_365_000,  "Radio Marti", "Spanish", "Cuba", "relay", "1100", "1400", "daily", ""),
    (15_580_000, "Voice of America", "English", "Africa", "Botswana/Sao Tome", "1600", "2100", "daily", "VOA Africa"),
    (9_885_000,  "Radio Free Asia", "Mandarin/Tibetan", "Asia", "relay", "1400", "1600", "daily", "RFA"),
    # ---- Radio Nacional de Amazonia / Brazilian (Portuguese, night) ----
    (11_780_000, "Radio Nacional Amazonia", "Portuguese", "Brazil", "Brasilia BR", "2100", "0200", "daily", "Portuguese, night on 25m"),
    (6_180_000,  "Radio Nacional Amazonia", "Portuguese", "Brazil", "Brasilia BR", "0900", "0200", "daily", "49m"),
    # ---- Radio Exterior de España (Spanish to Americas) ----
    (17_855_000, "Radio Exterior Espana", "Spanish", "Americas", "Noblejas ES", "1400", "1800", "M-F", "REE 16m to Americas"),
    # ---- Reach Beyond / religious multilingual (broad language coverage) ----
    (11_920_000, "Reach Beyond", "Multiple", "S.America", "relay", "0000", "0300", "daily", "Multi-language mission bcast"),
]

SCHEDULE = [
    {"hz": hz, "station": st, "language": lg, "target": tg, "site": si,
     "utc_start": _t(s), "utc_end": _t(e), "days": d, "note": n}
    for (hz, st, lg, tg, si, s, e, d, n) in _RAW_SCHEDULE
]


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def band_for(hz: int) -> Optional[dict]:
    """Which broadcast band (or time/utility segment) contains `hz`?"""
    for name, lo, hi, kind, note in BANDS:
        if lo <= hz <= hi:
            return {"band": name, "lo_hz": lo, "hi_hz": hi, "kind": kind,
                    "note": note, "rx_ok": hz <= RX_MAX_HZ}
    return None


def receivable_bands() -> list[dict]:
    """All bands this station's RX can tune (all of them, actually — SWBC is
    well under 74.8 MHz — but we annotate day/night suitability)."""
    out = []
    for name, lo, hi, kind, note in BANDS:
        out.append({"band": name, "lo_hz": lo, "hi_hz": hi, "kind": kind,
                    "note": note, "rx_ok": lo <= RX_MAX_HZ,
                    "best": _day_night_hint(name)})
    return out


def _day_night_hint(band: str) -> str:
    """Rule-of-thumb: low bands (>25m wavelength) propagate at night; high bands
    (<=22m) by day. Central-US general guidance."""
    night = {"LW", "MW", "120m", "90m", "75m", "60m", "49m", "41m"}
    day = {"22m", "19m", "16m", "15m", "13m", "11m"}
    if band in night:
        return "night"
    if band in day:
        return "day"
    return "day+night"   # 31m/25m span both


def _now_utc_min() -> int:
    n = _dt.datetime.now(_dt.timezone.utc)
    return n.hour * 60 + n.minute


def _active_at(entry: dict, utc_min: int) -> bool:
    s, e = entry["utc_start"], entry["utc_end"]
    if s <= e:
        return s <= utc_min < e
    return utc_min >= s or utc_min < e     # wraps midnight


def now_on_air(utc_min: Optional[int] = None, lang: Optional[str] = None,
               target: Optional[str] = None) -> dict:
    """Scheduled broadcasts likely on the air now (UTC), with a day/night
    propagation hint per band. Optionally filter by language/target substring."""
    if utc_min is None:
        utc_min = _now_utc_min()
    rows = []
    for e in SCHEDULE:
        if not _active_at(e, utc_min):
            continue
        if lang and lang.lower() not in e["language"].lower():
            continue
        if target and target.lower() not in e["target"].lower():
            continue
        b = band_for(e["hz"])
        rows.append({**e,
                     "band": b["band"] if b else None,
                     "prop": _day_night_hint(b["band"]) if b else None,
                     "freq_khz": round(e["hz"] / 1000)})
    rows.sort(key=lambda r: r["hz"])
    return {"utc_now": f"{utc_min // 60:02d}{utc_min % 60:02d}",
            "count": len(rows), "broadcasts": rows}


def whats_on(hz: int, tol_hz: int = 5000, utc_min: Optional[int] = None) -> dict:
    """Everything indexed near `hz` (schedule + time stations), with which are
    active now."""
    if utc_min is None:
        utc_min = _now_utc_min()
    sched = [{**e, "active_now": _active_at(e, utc_min),
              "freq_khz": round(e["hz"] / 1000)}
             for e in SCHEDULE if abs(e["hz"] - hz) <= tol_hz]
    times = [{"station": c, "hz": f, "site": s, "note": n,
              "freq_khz": round(f / 1000)}
             for (c, f, s, n) in TIME_STATIONS if abs(f - hz) <= tol_hz]
    return {"freq_hz": hz, "band": band_for(hz), "tol_hz": tol_hz,
            "scheduled": sched, "time_stations": times}


def find(lang: Optional[str] = None, station: Optional[str] = None,
         target: Optional[str] = None, band: Optional[str] = None) -> dict:
    """Filter the whole schedule index by language/station/target/band."""
    rows = []
    for e in SCHEDULE:
        b = band_for(e["hz"])
        bn = b["band"] if b else ""
        if lang and lang.lower() not in e["language"].lower():
            continue
        if station and station.lower() not in e["station"].lower():
            continue
        if target and target.lower() not in e["target"].lower():
            continue
        if band and band.lower() != bn.lower():
            continue
        rows.append({**e, "band": bn, "freq_khz": round(e["hz"] / 1000)})
    rows.sort(key=lambda r: (r["hz"], r["utc_start"]))
    return {"count": len(rows), "broadcasts": rows}


def languages() -> list[str]:
    """All languages present in the index."""
    langs = set()
    for e in SCHEDULE:
        for part in e["language"].replace("/", ",").split(","):
            langs.add(part.strip())
    return sorted(langs)


# ISO code hints so the speech decoder can be pointed at the right language.
LANG_ISO = {
    "english": "en", "spanish": "es", "portuguese": "pt", "chinese": "zh",
    "mandarin": "zh", "french": "fr", "german": "de", "russian": "ru",
    "arabic": "ar", "tibetan": "bo", "multiple": "auto",
}


def iso_for(lang: str) -> str:
    return LANG_ISO.get(lang.strip().lower(), "auto")


# ---------------------------------------------------------------------------
# 3. LIVE survey: scan a broadcast band and label detected carriers against
#    the index. Reuses the generic scan; cross-references frequencies.
# ---------------------------------------------------------------------------
def survey(scan_fn, band: str, utc_min: Optional[int] = None) -> dict:
    """Scan a broadcast band (via a provided scan callable) and annotate each
    detected active segment with the most likely station from the index.

    scan_fn(lo_hz, hi_hz) -> dict with 'segments':[{lo_hz,hi_hz,peak_db}], and
    'noise_floor_db'. (Pass a lambda wrapping hamradio.scan.sweep.)
    """
    edges = None
    for name, lo, hi, kind, note in BANDS:
        if name.lower() == band.lower():
            edges = (lo, hi, kind)
            break
    if not edges:
        return {"error": f"unknown broadcast band {band!r}",
                "known": [b[0] for b in BANDS]}
    lo, hi, kind = edges
    if utc_min is None:
        utc_min = _now_utc_min()
    scan = scan_fn(lo, hi)
    labeled = []
    segs = scan.get("active_segments", scan.get("segments", []))
    for seg in segs:
        center = (seg.get("lo_hz", 0) + seg.get("hi_hz", 0)) // 2
        cand = whats_on(center, tol_hz=6000, utc_min=utc_min)
        active = [s for s in cand["scheduled"] if s["active_now"]]
        best = active[0] if active else (cand["scheduled"][0]
                                         if cand["scheduled"] else None)
        labeled.append({
            "freq_khz": round(center / 1000),
            "peak_db": seg.get("peak_db"),
            "likely": (f"{best['station']} ({best['language']})"
                       if best else "unidentified"),
            "site": best["site"] if best else None,
        })
    labeled.sort(key=lambda r: -(r["peak_db"] or -999))
    return {"band": band, "range_khz": [lo // 1000, hi // 1000],
            "prop_hint": _day_night_hint(band),
            "noise_floor_db": scan.get("noise_floor_db"),
            "utc_now": f"{utc_min // 60:02d}{utc_min % 60:02d}",
            "detected": labeled}
