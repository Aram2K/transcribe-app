import hashlib
import json
import platform
import re
import threading
import time
import uuid

import requests

import storage


QUEUE_PATH = storage.path_for("telemetry_queue.json")
INSTALL_ID_PATH = storage.path_for("install_id.txt")
MAX_QUEUE = 500            # events kept on disk while offline (oldest dropped first)
BATCH_SIZE = 50            # events per upload request
FLUSH_THRESHOLD = 20       # upload right away once this many are waiting...
FLUSH_INTERVAL = 60        # ...otherwise at most this many seconds later
MAX_BACKOFF = 15 * 60      # retry delay cap while the server is unreachable
MAX_BATCHES_PER_FLUSH = 10
HTTP_TIMEOUT = 10
SCHEMA_VERSION = 2         # 2 = events carry an event_id (server de-duplicates)
SESSION_ID = uuid.uuid4().hex

# Keep in sync with sensitiveKeys in supabase/functions/transcribe-analytics
# (tests/test_telemetry.py checks the server strips every key listed here).
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
    "token",
    "path",
    "file",
    "filename",
    "device",
    "device_name",
    "microphone",
    "window_title",
    "title",
    "question",
    "url",
    "email",
    "name",
    "full_name",
}

# Keep in sync with allowedEvents in supabase/functions/transcribe-analytics -
# the server silently drops any event it doesn't list (tests/test_telemetry.py
# fails when the two drift apart).
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
    # Meeting feature (previously dropped - these names weren't allow-listed, so
    # we had zero data on meeting usage).
    "meeting_recording_started",
    "meeting_recording_failed",
    "meeting_notes_completed",
    "meeting_notes_failed",
    "meeting_exported",
    # Monetization / accounts funnel.
    "paywall_viewed",
    "upgrade_clicked",
    "checkout_opened",
    "trial_started",
    "guest_trial_exhausted",
    "login_succeeded",
    "login_failed",
    "signup_verification_sent",
    "signed_out",
    "pro_activated",
    "onboarding_completed",
    "feedback_sent",
    # Live Assistance.
    "live_prompter_opened",
    "live_prompter_started",
    "live_prompter_suggestion",
    # Transcribe Files.
    "file_transcription_started",
    "file_transcription_completed",
    "file_transcription_failed",
    "file_transcription_saved",
}

_queue_lock = threading.Lock()   # guards every read-modify-write of the queue file
_sender_lock = threading.Lock()
_sender = None
_wake = threading.Event()
_config = {}
_drained_once = False
_context_provider = None


def enabled(config):
    # Analytics are independent of Privacy Mode. The sanitizer in
    # `_sanitize` already strips audio/transcripts/keys/paths, so events
    # carry only metadata about feature usage.
    return (
        bool(config.get("analytics_enabled"))
        and bool(config.get("analytics_endpoint"))
    )


def set_context_provider(fn):
    """Register ``fn() -> dict`` whose props are added to every event. The app
    uses it for the account tier, so usage can be split by guest/free/pro
    without identifying anyone."""
    global _context_provider
    _context_provider = fn


def track(event, props=None, config=None, app_version=""):
    # Telemetry must never be able to break a feature, so nothing here raises.
    try:
        if config is None or not enabled(config):
            return
        if event not in ALLOWED_EVENTS:
            return
        merged = {}
        if _context_provider is not None:
            try:
                merged.update(_context_provider() or {})
            except Exception:
                pass
        merged.update(props or {})
        item = {
            "schema": SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "event": event,
            "timestamp": int(time.time()),
            "install_id": _install_id(),
            "session_id": SESSION_ID,
            "app_version": app_version,
            "os": _os_name(),
            "props": _sanitize(merged),
        }
        pending = _append(item)
        if pending >= FLUSH_THRESHOLD or not _drained_once:
            # The first event of a run also delivers whatever the previous
            # run left queued.
            flush_async(config)
        else:
            _ensure_sender(config)  # uploads within FLUSH_INTERVAL
    except Exception:
        pass


def flush_async(config):
    """Upload queued events now, on the background sender thread."""
    global _drained_once
    if not enabled(config):
        return
    _drained_once = True
    _ensure_sender(config)
    _wake.set()


def _ensure_sender(config):
    global _sender, _config
    # Keep the caller's live config dict (not a copy) so switching analytics
    # off in Settings stops uploads straight away.
    _config = config
    with _sender_lock:
        if _sender is None or not _sender.is_alive():
            _sender = threading.Thread(target=_sender_loop, name="telemetry-sender", daemon=True)
            _sender.start()


def _sender_loop():
    # Batches uploads: one request per FLUSH_INTERVAL instead of one per event,
    # backing off while the server is unreachable.
    delay = FLUSH_INTERVAL
    while True:
        _wake.wait(timeout=delay)
        _wake.clear()
        try:
            ok = _flush(_config)
        except Exception:
            ok = False
        if ok:
            delay = FLUSH_INTERVAL
            continue
        # Serve the whole backoff: with a full queue every new event wakes the
        # sender, and retrying on each one hammered a failing backend.
        delay = min(delay * 2, MAX_BACKOFF)
        deadline = time.monotonic() + delay
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            _wake.wait(timeout=left)
            _wake.clear()
        _wake.set()  # backoff served: retry at once, keeping the longer delay


def _flush(config):
    """Upload the queue in batches; returns False if the server couldn't take
    them. A batch leaves the queue only after the server accepted it, and
    events recorded while an upload is in flight are never discarded."""
    if not enabled(config):
        return True
    endpoint = config.get("analytics_endpoint")
    try:
        for _ in range(MAX_BATCHES_PER_FLUSH):
            with _queue_lock:
                batch = _load_queue()[:BATCH_SIZE]
            if not batch:
                return True
            resp = requests.post(endpoint, json={"events": batch}, timeout=HTTP_TIMEOUT)
            if not 200 <= getattr(resp, "status_code", 0) < 300:
                return False
            sent = {e["event_id"] for e in batch}
            with _queue_lock:
                remaining = [e for e in _load_queue() if e["event_id"] not in sent]
                storage.atomic_write_json(QUEUE_PATH, remaining, ensure_ascii=False)
        return True
    except Exception:
        return False


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


# Any value that looks like it quotes an email address is dropped whatever its
# key - server error text and the like can echo the address back.
_EMAIL_RE = re.compile(r"[^\s@\"'<>]+@[^\s@\"'<>]+\.[a-z]{2,}", re.I)


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
            if _EMAIL_RE.search(text):
                continue
            clean[key] = text[:80]
    return clean


def _load_queue():
    data = storage.read_json(QUEUE_PATH, [])
    if not isinstance(data, list):
        return []
    events = []
    for e in data:
        if not isinstance(e, dict):
            continue
        if not e.get("event_id"):
            # Queued by an app version from before event ids: derive a stable
            # id from the content so delivery bookkeeping still works.
            raw = json.dumps(e, sort_keys=True, ensure_ascii=False)
            e["event_id"] = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        events.append(e)
    return events


def _append(item):
    """Queue one event on disk; returns how many are now waiting."""
    with _queue_lock:
        events = _load_queue()
        events.append(item)
        events = events[-MAX_QUEUE:]
        storage.atomic_write_json(QUEUE_PATH, events, ensure_ascii=False)
        return len(events)
