import gc
import logging
import shutil
import threading
import time
import weakref
from contextlib import contextmanager
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

logger = logging.getLogger("transcribe")

_llms = {}
_llm_lock = threading.Lock()
# Generations holding each model right now (_lease) and when it was last used:
# unload_idle frees a model only at 0 leases - and only ever by dropping the
# cache's reference, never Llama.close(), which would free the native context
# under a generation still running on another thread (an uncatchable crash).
_in_use = {}
_last_used = {}
_removing = set()           # being deleted from disk: no new load may start
# A model that just failed to load: refused for a while instead of rebuilt
# (GBs each time) by every 20 s live recap. forget_load_failures() = Retry.
_load_failures = {}
_LOAD_RETRY_S = 600


def forget_load_failures():
    with _llm_lock:
        _load_failures.clear()
# One inference lock per model: llama-cpp's Llama shares a single native
# context, so two concurrent create_chat_completion calls corrupt state or
# crash the whole process (an access violation Python cannot catch). Dictation
# smart actions and meeting summaries run on different worker threads and
# default to the same model - same reasoning as the Whisper _infer_lock in
# main.py, which fixed the equivalent CUDA hang.
_infer_locks = {}


class LocalLLMError(RuntimeError):
    pass


class LocalModelLoadError(LocalLLMError):
    """The model file is there but won't load (memory, a damaged file): the
    user must hear about it - unlike a missing optional runtime, it never
    silently degrades to the built-in formatter."""


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
    # Check, mark and drop in ONE lock hold: no lease can slip in between.
    with _llm_lock:
        if _in_use.get(model_id):
            raise LocalLLMError("The model is busy right now - try again when it finishes.")
        _removing.add(model_id)
        _llms.pop(model_id, None)
        _load_failures.pop(model_id, None)
    try:
        return _remove_files(model_id)
    finally:
        with _llm_lock:
            _removing.discard(model_id)


def _remove_files(model_id):
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


def unload_idle(max_idle_s, keep=None, now=None):
    """Free cached models unused for ``max_idle_s`` seconds (0 = every model
    not in use), except ``keep``. For the idle sweep, on a worker thread:
    never waits (a load holds the lock for seconds - then it just skips),
    never touches a model with a generation running. Returns the freed ids.

    Measured (Qwen2.5-3B, n_ctx 8192): dropping the reference frees ~2.3 GB of
    working set and ~5.5 GB of commit; a reload takes ~1-2 s."""
    if max_idle_s is None or max_idle_s < 0:
        return []
    if not _llm_lock.acquire(blocking=False):
        return []
    try:
        t = time.monotonic() if now is None else now
        keep = normalize_model_id(keep) if keep else None
        names = [m for m in _llms
                 if m != keep and not _in_use.get(m)
                 and t - _last_used.get(m, 0.0) >= max_idle_s]
        freed = [_llms.pop(m) for m in names]
    finally:
        _llm_lock.release()
    if not freed:
        return []
    refs = []
    for entry in freed:
        try:
            refs.append(weakref.ref(entry["llm"]))
        except TypeError:
            refs.append(None)
    del freed, entry
    gc.collect()
    alive = [n for n, r in zip(names, refs) if r is not None and r() is not None]
    if alive:
        logger.warning("Local model(s) %s still referenced after unload", alive)
    logger.info("Freed idle local AI model(s): %s", ", ".join(names))
    return names


@contextmanager
def _lease(model_id):
    """The model for one generation, loaded if needed and counted as in use
    until the block ends (streamed tokens included), so unload_idle never
    frees it underneath. Load and count happen in one lock hold."""
    model_id = normalize_model_id(model_id)
    llm = _load_model(model_id, lease=True)
    try:
        yield llm
    finally:
        with _llm_lock:
            _in_use[model_id] = max(0, _in_use.get(model_id, 0) - 1)
            _last_used[model_id] = time.monotonic()


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
    "live_assist": 300,
    "live_recap": 220,
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
    # Serialize ALL inference on this model (tokenize included) - see
    # _infer_locks. The map-reduce path below can hold a model busy for
    # minutes, which is exactly when a dictation smart action would otherwise
    # land on the same Llama from another thread. The lease keeps the idle
    # sweep off it for the whole run.
    with _lease(model_id) as llm, _infer_lock_for(model_id):
        return _run_action_locked(llm, text, mode, source_lang, target_lang,
                                  vocab_block)


