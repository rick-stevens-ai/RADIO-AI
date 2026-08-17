import numpy as np, wave, sys
import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hamradio import cwdecode
sr = 16000
morse = {"C":"-.-.","Q":"--.-","D":"-..","E":".","K":"-.-","9":"----.","N":"-.",
         "W":".--","A":".-","T":"-","5":".....","R":".-.","I":"..","S":"...",
         "0":"-----","3":"...--","B":"-...","U":"..-","L":".-..","P":".--.",
         "O":"---","M":"--","G":"--.","H":"....","F":"..-.","V":"...-","X":"-..-",
         "Y":"-.--","Z":"--..","J":".---","1":".----","2":"..---","4":"....-",
         "6":"-....","7":"--...","8":"---..","/":"-..-."," ":" "}
def gen(text, wpm, tone, noise, qsb=False, drift=0):
    dit = 1.2 / wpm; seq = []
    for ch in text:
        if ch == " ":
            seq.append(("g", dit*7)); continue
        for s in morse[ch]:
            seq.append(("on", dit if s == "." else dit*3)); seq.append(("g", dit))
        seq[-1] = ("g", dit*3)
    a = np.array([])
    for kind, dur in seq:
        n = int(sr*dur); t = np.arange(n)/sr
        if kind == "on":
            a = np.concatenate([a, np.sin(2*np.pi*(tone+drift*len(a)/sr)*t)])
        else:
            a = np.concatenate([a, np.zeros(n)])
    a = a / max(1e-9, np.max(np.abs(a))) * 0.7
    if qsb:
        a *= (0.4 + 0.6*np.sin(2*np.pi*0.3*np.arange(len(a))/sr))
    a += np.random.normal(0, noise, len(a))
    a = (np.clip(a, -1, 1)*32767).astype(np.int16)
    w = wave.open("/tmp/t.wav", "w"); w.setnchannels(1); w.setsampwidth(2)
    w.setframerate(sr); w.writeframes(a.tobytes()); w.close()
tests = [
    ("clean 20",  ("CQ CQ DE KD9NWA", 20, 600, 0.02, False, 0)),
    ("noisy",     ("CQ CQ DE KD9NWA", 20, 600, 0.30, False, 0)),
    ("very noisy",("CQ CQ DE KD9NWA", 20, 600, 0.60, False, 0)),
    ("QSB deep",  ("CQ CQ DE KD9NWA", 20, 600, 0.15, True,  0)),
    ("QSB+noise", ("CQ CQ DE KD9NWA", 20, 600, 0.30, True,  0)),
    ("fast 30",   ("CQ CQ DE KD9NWA", 30, 600, 0.10, False, 0)),
    ("slow 12",   ("CQ CQ DE KD9NWA", 12, 600, 0.10, False, 0)),
    ("drift",     ("CQ CQ DE KD9NWA", 20, 600, 0.10, False, 20)),
    ("rst",       ("CQ DE W3ABC 599", 20, 600, 0.10, False, 0)),
]
ok = 0
for label, args in tests:
    exp = args[0]; gen(*args); r = cwdecode.decode("/tmp/t.wav")
    good = r["text"] == exp; ok += good
    status = "PASS" if good else "FAIL"
    print("%s %-11s wpm=%s conf=%s  %r" % (status, label, r.get("wpm"), r.get("confidence"), r["text"]))
print("--- %d/%d passed ---" % (ok, len(tests)))
