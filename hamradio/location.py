"""
hamradio.location — fast, fully-local callsign -> location lookup.

Three data sources, merged, no network at query time:
  1. FCC ULS amateur DB (US hams): name, city, state -> SQLite indexed by call.
     Imported from the FCC weekly dump (EN.dat + HD.dat) via build_fcc_db().
  2. DXCC prefix table (below): country/entity + representative lat/lon + CQ zone
     for ANY callsign worldwide (this is how we ID DX like V31DL, CO8LY...).
  3. Maidenhead grid -> lat/lon (when the station sent a grid in the FT8 msg).

lookup(call, grid) returns the richest merged result: country, us_state, city,
name, lat/lon, distance_km + bearing from our QTH (EN51TP), and the source(s).
"""
from __future__ import annotations
import os
import re
import math
import sqlite3
import pathlib
from typing import Optional

DATA_DIR = pathlib.Path(os.path.expanduser("~/radio/data"))
FCC_DB = DATA_DIR / "fcc_amat.sqlite"
MY_GRID = "EN51TP"

# --- Maidenhead grid -> lat/lon (shared with pskreporter logic) -------------
def grid_to_ll(g: str):
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


def distance_km(a, b) -> Optional[int]:
    if not a or not b:
        return None
    R = 6371.0
    la1, lo1 = map(math.radians, a)
    la2, lo2 = map(math.radians, b)
    d = 2 * R * math.asin(math.sqrt(
        math.sin((la2 - la1) / 2) ** 2 +
        math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    return int(d)


def bearing_deg(a, b) -> Optional[int]:
    if not a or not b:
        return None
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dl = lo2 - lo1
    x = math.sin(dl) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return int((math.degrees(math.atan2(x, y)) + 360) % 360)


# --- DXCC prefix table ------------------------------------------------------
# (prefix_pattern, country, lat, lon). Longest-prefix match wins. lat/lon are a
# representative point for the entity (good enough for distance/bearing/ID).
# US split into call-area/state handled separately via FCC DB; the K/W/N/A here
# is a coarse continental-US fallback for when FCC lookup misses.
_DXCC = [
    # --- most specific first (order matters; we sort by length) ---
    ("KH6", "Hawaii", 21.3, -157.9), ("KL7", "Alaska", 64.2, -149.5),
    ("KL", "Alaska", 64.2, -149.5), ("KH2", "Guam", 13.4, 144.7),
    ("KP4", "Puerto Rico", 18.2, -66.5), ("KP3", "Puerto Rico", 18.2, -66.5),
    ("KP2", "US Virgin Islands", 17.7, -64.8), ("NP4", "Puerto Rico", 18.2, -66.5),
    ("WP4", "Puerto Rico", 18.2, -66.5),
    ("VE", "Canada", 56.1, -106.3), ("VA", "Canada", 56.1, -106.3),
    ("VO", "Canada", 53.1, -60.0), ("VY", "Canada", 64.0, -110.0),
    ("XE", "Mexico", 23.6, -102.5), ("XF", "Mexico", 23.6, -102.5),
    ("4A", "Mexico", 23.6, -102.5), ("6D", "Mexico", 23.6, -102.5),
    ("CO", "Cuba", 21.5, -79.5), ("CM", "Cuba", 21.5, -79.5),
    ("CL", "Cuba", 21.5, -79.5),
    ("V3", "Belize", 17.2, -88.5), ("TG", "Guatemala", 15.5, -90.3),
    ("TI", "Costa Rica", 9.7, -83.8), ("HP", "Panama", 8.5, -80.8),
    ("YN", "Nicaragua", 12.9, -85.2), ("HR", "Honduras", 15.2, -86.2),
    ("YS", "El Salvador", 13.8, -88.9),
    ("HI", "Dominican Republic", 18.7, -70.2), ("HH", "Haiti", 19.0, -72.4),
    ("6Y", "Jamaica", 18.1, -77.3), ("8P", "Barbados", 13.2, -59.5),
    ("9Y", "Trinidad & Tobago", 10.7, -61.2), ("J3", "Grenada", 12.1, -61.7),
    ("J6", "St. Lucia", 13.9, -61.0), ("J7", "Dominica", 15.4, -61.4),
    ("J8", "St. Vincent", 13.0, -61.2), ("V4", "St. Kitts & Nevis", 17.3, -62.7),
    ("FM", "Martinique", 14.6, -61.0), ("FG", "Guadeloupe", 16.2, -61.6),
    ("ZF", "Cayman Is", 19.3, -81.3), ("C6", "Bahamas", 25.0, -77.4),
    ("VP2", "Anguilla/Montserrat", 18.2, -63.1), ("VP5", "Turks & Caicos", 21.7, -71.6),
    ("VP9", "Bermuda", 32.3, -64.8),
    # South America
    ("PY", "Brazil", -14.2, -51.9), ("PP", "Brazil", -14.2, -51.9),
    ("PT", "Brazil", -14.2, -51.9), ("PR", "Brazil", -14.2, -51.9),
    ("PU", "Brazil", -14.2, -51.9), ("ZV", "Brazil", -14.2, -51.9),
    ("ZZ", "Brazil", -14.2, -51.9),
    ("LU", "Argentina", -38.4, -63.6), ("CE", "Chile", -35.7, -71.5),
    ("CX", "Uruguay", -32.5, -55.8), ("CP", "Bolivia", -16.3, -63.6),
    ("OA", "Peru", -9.2, -75.0), ("HC", "Ecuador", -1.8, -78.2),
    ("HK", "Colombia", 4.6, -74.3), ("YV", "Venezuela", 6.4, -66.6),
    ("ZP", "Paraguay", -23.4, -58.4), ("PZ", "Suriname", 4.0, -56.0),
    ("8R", "Guyana", 5.0, -58.9), ("HZ", "Saudi Arabia", 24.0, 45.0),
    # Europe
    ("G", "England", 52.5, -1.5), ("M", "England", 52.5, -1.5),
    ("2E", "England", 52.5, -1.5), ("GM", "Scotland", 56.5, -4.2),
    ("GW", "Wales", 52.3, -3.8), ("GI", "N. Ireland", 54.6, -6.6),
    ("GD", "Isle of Man", 54.2, -4.5), ("GJ", "Jersey", 49.2, -2.1),
    ("EI", "Ireland", 53.4, -8.0), ("EJ", "Ireland", 53.4, -8.0),
    ("F", "France", 46.6, 2.4), ("TM", "France", 46.6, 2.4),
    ("DL", "Germany", 51.2, 10.4), ("DA", "Germany", 51.2, 10.4),
    ("DB", "Germany", 51.2, 10.4), ("DD", "Germany", 51.2, 10.4),
    ("DF", "Germany", 51.2, 10.4), ("DG", "Germany", 51.2, 10.4),
    ("DH", "Germany", 51.2, 10.4), ("DJ", "Germany", 51.2, 10.4),
    ("DK", "Germany", 51.2, 10.4), ("DM", "Germany", 51.2, 10.4),
    ("DO", "Germany", 51.2, 10.4),
    ("I", "Italy", 41.9, 12.6), ("EA", "Spain", 40.4, -3.7),
    ("EB", "Spain", 40.4, -3.7), ("EC", "Spain", 40.4, -3.7),
    ("CT", "Portugal", 39.4, -8.2), ("CU", "Azores", 38.5, -28.2),
    ("PA", "Netherlands", 52.1, 5.3), ("PB", "Netherlands", 52.1, 5.3),
    ("PC", "Netherlands", 52.1, 5.3), ("PD", "Netherlands", 52.1, 5.3),
    ("PE", "Netherlands", 52.1, 5.3), ("PF", "Netherlands", 52.1, 5.3),
    ("PG", "Netherlands", 52.1, 5.3), ("PH", "Netherlands", 52.1, 5.3),
    ("PI", "Netherlands", 52.1, 5.3),
    ("ON", "Belgium", 50.5, 4.5), ("OO", "Belgium", 50.5, 4.5),
    ("LX", "Luxembourg", 49.8, 6.1), ("HB", "Switzerland", 46.8, 8.2),
    ("OE", "Austria", 47.5, 14.6), ("OK", "Czech Rep", 49.8, 15.5),
    ("OM", "Slovakia", 48.7, 19.7), ("HA", "Hungary", 47.2, 19.5),
    ("HG", "Hungary", 47.2, 19.5), ("SP", "Poland", 51.9, 19.1),
    ("SN", "Poland", 51.9, 19.1), ("OZ", "Denmark", 56.3, 9.5),
    ("LA", "Norway", 60.5, 8.5), ("LB", "Norway", 60.5, 8.5),
    ("SM", "Sweden", 60.1, 18.6), ("SA", "Sweden", 60.1, 18.6),
    ("OH", "Finland", 61.9, 25.7), ("ES", "Estonia", 58.6, 25.0),
    ("YL", "Latvia", 56.9, 24.6), ("LY", "Lithuania", 55.2, 23.9),
    ("UR", "Ukraine", 48.4, 31.2), ("US", "Ukraine", 48.4, 31.2),
    ("EW", "Belarus", 53.7, 27.9), ("R", "Russia", 61.5, 105.3),
    ("UA", "Russia", 61.5, 105.3), ("YO", "Romania", 45.9, 25.0),
    ("LZ", "Bulgaria", 42.7, 25.5), ("YU", "Serbia", 44.0, 21.0),
    ("9A", "Croatia", 45.1, 15.2), ("S5", "Slovenia", 46.2, 15.0),
    ("SV", "Greece", 39.1, 22.0), ("TA", "Turkey", 39.0, 35.2),
    ("Z3", "N. Macedonia", 41.6, 21.7), ("E7", "Bosnia", 43.9, 17.7),
    ("4O", "Montenegro", 42.7, 19.4), ("ZA", "Albania", 41.2, 20.2),
    ("EA6", "Balearic Is", 39.6, 2.9), ("EA8", "Canary Is", 28.3, -16.5),
    # Africa / Asia / Oceania (common ones)
    ("ZS", "South Africa", -30.6, 22.9), ("CN", "Morocco", 31.8, -7.1),
    ("SU", "Egypt", 26.8, 30.8), ("5Z", "Kenya", 0.0, 37.9),
    ("JA", "Japan", 36.2, 138.3), ("JH", "Japan", 36.2, 138.3),
    ("JR", "Japan", 36.2, 138.3), ("JE", "Japan", 36.2, 138.3),
    ("HL", "South Korea", 36.5, 127.9), ("BV", "Taiwan", 23.7, 121.0),
    ("BY", "China", 35.9, 104.2), ("BG", "China", 35.9, 104.2),
    ("BH", "China", 35.9, 104.2), ("BD", "China", 35.9, 104.2),
    ("VU", "India", 20.6, 79.0), ("9V", "Singapore", 1.35, 103.8),
    ("YB", "Indonesia", -0.8, 113.9), ("DU", "Philippines", 12.9, 121.8),
    ("VK", "Australia", -25.3, 133.8), ("ZL", "New Zealand", -41.0, 174.9),
    ("KH", "US Pacific", 21.3, -157.9),
    # Coarse continental-US fallback (FCC DB gives the real state)
    ("K", "United States", 39.8, -98.6), ("W", "United States", 39.8, -98.6),
    ("N", "United States", 39.8, -98.6), ("A", "United States", 39.8, -98.6),
]
# longest-prefix-first for correct matching
_DXCC_SORTED = sorted(_DXCC, key=lambda x: -len(x[0]))


def _strip_call(call: str) -> str:
    """Remove portable/suffix decorations (e.g. W4/DL1ABC, KD9NWA/P) -> core."""
    c = call.upper().strip()
    # take the part with the most 'callsign-like' shape
    parts = c.split("/")
    if len(parts) > 1:
        # portable prefix like 'W4/DL1ABC' -> country is the prefix W4; but
        # 'DL1ABC/P' -> DL1ABC. Heuristic: pick the longest alnum part that
        # contains a digit (real call), unless a short leading prefix is a
        # known DXCC prefix.
        cand = max(parts, key=lambda p: (any(ch.isdigit() for ch in p), len(p)))
        # if a short leading part is a location prefix (<=3, ends alnum), honor it
        if len(parts[0]) <= 3 and parts[0] != cand and re.match(r"^[A-Z0-9]+$", parts[0]):
            return parts[0]  # portable INTO that prefix's area
        return cand
    return c


def dxcc_lookup(call: str):
    c = _strip_call(call)
    for pfx, country, lat, lon in _DXCC_SORTED:
        if c.startswith(pfx):
            return {"country": country, "lat": lat, "lon": lon, "prefix": pfx}
    return None


# --- FCC ULS SQLite ---------------------------------------------------------
def build_fcc_db(en_dat: str, hd_dat: str, db_path: str = str(FCC_DB)) -> dict:
    """Build the FCC amateur SQLite from EN.dat (entities/names/addresses).
    EN.dat fields (0-indexed, pipe-delimited): [0]='EN', [1]=unique_sys_id,
    [4]=call_sign, [7]=entity_name, [16]=city, [17]=state, [18]=zip_code.
    HD.dat gives license status ([5]='A' active) keyed by unique_sys_id [1].
    """
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS hams")
    cur.execute("""CREATE TABLE hams(
        call TEXT PRIMARY KEY, name TEXT, city TEXT, state TEXT, zip TEXT)""")
    # active license set from HD.dat
    active = set()
    if os.path.exists(hd_dat):
        with open(hd_dat, encoding="latin-1") as f:
            for line in f:
                p = line.rstrip("\n").split("|")
                if len(p) > 5 and p[5] == "A":
                    active.add(p[1])
    rows = []
    n = 0
    with open(en_dat, encoding="latin-1") as f:
        for line in f:
            p = line.rstrip("\n").split("|")
            if len(p) < 19 or not p[4]:
                continue
            if active and p[1] not in active:
                continue
            rows.append((p[4].upper(), p[7].strip(), p[16].strip(),
                         p[17].strip(), p[18].strip()))
            if len(rows) >= 5000:
                cur.executemany("INSERT OR REPLACE INTO hams VALUES(?,?,?,?,?)", rows)
                n += len(rows); rows = []
    if rows:
        cur.executemany("INSERT OR REPLACE INTO hams VALUES(?,?,?,?,?)", rows)
        n += len(rows)
    con.commit()
    cur.execute("SELECT COUNT(*) FROM hams")
    total = cur.fetchone()[0]
    con.close()
    return {"imported": n, "rows": total, "db": db_path}


_fcc_con = None
def _fcc():
    global _fcc_con
    if _fcc_con is None and FCC_DB.exists():
        _fcc_con = sqlite3.connect(f"file:{FCC_DB}?mode=ro", uri=True,
                                   check_same_thread=False)
    return _fcc_con


def fcc_lookup(call: str):
    con = _fcc()
    if not con:
        return None
    row = con.execute(
        "SELECT call,name,city,state,zip FROM hams WHERE call=?",
        (_strip_call(call),)).fetchone()
    if not row:
        return None
    return {"call": row[0], "name": row[1], "city": row[2],
            "us_state": row[3], "zip": row[4]}


# US state centroids for lat/lon when FCC gives a state but no grid ----------
_STATE_LL = {
    "AL": (32.8, -86.8), "AK": (64.2, -149.5), "AZ": (34.2, -111.9),
    "AR": (34.9, -92.4), "CA": (37.2, -119.7), "CO": (39.0, -105.5),
    "CT": (41.6, -72.7), "DE": (39.0, -75.5), "FL": (28.6, -82.4),
    "GA": (32.6, -83.4), "HI": (21.3, -157.9), "ID": (44.4, -114.6),
    "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.0, -93.5),
    "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.0, -92.0),
    "ME": (45.4, -69.2), "MD": (39.0, -76.8), "MA": (42.3, -71.8),
    "MI": (44.3, -85.4), "MN": (46.3, -94.3), "MS": (32.7, -89.7),
    "MO": (38.4, -92.5), "MT": (46.9, -110.4), "NE": (41.5, -99.8),
    "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.1, -74.7),
    "NM": (34.4, -106.1), "NY": (42.9, -75.5), "NC": (35.5, -79.4),
    "ND": (47.4, -100.5), "OH": (40.3, -82.8), "OK": (35.6, -97.5),
    "OR": (44.0, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.5),
    "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.9, -86.4),
    "TX": (31.5, -99.3), "UT": (39.3, -111.7), "VT": (44.1, -72.7),
    "VA": (37.5, -78.9), "WA": (47.4, -120.5), "WV": (38.6, -80.6),
    "WI": (44.6, -89.9), "WY": (43.0, -107.6), "DC": (38.9, -77.0),
}


def lookup(call: str, grid: str = "", my_grid: str = MY_GRID) -> dict:
    """Merge FCC + DXCC + grid into one location record for `call`."""
    call = call.upper().strip()
    out = {"call": call, "sources": []}
    dx = dxcc_lookup(call)
    if dx:
        out["country"] = dx["country"]
        out["lat"], out["lon"] = dx["lat"], dx["lon"]
        out["sources"].append("dxcc")
    fcc = fcc_lookup(call)
    if fcc:
        out.update({k: v for k, v in fcc.items() if v and k != "call"})
        st = fcc.get("us_state")
        if st in _STATE_LL:
            out["lat"], out["lon"] = _STATE_LL[st]  # better than country centroid
        out["sources"].append("fcc")
    if grid:
        ll = grid_to_ll(grid)
        if ll:
            out["grid"] = grid.upper()
            out["lat"], out["lon"] = ll          # most precise -> wins
            out["sources"].append("grid")
    me = grid_to_ll(my_grid)
    ll = (out.get("lat"), out.get("lon")) if out.get("lat") is not None else None
    if ll and ll[0] is not None:
        out["distance_km"] = distance_km(me, ll)
        out["bearing_deg"] = bearing_deg(me, ll)
    out["is_dx"] = bool(out.get("country") and out["country"] not in ("United States",))
    if not out["sources"]:
        out["note"] = "unknown callsign/prefix"
    return out
