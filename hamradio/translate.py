"""
hamradio.translate — OFFLINE machine translation for decoded radio voice/text.

Backed by Argos Translate (CTranslate2 engine, fully offline, no GPU). Pairs are
per-language packages installed under ~/.local/share/argos-translate. English is
the pivot: any non-English <-> non-English request is chained via English when a
direct package is not installed (e.g. es->ru = es->en then en->ru).

Core set installed: es, fr, pt, ru, zh, ar  (each <-> en). Extend by installing
more Argos packages (see install_pairs / the module docstring in swbc/README).

Typical use: transcribe a foreign SWBC with whisper in its own language, then
translate to English (or any target) here — higher quality than whisper's own
built-in ->English translation, and it also does English->foreign and other
pairs whisper cannot.
"""
from __future__ import annotations
import os
import warnings

# Argos pulls in stanza/torch for sentence splitting; silence its noisy
# FutureWarning about torch.load weights_only. Harmless for inference.
warnings.filterwarnings("ignore", category=FutureWarning)
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
# pip/keyring can hang headless; irrelevant to inference but set for any
# package-index calls made via install_pairs().
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

# The core-set languages we target, with display names + whisper ISO codes.
TOP_LANGS = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
    "ar": "Arabic",
    # aliases the whisper side may emit / users may pass
}
ALIASES = {
    "english": "en", "spanish": "es", "french": "fr", "portuguese": "pt",
    "russian": "ru", "chinese": "zh", "mandarin": "zh", "arabic": "ar",
    "castellano": "es", "espanol": "es",
}


def norm_code(code: str) -> str:
    """Normalise a language name/ISO to a 2-letter Argos code."""
    c = (code or "").strip().lower()
    if c in TOP_LANGS:
        return c
    if c in ALIASES:
        return ALIASES[c]
    return c[:2]


def _argos():
    import argostranslate.translate as tr
    return tr


# --- OPUS-MT overrides for pairs where the Argos package is broken -----------
# The official Argos es->en 1.9 package ships an OpenNMT bpe.model that the
# current sentencepiece-only argostranslate mis-tokenises (garbage/"mainstre"
# loop). We ship a locally-converted OPUS-MT model (CTranslate2 + source/target
# SentencePiece) and translate that pair directly. Add more here as needed.
import os as _os
_OPUS_DIR = _os.path.expanduser("~/radio/models")
OPUS_PAIRS = {
    ("es", "en"): _os.path.join(_OPUS_DIR, "opus-es-en"),
}
_opus_cache: dict = {}


def _opus_translate(text: str, frm: str, to: str):
    """Translate via a local OPUS-MT CTranslate2 model, or return None if the
    pair has no local model / deps missing."""
    path = OPUS_PAIRS.get((frm, to))
    if not path or not _os.path.isdir(path):
        return None
    try:
        import ctranslate2
        import sentencepiece as spm
    except Exception:
        return None
    key = (frm, to)
    if key not in _opus_cache:
        tr = ctranslate2.Translator(path)
        sp_src = spm.SentencePieceProcessor(_os.path.join(path, "source.spm"))
        sp_tgt = spm.SentencePieceProcessor(_os.path.join(path, "target.spm"))
        _opus_cache[key] = (tr, sp_src, sp_tgt)
    tr, sp_src, sp_tgt = _opus_cache[key]
    # translate sentence-by-sentence for long transcripts (keeps quality up)
    import re as _re
    sents = _re.split(r"(?<=[.!?])\s+", text.strip()) or [text]
    out = []
    for s in sents:
        if not s.strip():
            continue
        toks = sp_src.encode(s, out_type=str) + ["</s>"]
        res = tr.translate_batch([toks], max_decoding_length=256,
                                 repetition_penalty=1.1, beam_size=4)
        hyp = [x for x in res[0].hypotheses[0] if x not in ("</s>", "<pad>")]
        out.append(sp_tgt.decode(hyp))
    return " ".join(out).strip()


def installed_pairs() -> list[tuple[str, str]]:
    """List (from_code, to_code) translation pairs currently installed."""
    tr = _argos()
    pairs = []
    for lang in tr.get_installed_languages():
        for tgt in tr.get_installed_languages():
            if lang.code == tgt.code:
                continue
            if lang.get_translation(tgt) is not None:
                pairs.append((lang.code, tgt.code))
    return pairs


def available() -> dict:
    """Report the translation capability: installed languages + pairs."""
    try:
        tr = _argos()
    except Exception as e:
        return {"engine": "argostranslate", "ok": False, "error": str(e),
                "hint": "pip install argostranslate (Python 3.8: pin "
                        "argostranslate==1.9.1 ctranslate2==3.20.0) and set "
                        "PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring"}
    langs = sorted({l.code for l in tr.get_installed_languages()})
    pairs = installed_pairs()
    return {
        "engine": "argostranslate (offline, ctranslate2)",
        "ok": True,
        "languages": langs,
        "language_names": {c: TOP_LANGS.get(c, c) for c in langs},
        "pairs": [f"{a}->{b}" for a, b in pairs],
        "pivot": "en",
    }


