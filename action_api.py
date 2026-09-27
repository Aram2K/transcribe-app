import json
import re

import requests


PROVIDER_OPENAI = "openai_compatible"
PROVIDER_GEMINI = "gemini"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CEREBRAS = "cerebras"      # OpenAI-compatible wire; fastest inference (vision-capable Qwen)
PROVIDER_MISTRAL = "mistral"        # OpenAI-compatible wire; reuses the Voxtral speech key

PROVIDERS = {
    PROVIDER_OPENAI: {
        "label": "OpenAI-compatible API",
        "description": "Works with OpenAI, OpenRouter, Groq, Together, Modal, LM Studio, and compatible servers.",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.4-mini",
    },
    PROVIDER_GEMINI: {
        "label": "Google Gemini API",
        "description": "Use your own Gemini API key for cloud action modes.",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-flash",
    },
    PROVIDER_ANTHROPIC: {
        "label": "Anthropic API",
        "description": "Use your own Anthropic API key for cloud action modes.",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-6",
    },
    PROVIDER_CEREBRAS: {
        # Verified Sept 2026: qwen-3.8-27b streams ~1,850 tok/s, accepts PNG
        # images as data URIs, and needs reasoning_effort "none" or it thinks
        # for seconds first. Same /chat/completions wire as OpenAI.
        "label": "Cerebras (fastest · vision)",
        "description": "Sub-second answers for Live Assistance. Key from cloud.cerebras.ai.",
        "default_base_url": "https://api.cerebras.ai/v1",
        "default_model": "qwen-3.8-27b",
        "default_recap_model": "gpt-oss-120b",
    },
    PROVIDER_MISTRAL: {
        # One Mistral key covers speech (Voxtral), the copilot (Ministral 3,
        # image input) and OCR. Same /chat/completions wire as OpenAI; Mistral
        # rejects unknown fields, so no reasoning_effort is sent to it.
        "label": "Mistral AI (Ministral · vision)",
        "description": "Ministral / Mistral models; reuses your Mistral (Voxtral) key.",
        "default_base_url": "https://api.mistral.ai/v1",
        "default_model": "ministral-14b-2512",
        "default_recap_model": "ministral-8b-2512",
    },
}

MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"


def mistral_ocr(image_b64, key, timeout=30):
    """Text of a screenshot via Mistral OCR (markdown, all pages joined).
    Returns "" on any failure - callers treat OCR as best-effort context."""
    if not (image_b64 and (key or "").strip()):
        return ""
    try:
        resp = requests.post(
            MISTRAL_OCR_URL,
            headers={"Authorization": f"Bearer {key.strip()}", "Content-Type": "application/json"},
            json={"model": "mistral-ocr-latest",
                  "document": {"type": "image_url", "image_url": _image_data_url(image_b64)}},
            timeout=timeout,
        )
        if not (200 <= resp.status_code < 300):
            return ""
        pages = resp.json().get("pages") or []
        return "\n\n".join((p.get("markdown") or "") for p in pages).strip()
    except Exception:
        return ""

# Models that think before answering unless told not to. For the live
# features every second of hidden reasoning is a second the user waits.
_NO_REASONING_FAMILIES = ("qwen-3.", "qwen3.", "qwen/qwen3.")
_MIN_LOW_FAMILIES = ("gpt-oss",)          # cannot disable reasoning, only lower it


def reasoning_effort_for(model, configured=""):
    """The reasoning_effort to send for ``model`` (or None to omit): the
    explicit setting wins; otherwise thinking models default to "none", and
    gpt-oss (which rejects "none") is floored at "low"."""
    m = (model or "").lower()
    val = (configured or "").strip().lower()
    if not val:
        if any(f in m for f in _NO_REASONING_FAMILIES):
            val = "none"
        elif any(f in m for f in _MIN_LOW_FAMILIES):
            val = "low"
        else:
            return None
    if val == "none" and any(f in m for f in _MIN_LOW_FAMILIES):
        val = "low"
    return val


def self_hosted_family(model, base_url):
    """"qwen" / "deepseek" for a thinking model served by vLLM/SGLang - a Modal
    endpoint or your own server, addressed by its Hugging Face id
    ("deepseek-ai/DeepSeek-V4.1-Flash", "Qwen/Qwen3.6-35B-A3B") - else None.
    Such servers switch thinking off through the chat template, and older ones
    reject reasoning_effort="none" (hosted APIs like Cerebras/Groq use their
    own ids and keep reasoning_effort)."""
    m = model or ""
    on_modal = "modal.run" in (base_url or "")
    if m.startswith("deepseek-ai/") or (on_modal and "deepseek" in m.lower()):
        return "deepseek"
    if m.startswith("Qwen/") or (on_modal and "qwen" in m.lower()):
        return "qwen"
    return None


