# Smart Auto-Action prompt and few-shot examples.
#
# This module is the single source of truth for Smart-mode behaviour.
# Both the local LLM runtime (local_llm.py) and the cloud API runtime
# (action_api.py) import from here so the user gets identical behaviour
# regardless of which engine is selected.
#
# Users can override the examples by creating
# %APPDATA%/Transcribe/smart_examples.json (or the platform equivalent).
# The override file replaces the defaults entirely - copy-paste the
# DEFAULT_EXAMPLES list and edit as needed.

import json
import logging
import re

import storage


logger = logging.getLogger("transcribe.smart")


SMART_SYSTEM_PROMPT = (
    "You are a private voice-dictation assistant. The user just dictated "
    "something. Decide what they want and output ONLY the final result - "
    "no preamble, no quotation marks, no commentary, no markdown fences.\n"
    "\n"
    "Rules:\n"
    "- If they explicitly ask to TRANSLATE → output only the translation "
    "in the target language they named. Do not include the original.\n"
    "- If they ask to WRITE/COMPOSE/DRAFT an email → output a clean "
    "email (Subject line, greeting, body, sign-off). Use \"Best,\" by default.\n"
    "- If they ask to MAKE A LIST / TODO / CHECKLIST → output a Markdown "
    "checklist with `- [ ]` items. One item per line.\n"
    "- If they ask to SUMMARIZE / SUMMARY / RECAP / TLDR → output a 2-4 "
    "sentence summary capturing the main points.\n"
    "- If they ask to REWRITE / CLEAN UP / POLISH / FIX → improve "
    "grammar and clarity while keeping the meaning. Strip filler words "
    "(um, uh, like, you know).\n"
    "- If they ask for MEETING NOTES → use the structure: ## Summary, "
    "## Key decisions, ## Action items.\n"
    "- Otherwise → return their words verbatim with correct punctuation "
    "and capitalization. Strip filler words. Never repeat their "
    "instruction in the output.\n"
    "\n"
    "Never include the action verb or instruction phrase in the output. "
    "If they said \"translate to russian: I will meet you at 5pm\", the "
    "output is the Russian translation only - not the word \"translate\"."
)


# Each example is (user-transcript, expected-output). Picked to cover the
# common intent buckets so the model can pattern-match against them. Keep
# them short - long examples eat into the context window and slow down
# small local models.
DEFAULT_EXAMPLES = [
    (
        "translate to russian I will meet you at 5pm",
        "Встретимся в 5 вечера",
    ),
    (
        "translate this to french the report is ready and i will send it tonight",
        "Le rapport est prêt et je l'enverrai ce soir.",
    ),
    (
        "write an email to john that the q3 report is ready and i will send draft tonight",
        "Subject: Q3 Report Ready\n\nHi John,\n\nThe Q3 report is ready - I'll send the draft over tonight.\n\nBest,",
    ),
    (
        "make a todo list call mom buy groceries finish slides for friday",
        "- [ ] Call mom\n- [ ] Buy groceries\n- [ ] Finish slides for Friday",
    ),
    (
        "summarize this we talked about q3 numbers being below target we need to revisit hiring plan and marketing budget john will draft proposal",
        "Q3 numbers were below target. The team needs to revisit the hiring plan and marketing budget. John will draft a proposal.",
    ),
    (
        "clean this up um so basically we like need to ship the feature next week i think",
        "We need to ship the feature next week.",
    ),
    (
        "the meeting is at three pm in conference room b",
        "The meeting is at 3pm in Conference Room B.",
    ),
]


# Fast pre-check: if none of these tokens appear, the user almost
# certainly wants raw transcription, so we skip the LLM entirely.
# Word boundaries matter - "fixed" shouldn't match "fix", etc.
_ACTION_INTENT_RE = re.compile(
    r"\b("
    r"translate|translation|"
    r"email|e-?mail|"
    r"write\s+(?:an?\s+)?(?:email|e-?mail|message)|"
    r"send\s+(?:an?\s+)?(?:email|e-?mail|message)|"
    r"compose|draft (an? )?(email|message|reply)|"
    r"todo|to-?do|to do list|checklist|"
    r"list of|make (a |the )?list|"
    r"remind me to|things to do|"
    r"summari[sz]e|summary|recap|tldr|tl;dr|brief(ly)? (this|that|it)|"
    r"rewrite|clean (this|that|it) up|polish|fix (grammar|the grammar|typo|typos|this)|"
    r"meeting notes|take notes"
    r")\b",
    re.IGNORECASE,
)


# Maps spoken intent → concrete action mode (see actions.py constants).
_DETECT_RULES = (
    ("translate", re.compile(r"\b(translate|translation)\b", re.I)),
    ("write_email", re.compile(
        r"\b(email|e-?mail|compose|draft|reply)\b|"
        r"\bwrite\s+(?:an?\s+)?(?:email|e-?mail|message)\b|"
        r"\bsend\s+(?:an?\s+)?(?:email|e-?mail|message)\b",
        re.I,
    )),
    ("make_todo_list", re.compile(
        r"\b(todo|to-?do|checklist|make (a |the )?list|things to do|remind me to)\b",
        re.I,
    )),
    ("summarize", re.compile(
        r"\b(summari[sz]e|summary|recap|tldr|tl;dr|brief(ly)? (this|that|it))\b",
        re.I,
    )),
    ("meeting_notes", re.compile(r"\b(meeting notes|take notes)\b", re.I)),
    ("rewrite", re.compile(
        r"\b(rewrite|clean (this|that|it) up|polish|"
        r"fix (grammar|the grammar|typo|typos|this))\b",
        re.I,
    )),
)

