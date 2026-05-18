import requests


PROVIDER_OPENAI = "openai_compatible"
PROVIDER_GEMINI = "gemini"
PROVIDER_ANTHROPIC = "anthropic"

PROVIDERS = {
    PROVIDER_OPENAI: {
        "label": "OpenAI-compatible API",
        "description": "Works with OpenAI, OpenRouter, Groq, Together, LM Studio, and compatible servers.",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    PROVIDER_GEMINI: {
        "label": "Google Gemini API",
        "description": "Use your own Gemini API key for cloud action modes.",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-1.5-flash",
    },
    PROVIDER_ANTHROPIC: {
        "label": "Anthropic API",
        "description": "Use your own Anthropic API key for cloud action modes.",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-haiku-20240307",
    },
}


class ActionAPIError(RuntimeError):
    pass


def normalize_provider(provider):
    return provider if provider in PROVIDERS else PROVIDER_OPENAI


def defaults(provider):
    return PROVIDERS[normalize_provider(provider)]


def build_messages(text, mode, source_lang="auto", target_lang="en"):
    text = (text or "").strip()
    if mode == "write_email":
        instruction = "Turn this dictated text into a concise email draft. Output only the email."
    elif mode == "make_todo_list":
        instruction = "Extract a clear Markdown todo checklist. Output only '- [ ]' checklist items."
    elif mode == "translate":
        instruction = f"Translate from {source_lang or 'auto'} to {target_lang}. Output only the translation."
    else:
        instruction = "Rewrite this text clearly while preserving meaning. Output only the result."
    return [
        {"role": "system", "content": "You are a concise assistant. Never add commentary."},
        {"role": "user", "content": f"{instruction}\n\nText:\n{text}"},
    ]


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
        "messages": build_messages(text, mode, source_lang, target_lang),
        "temperature": 0.1,
        "max_tokens": 360 if mode == "write_email" else 240,
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
    messages = build_messages(text, mode, source_lang, target_lang)
    prompt = "\n\n".join(m["content"] for m in messages)
    resp = requests.post(
        f"{base_url}/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}},
        timeout=45,
    )
    data = _json_or_error(resp)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "\n".join(part.get("text", "") for part in parts).strip()


def _run_anthropic(text, mode, config, source_lang, target_lang, key):
    provider_defaults = defaults(PROVIDER_ANTHROPIC)
    base_url = (config.get("action_api_base_url") or provider_defaults["default_base_url"]).rstrip("/")
    model = (config.get("action_api_model") or provider_defaults["default_model"]).strip()
    messages = build_messages(text, mode, source_lang, target_lang)
    system = messages[0]["content"]
    user = messages[1]["content"]
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
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": 360 if mode == "write_email" else 240,
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