# Recommended non-thinking sampling for live answers, per model card: Qwen3's
# own numbers; DeepSeek's card gives temperature 1.0 / top_p 0.95 for thinking
# benchmarks - a little cooler here for short, factual answers.
_LIVE_SAMPLING = {
    "qwen": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5},
    "deepseek": {"temperature": 0.6, "top_p": 0.95},
}


def openai_payload_extras(model, base_url, mode, configured_effort=""):
    """Provider-specific fields for an OpenAI-compatible chat request."""
    family = self_hosted_family(model, base_url)
    if family:
        # DeepSeek V4.x reads {thinking}, Qwen3 {enable_thinking}; vLLM takes
        # both as long as they agree. Thinking must be off: DeepSeek thinks by
        # default, and with a short max_tokens the trace eats the whole budget
        # and the answer comes back empty.
        extras = {"chat_template_kwargs": {"thinking": False, "enable_thinking": False}}
        if mode in ("live_assist", "live_recap"):
            # The rest of the actions keep the near-deterministic default.
            extras.update(_LIVE_SAMPLING[family])
        return extras
    effort = reasoning_effort_for(model, configured_effort)
    return {"reasoning_effort": effort} if effort else {}


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
# Self-hosted DeepSeek (V3.1+/V4/R1) and Qwen3 templates OPEN the reasoning
# block in the prompt, so a server without a reasoning parser returns
# "{reasoning}</think>{answer}" - only the close tag, often with no newline.
# Such a model never writes the tag in a normal (non-thinking) answer, so for
# these families the first tag outside code ends the reasoning. Every other
# model gets no such guess: a hosted model that mentions "</think>" is talking
# about it.
_FENCE = re.compile(r"(?m)^[ \t]*```")
_PROBE_CHARS = 12000          # past this much visible text, stop looking


def _orphan_close_end(text, start=0):
    """End of a template-opened block's close tag in ``text`` - the first
    "</think>" outside inline code and ``` fences (fences count only where a
    line starts, so a fence mentioned mid-sentence doesn't flip them) - or -1."""
    i = text.find(_THINK_CLOSE, start)
    while i >= 0:
        before = text[:i]
        line = before[before.rfind("\n") + 1:]
        if len(_FENCE.findall(before)) % 2 == 0 and line.replace("```", "").count("`") % 2 == 0:
            return i + len(_THINK_CLOSE)
        i = text.find(_THINK_CLOSE, i + 1)
    return -1


class ReplaceText(str):
    """A stream delta meaning "replace everything shown so far with this":
    the reasoning a template-opened block streamed is only recognisable once
    its close tag arrives."""


class ThinkFilter:
    """Keeps reasoning a model writes inline out of a token stream. Servers
    with a reasoning parser send reasoning as a separate field, which the
    parsers here never read; this covers servers without one:

    * a leading ``<think>...</think>`` block is held back and dropped;
    * with ``template_opened`` (a self-hosted DeepSeek/Qwen - see
      :func:`self_hosted_family`), a block the template opened is recognised
      when its close tag arrives, and :meth:`feed` returns a
      :class:`ReplaceText` with the answer that follows, so the caller can
      drop what it showed.

    ``saw_reasoning`` tells the caller reasoning was removed, so an empty
    result means "spent the whole budget thinking", not "had nothing to say".
    """

    def __init__(self, template_opened=False):
        self._buf = ""
        self._inside = False
        self._started = False
        self._shown = ""                    # visible text, while probing
        self._probing = bool(template_opened)
        self.saw_reasoning = False

    def feed(self, delta):
        """The visible part of ``delta`` ("" while inside a thinking block or
        while a possible tag is still incomplete), or a ReplaceText."""
        if self._started:
            return self._probe(delta or "")
        self._buf += delta or ""
        if self._inside:
            i = self._buf.find(_THINK_CLOSE)
            if i < 0:
                self._buf = self._buf[-(len(_THINK_CLOSE) - 1):]
                return ""
            self._inside = False
            self._probing = False           # the block is over: an answer follows
            self._buf = self._buf[i + len(_THINK_CLOSE):].lstrip()
            return self.feed("")
        head = self._buf.lstrip()
        if head.startswith(_THINK_OPEN):
            self._inside = True
            self.saw_reasoning = True
            self._buf = head[len(_THINK_OPEN):]
            return self.feed("")
        if not head or _THINK_OPEN.startswith(head):
            return ""                       # whitespace or a partial "<thi" so far
        self._started = True
        out, self._buf = self._buf, ""
        return self._probe(out)

    def _probe(self, out):
        if not self._probing or not out:
            return out
        scan_from = max(0, len(self._shown) - len(_THINK_CLOSE))
        self._shown += out
        end = _orphan_close_end(self._shown, scan_from)
        if end >= 0:
            self._probing = False
            self.saw_reasoning = True
            rest, self._shown = self._shown[end:].lstrip(), ""
            return ReplaceText(rest)
        if len(self._shown) > _PROBE_CHARS:
            self._probing, self._shown = False, ""
        return out

    def flush(self):
        """Whatever is still held back when the stream ends."""
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


