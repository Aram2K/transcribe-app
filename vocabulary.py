"""Custom vocabulary: the user's names, jargon and product spellings.

Historically this was one free-text box wired straight into Whisper's
``initial_prompt``. That biases the local decoder nicely, but it was **silently
dropped by every cloud backend** - configure your vocabulary, switch to Pro
cloud, and it quietly stopped working. This module turns it into a structured
list with one renderer per consumer:

* :func:`whisper_prompt`            - local faster-whisper ``initial_prompt``
* :func:`cloud_transcription_hint`  - prompt-driven cloud STT (Gemini, managed)
* :func:`spelling_authority_block`  - the LLM step, which can fix what the
  decoder still got wrong

``cfg["initial_prompt"]`` is kept as a **derived mirror** of the term list. That
is deliberate: the two local call sites keep reading it unchanged, so the tuned
decoding block they sit in never has to be touched, and downgrading to an older
build still works.

Terms are capped and normalized. An over-long glossary does not just waste
Whisper's 224-token prompt window - it measurably raises the chance the model
echoes the prompt back on near-silence, a failure this codebase already fights.

The rule everything here follows: a term may fix the SPELLING of something
that was actually said - it must never put itself where it wasn't. A prompt
alone breaks that rule on short or unclear audio (the decoder writes the
terms in), so the local decoder's output is checked against a decode without
the prompt and only the terms that decode shows a trace of are kept
(:func:`reconcile_prompted`). Elsewhere only near-miss spellings are snapped
to the term (:func:`correct_spellings`), and an ordinary word is never
re-cased into one ("will" stays "will" with "Will" in the list).

Pure stdlib, no project imports.
"""
import bisect
import collections
import difflib
import functools
import re
import unicodedata

MAX_TERMS = 100
MAX_TERM_CHARS = 50
MAX_TERM_WORDS = 6
MAX_PROMPT_CHARS = 800

_SPLIT = re.compile(r"[,\n;]+")


def normalize_terms(raw):
    """Coerce a list or free-text blob into a clean, deduped term list."""
    if not raw:
        return []
    if isinstance(raw, str):
        items = _SPLIT.split(raw)
    else:
        try:
            items = list(raw)
        except TypeError:
            return []

    out, seen = [], set()
    for item in items:
        term = " ".join(str(item).split())
        if not term or len(term) > MAX_TERM_CHARS:
            continue
        if len(term.split()) > MAX_TERM_WORDS:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= MAX_TERMS:
            break
    return out


def looks_like_term_list(raw):
    """Is this free text a vocabulary list, or a hand-written prose prompt?

    Older builds shipped one free-text box wired straight to Whisper's
    ``initial_prompt``. Almost everyone used it as the comma-separated list the
    placeholder asked for, but a few wrote a sentence. A separator means list; a
    lone run of several words means prose, and is left alone.
    """
    text = (raw or "").strip()
    if not text:
        return False
    if any(sep in text for sep in (",", ";", "\n")):
        return True
    return len(text.split()) <= 3


def load_terms(cfg):
    """The active term list: the structured key, else the legacy free-text one."""
    cfg = cfg or {}
    terms = normalize_terms(cfg.get("vocabulary"))
    if terms:
        return terms
    return normalize_terms(cfg.get("initial_prompt"))


def _render(terms):
    """"Glossary: A, B, C." truncated on a term boundary, never mid-term."""
    if not terms:
        return ""
    prefix = "Glossary: "
    kept, length = [], len(prefix)
    for term in terms:
        extra = len(term) + (2 if kept else 0)
        if length + extra + 1 > MAX_PROMPT_CHARS:
            break
        kept.append(term)
        length += extra
    return prefix + ", ".join(kept) + "." if kept else ""


def whisper_prompt(cfg):
    """``initial_prompt`` for local faster-whisper, or None when empty."""
    return _render(load_terms(cfg)) or None


def _cloud_allowed(cfg):
    """Vocabulary is usually colleagues' and clients' names - treat it as
    personal data and keep it local when the user asked for that. Gating lives
    here so no caller can forget it."""
    cfg = cfg or {}
    if cfg.get("privacy_mode"):
        return False
    return bool(cfg.get("vocabulary_share_with_cloud", True))