def run_action_stream(text, mode, on_token, source_lang="auto", target_lang="en",
                      model_id=QWEN_TINY_ID, vocab_block=""):
    """Like run_action but streams: ``on_token(delta)`` fires for every piece
    of text as the model produces it; returns the complete text. Inputs that
    would need the map-reduce path fall back to a single-shot run (one
    callback with the whole result)."""
    text = (text or "").strip()
    if not text:
        return ""
    with _lease(model_id) as llm, _infer_lock_for(model_id):
        messages = _messages_for(mode, text, source_lang, target_lang, vocab_block)
        max_out = _MAX_TOKENS_BY_MODE.get(mode, 240)
        if mode == "translate" or _messages_tokens(llm, messages) > _N_CTX - max_out - 128:
            out = _run_action_locked(llm, text, mode, source_lang, target_lang, vocab_block)
            if out:
                on_token(out)
            return out
        parts = []
        for chunk in llm.create_chat_completion(
                messages=messages, temperature=0.1, top_p=0.9, max_tokens=max_out,
                repeat_penalty=1.08, stream=True):
            try:
                delta = chunk["choices"][0].get("delta", {}).get("content") or ""
            except Exception:
                delta = ""
            if delta:
                parts.append(delta)
                on_token(delta)
        return "".join(parts).strip()


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


def _load_model(model_id, lease=False):
    model_id = normalize_model_id(model_id)
    path = model_path(model_id)
    if not model_downloaded(model_id):
        raise LocalLLMError(f"{model_info(model_id)['label']} is not downloaded yet.")
    with _llm_lock:
        if model_id in _removing:
            raise LocalLLMError(f"{model_info(model_id)['label']} is being removed.")
        failed = _load_failures.get(model_id)
        if failed and time.monotonic() - failed[0] < _LOAD_RETRY_S:
            raise LocalModelLoadError(failed[1])
        cached = _llms.get(model_id)
        if cached and cached.get("path") == str(path):
            if lease:
                _in_use[model_id] = _in_use.get(model_id, 0) + 1
            _last_used[model_id] = time.monotonic()
            return cached["llm"]
        # One local model resident at a time: an idle one (no generation
        # running) goes before another is built - each holds GBs.
        for other in [m for m in _llms if m != model_id and not _in_use.get(m)]:
            _llms.pop(other, None)
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
        except Exception as first:
            if gpu_layers == 0:
                msg = f"Couldn't load {model_info(model_id)['label']}: {first}"
                _load_failures[model_id] = (time.monotonic(), msg)
                raise LocalModelLoadError(msg) from first
            try:
                llm = Llama(
                    model_path=str(path),
                    n_ctx=_N_CTX,
                    n_threads=max(2, min(8, psutil.cpu_count(logical=True) or 4)),
                    n_gpu_layers=0,
                    verbose=False,
                )
            except Exception as e:
                msg = f"Couldn't load {model_info(model_id)['label']}: {e}"
                _load_failures[model_id] = (time.monotonic(), msg)
                raise LocalModelLoadError(msg) from e
        _llms[model_id] = {"path": str(path), "llm": llm}
        _load_failures.pop(model_id, None)
        if lease:
            _in_use[model_id] = _in_use.get(model_id, 0) + 1
        _last_used[model_id] = time.monotonic()
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
    elif mode == "live_recap":
        instruction = (
            "Keep a running summary of an ongoing meeting from the transcript so far "
            "(speech recognition, may be imperfect). Output Markdown bullets only, at "
            "most 6, under 90 words: what has been discussed, decisions, open "
            "questions - most recent last. No headings, no preamble, never invent."
        )
    elif mode == "live_assist":
        # Tighter than the cloud prompt - small local models follow short,
        # direct instructions better. Same contract: answer, never summarise.
        instruction = (
            "You are the user's real-time companion during a live call. The text is "
            "the latest part of the conversation (speech recognition, may be "
            "imperfect), optionally followed by the user's question. It may start "
            "with \"About this session\": the user's own notes on the call - true "
            "for the whole call; use its facts and follow its focus.\n"
            "Answer the user's question if there is one; otherwise answer the LAST "
            "question or request in the text. Start with the answer itself - the "
            "words to say or the solution - then at most 2 short bullets. Simple "
            "words, under 45 words. Put code or commands in a fenced code block.\n"
            "No summary, no recap, no headings. Do not invent facts, and never make "
            "claims about the user's own background or experience beyond those notes."
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
