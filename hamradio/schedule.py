#!/usr/bin/env python3
"""hamradio.schedule — query the EiBi broadcast DB (~/radio/db/broadcasts.sqlite).

Functions used by the `radio now` / `radio guide` / `radio find` commands.
UTC-aware: `now` returns broadcasts scheduled to be on air at the current UTC.
"""
import sqlite3, datetime, pathlib, re

DBF = pathlib.Path.home() / "radio/db/broadcasts.sqlite"
# non-voice language/mode codes to hide by default
NONVOICE = re.compile(r"^-|^\(data|CW/Morse|^-[A-Z]{2}$")
SWBC_LO, SWBC_HI = 2300, 26100  # HF voice broadcast range

def _con():
    if not DBF.exists():
        raise RuntimeError(f"broadcast DB not found: {DBF} (run the builder)")
    c = sqlite3.connect(DBF)
    c.row_factory = sqlite3.Row
    return c

def _utc_hhmm():
    n = datetime.datetime.now(datetime.timezone.utc)
    return n.hour * 100 + n.minute, n.strftime("%H:%M UTC")

def _on_air(row, hhmm):
    a, b = row["start_utc"], row["stop_utc"]
    if a == 0 and b in (2400, 0):
        return True
    if a <= b:
        return a <= hhmm < b
    return hhmm >= a or hhmm < b  # wraps midnight

def _voice(row, include_nonvoice):
    if include_nonvoice:
        return True
    lg = row["language"] or ""
    return not NONVOICE.match(lg) and lg not in ("", "M", "CR")

def now(nam_only=True, lang=None, band=None, include_nonvoice=False, limit=60):
    hhmm, label = _utc_hhmm()
    con = _con()
    q = f"SELECT * FROM broadcasts WHERE freq_khz BETWEEN {SWBC_LO} AND {SWBC_HI}"
    if nam_only:
        q += " AND nam=1"
    rows = [r for r in con.execute(q)]
    con.close()
    out = []
    for r in rows:
        if not _on_air(r, hhmm):
            continue
        if not _voice(r, include_nonvoice):
            continue
        if lang and lang.lower() not in (r["language"] or "").lower():
            continue
        if band and not _in_band(r["freq_khz"], band):
            continue
        out.append(r)
    out.sort(key=lambda r: r["freq_khz"])
    return label, [_fmt(r) for r in out[:limit]]

def guide(freq_khz, window_khz=5.0):
    con = _con()
    rows = [r for r in con.execute(
        "SELECT * FROM broadcasts WHERE freq_khz BETWEEN ? AND ? ORDER BY start_utc",
        (freq_khz - window_khz, freq_khz + window_khz))]
    con.close()
    hhmm, _ = _utc_hhmm()
    res = []
    for r in rows:
        d = _fmt(r); d["on_air_now"] = _on_air(r, hhmm)
        res.append(d)
    return res

def find(lang, nam_only=True, now_only=True, limit=80):
    label, rows = now(nam_only=nam_only, lang=lang) if now_only else (None, _all_lang(lang, nam_only, limit))
    return label, rows

def _all_lang(lang, nam_only, limit):
    con = _con()
    q = "SELECT * FROM broadcasts WHERE language LIKE ?"
    if nam_only: q += " AND nam=1"
    q += " ORDER BY freq_khz LIMIT ?"
    rows = [_fmt(r) for r in con.execute(q, (f"%{lang}%", limit))]
    con.close()
    return rows

BANDS = {"120m":(2300,2495),"90m":(3200,3400),"75m":(3900,4000),"60m":(4750,5060),
         "49m":(5900,6200),"41m":(7200,7450),"31m":(9400,9900),"25m":(11600,12100),
         "22m":(13570,13870),"19m":(15100,15830),"16m":(17480,17900),"13m":(21450,21850)}
def _in_band(khz, band):
    r = BANDS.get(band)
    return bool(r) and r[0] <= khz <= r[1]

def _fmt(r):
    return {"freq_khz": r["freq_khz"], "time_utc": r["time_raw"],
            "days": r["days"] or "daily", "station": r["station"],
            "language": r["language"], "target": r["target"],
            "itu": r["itu"], "remarks": r["remarks"]}

def lookup_freq(freq_khz, tol=3.0):
    """For scan-langs cross-ref: what SHOULD be on this channel right now."""
    hhmm, _ = _utc_hhmm()
    con = _con()
    rows = [r for r in con.execute(
        "SELECT * FROM broadcasts WHERE freq_khz BETWEEN ? AND ?",
        (freq_khz - tol, freq_khz + tol))]
    con.close()
    live = [r for r in rows if _on_air(r, hhmm)]
    cand = live or rows
    if not cand:
        return None
    r = cand[0]
    return f"{r['station']} [{r['language']}] {r['target']}"