def strip_think(text, template_opened=False):
    """``text`` without its reasoning: a leading ``<think>`` block, or - with
    ``template_opened`` - a template-opened block that only shows its close
    tag."""
    t = text or ""
    lead = t.lstrip()
    if lead.startswith(_THINK_OPEN):
        end = lead.find(_THINK_CLOSE)
        return "" if end < 0 else lead[end + len(_THINK_CLOSE):].strip()
    if template_opened:
        end = _orphan_close_end(t)
        if end >= 0:
            return t[end:].strip()
    return t.strip()


THOUGHT_ONLY = ("The AI model spent its whole answer thinking and never answered - "
                "ask again.")


def _emit(delta, parts, on_token):
    """Pass one filtered delta on: append it, or - for a ReplaceText - start
    over with the answer that followed the reasoning."""
    if isinstance(delta, ReplaceText):
        parts[:] = [str(delta)]
        on_token(delta)
    elif delta:
        parts.append(delta)
        on_token(delta)


def model_for(config, mode, provider_defaults):
    """Model id for this call: an optional cheaper/faster model for the
    rolling recap (action_api_model_recap), else the configured model, else
    the provider default."""
    if mode == "live_recap":
        recap = (config.get("action_api_model_recap") or "").strip()
        if recap:
            return recap
    return (config.get("action_api_model") or provider_defaults["default_model"]).strip()


class ActionAPIError(RuntimeError):
    pass


import smart_prompt


def _max_tokens_for(mode):
    """Approximate completion budget per action mode.

    Meeting notes need the most room because the prompt expects four
    sections of structured output. Standalone summaries are short."""
    if mode == "meeting_notes":
        return 1200
    if mode == "smart_auto":
        return 600
    if mode == "live_assist":
        return 700       # room for a complete code solution; the stream shows it as it lands
    if mode == "live_recap":
        return 220
    if mode == "summarize":
        return 400
    if mode == "write_email":
        return 360
    return 240


_ENGINE_TO_PROVIDER = {
    "api_openai_compatible": PROVIDER_OPENAI,
    "api_gemini": PROVIDER_GEMINI,
    "api_anthropic": PROVIDER_ANTHROPIC,
    "api_cerebras": PROVIDER_CEREBRAS,
    "api_mistral": PROVIDER_MISTRAL,
}


def normalize_provider(provider):
    if provider in PROVIDERS:
        return provider
    return _ENGINE_TO_PROVIDER.get(provider, PROVIDER_OPENAI)


def defaults(provider):
    return PROVIDERS[normalize_provider(provider)]


# Live Assistance: who the model is, what it gets, and WHY it must be brief -
# the brief is the system message (identical on every call, so the endpoint
# can cache it - cheaper and a faster first word), the user turn carries only
# live_context.rolling_context's sections.
LIVE_ASSIST_SYSTEM = (
    "You are Live Assistance, a real-time companion built into a meeting "
    "app. The user - the person running the app - is in a live call or "
    "meeting right now. Whatever you write appears on a small card on their "
    "screen while they are talking: they read it at a glance, often "
    "mid-sentence, or say it out loud. Every extra word costs them attention "
    "and every second counts, so you answer and solve - you never summarise.\n"
    "\n"
    "What you receive:\n"
    "- \"Conversation (latest part)\": the last few minutes, transcribed "
    "automatically as people speak. It has no speaker labels and mixes the "
    "user's voice with the other participants'. Words may be misheard, "
    "missing or cut mid-sentence. Read it like a sharp colleague listening "
    "in: infer the intended words (names, technical terms) and work out from "
    "context who is asking whom. You are also called automatically on "
    "anything that sounds like a question - even the user's own, or the user "
    "reading your answers aloud.\n"
    "- Sometimes: the meeting title and attendees (use their exact "
    "spelling), your latest answers (up to two, clipped; for follow-ups such "
    "as \"and the second part?\"), and a question the user typed or picked.\n"
    "- With Screen on, every answer comes with a screenshot of the monitor "
    "under the mouse, relevant or not; ignore the Live Assistance card if it "
    "appears in it. If asked about the screen with no screenshot or nothing "
    "on it to solve, say so in a few words - never guess.\n"
    "\n"
    "What to do:\n"
    "1. If there is a \"User's question\", answer exactly that.\n"
    "2. Otherwise answer the most recent question or request the user now "
    "has to respond to, not an earlier one. Speech comes first, the screen "
    "is context: solve what it shows (code, an error, a form, a question on "
    "a slide) only when that is what is asked or nothing was asked.\n"
    "3. Lead with the answer itself, ready to use: the words to say if "
    "someone asked the user something (1-2 natural spoken sentences, first "
    "person), else the plain fact, number, command or solution. Then at most "
    "3 short bullets, only if they add real value (the key reason, a caveat, "
    "something to double-check). Under ~70 words outside code.\n"
    "4. Code, commands, queries and spreadsheet formulas go in a fenced code "
    "block with the language tag - runnable, no TODO stubs; for a fix, only "
    "the changed lines with enough around them to place them - never retype "
    "code you can't see.\n"
    "5. Answer in the language of the \"(Respond in ...)\" line; without "
    "one, in the conversation's language - not that of these instructions or "
    "a picked question.\n"
    "\n"
    "Never recap the conversation or repeat the question back; no headings, "
    "tables, LaTeX or preamble. The user may say your words as their own, so "
    "never invent facts about them or their work (background, experience, "
    "numbers, dates, commitments) - leave a short [blank] in the words to "
    "say instead. If nothing needs an answer from the user right now, reply "
    "with one short line they could say next. Not for exams or assessments "
    "of the user where outside help isn't allowed: if one is clearly on "
    "screen or in the talk, say so in one line instead of solving it."
)


