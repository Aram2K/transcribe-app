"""Spoken languages for transcription: what a prompt-driven cloud model is told
about the language(s) (one language, or several mixed inside a sentence), and
the helpers the local Whisper path uses for mixed speech.

"Mixed languages" is the language setting "multi" plus cfg["mix_languages"],
the languages the user switches between - e.g. English and Spanish in one
sentence. Every word should come out in the language it was spoken in, in that
language's own script: never translated, never transliterated. With fewer than
two ticked, any language may be detected (how "multi" always worked).
"""
import re

# The languages the app offers, in picker order.
NAMES = {"en": "English", "ar": "Arabic", "hy": "Armenian", "fr": "French",
         "de": "German", "ru": "Russian", "es": "Spanish"}

# Non-Latin scripts; the rest are written in Latin letters.
_SCRIPT = {"hy": "the Armenian alphabet", "ru": "Cyrillic", "ar": "Arabic script"}

# One short, natural line per language, in its own script. A mixed primer as
# Whisper's prompt nudges it to keep each language in its script instead of
# transliterating everything into the language it detected.
_PRIMER = {
    "hy": "Բարև ձեզ, այսօր կխոսենք մեր նախագծի մասին։",
    "en": "Okay, let's get started.",
    "ru": "Хорошо, давайте начнём.",
    "fr": "D'accord, on commence.",
    "de": "Okay, fangen wir an.",
    "es": "Vale, empecemos.",
    "ar": "حسناً، لنبدأ.",
}


def saved_mix(cfg):
    """The languages ticked in Settings, in picker order (any number)."""
    raw = (cfg or {}).get("mix_languages")
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [code for code in NAMES if code in raw]


def mix_languages(cfg):
    """The languages the user mixes - only when at least two are ticked; []
    means no restriction (any language may be detected)."""
    picked = saved_mix(cfg)
    return picked if len(picked) >= 2 else []


def _join(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _scripts_note(codes):
    """"(Armenian in the Armenian alphabet, English in Latin letters)"."""
    bits = []
    latin = [NAMES[c] for c in codes if c not in _SCRIPT]
    for c in codes:
        if c in _SCRIPT:
            bits.append(f"{NAMES[c]} in {_SCRIPT[c]}")
    if latin:
        bits.append(f"{_join(latin)} in Latin letters")
    return "(" + ", ".join(bits) + ")" if bits else ""


def cloud_hint(lang_setting, mix=None):
    """The language instruction appended to a cloud transcription prompt."""
    if lang_setting in NAMES:
        nm = NAMES[lang_setting]
        script = _SCRIPT.get(lang_setting)
        how = (f" in {script}, never transliterated into Latin letters" if script else "")
        return (f" The speaker speaks mainly {nm}. Write {nm}{how}. If they use words or "
                f"phrases from another language, keep those exactly as spoken, in that "
                f"language's own script - don't translate them.")
    if lang_setting == "multi":
        codes = [c for c in (mix or []) if c in NAMES]
        if len(codes) < 2:
            return (" The speaker may switch languages, even in the middle of a sentence. "
                    "Write every word in the language it was spoken in, in that language's "
                    "own script. Never translate, and never turn the whole text into one "
                    "language.")
        return (f" The speaker switches between {_join(NAMES[c] for c in codes)}, often in "
                f"the middle of a sentence. Write every word in the language it was spoken "
                f"in, in that language's own script {_scripts_note(codes)}. Never translate, "
                f"and never turn the whole text into one language.")
    return (" If the speaker switches languages, keep every word in the language it was "
            "spoken in, in its own script - don't translate.")


def whisper_primer(mix):
    return " ".join(_PRIMER[c] for c in mix if c in _PRIMER)


def dominant_among(all_probs, mix, fallback):
    """The most likely spoken language among the ones the user mixes, from
    Whisper's (language, probability) list. Armenian in particular is easily
    heard as another language when every language may win."""
    best, best_p = None, -1.0
    for lang, prob in (all_probs or []):
        if lang in mix and prob > best_p:
            best, best_p = lang, prob
    return best or fallback


def _norm(text):
    return re.sub(r"[\W_]+", " ", (text or "").lower()).strip()


def is_primer_echo(text, mix, quiet=False):
    """True when a decode is only the primer read back - Whisper sometimes
    writes its prompt into near-silent audio. The whole primer, or two of its
    lines, is an echo; a single line only on near-silent audio (``quiet``) -
    someone may really say "Okay, let's get started"."""
    t = _norm(text)
    if not t:
        return False
    lines = [_norm(_PRIMER[c]) for c in mix if c in _PRIMER]
    whole = _norm(whisper_primer(mix))
    if t == whole:
        return True
    found = [line for line in lines if line and line in t]
    rest = t
    for line in found:
        rest = rest.replace(line, " ")
    only_primer = not rest.strip()
    return only_primer and (len(found) >= 2 or (quiet and len(found) == 1))