def _direct(tr, text: str, frm: str, to: str):
    """Direct translation if the pair is installed, else None."""
    langs = {l.code: l for l in tr.get_installed_languages()}
    if frm not in langs or to not in langs:
        return None
    t = langs[frm].get_translation(langs[to])
    if t is None:
        return None
    return t.translate(text)


def translate(text: str, to: str = "en", frm: str = "auto") -> dict:
    """Translate `text` into `to`, from `frm` (or 'auto' to detect).

    Uses a direct Argos pair when installed, otherwise pivots through English.
    Returns {text, from, to, translated, path, engine}.
    """
    text = (text or "").strip()
    to = norm_code(to)
    if not text:
        return {"text": "", "from": frm, "to": to, "translated": "",
                "note": "empty input"}

    tr = _argos()

    src = norm_code(frm) if frm and frm != "auto" else _detect(text, tr)

    if src == to:
        return {"text": text, "from": src, "to": to, "translated": text,
                "path": [src], "engine": "argostranslate", "note": "same language"}

    # 0) local OPUS-MT override for known-broken Argos pairs
    opus = _opus_translate(text, src, to)
    if opus is not None:
        return {"text": text, "from": src, "to": to, "translated": opus,
                "path": [src, to], "engine": "opus-mt/ct2"}

    # 1) try direct
    out = _direct(tr, text, src, to)
    if out is not None:
        return {"text": text, "from": src, "to": to, "translated": out,
                "path": [src, to], "engine": "argostranslate"}

    # 2) pivot through English (use OPUS override for the src->en leg if present)
    if src != "en" and to != "en":
        via = _opus_translate(text, src, "en")
        if via is None:
            via = _direct(tr, text, src, "en")
        if via is not None:
            out = _direct(tr, via, "en", to)
            if out is not None:
                return {"text": text, "from": src, "to": to, "translated": out,
                        "path": [src, "en", to], "engine": "argostranslate+opus",
                        "pivot": "en"}

    return {"text": text, "from": src, "to": to, "translated": "",
            "error": f"no installed path {src}->{to} (install the Argos pair "
                     f"or an en-pivot pair)",
            "installed": [f"{a}->{b}" for a, b in installed_pairs()]}


# Common function words per Latin-script language — enough to disambiguate
# es/fr/pt/en on a sentence of decoded audio. (Non-Latin handled by script.)
_STOPWORDS = {
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una",
           "es", "esta", "por", "con", "para", "aquí", "desde", "muy",
           "buenos", "días", "tardes", "noches", "señor", "programa"},
    "fr": {"le", "la", "les", "de", "des", "un", "une", "et", "est", "que",
           "qui", "dans", "pour", "avec", "vous", "nous", "ce", "cette",
           "sur", "ici", "bonjour", "bonsoir", "émission", "chaîne"},
    "pt": {"o", "a", "os", "as", "de", "que", "e", "em", "um", "uma", "do",
           "da", "para", "com", "por", "não", "aqui", "muito", "boa",
           "bom", "dia", "noite", "senhor", "programa", "rádio"},
    "en": {"the", "a", "an", "of", "and", "to", "in", "is", "that", "for",
           "with", "this", "on", "from", "you", "we", "here", "good",
           "evening", "morning", "program", "radio", "station", "news"},
}


def _detect(text: str, tr=None) -> str:
    """Best-effort source-language detection.

    1) Script heuristics: Cyrillic->ru, CJK->zh, Arabic->ar (unambiguous).
    2) Latin script: score against per-language function-word sets (es/fr/pt/en)
       plus a couple of orthographic hints. Robust for a sentence of audio.
    Callers who already know the language (e.g. the whisper decode reports it)
    should pass frm explicitly and skip this entirely."""
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            return "ru"
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            return "zh"
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
            return "ar"
    # Latin-script: function-word voting
    words = [w.strip(".,!?;:\"'()[]").lower() for w in text.split()]
    wset = set(words)
    scores = {lang: sum(1 for w in words if w in sw)
              for lang, sw in _STOPWORDS.items()}
    # orthographic nudges
    if any(c in text for c in "ñ¡¿"):
        scores["es"] += 2
    if "ç" in text or "ã" in text or "õ" in text:
        scores["pt"] += 2
    if any(c in text.lower() for c in "àâêîôûëïœ") or "'" in text:
        scores["fr"] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "en"


def install_pairs(codes, timeout=None) -> dict:
    """Download+install Argos packages for each code <-> en (admin helper)."""
    import argostranslate.package as pkg
    pkg.update_package_index()
    avail = pkg.get_available_packages()
    codes = {norm_code(c) for c in codes}
    want = [p for p in avail
            if (p.from_code in codes and p.to_code == "en")
            or (p.from_code == "en" and p.to_code in codes)]
    done = []
    for p in want:
        pkg.install_from_path(p.download())
        done.append(f"{p.from_code}->{p.to_code}")
    return {"installed": done, "count": len(done)}