_WRITE_EMAIL_PREFIX = re.compile(
    r"^(?:please\s+)?(?:"
    r"(?:write|compose|draft|send)\s+(?:an?\s+)?(?:email|e-mail|message|reply)"
    r"|email\s+)"
    r"(?:to\s+[\w.@\-']+(?:\s+[\w.@\-']+)?\s+)?"
    r"(?:saying|that|about|regarding|:)?\s*",
    re.I,
)

_TRANSLATE_PREFIX = re.compile(
    r"^(?:please\s+)?translate(?:\s+this)?\s+to\s+(\w+)\s*[:,-]?\s*",
    re.I,
)

_TODO_PREFIX = re.compile(
    r"^(?:please\s+)?(?:make\s+(?:a\s+)?(?:todo|to-?do)\s*list|todo\s*list)\s*[:,-]?\s*",
    re.I,
)

_SUMMARY_PREFIX = re.compile(
    r"^(?:please\s+)?(?:summari[sz]e(?:\s+this)?|summary(?:\s+of)?|recap)\s*[:,-]?\s*",
    re.I,
)

_REWRITE_PREFIX = re.compile(
    r"^(?:please\s+)?(?:rewrite|clean (?:this|that|it) up|polish|fix (?:grammar|this))\s*[:,-]?\s*",
    re.I,
)

_LANG_NAME_TO_CODE = {
    "english": "en", "armenian": "hy", "russian": "ru", "french": "fr",
    "german": "de", "spanish": "es", "italian": "it", "portuguese": "pt",
    "arabic": "ar",
}


def detect_action_mode(text):
    """Pick a concrete action mode from a Smart-mode transcript, or None."""
    if not text:
        return None
    for mode, pattern in _DETECT_RULES:
        if pattern.search(text):
            return mode
    return None


def parse_translate_target(text):
    """Parse 'translate to french …' → language code."""
    m = _TRANSLATE_PREFIX.match((text or "").strip())
    if not m:
        return None
    word = m.group(1).lower()
    if len(word) <= 3 and word.isalpha():
        return word
    return _LANG_NAME_TO_CODE.get(word)


def extract_payload(text, mode):
    """Strip the instruction phrase; keep what should be transformed."""
    text = (text or "").strip()
    if not text:
        return ""
    if mode == "write_email":
        m = _WRITE_EMAIL_PREFIX.match(text)
        if m:
            rest = text[m.end() :].strip()
            if rest:
                return rest
    elif mode == "translate":
        m = _TRANSLATE_PREFIX.match(text)
        if m:
            rest = text[m.end() :].strip()
            if rest:
                return rest
    elif mode == "make_todo_list":
        m = _TODO_PREFIX.match(text)
        if m:
            rest = text[m.end() :].strip()
            if rest:
                return rest
    elif mode == "summarize":
        m = _SUMMARY_PREFIX.match(text)
        if m:
            rest = text[m.end() :].strip()
            if rest:
                return rest
    elif mode == "rewrite":
        m = _REWRITE_PREFIX.match(text)
        if m:
            rest = text[m.end() :].strip()
            if rest:
                return rest
    return text


def has_action_intent(text):
    """Fast regex check - does this transcript look like an action request?

    Returning False means the Smart-mode pipeline should bypass the LLM and
    just pass the (cleaned) text through. Returning True means we run the
    LLM with the smart prompt.

    Conservative: false negatives are fine (LLM not called, user gets raw
    text) but false positives waste an LLM call.
    """
    if not text:
        return False
    return bool(_ACTION_INTENT_RE.search(text))


def _user_examples_path():
    return storage.path_for("smart_examples.json")


def load_examples():
    """Return the active example list.

    If the user has placed smart_examples.json in their app-data dir, load
    and validate it. Otherwise return DEFAULT_EXAMPLES. Invalid user
    overrides log a warning and fall back to defaults.
    """
    path = _user_examples_path()
    if not path.exists():
        return DEFAULT_EXAMPLES
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("examples") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise ValueError("examples is not a list")
        out = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict) and "input" in item and "output" in item:
                out.append((str(item["input"]), str(item["output"])))
        if not out:
            raise ValueError("no valid examples after parsing")
        return out
    except Exception as e:
        logger.warning("smart_examples.json invalid (%s); using defaults", e)
        return DEFAULT_EXAMPLES


def build_messages(transcript):
    """Return chat-format messages: system + few-shot example pairs + user.

    Works for any chat-completions API (OpenAI, Anthropic via adapter,
    llama-cpp-python create_chat_completion).
    """
    messages = [{"role": "system", "content": SMART_SYSTEM_PROMPT}]
    for user_in, assistant_out in load_examples():
        messages.append({"role": "user", "content": user_in})
        messages.append({"role": "assistant", "content": assistant_out})
    messages.append({"role": "user", "content": (transcript or "").strip()})
    return messages
