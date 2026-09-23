import json

import requests


PROVIDER_OPENAI = "openai_compatible"
PROVIDER_GEMINI = "gemini"
PROVIDER_ANTHROPIC = "anthropic"

PROVIDERS = {
    PROVIDER_OPENAI: {
        "label": "OpenAI-compatible API",
        "description": "Works with OpenAI, OpenRouter, Groq, Together, LM Studio, and compatible servers.",
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
}


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
        return 320
    if mode == "live_recap":
        return 220
    if mode == "summarize":
        return 400
    if mode == "write_email":
        return 360
    return 240


def normalize_provider(provider):
    return provider if provider in PROVIDERS else PROVIDER_OPENAI


def defaults(provider):
    return PROVIDERS[normalize_provider(provider)]


def build_messages(text, mode, source_lang="auto", target_lang="en", vocab_block=""):
    text = (text or "").strip()
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
    elif mode == "live_assist":
        # Real-time copilot during a call. The input is the TAIL of a live
        # transcript (imperfect ASR, maybe mid-sentence) plus an optional
        # question from the user. Short, scannable output - it is read at a
        # glance while the user is talking to someone.
        instruction = (
            "You are a discreet real-time meeting copilot for the user - the person "
            "running this app - during a live call. The text is the most recent part "
            "of the conversation (automatic speech recognition, may be imperfect or "
            "cut mid-sentence), optionally followed by a question from the user.\n"
            "If the user asked a question, answer it using the conversation.\n"
            "Otherwise, work out what was MOST RECENTLY asked or expected of the "
            "user - the last question or request in the text, not an earlier one - "
            "and give them what to say next.\n"
            "Format (Markdown, at most ~120 words - it is read at a glance):\n"
            "**They're asking:** <one line - or **Latest:** if nothing was asked>\n"
            "- 2 to 4 short, concrete talking points or the direct answer; put the "
            "key phrase of each point in **bold**\n"
            "**Watch out:** <one line, only if there is a real risk or open point>\n"
            "If code, a command or a formula is genuinely what's needed, give it in "
            "a fenced code block with the language tag; keep prose out of the block.\n"
            "Be specific to what was actually said. Use ONLY the conversation and any "
            "meeting details given - never invent facts, numbers or names, and never "
            "make claims about the user's own background, experience or credentials. "
            "If the transcript is too thin to help, say so in one line."
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
                       vocab_block="", image_b64=None):
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
        if image_b64 and resp.status_code in (400, 413, 415, 422):
            resp = _post(messages)
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching Smart Actions: {e}")
    if resp.status_code == 403:
        raise ActionAPIError("Pro is required for managed Smart Actions.")
    if resp.status_code == 429:
        raise ActionAPIError("Daily Smart Actions limit reached - try again tomorrow.")
    if resp.status_code == 503:
        raise ActionAPIError("Managed Smart Actions aren't set up on the server yet.")
    data = _json_or_error(resp)
    return (data.get("text") or "").strip()


def _image_data_url(b64):
    return f"data:image/png;base64,{b64}"


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


def gemini_parts(prompt, image_b64=None):
    parts = [{"text": prompt}]
    if image_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": image_b64}})
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
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                              "data": image_b64}},
                {"type": "text", "text": out[i]["content"]},
            ]
            break
    return out


def _sse_data_lines(resp):
    """Yield the payload of each `data:` line of a server-sent-events stream."""
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        yield line[5:].strip()


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
    provider_defaults = defaults(PROVIDER_OPENAI)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    payload = {
        "model": model,
        "messages": openai_messages_with_image(
            build_messages(text, mode, source_lang, target_lang,
                           vocab_block=config.get("_vocab_block", "")),
            config.get("_image_png_b64")),
        "temperature": 0.1,
        "max_tokens": _max_tokens_for(mode),
        "stream": True,
    }
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, stream=True, timeout=(10, 60),
        )
    except requests.RequestException as e:
        raise ActionAPIError(f"Network error reaching the action API: {e}")
    if not (200 <= resp.status_code < 300):
        _json_or_error(resp)
    parts = []
    for data in _sse_data_lines(resp):
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
            delta = obj["choices"][0].get("delta", {}).get("content") or ""
        except Exception:
            continue
        if delta:
            parts.append(delta)
            on_token(delta)
    return "".join(parts).strip()


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
    for data in _sse_data_lines(resp):
        try:
            obj = json.loads(data)
            for part in obj.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                delta = part.get("text") or ""
                if delta:
                    parts.append(delta)
                    on_token(delta)
        except Exception:
            continue
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
    provider_defaults = defaults(PROVIDER_OPENAI)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    payload = {
        "model": model,
        "messages": openai_messages_with_image(
            build_messages(text, mode, source_lang, target_lang,
                           vocab_block=config.get("_vocab_block", "")),
            config.get("_image_png_b64")),
        "temperature": 0.1,
        "max_tokens": _max_tokens_for(mode),
    }
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=45,
    )
    data = _json_or_error(resp)
    return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


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
        message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise ActionAPIError(str(message or f"Action API returned HTTP {resp.status_code}."))
    return data