def cloud_transcription_hint(cfg):
    """One sentence for prompt-driven cloud STT. "" when empty or gated.

    Worded to fix spellings only: the old "including similar-sounding
    variants" invited the model to turn look-alike words into the terms."""
    if not _cloud_allowed(cfg):
        return ""
    terms = load_terms(cfg)
    if not terms:
        return ""
    joined = ", ".join(terms)[:MAX_PROMPT_CHARS]
    return (" These names and terms may come up: %s. Only if the speaker actually "
            "says one of them, spell it exactly like that. Never add them otherwise, "
            "and never replace other words with them." % joined)


def cloud_terms(cfg):
    """Term list for backends that take a structured field. [] when gated."""
    return load_terms(cfg) if _cloud_allowed(cfg) else []


def spelling_authority_block(cfg):
    """Block for the LLM step, so it can repair what the decoder still missed."""
    if not _cloud_allowed(cfg):
        return ""
    terms = load_terms(cfg)
    if not terms:
        return ""
    return (
        "Correct spellings for names and terms the user may mention (use them only "
        "to fix the spelling of a word that clearly IS one of these; never replace "
        "an ordinary word with them, and never add one the text doesn't mention):\n"
        + ", ".join(terms)
    )


# ── keeping the vocabulary honest ────────────────────────────────────────────
# Armenian and Cyrillic to rough Latin, so a term compares with however a
# decode wrote it: "Այբուբեն" is "Aibuben", "Доктолиб" is "Doctolib".
_TRANSLIT = str.maketrans({
    "ա": "a", "բ": "b", "գ": "g", "դ": "d", "ե": "e", "զ": "z", "է": "e", "ը": "y",
    "թ": "t", "ժ": "zh", "ի": "i", "լ": "l", "խ": "kh", "ծ": "ts", "կ": "k", "հ": "h",
    "ձ": "dz", "ղ": "gh", "ճ": "ch", "մ": "m", "յ": "y", "ն": "n", "շ": "sh", "ո": "o",
    "չ": "ch", "պ": "p", "ջ": "j", "ռ": "r", "ս": "s", "վ": "v", "տ": "t", "ր": "r",
    "ց": "ts", "ւ": "v", "փ": "p", "ք": "k", "օ": "o", "ֆ": "f", "և": "ev",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})
EVIDENCE_THRESHOLD = 0.75     # "the unprompted decode heard something like it"
EVIDENCE_LETTERS_MIN = 0.55   # ...when it also SOUNDS like it ("Akopian", "cube control")
EVIDENCE_SOUND = 0.82         # consonant skeletons; up to 3 consonants they must be equal
CORRECTION_THRESHOLD = 0.88   # "this word is a misspelling of the term"


def _letters(text):
    """Lowercase Latin letters and digits only, for sound-alike comparison."""
    s = unicodedata.normalize("NFKC", text or "").casefold().replace("ու", "u")
    s = unicodedata.normalize("NFKD", s.translate(_TRANSLIT))
    return "".join(ch for ch in s if ch.isascii() and ch.isalnum())


def _term_regex(term):
    return re.compile(r"(?<!\w)%s(?!\w)" % re.escape(term), re.IGNORECASE)


def contains_any(text, terms):
    return any(_term_regex(t).search(text or "") for t in terms)


@functools.lru_cache(maxsize=1024)
def _trigger_regex(term):
    """The term as the glossary spells it, or capitalised at a sentence start.
    A decode copying the prompt writes it exactly so; matched case-blind,
    "IT", "Will" or "Hope" hit "it", "will", "hope" in nearly every sentence
    and doubled every decode."""
    forms = sorted({term, term[:1].upper() + term[1:]}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(map(re.escape, forms)))


def evidence_trigger(text, terms):
    """Does ``text`` contain a term worth an evidence check? A term with no
    letters is skipped - the check can't judge it anyway."""
    return any(_letters(t) and _trigger_regex(t).search(text or "") for t in terms)


_NUMBER_WORDS = dict(zip("0123456789", ("zero", "one", "two", "three", "four", "five",
                                        "six", "seven", "eight", "nine")))
# Rough consonant classes - what survives a name being heard and re-spelled.
_DIGRAPHS = (("sch", "s"), ("tch", "j"), ("ch", "j"), ("sh", "s"), ("zh", "j"),
             ("dj", "j"), ("kh", "k"), ("gh", "k"), ("ph", "f"), ("th", "t"),
             ("ck", "k"), ("qu", "k"))
_SOUND_CLASS = {**dict.fromkeys("bpfv", "P"), **dict.fromkeys("cgkq", "K"),
                **dict.fromkeys("sz", "S"), "x": "KS", "j": "J", **dict.fromkeys("dt", "T"),
                "l": "L", **dict.fromkeys("mn", "N"), "r": "R"}
