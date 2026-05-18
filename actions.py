import re

import action_api
import local_llm


ACTION_TRANSCRIBE_ONLY = "transcribe_only"
ACTION_WRITE_EMAIL = "write_email"
ACTION_MAKE_TODO = "make_todo_list"
ACTION_TRANSLATE = "translate"

RULE_BASED_ID = "rule_based"
API_OPENAI_ID = "api_openai_compatible"
API_GEMINI_ID = "api_gemini"
API_ANTHROPIC_ID = "api_anthropic"

ACTION_MODES = {
    ACTION_TRANSCRIBE_ONLY: {
        "label": "Transcribe only",
        "description": "Paste exactly what was transcribed.",
    },
    ACTION_WRITE_EMAIL: {
        "label": "Write email",
        "description": "Turn the transcription into an email draft.",
    },
    ACTION_MAKE_TODO: {
        "label": "Make todo list",
        "description": "Extract a checklist from the transcription.",
    },
    ACTION_TRANSLATE: {
        "label": "Translate",
        "description": "Translate with a local language pack, local LLM, or your own action API.",
    },
}

ACTION_MODELS = {
    RULE_BASED_ID: {
        "label": "Rule-based formatter",
        "description": "Fast local formatting for email/todo. Simple rules, not an LLM.",
        "available": True,
        "kind": "rules",
    },
    API_OPENAI_ID: {
        "label": "OpenAI-compatible API",
        "description": "Bring your own key for OpenAI, OpenRouter, Groq, Together, LM Studio, or compatible APIs.",
        "available": True,
        "kind": "cloud",
        "provider": action_api.PROVIDER_OPENAI,
    },
    API_GEMINI_ID: {
        "label": "Google Gemini API",
        "description": "Bring your own Gemini API key for cloud action modes.",
        "available": True,
        "kind": "cloud",
        "provider": action_api.PROVIDER_GEMINI,
    },
    API_ANTHROPIC_ID: {
        "label": "Anthropic API",
        "description": "Bring your own Anthropic API key for cloud action modes.",
        "available": True,
        "kind": "cloud",
        "provider": action_api.PROVIDER_ANTHROPIC,
    },
}

for _code, _info in local_llm.MODEL_CATALOG.items():
    ACTION_MODELS[_code] = {
        "label": _info["label"],
        "description": _info["description"],
        "available": True,
        "downloadable": True,
        "kind": "local_llm",
        "min_ram": _info["min_ram"],
        "size": _info["size"],
        "gpu_recommended": _info["gpu_recommended"],
    }

ACTION_MODEL_ALIASES = {
    "built_in": RULE_BASED_ID,
    "aibuben_tiny": local_llm.QWEN_TINY_ID,
    "aibuben_balanced": local_llm.QWEN_3B_ID,
    "aibuben_gpu": local_llm.QWEN_7B_ID,
    "openai": API_OPENAI_ID,
    "gemini": API_GEMINI_ID,
    "anthropic": API_ANTHROPIC_ID,
}

ACTION_API_MODELS = {
    API_OPENAI_ID: {
        "provider": action_api.PROVIDER_OPENAI,
        **action_api.PROVIDERS[action_api.PROVIDER_OPENAI],
    },
    API_GEMINI_ID: {
        "provider": action_api.PROVIDER_GEMINI,
        **action_api.PROVIDERS[action_api.PROVIDER_GEMINI],
    },
    API_ANTHROPIC_ID: {
        "provider": action_api.PROVIDER_ANTHROPIC,
        **action_api.PROVIDERS[action_api.PROVIDER_ANTHROPIC],
    },
}

TRANSLATE_TARGETS = {
    "en": "English",
    "hy": "Armenian",
    "ru": "Russian",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ar": "Arabic",
}


class ActionError(RuntimeError):
    pass


def normalize_action_mode(mode):
    return mode if mode in ACTION_MODES else ACTION_TRANSCRIBE_ONLY


def normalize_action_model(model):
    model = ACTION_MODEL_ALIASES.get(model or RULE_BASED_ID, model or RULE_BASED_ID)
    return model if model in ACTION_MODELS else RULE_BASED_ID


def normalize_translate_target(code):
    return code if code in TRANSLATE_TARGETS else "en"