def build_messages(text, mode, source_lang="auto", target_lang="en", vocab_block=""):
    text = (text or "").strip()
    if mode == "live_assist":
        # The live context is already labelled section by section; the brief
        # lives in the system message.
        return [
            {"role": "system", "content": LIVE_ASSIST_SYSTEM},
            {"role": "user", "content": text},
        ]
    if mode == "smart_auto":
        return smart_prompt.build_messages(text, vocab_block=vocab_block)
    if mode == "write_email":
        instruction = "Turn this dictated text into a concise email draft. Output only the email."
    elif mode == "make_todo_list":
        instruction = "Extract a clear Markdown todo checklist. Output only '- [ ]' checklist items."
    elif mode == "translate":
        instruction = f"Translate from {source_lang or 'auto'} to {target_lang}. Output only the translation."
    elif mode == "summarize":
        instruction = (
            "Summarize this text in 3-5 sentences capturing the main points. "
            "Output only the summary - no preamble, no headings."
        )
    elif mode == "meeting_notes":
        # Prompt engineering applied from Microsoft Research's meeting-recap
        # paper (arxiv 2307.15793): two-stage thinking (identify utterances
        # first, then summarise with context), third-person rephrasing so
        # the notes are shareable, explicit owner attribution, and careful
        # name preservation for non-Western names.
        instruction = (
            "You are summarising a meeting transcript. Think carefully before writing:\n"
            "1. First mentally identify the most important utterances in the transcript "
            "(decisions, commitments, questions, blockers). DO NOT output this - it is "
            "internal reasoning to ground the notes.\n"
            "2. Then produce well-formed Markdown with EXACTLY these four sections in "
            "this order, even if a section is empty:\n\n"
            "## Summary\n"
            "2-4 sentences in third person ('The team discussed…', 'Aram committed to…'). "
            "Never use first-person ('I will…') - convert to third person using whoever "
            "spoke when known, or 'the speaker' otherwise.\n\n"
            "## Key decisions\n"
            "Bullet list of concrete decisions reached. Skip if none.\n\n"
            "## Action items\n"
            "Markdown checkbox list. Format each as:\n"
            "`- [ ] <task>` (Owner: <name>, Due: <date>)\n"
            "Attribute owners using these heuristics:\n"
            "  - 'I'll do X' → Owner is the speaker (use their name if known)\n"
            "  - 'Aram should review' → Owner: Aram\n"
            "  - 'we need to' → leave owner blank\n"
            "Skip the section if no action items.\n\n"
            "## Open questions\n"
            "Bullet list of questions raised but not answered. Skip if none.\n\n"
            "CRITICAL RULES:\n"
            "- Preserve names EXACTLY as written, including Armenian, Russian, and "
            "other non-English names. Never anglicise or guess at spellings.\n"
            "- Do not invent decisions, owners, dates, or facts that aren't in the "
            "transcript. If unsure, omit rather than fabricate.\n"
            "- The transcript may contain `[speaker change]` markers indicating likely "
            "speaker transitions; use them to attribute who said what when names are "
            "available from the attendees list.\n"
            "- If an attendee list is provided in context, prefer those exact names "
            "when attributing owners."
        )
    elif mode == "live_recap":
        # Rolling "so far" panel during a call: regenerated every ~20 s, read
        # at a glance. Bullets only.
        instruction = (
            "You maintain a live running summary of an ongoing meeting from its "
            "transcript so far (automatic speech recognition, may be imperfect). "
            "Output Markdown bullets only - at most 6, under 90 words in total: what "
            "has been discussed, any decisions, open questions; put the most recent "
            "developments last and bold the key phrase of each bullet. No headings, "
            "no preamble. Never invent facts or names."
        )
    else:
        instruction = "Rewrite this text clearly while preserving meaning. Output only the result."
    system = "You are a concise assistant. Never add commentary."
    if vocab_block:
        system = f"{system}\n\n{vocab_block}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{instruction}\n\nText:\n{text}"},
    ]


