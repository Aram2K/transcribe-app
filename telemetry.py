import json
import platform
import queue
import threading
import time
import uuid

import requests

import storage


QUEUE_PATH = storage.path_for("telemetry_queue.json")
INSTALL_ID_PATH = storage.path_for("install_id.txt")
MAX_QUEUE = 200
SESSION_ID = uuid.uuid4().hex

SENSITIVE_KEYS = {
    "text",
    "transcript",
    "transcription",
    "audio",
    "clipboard",
    "api_key",
    "google_api_key",
    "action_api_key",
    "authorization",
    "x_api_key",
    "path",
    "file",
    "filename",
    "device",
    "device_name",
    "microphone",
}

ALLOWED_EVENTS = {
    "app_started",
    "settings_opened",
    "settings_saved",
    "settings_tab_opened",
    "backend_selected",
    "privacy_mode_enabled",
    "privacy_mode_disabled",
    "history_opened",
    "history_exported",
    "history_cleared",
    "model_download_started",
    "model_download_completed",
    "model_download_failed",
    "model_removed",
    "transcription_completed",
    "action_completed",
    "action_failed",
    "update_check_started",
    "update_check_result",
    "update_install_started",
    "update_install_result",
    # Meeting feature (previously dropped — these names weren't allow-listed, so
    # we had zero data on meeting usage).
    "meeting_recording_started",
    "meeting_recording_failed",
    "meeting_notes_completed",
    # Monetization / accounts funnel.
    "paywall_viewed",
    "upgrade_clicked",
    "checkout_opened",
    "trial_started",
    "guest_trial_exhausted",
    "login_succeeded",
    "login_failed",
    "signed_out",
    "pro_activated",
}

_flush_lock = threading.Lock()
_flush_running = False


def enabled(config):
    # Analytics are independent of Privacy Mode. The sanitizer in
    # `_sanitize` already strips audio/transcripts/keys/paths, so events
    # carry only metadata about feature usage.
    return (
        bool(config.get("analytics_enabled"))
        and bool(config.get("analytics_endpoint"))
    )


def track(event, props=None, config=None, app_version=""):
    if config is None or not enabled(config):
        return
    if event not in ALLOWED_EVENTS:
        return
    item = {
        "schema": 1,
        "event": event,
        "timestamp": int(time.time()),
        "install_id": _install_id(),
        "session_id": SESSION_ID,
        "app_version": app_version,
        "os": _os_name(),
        "props": _sanitize(props or {}),
    }
    _append(item)
    flush_async(config)


def flush_async(config):
    global _flush_running
    if not enabled(config):
        return
    with _flush_lock:
        if _flush_running:
            return
        _flush_running = True
    threading.Thread(target=_flush, args=(dict(config),), daemon=True).start()


def _flush(config):
    global _flush_running
    try:
        endpoint = config.get("analytics_endpoint")
        if not endpoint:
            return
        events = _load_queue()
        if not events:
            return
        resp = requests.post(endpoint, json={"events": events}, timeout=4)
        if 200 <= getattr(resp, "status_code", 0) < 300:
            storage.atomic_write_json(QUEUE_PATH, [])
    except Exception:
        pass
    finally:
        with _flush_lock:
            _flush_running = False


def _install_id():
    try:
        if INSTALL_ID_PATH.exists():
            value = INSTALL_ID_PATH.read_text(encoding="ascii").strip()
            if value:
                return value
        value = uuid.uuid4().hex
        INSTALL_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        INSTALL_ID_PATH.write_text(value, encoding="ascii")
        return value
    except Exception:
        return "unknown"


def _os_name():
    return f"{platform.system()} {platform.release()}".strip()


def _sanitize(props):
    clean = {}
    for key, value in props.items():
        key = str(key)
        if key.lower() in SENSITIVE_KEYS:
            continue
        if isinstance(value, bool) or value is None:
            clean[key] = value
        elif isinstance(value, (int, float)):
            clean[key] = value
        else:
            text = str(value)
            clean[key] = text[:80]
    return clean


def _load_queue():
    data = storage.read_json(QUEUE_PATH, [])
    return data if isinstance(data, list) else []


def _append(item):
    events = _load_queue()
    events.append(item)
    storage.atomic_write_json(QUEUE_PATH, events[-MAX_QUEUE:], ensure_ascii=False)
