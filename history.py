import csv
from datetime import datetime
from pathlib import Path

import storage


HISTORY_PATH = str(storage.path_for("history.json"))
LEGACY_HISTORY_PATH = "history.json"
MAX_ENTRIES = 200

storage.migrate_legacy_file(LEGACY_HISTORY_PATH, HISTORY_PATH)


def load():
    entries = storage.read_json(HISTORY_PATH, [])
    if isinstance(entries, list):
        return entries
    return []


def save_all(entries):
    storage.atomic_write_json(HISTORY_PATH, entries[:MAX_ENTRIES], ensure_ascii=False)


def search(query):
    q = (query or "").strip().lower()
    entries = load()
    if not q:
        return entries
    return [
        e for e in entries
        if q in e.get("text", "").lower()
        or q in e.get("language", "").lower()
        or q in e.get("backend", "").lower()
        or q in e.get("timestamp", "").lower()
    ]


def delete(index):
    entries = load()
    if 0 <= index < len(entries):
        del entries[index]
        save_all(entries)
        return True
    return False


def _export_entries(entries=None):
    if entries is None:
        return load()
    return list(entries)


def export_csv(path, entries=None):
    entries = _export_entries(entries)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "language", "backend", "text"])
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "timestamp": entry.get("timestamp", ""),
                "language": entry.get("language", ""),
                "backend": entry.get("backend", ""),
                "text": entry.get("text", ""),
            })
    return len(entries)


def export_txt(path, entries=None):
    entries = _export_entries(entries)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(f"[{entry.get('timestamp', '')}] {entry.get('language', '')} / {entry.get('backend', '')}\n")
            f.write(f"{entry.get('text', '')}\n\n")
    return len(entries)


def save_entry(text, language, backend):
    if not text.strip():
        return
    entries = load()
    entries.insert(0, {
        "text":      text,
        "language":  language,
        "backend":   backend,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_all(entries)


def clear():
    save_all([])