SMART_ACTION_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/smart-action"


def run_managed_action(text, mode, token, source_lang="auto", target_lang="en",
                       vocab_block="", image_b64=None, image_status=None):
    """Pro Smart Actions via the server (no BYO key): we build the messages here
    and the edge function runs them through the founder's Mistral key.

    ``image_b64`` (a PNG screenshot) rides along as OpenAI-style content parts,
    which the server forwards verbatim; if the server or model rejects the
    multimodal shape, the call is retried once text-only so a screenshot can
    never turn a working suggestion into an error."""
    if not token:
        raise ActionAPIError("Sign in with Pro to use managed Smart Actions.")
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=vocab_block)

    def _post(msgs):
        return requests.post(
            SMART_ACTION_URL,
            json={"messages": msgs, "max_tokens": _max_tokens_for(mode)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=45,
        )
    try:
        resp = _post(openai_messages_with_image(messages, image_b64))
        if image_b64 and resp.status_code in _IMAGE_REFUSED:
            resp = _post(without_screenshot(messages))
            _image_dropped(image_status)
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching Smart Actions: {e}")
    _raise_for_managed_status(resp)
    data = _json_or_error(resp)
    return strip_think(data.get("text") or "", _answer_family(resp) in ("deepseek", "qwen"))


def _raise_for_managed_status(resp):
    if resp.status_code == 403:
        raise ActionAPIError("Pro is required for managed Smart Actions.")
    if resp.status_code == 429:
        raise ActionAPIError("Daily Smart Actions limit reached - try again tomorrow.")
    if resp.status_code == 503:
        raise ActionAPIError("Managed Smart Actions aren't set up on the server yet.")


def run_managed_action_stream(text, mode, token, on_token, source_lang="auto",
                              target_lang="en", vocab_block="", image_b64=None,
                              image_status=None):
    """Streaming Pro Smart Actions: the server relays the model's event stream,
    so the first words show as soon as they are generated. A server that
    predates streaming answers with plain JSON, delivered in one piece."""
    if not token:
        raise ActionAPIError("Sign in with Pro to use managed Smart Actions.")
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=vocab_block)

    def _post(msgs):
        return requests.post(
            SMART_ACTION_URL,
            json={"messages": msgs, "max_tokens": _max_tokens_for(mode),
                  "mode": mode, "stream": True},
            headers={"Authorization": f"Bearer {token}"},
            stream=True, timeout=(10, 90),
        )
    try:
        resp = _post(openai_messages_with_image(messages, image_b64))
        if image_b64 and resp.status_code in _IMAGE_REFUSED:
            _close(resp)
            resp = _post(without_screenshot(messages))
            _image_dropped(image_status)
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching Smart Actions: {e}")
    _raise_for_managed_status(resp)
    template_opened = _answer_family(resp) in ("deepseek", "qwen")
    if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
        out = strip_think(_json_or_error(resp).get("text") or "", template_opened)
        if out:
            on_token(out)
        return out
    parts = []
    think = ThinkFilter(template_opened)
    try:
        for data in _sse_data_lines(resp):
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("error"):
                raise ActionAPIError(str(obj["error"]))
            _emit(think.feed(((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""),
                  parts, on_token)
    except requests.RequestException:
        _interrupted(parts)
    finally:
        _close(resp)                        # also stops the model upstream
    _emit(think.flush(), parts, on_token)
    return _final(parts, think, template_opened)


def warm_up_managed(token, timeout=5):
    """Ask the server to wake the live model - a scale-to-zero GPU endpoint
    can take a while to start - while the user is still settling into the
    call. Fire-and-forget: errors are ignored."""
    if not token:
        return
    try:
        requests.post(SMART_ACTION_URL, json={"warmup": True, "mode": "live_assist"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except requests.RequestException:
        pass


def warm_up(config, timeout=180):
    """Wake a self-hosted, scale-to-zero endpoint (a dedicated Modal DeepSeek
    or Qwen) with a one-token request. Hosted APIs have no cold start, so
    nothing is sent to them. Blocking - call from a worker thread; errors are
    ignored."""
    provider = normalize_provider(config.get("action_api_provider"))
    key = (config.get("action_api_key") or "").strip()
    if provider in (PROVIDER_GEMINI, PROVIDER_ANTHROPIC) or not key:
        return
    d = defaults(provider)
    base_url = (config.get("action_api_base_url") or d["default_base_url"]).rstrip("/")
    model = model_for(config, "live_assist", d)
    if not self_hosted_family(model, base_url):
        return
    payload = {"model": model, "max_tokens": 1,
               "messages": [{"role": "user", "content": "ping"}]}
    payload.update(openai_payload_extras(model, base_url, "warmup"))
    try:
        requests.post(f"{base_url}/chat/completions", json=payload, timeout=timeout,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    except requests.RequestException:
        pass


def _image_mime(b64):
    # Screenshots are sent as JPEG (smaller, faster upload); older callers and
    # OCR still pass PNG. Base64 of a JPEG always starts with "/9j/".
    return "image/jpeg" if (b64 or "").startswith("/9j/") else "image/png"


def _image_data_url(b64):
    return f"data:{_image_mime(b64)};base64,{b64}"


def openai_messages_with_image(messages, image_b64):
    """Attach a PNG to the LAST user turn as OpenAI-style content parts (the
    shape the managed server forwards unchanged). No image -> untouched copy."""
    out = [dict(m) for m in messages]
    if not image_b64:
        return out
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user" and isinstance(out[i].get("content"), str):
            out[i]["content"] = [
                {"type": "text", "text": out[i]["content"]},
                {"type": "image_url", "image_url": {"url": _image_data_url(image_b64)}},
            ]
            break
    return out


# The note live_context.rolling_context adds when a screenshot rides along;
# dropped again when the screenshot has to be left out.
_SCREEN_NOTE = re.compile(r"\n*\(A screenshot of the user's screen is attached\.[^)]*\)")
_IMAGE_REFUSED = (400, 413, 415, 422)


_NO_SCREEN_NOTE = ("(No screenshot could be sent - you cannot see the user's screen. If "
                   "the question is about the screen, say so in one short line.)")

# (base_url, model) pairs that refused a screenshot in this run: later calls
# leave the image out instead of paying the failed round trip every time.
_NO_VISION = set()


def without_screenshot(messages):
    """``messages`` with no screenshot, and the note claiming one is attached
    replaced by one saying it could not be sent - so a model that refused the
    image (or a Solve screen request) doesn't answer about a screen it never
    saw."""
    out = [dict(m) for m in messages]
    for m in out:
        if isinstance(m.get("content"), str):
            m["content"] = _SCREEN_NOTE.sub("", m["content"])
    for m in reversed(out):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] = f"{m['content']}\n\n{_NO_SCREEN_NOTE}"
            break
    return out


def _image_dropped(status):
    """Tell the caller (Live Assistance) its screenshot never reached the model."""
    if isinstance(status, dict):
        status["dropped"] = True


def _answer_family(resp):
    """The model family the server says answered ("deepseek"/"qwen"/""), so the
    thinking filter can use the right rule for a template-opened block."""
    try:
        return (resp.headers.get("X-Answer-Family") or "").strip().lower()
    except Exception:
        return ""


def _final(parts, think, template_opened):
    """The finished answer; an empty one after reasoning was removed means the
    model never got past thinking - say so rather than show "(no answer)"."""
    text = strip_think("".join(parts), template_opened)
    if not text and think.saw_reasoning:
        raise ActionAPIError(THOUGHT_ONLY)
    return text


def gemini_parts(prompt, image_b64=None):
    parts = [{"text": prompt}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": _image_mime(image_b64), "data": image_b64}})
    return parts


def anthropic_convo_with_image(convo, image_b64):
    """Image block before the text of the LAST user turn (Anthropic's
    recommended ordering for image + question)."""
    out = [dict(m) for m in convo]
    if not image_b64:
        return out
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user" and isinstance(out[i].get("content"), str):
            out[i]["content"] = [
                {"type": "image", "source": {"type": "base64",
                                              "media_type": _image_mime(image_b64),
                                              "data": image_b64}},
                {"type": "text", "text": out[i]["content"]},
            ]
            break
    return out


def _sse_data_lines(resp):
    """Yield the payload of each `data:` line of a server-sent-events stream.

    Lines are split on the raw bytes and decoded one at a time, as UTF-8 (the
    event-stream charset - `requests` would guess ISO-8859-1 for a text/*
    response without one and garble every Armenian or Russian token).
    Decoding first and splitting after would also break lines on U+2028,
    U+2029 and U+0085, which JSON leaves unescaped, cutting such an event in
    two and silently dropping its text; CR and LF never occur inside a
    multi-byte UTF-8 sequence, so byte splitting is exact."""
    resp.encoding = "utf-8"                 # for anyone reading resp.text
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not line.startswith("data:"):
            continue
        yield line[5:].strip()


def _close(resp):
    close = getattr(resp, "close", None)
    if close:
        try:
            close()
        except Exception:
            pass


def _interrupted(parts):
    """The connection dropped mid-stream: keep what already arrived (the
    caller returns it), or say so plainly when nothing did."""
    if not parts:
        raise ActionAPIError("The answer was interrupted - check your connection and try again.")


def _prepare(config, mode, source_lang, target_lang):
    provider = normalize_provider(config.get("action_api_provider"))
    key = (config.get("action_api_key") or "").strip()
    if not key:
        raise ActionAPIError("Add your action API key before using this action engine.")
    if config.get("privacy_mode"):
        raise ActionAPIError("Cloud action APIs are disabled in Privacy Mode.")
    return provider, key


def run_action_stream(text, mode, config, on_token, source_lang="auto", target_lang="en"):
    """Streaming counterpart of run_action: ``on_token(delta)`` fires as text
    arrives; returns the full text. Works for OpenAI-compatible endpoints
    (OpenAI, Groq, Cerebras, ...), Gemini and Anthropic."""
    provider, key = _prepare(config, mode, source_lang, target_lang)
    if provider == PROVIDER_GEMINI:
        return _stream_gemini(text, mode, config, source_lang, target_lang, key, on_token)
    if provider == PROVIDER_ANTHROPIC:
        return _stream_anthropic(text, mode, config, source_lang, target_lang, key, on_token)
    return _stream_openai_compatible(text, mode, config, source_lang, target_lang, key, on_token)


def _stream_openai_compatible(text, mode, config, source_lang, target_lang, key, on_token):
    provider_defaults = defaults(config.get("action_api_provider"))
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = model_for(config, mode, provider_defaults)
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=config.get("_vocab_block", ""))
    image, status = config.get("_image_png_b64"), config.get("_image_status")
    if image and (base_url, model) in _NO_VISION:
        image, messages = None, without_screenshot(messages)
        _image_dropped(status)
    template_opened = bool(self_hosted_family(model, base_url))
    payload = {
        "model": model,
        "messages": openai_messages_with_image(messages, image),
        "temperature": 0.1,
        "max_tokens": _max_tokens_for(mode),
        "stream": True,
    }
    payload.update(openai_payload_extras(model, base_url, mode,
                                         config.get("action_api_reasoning_effort")))

    def _post(body):
        return requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body, stream=True, timeout=(10, 60),
        )
    try:
        resp = _post(payload)
        if image and resp.status_code in _IMAGE_REFUSED:
            # A text-only model (most DeepSeek, many self-hosted ones): answer
            # from the conversation rather than fail every Screen answer.
            _close(resp)
            resp = _post(dict(payload, messages=without_screenshot(messages)))
            _image_dropped(status)
            if 200 <= resp.status_code < 300:
                _NO_VISION.add((base_url, model))
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching the action API: {e}")
    if not (200 <= resp.status_code < 300):
        _json_or_error(resp)
    parts = []
    think = ThinkFilter(template_opened)
    try:
        for data in _sse_data_lines(resp):
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
                delta = think.feed(obj["choices"][0].get("delta", {}).get("content") or "")
            except Exception:
                continue
            _emit(delta, parts, on_token)
    except requests.RequestException:
        _interrupted(parts)
    finally:
        _close(resp)
    _emit(think.flush(), parts, on_token)
    return _final(parts, think, template_opened)


def _stream_gemini(text, mode, config, source_lang, target_lang, key, on_token):
    provider_defaults = defaults(PROVIDER_GEMINI)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=config.get("_vocab_block", ""))
    prompt = "\n\n".join(m["content"] for m in messages)
    try:
        resp = requests.post(
            f"{base_url}/models/{model}:streamGenerateContent?alt=sse",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": gemini_parts(prompt, config.get("_image_png_b64"))}],
                  "generationConfig": {"temperature": 0.1}},
            stream=True, timeout=(10, 60),
        )
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching Gemini: {e}")
    if not (200 <= resp.status_code < 300):
        _json_or_error(resp)
    parts = []
    try:
        for data in _sse_data_lines(resp):
            try:
                obj = json.loads(data)
                deltas = [part.get("text") or "" for part in
                          obj.get("candidates", [{}])[0].get("content", {}).get("parts", [])]
            except Exception:
                continue
            for delta in deltas:
                if delta:
                    parts.append(delta)
                    on_token(delta)
    except requests.RequestException:
        _interrupted(parts)
    finally:
        _close(resp)
    return "".join(parts).strip()


def _stream_anthropic(text, mode, config, source_lang, target_lang, key, on_token):
    provider_defaults = defaults(PROVIDER_ANTHROPIC)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=config.get("_vocab_block", ""))
    system = messages[0]["content"]
    convo = [m for m in messages[1:] if m.get("role") in ("user", "assistant")]
    if not convo:
        convo = [{"role": "user", "content": text}]
    convo = anthropic_convo_with_image(convo, config.get("_image_png_b64"))
    try:
        resp = requests.post(
            f"{base_url}/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json={"model": model, "system": system, "messages": convo,
                  "temperature": 0.1, "max_tokens": _max_tokens_for(mode), "stream": True},
            stream=True, timeout=(10, 60),
        )
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching Anthropic: {e}")
    if not (200 <= resp.status_code < 300):
        _json_or_error(resp)
    parts = []
    try:
        for data in _sse_data_lines(resp):
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("type") == "content_block_delta":
                delta = (obj.get("delta") or {}).get("text") or ""
                if delta:
                    parts.append(delta)
                    on_token(delta)
            elif obj.get("type") == "message_stop":
                break
    except requests.RequestException:
        _interrupted(parts)
    finally:
        _close(resp)
    return "".join(parts).strip()


