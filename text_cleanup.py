"""Output-quality pass applied to a finished transcription.

Whisper hands back more than the user said: it captions non-speech with
bracketed artifacts (``[BLANK_AUDIO]``, ``(music)``), and on silence or music it
invents filler and loops it ("Thank you. Thank you. Thank you."). v1.7.0 fixed
the *cause* at the decoder (VAD + restored confidence thresholds), but VAD is
deliberately off for push-to-talk dictation, so there was still nothing standing
between a hallucination and the user's cursor. This module is that safety net,
plus the user's own correction dictionary.

Design rules, all of them learned from ways this goes wrong:

* **Never delete on a substring match.** A phrase is only dropped when it is the
  *entire* utterance, so "Thank you for the update." is untouched.
* **Artifacts are matched against an allowlist of known tokens**, never a
  generic bracket pattern. A generic ``\\(.*?\\)`` also eats "(about 40%)" and
  ``arr[0]`` - real dictation - which is a bug worth not reproducing.
* **A lone utterance is never removed entirely.** One "Thank you." is far more
  likely to be deliberate than hallucinated; the tell for hallucination is
  *repetition*, which :func:`_collapse_repeats` handles on its own.
* **Never capitalize or add punctuation.** ``transcribe_only`` promises to paste
  what was said; re-styling it would break that contract.

Pure stdlib and no project imports (in particular no ``main``), so it stays
importable and testable on its own.
"""
import functools
import re
import unicodedata
from dataclasses import dataclass, field

# Whole-utterance phrases Whisper emits on silence/music. These come from its
# YouTube-subtitle training data, which is why they read like sign-offs. Folded
# form: lowercase, no punctuation.
DEFAULT_HALLUCINATION_PHRASES = (
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "thanks for watching this video",
    "please subscribe",
    "subscribe to my channel",
    "dont forget to subscribe",
    "like and subscribe",
    "see you next time",
    "ill see you next time",
    "see you in the next video",
    "thanks for listening",
    "the end",
    "bye",
    "bye bye",
    "goodbye",
    "okay",
    "you",
    "subtitles by the amaraorg community",
    "subtitles by the amara org community",
    "transcription by castingwords",
    "amaraorg",
    # Non-English variants seen on the same silent-audio failure.
    "спасибо за просмотр",
    "продолжение следует",
    "субтитры сделал димасл",
    "редактор субтитров ам павлов",
    "merci davoir regarde cette video",
    "untertitel im auftrag des zdf",
    "gracias por ver el video",
    "ご視聴ありがとうございました",
)

# Bracketed non-speech captions. Matched only inside (), [], {} or ** and only
# against this vocabulary - so legitimate parentheses survive.
DEFAULT_ARTIFACT_TOKENS = (
    "blank_audio",
    "blank audio",
    "inaudible",
    "unintelligible",
    "silence",
    "no audio",
    "no speech",
    "music",
    "music playing",
    "upbeat music",
    "soft music",
    "applause",
    "laughter",
    "laughs",
    "laughing",
    "sighs",
    "coughs",
    "coughing",
    "clears throat",
    "background noise",
    "noise",
    "static",
    "beep",
    "foreign",
    "speaking in foreign language",
    "foreign language",
    "музыка",
    "аплодисменты",
    "смех",
)

# Deliberately conservative. "like" is excluded: it is a real verb and
# preposition, and no regex can tell "I like it" from "it was, like, big".
DEFAULT_FILLERS = (
    "um", "uh", "erm", "uhm", "umm", "uhh", "hmm", "mmm", "mhm",
    "you know", "i mean", "kind of", "sort of",
)

_SENTENCE_SPLIT = re.compile(r"([.!?…]+[ \t]*|\n+)")
# Apostrophes are dropped outright, not turned into a space: "I'll" must fold to
# "ill" so contraction phrases match. Every other punctuation mark becomes a
# space so "hello,world" folds to two words.
_APOSTROPHES = re.compile(r"['‘’ʼ´`]", re.UNICODE)
_PUNCT_STRIP = re.compile(r"[^\w\s]", re.UNICODE)
_MULTISPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.!?;:…])")
_MUSIC_NOTES = re.compile(r"[♪♫♬�]+")


