import shutil
import threading
from pathlib import Path

import psutil
import requests

import smart_prompt
import storage


QWEN_TINY_ID = "qwen_tiny"
QWEN_3B_ID = "qwen_3b"
QWEN_7B_ID = "qwen_7b"
GEMMA_2B_ID = "gemma_2b"

MODEL_CATALOG = {
    GEMMA_2B_ID: {
        "label": "Gemma 2 2B Instruct",
        "description": "Google's state-of-the-art 2B model. Highly accurate for reasoning, translation, and summary on modern CPUs.",
        "repo": "bartowski/gemma-2-2b-it-GGUF",
        "filename": "gemma-2-2b-it-Q4_K_M.gguf",
        "size": 1_600_000_000,
        "min_ram": 8,
        "gpu_recommended": False,
    },
    QWEN_TINY_ID: {
        "label": "Qwen Tiny 1.5B",
        "description": "Small local LLM for 16 GB RAM computers. Good first download for email, todo, and short translations.",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "size": 1_120_000_000,
        "min_ram": 8,
        "gpu_recommended": False,
    },

    QWEN_3B_ID: {
        "label": "Qwen 3B",
        "description": "Stronger local action model for better writing and translation on newer CPUs.",
        "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "filename": "qwen2.5-3b-instruct-q4_k_m.gguf",
        "size": 2_300_000_000,
        "min_ram": 12,
        "gpu_recommended": False,
    },
    QWEN_7B_ID: {
        "label": "Qwen 7B",
        "description": "Higher quality local action model for strong machines. GPU acceleration is recommended.",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "size": 4_700_000_000,
        "min_ram": 16,
        "gpu_recommended": True,
    },
}

LEGACY_MODEL_IDS = {
    "aibuben_tiny": QWEN_TINY_ID,
    "aibuben_balanced": QWEN_3B_ID,
    "aibuben_gpu": QWEN_7B_ID,
}

_llms = {}
_llm_lock = threading.Lock()
# One inference lock per model: llama-cpp's Llama shares a single native
# context, so two concurrent create_chat_completion calls corrupt state or
# crash the whole process (an access violation Python cannot catch). Dictation
# smart actions and meeting summaries run on different worker threads and
# default to the same model - same reasoning as the Whisper _infer_lock in
# main.py, which fixed the equivalent CUDA hang.
_infer_locks = {}


class LocalLLMError(RuntimeError):
    pass


def normalize_model_id(model_id):
    model_id = model_id or QWEN_TINY_ID
    return LEGACY_MODEL_IDS.get(model_id, model_id)


def model_info(model_id):
    model_id = normalize_model_id(model_id)
    try:
        return MODEL_CATALOG[model_id]
    except KeyError as e:
        raise LocalLLMError("Unknown local action model.") from e


def model_url(model_id):
    info = model_info(model_id)
    return f"https://huggingface.co/{info['repo']}/resolve/main/{info['filename']}"


def model_dir(model_id=QWEN_TINY_ID):
    return storage.path_for("action_models") / normalize_model_id(model_id)


def model_path(model_id=QWEN_TINY_ID):
    info = model_info(model_id)
    return model_dir(model_id) / info["filename"]


def partial_path(model_id=QWEN_TINY_ID):
    return Path(f"{model_path(model_id)}.part")


def model_downloaded(model_id=QWEN_TINY_ID):
    path = model_path(model_id)
    return path.exists() and path.stat().st_size > 100 * 1024 * 1024


def remove_model(model_id=QWEN_TINY_ID):
    model_id = normalize_model_id(model_id)
    unload_model(model_id)
    directory = model_dir(model_id)
    removed = False
    if directory.exists():
        shutil.rmtree(directory)
        removed = True
    part = partial_path(model_id)
    if part.exists():
        part.unlink()
        removed = True
    return removed


def unload_model(model_id=None):
    with _llm_lock:
        if model_id is None:
            _llms.clear()
        else:
            _llms.pop(normalize_model_id(model_id), None)


def download_model(model_id=QWEN_TINY_ID, on_progress=None):
    model_id = normalize_model_id(model_id)
    info = model_info(model_id)
    directory = model_dir(model_id)
    directory.mkdir(parents=True, exist_ok=True)
    dest = model_path(model_id)
    part = partial_path(model_id)
    got = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={got}-"} if got else {}

    with requests.get(model_url(model_id), stream=True, timeout=60, headers=headers, allow_redirects=True) as resp:
        if resp.status_code == 416:
            part.replace(dest)
            if on_progress:
                on_progress(100, dest.stat().st_size, dest.stat().st_size)
            return dest
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        expected = got + total if total else info["size"]
        mode = "ab" if got and resp.status_code == 206 else "wb"
        if mode == "wb":
            got = 0
        with part.open(mode) as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if on_progress:
                    pct = int((got / expected) * 100) if expected else None
                    on_progress(min(pct, 99) if pct is not None else None, got, expected)

    part.replace(dest)
    if on_progress:
        size = dest.stat().st_size
        on_progress(100, size, size)
    return dest