_SOFT_C = re.compile(r"c(?=[eiy])")


def _spelled(letters):
    """Digits as words, so "PySide6" compares with "pie side six"."""
    return "".join(_NUMBER_WORDS.get(ch, ch) for ch in letters)


def _sound_key(letters):
    """Consonant skeleton: vowels, h, w and y dropped, look-alike consonants
    merged. "Hakobyan" and "Akopian" are both KPN, "Sargsyan" and "Sarkisian"
    both SRKSN."""
    s = letters
    for a, b in _DIGRAPHS:
        s = s.replace(a, b)
    s = _SOFT_C.sub("s", s)
    out, prev = [], ""
    for ch in s:
        cls = _SOUND_CLASS.get(ch, "")
        if cls and cls != prev:
            out.append(cls)
        prev = cls
    return "".join(out)


def _heard_as(key, cand, threshold=EVIDENCE_THRESHOLD):
    """Could ``cand`` (letters of the unprompted decode) be how the decoder
    heard the term ``key`` without help? By spelling or - for terms of 4+
    letters - by sound, since names come out re-spelled: "Hakobyan" as
    "Akopian", "kubectl" as "cube control", "PySide6" as "pie side six".
    "doctor" alone is not "Doctolib", nor "code" "Claude"."""
    if not cand:
        return False
    a, b = _spelled(key), _spelled(cand)
    letters = difflib.SequenceMatcher(None, a, b).ratio()
    if letters >= threshold:
        return True
    if len(key) < 4 or letters < EVIDENCE_LETTERS_MIN:
        return False
    sa, sb = _sound_key(a), _sound_key(b)
    if len(sa) <= 3:
        return bool(sa) and sa == sb
    return difflib.SequenceMatcher(None, sa, sb).ratio() >= EVIDENCE_SOUND


def _opaque(word):
    """Letters in a script _letters can't map to Latin (Greek, Arabic, CJK...):
    such a word can't be compared with a Latin term either way."""
    return not _letters(word) and any(unicodedata.category(c).startswith("L") for c in word)


_TOKEN = re.compile(r"\S+")
_LEAD = re.compile(r"^\W*")
_TRAIL = re.compile(r"\W*$")


@functools.lru_cache(maxsize=8192)
def _token_parts(raw):
    """One token, analysed once however many terms there are: the length of
    its leading and trailing punctuation, then its core's key and scripts."""
    lead = _LEAD.match(raw).end()
    core = raw[lead:]
    trail = len(core) - _TRAIL.search(core).start()
    core = core[:len(core) - trail]
    return lead, trail, _plain_key(core), frozenset(_scripts(core))


_Word = collections.namedtuple("_Word", "start end cstart cend key raw")
_Verdict = collections.namedtuple("_Verdict", "term a b start end supported qa qb")


def _decode_words(text):
    """A decode's words: where each one and its core (the word without the
    punctuation around it) start and end, and its letters."""
    out = []
    for m in _TOKEN.finditer(text or ""):
        lead, trail = _token_parts(m.group())[:2]
        out.append(_Word(m.start(), m.end(), m.start() + lead, m.end() - trail,
                         _letters(m.group()), m.group()))
    return out


def _occurrences(text, words, terms):
    """Each term the decode wrote, as (term, first word, end word, start,
    end); the longer one where two overlap ("New York" over "York")."""
    hits = sorted((m.start(), -len(m.group()), m.end(), t)
                  for t in terms if _letters(t)
                  for m in _trigger_regex(t).finditer(text or ""))
    starts, ends = [w.start for w in words], [w.end for w in words]
    out, taken_to = [], -1
    for start, _longest, end, term in hits:
        if start < taken_to:
            continue
        out.append((term, bisect.bisect_right(ends, start), bisect.bisect_left(starts, end),
                    start, end))
        taken_to = end
    return out


