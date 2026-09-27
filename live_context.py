"""Live Assistance logic with no Qt in it: what the model is sent, when the
screen goes along, and when a piece of the conversation is a question the
companion should answer on its own. Kept out of ui/live_assist.py so it is
testable without a display (and without PySide6)."""
import re

TAIL_CHARS = 2600                # ~3-4 minutes of speech fed to the model
HISTORY_TURNS = 2                # earlier answers kept for follow-up questions

SOLVE_SCREEN = ("Solve the problem or answer the question shown on my screen. "
                "Give the solution directly.")

_LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish",
               "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
               "hy": "Armenian", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese"}


def rolling_context(live_text, question="", title="", attendees="",
                    tail_chars=TAIL_CHARS, screen=False, output_lang="en", history=()):
    """The text handed to the model: recent conversation + optional question,
    plus the last answers of this session (``history``: (asked, answer) pairs)
    so a follow-up like "and the second part?" has something to refer to.
    Cuts at a sentence boundary when it can so the model doesn't start
    mid-word."""
    tail = (live_text or "").strip()
    if len(tail) > tail_chars:
        tail = tail[-tail_chars:]
        cut = re.search(r"[.!?]\s+", tail)
        if cut and cut.end() < len(tail) // 2:
            tail = tail[cut.end():]
    parts = []
    if title or attendees:
        meta = []
        if title:
            meta.append(f"Meeting: {title}")
        if attendees:
            meta.append(f"Attendees: {attendees}")
        parts.append("\n".join(meta))
    earlier = [(q, a) for q, a in list(history)[-HISTORY_TURNS:] if (a or "").strip()]
    if earlier:
        lines = ["Your earlier answers in this conversation (for follow-ups only):"]
        for q, a in earlier:
            a = " ".join(a.split())
            lines.append(f"- {('Q: ' + q.strip() + ' A: ') if (q or '').strip() else ''}"
                         f"{a[:280]}{'…' if len(a) > 280 else ''}")
        parts.append("\n".join(lines))
    parts.append("Conversation (latest part):\n" + (tail or "(nothing transcribed yet)"))
    if screen:
        # Context, not the task: with Screen on a screenshot rides along with
        # every answer, relevant or not (the system prompt says the same).
        parts.append("(A screenshot of the user's screen is attached. Use it as "
                     "context; solve what it shows only when that is what is being "
                     "asked or nothing was asked.)")
    if output_lang and output_lang != "auto":
        parts.append(f"(Respond in {_LANG_NAMES.get(output_lang, output_lang)}.)")
    if (question or "").strip():
        parts.append("User's question: " + question.strip())
    return "\n\n".join(parts)


def should_attach_screen(screen_on, force=False):
    """Screen context is on or off - no keyword guessing. Guessing from the
    words ("slide", "error"...) missed most real questions about the screen
    ("how do I solve this?"), so with Screen on every answer sees the screen.
    ``force`` is an explicit request (the Solve screen button)."""
    return bool(screen_on or force)


# Speech recognition without a "?": a request is one however it ends ("Walk me
# through it."); a wh-/yes-no opener only when the sentence has no full stop -
# "What I would do first is add a cache." is a statement (and often the user
# reading the answer aloud, which must not trigger a new one).
_REQUEST_OPENER = re.compile(
    r"^(can you|could you|would you|will you|tell me|explain|describe|"
    r"walk me through|give me|show me)\b", re.I)
_QUESTION_OPENER = re.compile(
    r"^(what|how|why|when|where|who|whom|whose|which|do you|did you|have you|"
    r"are you|is it|is there|are there|should (i|we))\b", re.I)


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?։՞])\s+", text or "") if s.strip()]


def looks_like_question(piece):
    """True when freshly transcribed speech asks something - the moment the
    companion answers without being asked. Whisper punctuates questions in
    most languages (Armenian marks them with '՞'); without punctuation, an
    opener like "what" or "tell me" counts."""
    p = (piece or "").strip()
    if not p:
        return False
    if any(mark in p for mark in ("?", "՞", "？", "¿")):
        return True
    for s in _sentences(p):
        if _REQUEST_OPENER.match(s):
            return True
        if _QUESTION_OPENER.match(s) and not s.endswith((".", "!", "։", "…")):
            return True
    return False


def last_question(text):
    """The last question-like sentence of ``text`` ("" if none)."""
    for s in reversed(_sentences(text)):
        if looks_like_question(s):
            return s[:200]
    return ""
