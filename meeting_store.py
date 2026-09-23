"""On-disk meeting library: %APPDATA%/Transcribe/meetings/<YYYYMMDD_HHMMSS>/.

Each folder holds meta.json (title, attendees, duration_sec, timestamp),
transcript.txt, notes.md, chunks.jsonl (live chunks) and audio_partN.wav
(one per recording segment - a resumed meeting adds a part). This module is
the single reader used by the History tab, the detail view and Resume.
"""
import json
import re
from pathlib import Path

import storage

_PART_RE = re.compile(r"^audio_part(\d+)\.wav$", re.I)


def meetings_dir():
    return Path(storage.path_for("meetings"))


def load_meta(folder):
    try:
        with open(Path(folder) / "meta.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read(folder, name):
    try:
        with open(Path(folder) / name, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def load_notes(folder):
    return _read(folder, "notes.md")


def load_transcript(folder):
    return _read(folder, "transcript.txt")


def audio_parts(folder):
    """Recording segments in order (audio_part1.wav, audio_part2.wav, ...)."""
    folder = Path(folder)
    parts = []
    try:
        for p in folder.iterdir():
            m = _PART_RE.match(p.name)
            if m and p.is_file():
                parts.append((int(m.group(1)), p))
    except Exception:
        return []
    return [p for _, p in sorted(parts)]


def next_audio_part_path(folder):
    parts = audio_parts(folder)
    n = 1
    if parts:
        n = int(_PART_RE.match(parts[-1].name).group(1)) + 1
    return Path(folder) / f"audio_part{n}.wav"


def summary_preview(notes, limit=140):
    """First real line of the notes (no headings/markers) for the list card."""
    for line in (notes or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        s = s.lstrip("-*• ").strip()
        if s:
            return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"
    return ""


def list_meetings():
    """Newest first. Only folders that actually hold a meeting (meta or
    transcript) are listed; an empty aborted folder is skipped."""
    root = meetings_dir()
    out = []
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except Exception:
        return out
    for folder in sorted(folders, key=lambda p: p.name, reverse=True):
        meta = load_meta(folder)
        has_transcript = (folder / "transcript.txt").is_file()
        has_notes = (folder / "notes.md").is_file()
        if not (meta or has_transcript or has_notes):
            continue
        out.append({
            "dir": str(folder),
            "id": folder.name,
            "title": (meta.get("title") or "Untitled Meeting").strip(),
            "attendees": meta.get("attendees") or "",
            "timestamp": meta.get("timestamp") or _folder_timestamp(folder.name),
            "duration_sec": int(meta.get("duration_sec") or 0),
            "has_transcript": has_transcript,
            "has_notes": has_notes,
            "audio_parts": [str(p) for p in audio_parts(folder)],
        })
    return out


def _folder_timestamp(name):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$", name)
    if not m:
        return name
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def format_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"