def _judge(prompted, plain, terms, threshold=EVIDENCE_THRESHOLD):
    """Line the prompted decode up with the unprompted one word by word and
    judge each term the prompted one wrote: SUPPORTED when the plain words in
    its place spell or sound like it (possibly several: "cube control" for
    kubectl), INVENTED when there is nothing there, or something else.

    Returns (prompted words, plain words, {prompted word: identical plain
    word}, verdicts); a verdict's qa:qb are the plain words in the term's place.
    """
    P, Q = _decode_words(prompted), _decode_words(plain)
    occ = _occurrences(prompted, P, terms)
    if not occ:
        return P, Q, {}, []
    # Punctuation and unmappable words get a key no letters can equal.
    sm = difflib.SequenceMatcher(None, [w.key or "\0" + w.raw for w in P],
                                 [w.key or "\0" + w.raw for w in Q], autojunk=False)
    matched = {}
    for i, j, size in sm.get_matching_blocks():
        for d in range(size):
            matched[i + d] = j + d
    verdicts = []
    for term, a, b, start, end in occ:
        if all(i in matched for i in range(a, b)) and matched[b - 1] - matched[a] == b - 1 - a:
            verdicts.append(_Verdict(term, a, b, start, end, True, matched[a], matched[b - 1] + 1))
            continue
        # The plain words between the nearest words both decodes agree on...
        lo = a - 1
        while lo >= 0 and lo not in matched:
            lo -= 1
        hi = b
        while hi < len(P) and hi not in matched:
            hi += 1
        ga = matched[lo] + 1 if lo >= 0 else 0
        gb = matched[hi] if hi < len(P) else len(Q)
        # ...less one for each differing prompted word beside the term.
        qa = min(ga + (a - lo - 1), gb)
        qb = max(qa, gb - (hi - b))
        key, k = _letters(term), len(term.split())
        wa, wb = max(ga, qa - 1), min(gb, qb + 1)
        supported = any(_opaque(Q[j].raw) for j in range(ga, gb)) or any(
            _heard_as(key, "".join(w.key for w in Q[x:y]), threshold)
            for x in range(wa, wb) for y in range(x + 1, min(wb, x + k + 3) + 1))
        verdicts.append(_Verdict(term, a, b, start, end, supported, qa, qb))
    return P, Q, matched, verdicts


def unsupported_terms(prompted, unprompted, terms, threshold=EVIDENCE_THRESHOLD):
    """Terms in the prompted decode that the SAME audio decoded without the
    prompt shows no trace of where they stand - the prompt wrote them in.
    "doctor lib", "Akopian" or "Այբուբեն" is a trace of the term; "doctor"
    alone is not. :func:`reconcile_prompted` acts on this."""
    bad = {v.term for v in _judge(prompted, unprompted, terms, threshold)[3]
           if not v.supported}
    return [t for t in terms if t in bad]


def _splice(text, start, end, rep):
    """Put ``rep`` where text[start:end] was. With nothing to put there, drop
    the word without leaving a double space or stray punctuation."""
    if rep:
        return text[:start] + rep + text[end:]
    left, right = text[:start].rstrip(" \t"), text[end:].lstrip(" \t")
    if not left.strip():
        return left + right.lstrip(",;: \t")
    if not right or right[0] in ".!?":
        return left.rstrip(",;:") + right
    if right[0] in ",;:" or left.endswith(("\n", "\r")):
        return left + right
    return left + " " + right


def reconcile_prompted(prompted, plain, terms):
    """Merge the glossary-prompted decode with an unprompted decode of the
    same audio. The prompt gets the user's terms spelled right, but on short
    or unclear audio it also writes terms in that nobody said. Each term the
    prompted decode wrote is judged against the plain words in its place
    (:func:`_judge`): a supported term stays, an invented one is swapped for
    those plain words, and everything else keeps the prompted wording.

    When the prompted decode is mostly glossary terms, or the plain one is
    empty, much shorter or another text altogether, the prompt echoed or took
    over: then the plain decode is used, with near-miss spellings snapped to
    the terms."""
    P, Q, matched, verdicts = _judge(prompted, plain, terms)
    invented = [v for v in verdicts if not v.supported]
    if not invented:
        return prompted
    words = [i for i, w in enumerate(P) if w.key]
    in_terms = {i for v in verdicts for i in range(v.a, v.b)}
    rest = [i for i in words if i not in in_terms]
    heard = sum(1 for w in Q if w.key)
    if (2 * heard < len(words) or 2 * len(rest) <= len(words)
            or 2 * sum(1 for i in rest if i in matched) < len(rest)):
        return correct_spellings(plain or "", terms)
    out, limit = prompted, len(prompted)
    for v in sorted(invented, key=lambda v: v.start, reverse=True):
        end = min(max(v.end, P[v.b - 1].cend), limit)
        rep = plain[Q[v.qa].cstart:Q[v.qb - 1].cend] if v.qa < v.qb else ""
        out, limit = _splice(out, v.start, end, rep), v.start
    return out