@dataclass(frozen=True)
class CleanupOptions:
    strip_hallucinations: bool = True
    strip_artifacts: bool = True
    collapse_repeats: bool = True
    remove_fillers: bool = False          # OFF by default - it changes the user's words
    replacements: tuple = ()              # ((from, to), ...)
    extra_hallucination_phrases: tuple = ()
    preserve_layout: bool = False         # True keeps newlines (meeting transcripts)


def fold(text):
    """Comparison key: NFKC, lowercase, punctuation stripped, spaces collapsed."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _APOSTROPHES.sub("", folded)
    folded = _PUNCT_STRIP.sub(" ", folded)
    return " ".join(folded.split())


def options_from_config(cfg, *, preserve_layout=False):
    """Build options from the app config dict.

    Lives here (rather than in main) so this module never imports the app.
    Tolerates missing/garbage values - a broken config must not break dictation.
    """
    cfg = cfg or {}
    pairs = []
    raw = cfg.get("cleanup_replacements") or []
    if isinstance(raw, dict):                      # tolerate a legacy/hand-edited mapping
        raw = [{"from": k, "to": v} for k, v in raw.items()]
    seen = set()
    for item in raw:
        try:
            src = str(item.get("from", "")).strip()
            dst = str(item.get("to", "")).strip()
        except AttributeError:
            continue
        key = fold(src)
        if not key or key in seen:
            continue
        seen.add(key)
        pairs.append((src, dst))
        if len(pairs) >= 500:                      # cap: don't build a monstrous regex
            break

    extra = tuple(
        fold(p) for p in (cfg.get("cleanup_custom_hallucinations") or []) if fold(p)
    )
    return CleanupOptions(
        strip_hallucinations=bool(cfg.get("cleanup_strip_hallucinations", True)),
        strip_artifacts=bool(cfg.get("cleanup_strip_artifacts", True)),
        remove_fillers=bool(cfg.get("cleanup_remove_fillers", False)),
        replacements=tuple(pairs),
        extra_hallucination_phrases=extra,
        preserve_layout=bool(preserve_layout),
    )


@functools.lru_cache(maxsize=8)
def _artifact_regex(tokens):
    alternation = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(
        r"[\(\[\{\*]\s*(?:%s)\s*[\)\]\}\*]" % alternation,
        re.IGNORECASE | re.UNICODE,
    )


@functools.lru_cache(maxsize=32)
def _replacement_regex(pairs):
    """One compiled alternation for the whole dictionary.

    Longest source first, so a longer entry wins over a shorter overlapping one,
    and a single pass so ``a->b`` followed by ``b->c`` can never cascade into c.
    """
    if not pairs:
        return None, {}
    lookup = {fold(src): dst for src, dst in pairs if fold(src)}
    ordered = sorted(lookup, key=len, reverse=True)
    alts = []
    for key in ordered:
        # Rebuild from the folded key so "pyside 6" also matches "pyside  6".
        alts.append(r"\s+".join(re.escape(w) for w in key.split()))
    pattern = r"(?<!\w)(?:%s)(?!\w)" % "|".join(alts)
    return re.compile(pattern, re.IGNORECASE | re.UNICODE), lookup


@functools.lru_cache(maxsize=8)
def _filler_regex(fillers):
    alts = [r"\s+".join(re.escape(w) for w in f.split()) for f in
            sorted(fillers, key=len, reverse=True)]
    return re.compile(r"(?<!\w)(?:%s)(?!\w)[,]?" % "|".join(alts),
                      re.IGNORECASE | re.UNICODE)


def _split_utterances(text):
    """Split into (body, delimiter) pairs so the text can be rebuilt exactly."""
    parts = _SENTENCE_SPLIT.split(text)
    out = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if body or delim:
            out.append((body, delim))
    return out


def _join_utterances(pairs):
    return "".join(body + delim for body, delim in pairs)


def _normalize_whitespace(text, preserve_layout):
    if preserve_layout:
        # Collapse runs of spaces/tabs but keep line structure intact.
        return "\n".join(_MULTISPACE.sub(" ", line).rstrip() for line in text.split("\n"))
    return _MULTISPACE.sub(" ", text.replace("\r", " ").replace("\n", " "))


def _strip_artifacts(text):
    text = _artifact_regex(DEFAULT_ARTIFACT_TOKENS).sub(" ", text)
    return _MUSIC_NOTES.sub(" ", text)


def _collapse_repeats(text, min_run=3):
    """Collapse an utterance repeated >= min_run times in a row to one copy.

    This is the stage that actually kills hallucination loops, and it does so
    without needing the phrase to be in any list - which is what makes it robust
    to the phrases we never thought of.
    """
    pairs = _split_utterances(text)
    if len(pairs) < min_run:
        return text
    out = []
    i = 0
    while i < len(pairs):
        key = fold(pairs[i][0])
        j = i + 1
        while j < len(pairs) and key and fold(pairs[j][0]) == key:
            j += 1
        run = j - i
        if key and run >= min_run:
            out.append(pairs[i])          # keep exactly one
        else:
            out.extend(pairs[i:j])
        i = j
    return _join_utterances(out)


def _strip_hallucinations(text, phrases):
    pairs = _split_utterances(text)
    meaningful = [p for p in pairs if fold(p[0])]
    if len(meaningful) <= 1:
        # Single utterance: far more likely to be real dictation than a
        # hallucination. Repetition is the tell, and that is handled above.
        return text
    kept = [p for p in pairs if not (fold(p[0]) and fold(p[0]) in phrases)]
    if not any(fold(b) for b, _ in kept):
        return ""                          # everything was boilerplate
    return _join_utterances(kept)


def _preserve_case(matched, replacement):
    """Apply the matched text's case shape - but only when the replacement has
    no case of its own. A replacement like "PySide6" is a canonical spelling and
    must be emitted verbatim; a plain "the" should follow the match ("Teh"->"The").
    """
    if any(c.isupper() for c in replacement):
        return replacement
    if matched.isupper() and any(c.isalpha() for c in matched):
        return replacement.upper()
    if matched[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _apply_replacements(text, pairs):
    regex, lookup = _replacement_regex(pairs)
    if regex is None:
        return text

    def _sub(m):
        dst = lookup.get(fold(m.group(0)))
        return m.group(0) if dst is None else _preserve_case(m.group(0), dst)

    return regex.sub(_sub, text)


def _remove_fillers(text):
    text = _filler_regex(DEFAULT_FILLERS).sub("", text)
    text = re.sub(r"(?<=[,.!?;:])\s*,", "", text)
    text = re.sub(r"^\s*[,]\s*", "", text)
    return text


def _final_tidy(text, preserve_layout):
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    if preserve_layout:
        lines = [_MULTISPACE.sub(" ", ln).strip() for ln in text.split("\n")]
        return "\n".join(lines).strip("\n").strip() if any(lines) else ""
    return _MULTISPACE.sub(" ", text).strip()


def clean(text, *, options=None):
    """Run the full pass. Returns cleaned text (possibly "")."""
    result, _ = clean_with_report(text, options=options)
    return result


def clean_with_report(text, *, options=None):
    """Same as :func:`clean` but also returns counts for logging/telemetry.

    The report holds integers only - never transcript text - so it is safe to
    log or send anywhere.
    """
    opts = options or CleanupOptions()
    report = {"artifacts": 0, "repeats": 0, "hallucinations": 0, "replacements": 0}
    if not text or not text.strip():
        return "", report

    original = text
    out = _normalize_whitespace(text, opts.preserve_layout)

    if opts.strip_artifacts:
        before = out
        out = _strip_artifacts(out)
        report["artifacts"] = int(before != out)

    if opts.collapse_repeats:
        before = out
        out = _collapse_repeats(out)
        report["repeats"] = int(before != out)

    if opts.strip_hallucinations:
        phrases = frozenset(DEFAULT_HALLUCINATION_PHRASES) | frozenset(
            opts.extra_hallucination_phrases)
        before = out
        out = _strip_hallucinations(out, phrases)
        report["hallucinations"] = int(before != out)

    if opts.replacements:
        before = out
        out = _apply_replacements(out, opts.replacements)
        report["replacements"] = int(before != out)

    if opts.remove_fillers:
        out = _remove_fillers(out)

    out = _final_tidy(out, opts.preserve_layout)

    # Safety valve: if every stage combined ate a transcript that started with
    # real content and nothing was flagged as a hallucination or artifact, hand
    # back the original rather than silently losing the user's words.
    if not out and original.strip() and not (report["hallucinations"] or report["artifacts"]):
        return _final_tidy(_normalize_whitespace(original, opts.preserve_layout),
                           opts.preserve_layout), report
    return out, report
