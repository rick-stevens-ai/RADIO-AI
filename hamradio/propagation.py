#!/usr/bin/env python3
"""hamradio.propagation — live HF propagation prior from PSKReporter reception
reports, to prioritize which SWBC bands to scan.

Approach: pull recent reception reports (FT8/WSPR/etc.), keep those whose TX or
RX endpoint is near our QTH (EN51), aggregate per HAM band, then map each ham
band to the adjacent SWBC broadcast band and emit an openness score. Rationale:
propagation on 7 MHz ham (40m) is an excellent proxy for 6-7.4 MHz SWBC (49m/41m),
30m for 31m SWBC, 20m/17m for 25m/19m, etc.
"""
import urllib.request, xml.etree.ElementTree as ET, math, time, statistics, os

QUERY_URL = "https://retrieve.pskreporter.info/query"
MY_GRID = os.environ.get("RADIO_GRID", "EN51TP")
NEAR_KM = 2500       # endpoint within this of QTH counts as a "local path"
_cache = {}
_TTL = 240

def _grid_to_ll(g):
    g = (g or "").upper()
    if len(g) < 4 or not g[0].isalpha() or not g[1].isalpha():
        return None
    try:
        lon = (ord(g[0])-65)*20 - 180 + int(g[2])*2 + 1
        lat = (ord(g[1])-65)*10 - 90  + int(g[3])*1 + 0.5
        return lat, lon
    except (ValueError, IndexError):
        return None

def _dist_km(a, b):
    if not a or not b: return None
    R=6371.0; la1,lo1=map(math.radians,a); la2,lo2=map(math.radians,b)
    return int(2*R*math.asin(math.sqrt(math.sin((la2-la1)/2)**2 +
               math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2)))

def _ham_band(freq_hz):
    m = freq_hz/1e6
    for name,(lo,hi) in (("80m",(3.5,4.0)),("60m",(5.3,5.5)),("40m",(7.0,7.3)),
                         ("30m",(10.1,10.15)),("20m",(14.0,14.35)),("17m",(18.06,18.17)),
                         ("15m",(21.0,21.45)),("12m",(24.89,24.99)),("10m",(28.0,29.7))):
        if lo<=m<=hi: return name
    return None

# adjacent-band proxy: SWBC band -> ham band(s) that best indicate its openness
SWBC_PROXY = {
    "49m": ["40m","60m"], "41m": ["40m"], "31m": ["30m","40m"],
    "25m": ["20m","30m"], "22m": ["20m","17m"], "19m": ["17m","20m"],
    "16m": ["17m","15m"], "13m": ["15m","12m"], "11m": ["10m","12m"],
}

def fetch_regional(since_s=1200, timeout=30.0):
    key = since_s//60
    c = _cache.get(key)
    if c and time.time()-c[0] < _TTL:
        return c[1]
    url = f"{QUERY_URL}?flowStartSeconds=-{int(since_s)}&rronly=1"
    req = urllib.request.Request(url, headers={"User-Agent":"hamradio-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        root = ET.fromstring(r.read())
    me = _grid_to_ll(MY_GRID)
    rows=[]
    for rr in root.iter("receptionReport"):
        a=rr.attrib
        try: f=int(a.get("frequency","0"))
        except ValueError: continue
        band=_ham_band(f)
        if not band: continue
        rxll=_grid_to_ll(a.get("receiverLocator","")); txll=_grid_to_ll(a.get("senderLocator",""))
        d_rx=_dist_km(me,rxll); d_tx=_dist_km(me,txll)
        # keep path if either endpoint is near our QTH (i.e. propagation we can use)
        near = (d_rx is not None and d_rx<=NEAR_KM) or (d_tx is not None and d_tx<=NEAR_KM)
        if not near: continue
        try: snr=int(a.get("sNR", a.get("snr","0")))
        except ValueError: snr=None
        # path length = tx<->rx distance (skip hop estimate)
        path=_dist_km(rxll,txll)
        rows.append({"band":band,"snr":snr,"path_km":path,
                     "d_rx":d_rx,"d_tx":d_tx})
    _cache[key]=(time.time(),rows)
    return rows

def band_openness(since_s=1200):
    rows=fetch_regional(since_s)
    agg={}
    for r in rows:
        b=r["band"]; agg.setdefault(b,{"n":0,"snr":[],"dist":[]})
        agg[b]["n"]+=1
        if r["snr"] is not None: agg[b]["snr"].append(r["snr"])
        if r["path_km"]: agg[b]["dist"].append(r["path_km"])
    out={}
    for b,d in agg.items():
        med_snr = statistics.median(d["snr"]) if d["snr"] else None
        maxdist = max(d["dist"]) if d["dist"] else None
        # openness 0-100: count(log) 50% + snr 30% + reach 20%
        c_score = min(1.0, math.log10(d["n"]+1)/2.0)          # ~100 spots -> 1.0
        s_score = 0.0 if med_snr is None else max(0.0,min(1.0,(med_snr+30)/40.0))
        r_score = 0.0 if not maxdist else min(1.0, maxdist/8000.0)
        score = round(100*(0.5*c_score + 0.3*s_score + 0.2*r_score))
        out[b]={"spots":d["n"],"median_snr":med_snr,"max_km":maxdist,"openness":score}
    return out

def swbc_priority(since_s=1200):
    """Openness score per SWBC band via adjacent-ham-band proxy, ranked."""
    ham=band_openness(since_s)
    res=[]
    for swbc,proxies in SWBC_PROXY.items():
        scores=[ham[p]["openness"] for p in proxies if p in ham]
        spots=sum(ham[p]["spots"] for p in proxies if p in ham)
        snrs=[ham[p]["median_snr"] for p in proxies if p in ham and ham[p]["median_snr"] is not None]
        score=max(scores) if scores else 0
        res.append({"swbc_band":swbc,"openness":score,"proxy_spots":spots,
                    "proxy_bands":proxies,
                    "proxy_median_snr": (statistics.median(snrs) if snrs else None)})
    res.sort(key=lambda x:-x["openness"])
    return {"qth":MY_GRID,"window_min":since_s//60,"ham_bands":ham,"swbc_ranked":res}
