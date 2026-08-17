"""
hamradio.cwcorrect — an "intelligence" correction pass for raw CW decodes.

Real off-air CW is rarely perfectly timed: ops run letters together, drop word
spaces, and QSB/QRM garbles characters. A human copies through this using
*context* — they know CW QSOs follow a tight script (CQ ... DE <call>, RST 599,
name/QTH, TU 73), what a callsign looks like, and the standard abbreviation set.
This module encodes that knowledge to clean up the DSP decoder's output.

Strategy (all heuristic, conservative — never invents content, only re-segments
and snaps tokens to known forms when the edit distance is tiny):

  1. Normalize prosigns/abbreviations (BT, AR, KN, SK, TU, GM, GE, ...).
  2. Re-segment run-together text using a small CW-vocabulary dictionary
     (greedy longest-match) so e.g. 'CQCQDE' -> 'CQ CQ DE'.
  3. Recognize the QSO grammar and pull out fields: CQ, the other station's
     CALLSIGN, RST, name, QTH — fixing near-miss callsigns against the FCC DB
     and near-miss RSTs against the canonical 'NNN' shape.
  4. Emit a cleaned string + a structured {cq, call, rst, name, qth, ...} dict
     and a corrections log so nothing is silently fabricated.

Returns are additive: the caller keeps the raw text and gets 'corrected' +
'fields' + 'corrections' alongside it.
"""
from __future__ import annotations
import re
from typing import Optional

# Common CW abbreviations / Q-codes / prosigns that anchor the grammar.
CW_VOCAB = [
    # prosigns / procedural
    "CQ", "DE", "DX", "TEST", "AGN", "PSE", "QRZ", "QTH", "QSL", "QRM", "QRN",
    "QRP", "QSB", "QSY", "QRT", "TU", "TNX", "TKS", "FB", "OM", "YL", "XYL",
    "GM", "GA", "GE", "GN", "HR", "HW", "UR", "RST", "RIG", "ANT", "WX", "PWR",
    "TEMP", "NAME", "ES", "BTU", "BK", "CUL", "CUAGN", "73", "72", "88", "OP",
    "WID", "ABT", "VY", "GUD", "GUD", "RPT", "RPRT", "K", "R", "AR", "SK", "KN",
    "BT", "NR", "POTA", "SOTA", "SST", "5NN", "599", "579", "559", "339",
]
# words that commonly appear so re-segmentation has anchors (kept short/safe)
_VOCAB_SET = sorted(set(CW_VOCAB), key=len, reverse=True)

# Prosign mappings from concatenated Morse the decoder may have merged.
PROSIGN = {"KN": "KN", "SK": "SK", "AR": "AR", "BT": "BT", "AS": "AS"}

# Callsign regex (ITU-ish): 1-2 char prefix, a digit, 1-4 char suffix; allow
# an optional /P /M /<area> portable decoration.
_CALL_RE = re.compile(r"^[A-Z0-9]{1,3}\d[A-Z]{1,4}(?:/[A-Z0-9]{1,3})?$")
_CALL_CORE = re.compile(r"[A-Z]{1,2}\d[A-Z]{1,4}")
_RST_RE = re.compile(r"^[1-5][1-9][1-9]$")


