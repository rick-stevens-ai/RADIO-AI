import sys, os
import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hamradio import cwcorrect
from hamradio.location import fcc_lookup

cases = [
    # (raw, expected_corrected_contains, expected_field_key, expected_field_val)
    ("CQCQDE AA8P AA8P",   "CQ CQ DE AA8P", "call", "AA8P"),
    ("5 CQSST DEAA8P AA8P","CQ SST DE AA8P", "call", "AA8P"),
    ("CQ DE W3ABC 599",    "CQ DE W3ABC 599", "rst", "599"),
    ("UR RST 5NN 5NN",     "599", "rst", "599"),
    ("TU 73 GM",           "TU 73", "sign_off", "73"),
    ("NAME RALPH QTH OH",  "NAME RALPH", "name", "RALPH"),
    ("QRZDE K5XYZ",        "QRZ DE K5XYZ", "call", "K5XYZ"),
]
passed = 0
for raw, want_sub, fkey, fval in cases:
    r = cwcorrect.correct(raw, fcc_lookup=fcc_lookup)
    corr = r["corrected"]
    fields = r["fields"]
    ok_sub = want_sub in corr
    ok_field = fields.get(fkey) == fval
    ok = ok_sub and ok_field
    passed += ok
    print("%s raw=%-22r -> %-24r fields=%s%s" % (
        "PASS" if ok else "FAIL", raw, corr, fields,
        "" if ok else ("  [want %r in corrected, %s=%s]" % (want_sub, fkey, fval))))
print("--- %d/%d passed ---" % (passed, len(cases)))

# FCC snap demo: intentionally corrupt one char of a real call
print("\n=== FCC near-miss callsign snap ===")
# AA8P is real (Ralph); corrupt to AA8Q (likely not licensed) and to a 1-char miss of a real call
for bad in ["AA8P", "W1AX", "K5XYZ"]:
    hit = fcc_lookup(bad)
    print("  fcc_lookup(%s) -> %s" % (bad, (hit.get("name") if hit else None)))
