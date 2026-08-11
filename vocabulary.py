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

Pure stdlib, no project imports.
"""
import re

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
    """One sentence for prompt-driven cloud STT. "" when empty or gated."""
    if not _cloud_allowed(cfg):
        return ""
    terms = load_terms(cfg)
    if not terms:
        return ""
    joined = ", ".join(terms)[:MAX_PROMPT_CHARS]
    return (" Spell these terms exactly when you hear them, including "
            "similar-sounding variants: %s." % joined)


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
        "Treat the following as the authoritative spelling for names, products "
        "and technical terms. When the transcript clearly refers to one of them, "
        "correct near-miss or phonetically similar spellings to match. Do not "
        "force one in where the text plainly means something else:\n"
        + ", ".join(terms)
    )


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