def run_action(text, mode, config, source_lang="auto", target_lang="en"):
    provider = normalize_provider(config.get("action_api_provider"))
    key = (config.get("action_api_key") or "").strip()
    if not key:
        raise ActionAPIError("Add your action API key before using this action engine.")
    if config.get("privacy_mode"):
        raise ActionAPIError("Cloud action APIs are disabled in Privacy Mode.")

    if provider == PROVIDER_GEMINI:
        return _run_gemini(text, mode, config, source_lang, target_lang, key)
    if provider == PROVIDER_ANTHROPIC:
        return _run_anthropic(text, mode, config, source_lang, target_lang, key)
    return _run_openai_compatible(text, mode, config, source_lang, target_lang, key)


def _run_openai_compatible(text, mode, config, source_lang, target_lang, key):
    provider_defaults = defaults(config.get("action_api_provider"))
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = model_for(config, mode, provider_defaults)
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=config.get("_vocab_block", ""))
    image, status = config.get("_image_png_b64"), config.get("_image_status")
    if image and (base_url, model) in _NO_VISION:
        image, messages = None, without_screenshot(messages)
        _image_dropped(status)
    payload = {
        "model": model,
        "messages": openai_messages_with_image(messages, image),
        "temperature": 0.1,
        "max_tokens": _max_tokens_for(mode),
    }
    payload.update(openai_payload_extras(model, base_url, mode,
                                         config.get("action_api_reasoning_effort")))

    def _post(body):
        return requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=45,
        )
    resp = _post(payload)
    if image and resp.status_code in _IMAGE_REFUSED:
        resp = _post(dict(payload, messages=without_screenshot(messages)))
        _image_dropped(status)
        if 200 <= resp.status_code < 300:
            _NO_VISION.add((base_url, model))
    data = _json_or_error(resp)
    return strip_think(data.get("choices", [{}])[0].get("message", {}).get("content") or "",
                       bool(self_hosted_family(model, base_url)))


