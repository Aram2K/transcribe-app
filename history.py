import json
import os
from datetime import datetime

HISTORY_PATH = "history.json"
MAX_ENTRIES  = 200

def load():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

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
    entries = entries[:MAX_ENTRIES]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

def clear():
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump([], f)
