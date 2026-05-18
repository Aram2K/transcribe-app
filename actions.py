import re

import local_llm


ACTION_TRANSCRIBE_ONLY = "transcribe_only"
ACTION_WRITE_EMAIL = "write_email"
ACTION_MAKE_TODO = "make_todo_list"
ACTION_TRANSLATE = "translate"

ACTION_MODES = {
    ACTION_TRANSCRIBE_ONLY: {
        "label": "Transcribe only",
        "description": "Paste exactly what was transcribed.",
    },
    ACTION_WRITE_EMAIL: {
        "label": "Write email",
        "description": "Turn the transcription into a simple email draft locally.",
    },
    ACTION_MAKE_TODO: {
        "label": "Make todo list",
        "description": "Extract a checklist from the transcription locally.",
    },
    ACTION_TRANSLATE: {
        "label": "Translate",
        "description": "Translate locally when an offline language pack is installed.",
    },
}

ACTION_MODELS = {
    "built_in": {
        "label": "Built-in local actions",
        "description": "Works now without downloads. Best for email/todo formatting.",
        "available": True,
    },
    local_llm.QWEN_TINY_ID: {
        "label": "Qwen Tiny local model",
        "description": "Downloadable GGUF action model for 16 GB RAM computers.",
        "available": True,
        "downloadable": True,
    },
    "aibuben_balanced": {
        "label": "Aibuben Balanced local model",
        "description": "Planned stronger local action model for faster CPUs or GPUs.",
        "available": False,
    },
    "aibuben_gpu": {
        "label": "Aibuben GPU local model",
        "description": "Planned high-quality GPU action model.",
        "available": False,
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
    model = model or "built_in"
    if model == "aibuben_tiny":
        model = local_llm.QWEN_TINY_ID
    return model if model in ACTION_MODELS else "built_in"


def normalize_translate_target(code):
    return code if code in TRANSLATE_TARGETS else "en"


def process(text, mode, source_lang="auto", target_lang="en", model="built_in"):
    mode = normalize_action_mode(mode)
    model = normalize_action_model(model)
    text = (text or "").strip()
    if not text:
        return ""
    if mode == ACTION_TRANSCRIBE_ONLY:
        return text
    if model == local_llm.QWEN_TINY_ID and local_llm.model_downloaded():
        try:
            return local_llm.run_action(text, mode, source_lang=source_lang, target_lang=target_lang)
        except local_llm.LocalLLMError as e:
            if mode == ACTION_TRANSLATE:
                raise ActionError(str(e)) from e
    elif model != "built_in" and not ACTION_MODELS[model]["available"]:
        raise ActionError("That local action model is not available yet. Use Built-in local actions for now.")
    if mode == ACTION_WRITE_EMAIL:
        return _write_email(text)
    if mode == ACTION_MAKE_TODO:
        return _make_todo_list(text)
    if mode == ACTION_TRANSLATE:
        try:
            return _translate_local(text, source_lang, target_lang)
        except ActionError:
            if local_llm.model_downloaded():
                try:
                    return local_llm.run_action(text, mode, source_lang=source_lang, target_lang=target_lang)
                except local_llm.LocalLLMError as e:
                    raise ActionError(str(e)) from e
            raise
    return text


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
            f"Missing local translation pack for {from_lang.code} -> {to_lang.code}."
        )
    return translation.translate(text).strip()