def _run_gemini(text, mode, config, source_lang, target_lang, key):
    provider_defaults = defaults(PROVIDER_GEMINI)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=config.get("_vocab_block", ""))
    prompt = "\n\n".join(m["content"] for m in messages)
    resp = requests.post(
        f"{base_url}/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": gemini_parts(prompt, config.get("_image_png_b64"))}],
              "generationConfig": {"temperature": 0.1}},
        timeout=45,
    )
    data = _json_or_error(resp)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "\n".join(part.get("text", "") for part in parts).strip()


def _run_anthropic(text, mode, config, source_lang, target_lang, key):
    provider_defaults = defaults(PROVIDER_ANTHROPIC)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    messages = build_messages(text, mode, source_lang, target_lang,
                              vocab_block=config.get("_vocab_block", ""))
    # messages is [system, *few-shot turns, user]. Taking messages[1] as the
    # user turn silently sent the FIRST FEW-SHOT EXAMPLE instead of what the
    # user actually dictated - so Anthropic transcribed a canned sample. Keep
    # the system turn and forward every remaining turn, few-shots included.
    system = messages[0]["content"]
    convo = [m for m in messages[1:] if m.get("role") in ("user", "assistant")]
    if not convo:
        convo = [{"role": "user", "content": text}]
    convo = anthropic_convo_with_image(convo, config.get("_image_png_b64"))
    resp = requests.post(
        f"{base_url}/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "system": system,
            "messages": convo,
            "temperature": 0.1,
            "max_tokens": _max_tokens_for(mode),
        },
        timeout=45,
    )
    data = _json_or_error(resp)
    return "\n".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text").strip()


def _json_or_error(resp):
    try:
        data = resp.json()
    except Exception as e:
        raise ActionAPIError(f"Action API returned HTTP {getattr(resp, 'status_code', 'error')}.") from e
    if not (200 <= getattr(resp, "status_code", 0) < 300):
        err = data.get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else err
        if not message and isinstance(data, dict):
            message = data.get("message") or data.get("detail")   # vLLM / FastAPI shapes
        raise ActionAPIError(str(message or f"Action API returned HTTP {resp.status_code}."))
    return data