# Context window for every local model. All four catalog models support at
# least 8k (Qwen2.5: 32k, Gemma 2: 8k); the old 2048 made any meeting longer
# than ~10 minutes fail with "Requested tokens exceed context window".
_N_CTX = 8192

_MAX_TOKENS_BY_MODE = {
    "meeting_notes": 1200,
    "summarize": 400,
    "write_email": 360,
    "smart_auto": 600,
}


def _count_tokens(llm, text):
    """Token count as the model sees it; falls back to a chars/3 estimate."""
    if not text:
        return 0
    try:
        return len(llm.tokenize(text.encode("utf-8"), add_bos=False, special=False))
    except TypeError:
        return len(llm.tokenize(text.encode("utf-8")))
    except Exception:
        return max(1, len(text) // 3)


def _messages_tokens(llm, messages):
    """Prompt-size estimate: content tokens plus per-message template overhead."""
    return 16 + sum(24 + _count_tokens(llm, m.get("content") or "") for m in messages)


def _chat(llm, messages, max_tokens):
    result = llm.create_chat_completion(
        messages=messages,
        temperature=0.1,
        top_p=0.9,
        max_tokens=max_tokens,
        repeat_penalty=1.08,
    )
    return _extract_text(result)


def _split_by_tokens(llm, text, chunk_tokens):
    """Split on line boundaries into pieces of at most ~chunk_tokens each.
    Transcripts are line-oriented ("Speaker N: ..."), so lines are the natural
    unit; a single monster line is split by characters as a last resort."""
    chunks, cur, cur_tok = [], [], 0
    for line in text.split("\n"):
        t = _count_tokens(llm, line) + 1
        if t > chunk_tokens:
            if cur:
                chunks.append("\n".join(cur)); cur, cur_tok = [], 0
            step = max(400, len(line) * chunk_tokens // (t + 1))
            chunks.extend(line[i:i + step] for i in range(0, len(line), step))
            continue
        if cur and cur_tok + t > chunk_tokens:
            chunks.append("\n".join(cur)); cur, cur_tok = [], 0
        cur.append(line); cur_tok += t
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


_CONDENSE_SYSTEM = "You condense transcripts precisely. Never invent facts."
_CONDENSE_INSTRUCTION = (
    "Condense this portion of a longer transcript. Keep every decision, action "
    "item, owner name, number, date and technical term; drop filler and "
    "repetition. Output only the condensed notes."
)


def _condense_to_fit(llm, text, target_tokens):
    """Map-reduce a too-long text down to ~target_tokens: condense each chunk,
    join, repeat if needed, and hard-truncate as the final safety net."""
    chunk_budget = _N_CTX - 900 - 128   # room for instruction + condensed output
    for _ in range(3):
        if _count_tokens(llm, text) <= target_tokens:
            return text
        parts = []
        for chunk in _split_by_tokens(llm, text, chunk_budget):
            parts.append(_chat(llm, [
                {"role": "system", "content": _CONDENSE_SYSTEM},
                {"role": "user",
                 "content": f"{_CONDENSE_INSTRUCTION}\n\nTranscript portion:\n{chunk}"},
            ], max_tokens=700))
        text = "\n\n".join(p.strip() for p in parts if p.strip())
    toks = None
    try:
        toks = llm.tokenize(text.encode("utf-8"), add_bos=False, special=False)
    except Exception:
        return text[: target_tokens * 3]
    try:
        return llm.detokenize(toks[:target_tokens]).decode("utf-8", "replace")
    except Exception:
        return text[: target_tokens * 3]


_TRANSLATE_CHUNK_TOKENS = 1600


def _infer_lock_for(model_id):
    with _llm_lock:
        return _infer_locks.setdefault(normalize_model_id(model_id),
                                       threading.Lock())


def run_action(text, mode, source_lang="auto", target_lang="en", model_id=QWEN_TINY_ID,
               vocab_block=""):
    text = (text or "").strip()
    if not text:
        return ""
    llm = _load_model(model_id)
    # Serialize ALL inference on this model (tokenize included) - see
    # _infer_locks. The map-reduce path below can hold a model busy for
    # minutes, which is exactly when a dictation smart action would otherwise
    # land on the same Llama from another thread.
    with _infer_lock_for(model_id):
        return _run_action_locked(llm, text, mode, source_lang, target_lang,
                                  vocab_block)


def _run_action_locked(llm, text, mode, source_lang, target_lang, vocab_block):
    messages = _messages_for(mode, text, source_lang, target_lang, vocab_block)
    overhead = _messages_tokens(
        llm, _messages_for(mode, "", source_lang, target_lang, vocab_block))

    if mode == "translate":
        # A translation is roughly the size of its input, so the output budget
        # must SCALE with the input. A flat cap silently truncates: llama-cpp
        # just stops at max_tokens with finish_reason="length" and no error,
        # so the user would get a plausible-looking fragment.
        in_toks = _count_tokens(llm, text)
        wanted_out = max(600, in_toks * 2 + 64)
        if overhead + in_toks + wanted_out + 64 > _N_CTX:
            # Chunks sized so chunk + its own doubled output always fit.
            parts = []
            for chunk in _split_by_tokens(llm, text, _TRANSLATE_CHUNK_TOKENS):
                ch_toks = _count_tokens(llm, chunk)
                mt = min(_N_CTX - overhead - ch_toks - 64,
                         max(600, ch_toks * 2 + 64))
                parts.append(_chat(
                    llm,
                    _messages_for(mode, chunk, source_lang, target_lang, vocab_block),
                    mt))
            return "\n\n".join(p.strip() for p in parts if p.strip())
        return _chat(llm, messages, wanted_out)

    max_out = _MAX_TOKENS_BY_MODE.get(mode, 240)
    budget = _N_CTX - max_out - 128
    if _messages_tokens(llm, messages) > budget:
        # Input outgrew the context window (long meetings did this even at 8k,
        # and at the old 2048 a ten-minute meeting was enough to fail with
        # "Requested tokens exceed context window").
        room = max(512, budget - overhead)
        text = _condense_to_fit(llm, text, room)
        messages = _messages_for(mode, text, source_lang, target_lang, vocab_block)

    return _chat(llm, messages, max_out)


def _has_cuda():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _load_model(model_id):
    model_id = normalize_model_id(model_id)
    path = model_path(model_id)
    if not model_downloaded(model_id):
        raise LocalLLMError(f"{model_info(model_id)['label']} is not downloaded yet.")
    with _llm_lock:
        cached = _llms.get(model_id)
        if cached and cached.get("path") == str(path):
            return cached["llm"]
        try:
            from llama_cpp import Llama
        except Exception as e:
            raise LocalLLMError("llama-cpp-python is required for local action models.") from e

        gpu_layers = -1 if _has_cuda() else 0
        try:
            llm = Llama(
                model_path=str(path),
                n_ctx=_N_CTX,
                n_threads=max(2, min(8, psutil.cpu_count(logical=True) or 4)),
                n_gpu_layers=gpu_layers,
                verbose=False,
            )
        except Exception:
            if gpu_layers == 0:
                raise
            llm = Llama(
                model_path=str(path),
                n_ctx=_N_CTX,
                n_threads=max(2, min(8, psutil.cpu_count(logical=True) or 4)),
                n_gpu_layers=0,
                verbose=False,
            )
        _llms[model_id] = {"path": str(path), "llm": llm}
        return llm


def _messages_for(mode, text, source_lang, target_lang, vocab_block=""):
    if mode == "smart_auto":
        return smart_prompt.build_messages(text, vocab_block=vocab_block)
    if mode == "write_email":
        instruction = (
            "Turn the user's dictated text into a concise email draft. "
            "Keep the user's intent, do not invent facts, and output only the email."
        )
    elif mode == "make_todo_list":
        instruction = (
            "Extract a clear Markdown todo checklist from the user's dictated text. "
            "Output only checklist items using '- [ ]'."
        )
    elif mode == "translate":
        instruction = (
            f"Translate the user's text from {source_lang or 'auto'} to {target_lang}. "
            "Preserve meaning and output only the translation."
        )
    elif mode == "summarize":
        instruction = (
            "Summarize the user's text in 3-5 sentences. "
            "Capture the main points only. Output the summary directly - no preamble."
        )
    elif mode == "meeting_notes":
        # Tighter version of the cloud prompt - smaller local models follow
        # short, direct instructions better than long bullet-point checklists.
        instruction = (
            "Summarise this meeting transcript into Markdown with EXACTLY these sections:\n"
            "## Summary  (2-4 sentences, third person, no 'I will')\n"
            "## Key decisions  (bullets; skip if none)\n"
            "## Action items  (- [ ] task (Owner: name) - derive owner from "
            "'I'll'/'name should'/etc.)\n"
            "## Open questions  (bullets; skip if none)\n\n"
            "Preserve names exactly (incl. Armenian/Russian). Don't invent facts. "
            "If `[speaker change]` markers appear, use them to attribute who said what."
        )
    else:
        instruction = "Rewrite the user's text clearly while preserving meaning. Output only the result."
    system = "You are a private local desktop assistant. Never add commentary."
    if vocab_block:
        system = f"{system}\n\n{vocab_block}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{instruction}\n\nUser text:\n{text}"},
    ]


def _extract_text(result):
    try:
        message = result["choices"][0].get("message", {})
        text = message.get("content", "")
        if not text:
            text = result["choices"][0].get("text", "")
        return (text or "").strip()
    except Exception as e:
        raise LocalLLMError("The local model returned an unreadable response.") from e
