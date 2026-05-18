import os
import shutil
import threading
from pathlib import Path

import psutil
import requests

import storage


QWEN_TINY_ID = "qwen_tiny"
QWEN_TINY_REPO = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
QWEN_TINY_FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
QWEN_TINY_URL = f"https://huggingface.co/{QWEN_TINY_REPO}/resolve/main/{QWEN_TINY_FILENAME}"
QWEN_TINY_SIZE = 1_120_000_000

_llm = None
_llm_path = None
_llm_lock = threading.Lock()


class LocalLLMError(RuntimeError):
    pass


def model_dir(model_id=QWEN_TINY_ID):
    return storage.path_for("action_models") / model_id


def model_path(model_id=QWEN_TINY_ID):
    return model_dir(model_id) / QWEN_TINY_FILENAME


def model_downloaded(model_id=QWEN_TINY_ID):
    path = model_path(model_id)
    return path.exists() and path.stat().st_size > 100 * 1024 * 1024


def remove_model(model_id=QWEN_TINY_ID):
    unload_model(model_id)
    directory = model_dir(model_id)
    if directory.exists():
        shutil.rmtree(directory)
        return True
    return False


def unload_model(model_id=None):
    global _llm, _llm_path
    with _llm_lock:
        if model_id is None or _llm_path == str(model_path(model_id)):
            _llm = None
            _llm_path = None


def download_model(model_id=QWEN_TINY_ID, on_progress=None):
    if model_id != QWEN_TINY_ID:
        raise LocalLLMError("Unknown local action model.")

    directory = model_dir(model_id)
    directory.mkdir(parents=True, exist_ok=True)
    dest = model_path(model_id)
    part = Path(f"{dest}.part")
    got = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={got}-"} if got else {}

    with requests.get(QWEN_TINY_URL, stream=True, timeout=60, headers=headers, allow_redirects=True) as resp:
        if resp.status_code == 416:
            part.replace(dest)
            if on_progress:
                on_progress(100, dest.stat().st_size, dest.stat().st_size)
            return dest
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        expected = got + total if total else QWEN_TINY_SIZE
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


def run_action(text, mode, source_lang="auto", target_lang="en"):
    text = (text or "").strip()
    if not text:
        return ""
    llm = _load_qwen()
    messages = _messages_for(mode, text, source_lang, target_lang)
    result = llm.create_chat_completion(
        messages=messages,
        temperature=0.1,
        top_p=0.9,
        max_tokens=320 if mode == "write_email" else 220,
        repeat_penalty=1.08,
    )
    return _extract_text(result)


def _load_qwen():
    global _llm, _llm_path
    path = model_path(QWEN_TINY_ID)
    if not model_downloaded(QWEN_TINY_ID):
        raise LocalLLMError("Qwen Tiny is not downloaded yet.")
    with _llm_lock:
        if _llm is not None and _llm_path == str(path):
            return _llm
        try:
            from llama_cpp import Llama
        except Exception as e:
            raise LocalLLMError("llama-cpp-python is required for Qwen Tiny actions.") from e
        _llm = Llama(
            model_path=str(path),
            n_ctx=2048,
            n_threads=max(2, min(8, psutil.cpu_count(logical=True) or 4)),
            n_gpu_layers=0,
            verbose=False,
        )
        _llm_path = str(path)
        return _llm


def _messages_for(mode, text, source_lang, target_lang):
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
    else:
        instruction = "Rewrite the user's text clearly while preserving meaning. Output only the result."
    return [
        {"role": "system", "content": "You are a private local desktop assistant. Never add commentary."},
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