def _lev(a: str, b: str) -> int:
    """Levenshtein distance (small strings)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _looks_like_call(tok: str) -> bool:
    return bool(_CALL_RE.match(tok))


def resegment(text: str) -> str:
    """Split run-together tokens on embedded vocabulary anchors.

    We only *insert* spaces (never delete characters). For each whitespace-
    delimited chunk, greedily peel known vocabulary words / prosigns from the
    front and back, leaving the middle (usually a callsign) intact.
    """
    out_words = []
    for chunk in text.split():
        if len(chunk) <= 2 or _looks_like_call(chunk):
            out_words.append(chunk)
            continue
        pieces = _peel(chunk)
        out_words.extend(pieces)
    return " ".join(out_words)


def _peel(chunk: str) -> list[str]:
    """Peel leading & trailing known tokens from a run-together chunk."""
    lead = []
    s = chunk
    changed = True
    while changed and s:
        changed = False
        for w in _VOCAB_SET:
            if len(w) >= 2 and s.startswith(w) and len(s) > len(w):
                # don't split a valid callsign apart
                if _looks_like_call(s):
                    break
                lead.append(w)
                s = s[len(w):]
                changed = True
                break
    trail = []
    changed = True
    while changed and s:
        changed = False
        for w in _VOCAB_SET:
            if len(w) >= 2 and s.endswith(w) and len(s) > len(w):
                if _looks_like_call(s):
                    break
                trail.insert(0, w)
                s = s[:-len(w)]
                changed = True
                break
    mid = [s] if s else []
    return lead + mid + trail


def _snap_call(tok: str, fcc_lookup=None) -> tuple[str, Optional[str]]:
    """If tok is nearly a callsign, snap it. If an FCC lookup is available and a
    tiny edit yields a *real* licensed call, prefer that. Returns (token, note).
    """
    # extract an embedded call-shaped core if present
    m = _CALL_CORE.search(tok)
    core = m.group(0) if m else tok
    note = f"call:{tok}->{core}" if core != tok else None
    if _looks_like_call(core):
        if fcc_lookup:
            if fcc_lookup(core):
                return core, note           # valid shape AND licensed: trust it
            # valid shape but NOT in FCC DB: if exactly one 1-edit neighbor is a
            # real US call, it's very likely a garbled character -> suggest it.
            neighbors = [c for c in _call_neighbors(core) if fcc_lookup(c)]
            if len(neighbors) == 1:
                return neighbors[0], f"call:{tok}->{neighbors[0]}(fcc-1edit)"
            # ambiguous or none: keep the decoded call as-is (don't fabricate)
        return core, note
    # token isn't a valid call shape: try to repair via a single edit to a real
    # US call.
    if fcc_lookup and 4 <= len(core) <= 6:
        for cand in _call_neighbors(core):
            if fcc_lookup(cand):
                return cand, f"call:{tok}->{cand}(fcc)"
    return tok, note


def _call_neighbors(s: str):
    """Generate edit-distance-1 callsign candidates (substitutions only, since
    CW garbles characters more than it inserts/deletes)."""
    alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    seen = set()
    for i in range(len(s)):
        for c in alpha:
            if c == s[i]:
                continue
            cand = s[:i] + c + s[i + 1:]
            if _looks_like_call(cand) and cand not in seen:
                seen.add(cand)
                yield cand


def _snap_rst(tok: str) -> tuple[str, Optional[str]]:
    """Snap near-miss RST reports. In CW, 599 is often sent 5NN (N=9). Map that,
    and snap 3-digit near-RSTs to the canonical shape."""
    t = tok.replace("N", "9").replace("O", "0")
    if _RST_RE.match(t):
        return (t, f"rst:{tok}->{t}") if t != tok else (t, None)
    return tok, None


def correct(text: str, fcc_lookup=None) -> dict:
    """Run the full correction pass on a raw CW decode string.

    fcc_lookup: optional callable(callsign)->record|None (e.g.
                hamradio.location.fcc_lookup) used to validate/snap callsigns.
    Returns {corrected, fields, corrections}.
    """
    corrections = []
    raw = re.sub(r"\s+", " ", text.strip().upper())
    if not raw:
        return {"corrected": "", "fields": {}, "corrections": []}

    # 1) re-segment run-together tokens
    seg = resegment(raw)
    if seg != raw:
        corrections.append(f"reseg:{raw!r}->{seg!r}")

    # 2) token-wise snapping
    tokens = seg.split()
    fixed = []
    fields: dict = {}
    for tok in tokens:
        # RST?
        if re.fullmatch(r"[0-9N O]{3}", tok) or _RST_RE.match(tok):
            r, note = _snap_rst(tok)
            if note:
                corrections.append(note)
            if _RST_RE.match(r):
                fields.setdefault("rst", r)
            fixed.append(r)
            continue
        # callsign-ish?
        if _CALL_CORE.search(tok) and any(ch.isdigit() for ch in tok):
            c, note = _snap_call(tok, fcc_lookup)
            if note:
                corrections.append(note)
            if _looks_like_call(c):
                fields.setdefault("call", c)
            fixed.append(c)
            continue
        fixed.append(tok)

    corrected = " ".join(fixed)

    # 3) grammar extraction
    up = corrected
    if re.search(r"\bCQ\b", up):
        fields["cq"] = True
        # station calling CQ is the token right after DE, else the first call
        m = re.search(r"\bDE\s+([A-Z0-9/]+)", up)
        if m and _looks_like_call(m.group(1)):
            fields["call"] = m.group(1)
    for kw, key in (("NAME", "name"), ("QTH", "qth"), ("OP", "name")):
        m = re.search(rf"\b{kw}\s+([A-Z0-9]+)", up)
        if m:
            fields[key] = m.group(1)
    if re.search(r"\b73\b", up):
        fields["sign_off"] = "73"

    return {"corrected": corrected, "fields": fields, "corrections": corrections}
