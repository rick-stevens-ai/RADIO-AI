#!/usr/bin/env python3
"""hamradio.scanlang — rapid multi-channel language scan with propagation
prioritization, per-station SNR, and schedule-DB cross-reference.

Pipeline:
  1. propagation prior (PSKReporter, adjacent-ham-band proxy) -> rank SWBC bands
  2. survey the top bands for live carriers (cheap RF sweep)
  3. per carrier: tune -> short dwell capture -> S-meter + audio SNR
                 -> language-detect on the spark GPU whisper-server
  4. annotate with EiBi schedule DB (expected station/language on that channel)
"""
import subprocess, json, os, tempfile, time, wave, math

def _spark_url():
    return os.environ.get("RADIO_WHISPER_URL", "http://100.90.211.89:8181").rstrip("/")

def _radio(*args, timeout=40):
    try:
        return subprocess.check_output(["radio", *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return ""

def _capture_source():
    out = subprocess.run(["pactl","list","short","sources"], text=True,
                         capture_output=True).stdout
    for line in out.splitlines():
        low = line.lower()
        if ("codec" in low or "usb" in low) and ".monitor" not in low:
            cols = line.split("\t")
            if len(cols) >= 2:
                return cols[1]
    return None

def _audio_snr_db(wav_path):
    """Segmental SNR: speech-band peak-frame energy vs noise-floor (10th pctile
    frame energy) in dB. Pure stdlib+math (no numpy dependency)."""
    try:
        w = wave.open(wav_path); sr = w.getframerate()
        raw = w.readframes(w.getnframes()); w.close()
        import array
        d = array.array("h"); d.frombytes(raw)
        if len(d) < sr//2: return None
        fl = sr//10  # 100ms frames
        energies=[]
        for i in range(0, len(d)-fl, fl):
            s=0
            for j in range(i, i+fl, 4):  # decimate for speed
                s += d[j]*d[j]
            energies.append(s/(fl/4))
        if not energies: return None
        energies.sort()
        noise = energies[max(0,len(energies)//10)] + 1
        peak  = energies[int(len(energies)*0.9)]
        return round(10*math.log10(peak/noise), 1)
    except Exception:
        return None

def _smeter_db():
    try:
        j = json.loads(_radio("smeter", timeout=8))
        for k in ("smeter_db","s_db","db","smeter"):
            if k in j: return j[k]
    except Exception:
        pass
    return None

def scan(bands=None, dwell=5.0, min_db=None, limit=None, prioritize=True,
         min_openness=25, top_bands=None):
    from . import schedule as sched
    try:
        from . import propagation as prop
    except Exception:
        prop = None

    # 1) propagation prior -> ordered band list
    prop_info = None
    if prioritize and prop is not None:
        try:
            pr = prop.swbc_priority(1200)
            prop_info = pr["swbc_ranked"]
            ranked_bands = [x["swbc_band"] for x in prop_info
                            if x["openness"] >= min_openness]
            if top_bands:
                ranked_bands = ranked_bands[:top_bands]
            if bands is None and ranked_bands:
                bands = ranked_bands
        except Exception:
            pass
    if bands is None:
        bands = ["49m","41m","31m","25m","22m","19m","16m"]

    subprocess.run(["radio","rfgain","1.0"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    # 2) survey the selected bands
    best = {}
    for band in bands:
        try:
            d = json.loads(_radio("swbc-survey", band))
        except Exception:
            continue
        for x in d.get("detected", []):
            k, db = x["freq_khz"], x["peak_db"]
            if k not in best or db > best[k][0]:
                best[k] = (db, x.get("likely",""))
    ranked = sorted(((k, v[0], v[1]) for k, v in best.items()), key=lambda r: -r[1])
    if min_db is not None:
        ranked = [r for r in ranked if r[1] >= min_db]
    if limit:
        ranked = ranked[:limit]

    src = _capture_source()
    if not src:
        return {"error":"no radio capture source (rig off?)","propagation":prop_info}

    # 3) per-carrier capture + metrics + language detect
    results = []
    for khz, db, likely in ranked:
        subprocess.run(["radio","freq-tune",str(int(khz*1000))], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(["radio","mode","AM"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        time.sleep(0.8)
        smeter = _smeter_db()
        wav = tempfile.mktemp(suffix=".wav", prefix=f"sl_{int(khz)}_")
        p = subprocess.Popen(["pw-record","--target",src,"--rate","16000",
                              "--channels","1","--format","s16",wav],
                             stderr=subprocess.DEVNULL)
        time.sleep(dwell); p.terminate()
        try: p.wait(timeout=2)
        except Exception: p.kill()
        snr = _audio_snr_db(wav)
        detected, text, has_speech = "?", "", False
        try:
            raw = subprocess.check_output(
                ["curl","-s","-m","40",f"{_spark_url()}/inference",
                 "-F",f"file=@{wav}","-F","language=auto","-F","response_format=verbose_json"],
                text=True, stderr=subprocess.DEVNULL)
            j = json.loads(raw) if raw.strip() else {}
            text = (j.get("text") or "").strip()
            detected = j.get("language") or "?"
            has_speech = bool(text) and not text.startswith(("[","("))
        except Exception:
            detected = "err"
        finally:
            try: os.unlink(wav)
            except OSError: pass
        expected = None
        try: expected = sched.lookup_freq(khz)
        except Exception: pass
        results.append({
            "freq_khz": khz, "peak_db": db, "smeter_db": smeter,
            "audio_snr_db": snr,
            "detected_language": detected if has_speech else None,
            "speech": has_speech,
            "sample": text[:90] if has_speech else None,
            "expected": expected, "survey_hint": likely,
        })
    return {"scanned": len(results), "dwell_s": dwell, "bands": bands,
            "propagation": prop_info, "carriers": results}