NEAR_MISS_MIN_LETTERS = 8     # shorter terms are too close to ordinary words to fix by sound
# Endings that make another English word of a term: "transcribed", "Transcriptional".
_LATIN_ENDINGS = ("s", "es", "d", "ed", "er", "ers", "ing", "ings", "ly", "al")


def _plain_key(text):
    """Casefolded letters and digits in their OWN script - no transliteration:
    rewriting only ever happens within one writing system."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text or "").casefold()
                   if ch.isalnum())


def _scripts(text):
    return {unicodedata.name(ch, "?").split(" ")[0] for ch in text or "" if ch.isalpha()}


def _distinctive_case(term):
    """Casing ordinary writing never produces - an internal capital, a digit or
    a symbol (PySide6, GitHub, iPhone, JSON, O'Brien). Only such a term is
    worth re-casing a word to: "will", "hope", "роман" are words, not "Will"."""
    if any(ch.isdigit() or not (ch.isalnum() or ch.isspace()) for ch in term):
        return True
    return any(ch.isupper() for word in term.split() for ch in word[1:])


_Term = collections.namedtuple(
    "_Term", "term key fold scripts symbols digits words distinctive latin")


@functools.lru_cache(maxsize=1024)
def _term_info(term):
    key = _plain_key(term)
    scripts = frozenset(_scripts(term))
    return _Term(term, key, term.casefold(), scripts,
                 frozenset(ch for ch in term if not ch.isalnum() and not ch.isspace()),
                 "".join(ch for ch in key if ch.isdecimal()), len(term.split()),
                 _distinctive_case(term), not scripts - {"LATIN"})


def _ordinary_case(word):
    """Lowercase, or a capital only on the first letter - what plain writing
    or a sentence start gives any word ("It works", "us")."""
    return not any(ch.isupper() for ch in [c for c in word if c.isalpha()][1:])


def _shared_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _other_form(term, cand, capitalised):
    """Is ``cand`` another form of the term rather than a misspelling of it?
    "Екатерине", "в Швейцарии" are the term with a case ending,
    "transcribed" or "transcriptional" with an English one, and "Alexander"
    or "Christine" is another name - each stays what it is."""
    key = term.key
    same = _shared_prefix(key, cand)
    if not term.latin:
        return same >= len(key) - 2       # the stem, whatever the ending
    if len(key) >= 4 and same == min(len(key), len(cand)) and abs(len(cand) - len(key)) <= 3:
        return True                       # the term with an ending added or dropped
    if any(cand == key + e or (key.endswith("e") and cand == key[:-1] + e)
           for e in _LATIN_ENDINGS):
        return True
    return capitalised and len(key) - 2 <= same < len(key)


def correct_spellings(text, terms, threshold=CORRECTION_THRESHOLD):
    """Fix how the user's terms are written - never put one where something
    else was said, never rewrite an ordinary word. What it changes:

    * the case of a term whose casing plain writing never produces
      ("pyside6" -> "PySide6", "github" -> "GitHub", "json" -> "JSON").
      A title-case term leaves words alone ("will", "hope", "роман" stay),
      a capital at a sentence start is left alone, and a short term only
      changes a word with a capital after its first letter ("aPI" -> "API";
      "It works" and "us" stay);
    * a near-miss of a long term (8+ letters) with the same first letter,
      digits and symbols ("Doctolibe" -> "Doctolib"; "Windows 11" never
      becomes 10) - but not another form of it ("Екатерине", "в Швейцарии",
      "transcribed") or another name ("Alexander" for "Alexandra");
    * a number the recognizer split off ("pyside 6" -> "PySide6").

    Only in the term's own script (Armenian or Russian words are never turned
    into a Latin term), keeping endings ("PySide6's", "iPhones"), never
    across punctuation or line breaks, and never turning one of the user's
    terms into another (Eric / Erica)."""
    if not text or not terms:
        return text
    infos = [t for t in map(_term_info, terms) if t.key]
    if not infos:
        return text
    # Every token is analysed once; each term then only looks at the spans
    # that start with its letter and could have its length. Per term and
    # token, 100 terms on a long segment took seconds.
    toks = []                                         # (start, end, core start, core end, key, scripts)
    for m in _TOKEN.finditer(text):
        lead, trail, key, scripts = _token_parts(m.group())
        toks.append((m.start(), m.end(), m.start() + lead, m.end() - trail, key, scripts))
    brk = []                                          # a sentence, clause or line ends after token j
    for j in range(len(toks) - 1):
        gap = text[toks[j][1]:toks[j + 1][0]]
        brk.append(text[toks[j][1] - 1] in ".!?,;:" or "\n" in gap or "\r" in gap)
    index = {}

    def spans(n):
        """Spans of n tokens by the first letter of their key."""
        if n not in index:
            by_first = index[n] = {}
            for i in range(len(toks) - n + 1):
                last = i + n - 1
                if n > 1 and (not toks[i][4] or not toks[last][4] or any(brk[i:last])):
                    continue                          # never across sentences or lines
                key = "".join(t[4] for t in toks[i:last + 1])
                if key:
                    by_first.setdefault(key[0], []).append((i, key))
        return index[n]

    folds = {t.fold for t in infos}
    keys = {t.key for t in infos}
    found = []                                        # (score, n, i, start, end, replacement)
    for t in infos:
        size = len(t.key)
        near = size >= NEAR_MISS_MIN_LETTERS
        for n in (t.words, t.words + 1):
            for i, cand in spans(n).get(t.key[0], ()):
                last = i + n - 1
                exactish = cand.startswith(t.key) and len(cand) <= size + 3
                if n > t.words:
                    # Only rejoin a number the recognizer split off.
                    if cand != t.key or not text[toks[last][0]].isdigit():
                        continue
                elif not exactish and not (near and 0.8 * size <= len(cand) <= 1.25 * size):
                    continue
                scripts = (toks[i][5] if n == 1
                           else frozenset().union(*(x[5] for x in toks[i:last + 1])))
                if not scripts <= t.scripts:
                    continue                          # other writing system: leave it
                start = toks[i][2]
                core, suffix = text[start:toks[last][3]], ""
                if size >= 4 and exactish:            # endings only on real words
                    m = re.match(r"^(.+?)(['’]\w{1,3})$", core)
                    if m and m.group(1).casefold() == t.fold:
                        core, suffix = m.group(1), m.group(2)
                    elif core[-1:] in "sS" and core[:-1].casefold() == t.fold:
                        core, suffix = core[:-1], core[-1:]
                    elif (not t.latin and core.casefold().startswith(t.fold)
                          and 0 < len(core) - len(t.term) <= 2):
                        # Armenian/Russian case endings: "Այբուբենը", "Ивана".
                        core, suffix = core[:len(t.term)], core[len(t.term):]
                cf = core.casefold()
                if cf == t.fold:
                    if (core == t.term or not t.distinctive
                            or (core == t.term[:1].upper() + t.term[1:]
                                and not (t.term[:1].islower()
                                         and any(ch.isupper() for ch in t.term[1:])))
                            or (size < 4 and _ordinary_case(core))):
                        continue
                    score = 1.0
                elif cf in folds:
                    continue                          # exactly another of the user's terms
                elif n > t.words:
                    score = 1.0
                else:
                    if (not near or cand in keys
                            or "".join(ch for ch in cand if ch.isdecimal()) != t.digits
                            or not t.symbols <= set(core)
                            or not 0.8 * size <= len(cand) <= 1.25 * size
                            or _other_form(t, cand, core[:1].isupper())):
                        continue
                    matcher = difflib.SequenceMatcher(None, t.key, cand)
                    if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                        continue
                    score = matcher.ratio()
                    if score < threshold:
                        continue
                found.append((score, n, i, start, start + len(core) + len(suffix), t.term + suffix))
    # Best match first ("pyside 6" as one exact span beats "pyside" alone),
    # shorter spans on ties; overlapping spans can't both apply.
    taken = [False] * len(toks)
    edits = []
    for score, n, i, start, end, rep in sorted(found, key=lambda f: (-f[0], f[1], f[2])):
        if any(taken[i:i + n]):
            continue
        for j in range(i, i + n):
            taken[j] = True
        edits.append((start, end, rep))
    for start, end, rep in sorted(edits, reverse=True):
        text = text[:start] + rep + text[end:]
    return text


def sync_config(cfg):
    """Mirror ``initial_prompt`` from the structured list, in place.

    Only overwrites when structured terms exist, so a user's hand-written prose
    prompt from an older build is preserved until they actually edit the list.
    """
    if not isinstance(cfg, dict):
        return
    terms = normalize_terms(cfg.get("vocabulary"))
    if terms:
        cfg["initial_prompt"] = _render(terms)