def process(text, mode, source_lang="auto", target_lang="en", model=RULE_BASED_ID, config=None):
    mode = normalize_action_mode(mode)
    model = normalize_action_model(model)
    text = (text or "").strip()
    if not text:
        return ""
    if mode == ACTION_TRANSCRIBE_ONLY:
        return text

    if ACTION_MODELS[model].get("kind") == "cloud":
        if not config:
            raise ActionError("Add your action API settings before using this action engine.")
        api_config = {**config, "action_api_provider": ACTION_MODELS[model]["provider"]}
        try:
            return action_api.run_action(text, mode, api_config, source_lang=source_lang, target_lang=target_lang)
        except action_api.ActionAPIError as e:
            raise ActionError(str(e)) from e

    if ACTION_MODELS[model].get("kind") == "local_llm" and local_llm.model_downloaded(model):
        try:
            return local_llm.run_action(text, mode, source_lang=source_lang, target_lang=target_lang, model_id=model)
        except local_llm.LocalLLMError as e:
            if mode == ACTION_TRANSLATE:
                raise ActionError(str(e)) from e

    if mode == ACTION_WRITE_EMAIL:
        return _write_email(text)
    if mode == ACTION_MAKE_TODO:
        return _make_todo_list(text)
    if mode == ACTION_TRANSLATE:
        try:
            return _translate_local(text, source_lang, target_lang)
        except ActionError:
            llm_model = model if ACTION_MODELS[model].get("kind") == "local_llm" else _first_downloaded_local_model()
            if llm_model and local_llm.model_downloaded(llm_model):
                try:
                    return local_llm.run_action(text, mode, source_lang=source_lang, target_lang=target_lang, model_id=llm_model)
                except local_llm.LocalLLMError as e:
                    raise ActionError(str(e)) from e
            raise
    return text


def _first_downloaded_local_model():
    for model_id in local_llm.MODEL_CATALOG:
        try:
            if local_llm.model_downloaded(model_id):
                return model_id
        except local_llm.LocalLLMError:
            pass
    return None


def _clean_sentence(text):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _subject_from_text(text):
    cleaned = _clean_sentence(text)
    words = cleaned.split()
    subject = " ".join(words[:8]).strip(" ,.;:")
    return subject or "Quick note"


def _write_email(text):
    body = _clean_sentence(text)
    if body and body[-1] not in ".!?":
        body += "."
    subject = _subject_from_text(body)
    return (
        f"Subject: {subject}\n\n"
        "Hi,\n\n"
        f"{body}\n\n"
        "Best,"
    )


def _make_todo_list(text):
    raw = re.sub(r"\b(and then|then|also|plus|after that)\b", ".", text, flags=re.I)
    pieces = re.split(r"[.;\n]+", raw)
    tasks = []
    for piece in pieces:
        item = re.sub(
            r"^\s*(please|can you|could you|i need to|we need to|remember to|todo|task)\s+",
            "",
            piece.strip(),
            flags=re.I,
        ).strip(" -")
        if len(item) < 2:
            continue
        item = _clean_sentence(item)
        if item[-1:] in ".!?":
            item = item[:-1]
        tasks.append(item)
    if not tasks:
        tasks = [_clean_sentence(text).rstrip(".!?")]
    return "\n".join(f"- [ ] {task}" for task in tasks if task)


def _translate_local(text, source_lang, target_lang):
    try:
        from argostranslate import translate
    except Exception as e:
        raise ActionError(
            "Local translation needs Argos Translate and an installed offline language pack."
        ) from e

    source_code = (source_lang or "auto").split("-")[0]
    target_code = normalize_translate_target(target_lang)
    installed = translate.get_installed_languages()
    from_lang = None
    if source_code != "auto":
        from_lang = next((lang for lang in installed if lang.code == source_code), None)
    if from_lang is None:
        from_lang = next((lang for lang in installed if lang.code != target_code), None)
    to_lang = next((lang for lang in installed if lang.code == target_code), None)
    if not from_lang or not to_lang:
        raise ActionError(
            f"Missing local translation pack for {source_code or 'auto'} -> {target_code}."
        )
    translation = from_lang.get_translation(to_lang)
    if not translation:
        raise ActionError(
            f"Missing local translation pack for {from_lang.code} -> {target_code}."
        )
    return translation.translate(text).strip()
