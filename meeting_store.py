"""On-disk meeting library: %APPDATA%/Transcribe/meetings/<YYYYMMDD_HHMMSS>/.

Each folder holds meta.json (title, attendees, duration_sec, timestamp),
transcript.txt, notes.md, chunks.jsonl (live chunks) and audio_partN.wav
(one per recording segment - a resumed meeting adds a part). This module is
the single reader used by the History tab, the detail view and Resume.
"""
import json
import re
import shutil
import wave
from pathlib import Path

import storage

_PART_RE = re.compile(r"^audio_part(\d+)\.wav$", re.I)
# A session the user aborted & discarded: its live chunks, kept on disk under
# this name (as before) but out of the History list.
DISCARDED_CHUNKS = "chunks.discarded.jsonl"


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
    """transcript.txt - or, for a meeting that never got that far (the app
    closed mid-meeting), what its live chunks captured, so it still shows
    and resumes with what was said."""
    text = _read(folder, "transcript.txt")
    return text if text.strip() else chunks_text(folder)


def chunks_text(folder):
    """The text of chunks.jsonl in spoken order. Chunk indices restart with
    every recording segment, so they're only sorted when unique (one
    segment); otherwise the file order is kept."""
    rows = []
    try:
        with open(Path(folder) / "chunks.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue            # a line cut short by a crash
                if isinstance(row, dict) and isinstance(row.get("text"), str) \
                        and row["text"].strip():
                    rows.append(row)
    except Exception:
        return ""
    idx = [r.get("index") for r in rows]
    if all(isinstance(i, int) for i in idx) and len(set(idx)) == len(idx):
        rows.sort(key=lambda r: r["index"])
    return " ".join(r["text"].strip() for r in rows)


def mark_discarded(folder):
    """Hide an aborted session's live chunks from History (see list_meetings)
    without deleting them. A chunk thread finishing after the rename can
    recreate chunks.jsonl - the marker still hides it."""
    folder = Path(folder)
    try:
        src = folder / "chunks.jsonl"
        if src.is_file():
            src.replace(folder / DISCARDED_CHUNKS)
        elif folder.is_dir():
            (folder / DISCARDED_CHUNKS).touch()
    except OSError:
        pass


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
    """Newest first. Every folder that holds any of a meeting is listed -
    including one with only its recording or live chunks (transcription or
    notes failed, or the app closed mid-meeting), so a saved recording is
    always reachable. Empty and discarded folders are skipped."""
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
        parts = audio_parts(folder)
        has_chunks = ((folder / "chunks.jsonl").is_file()
                      and not (folder / DISCARDED_CHUNKS).exists())
        # Live chunks alone don't make a meeting: that is the one being
        # recorded right now, or a session discarded before 1.9.1 (no marker
        # then) - both must stay out of History. With meta.json (written when
        # processing starts) they are a meeting whose transcription failed.
        # meta.json alone (nothing was captured) is no meeting either.
        if not (has_transcript or has_notes or parts or (meta and has_chunks)):
            continue
        out.append({
            "dir": str(folder),
            "id": folder.name,
            "title": str(meta.get("title") or "Untitled Meeting").strip(),
            "attendees": str(meta.get("attendees") or ""),
            "timestamp": str(meta.get("timestamp") or _folder_timestamp(folder.name)),
            # No meta.json (it's written when processing starts): the
            # recording's own length beats a "0 s" card.
            "duration_sec": _int(meta.get("duration_sec")) or _wav_seconds(parts),
            "has_transcript": has_transcript,
            "has_notes": has_notes,
            "audio_parts": [str(p) for p in parts],
        })
    return out


def _int(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _wav_seconds(parts):
    total = 0.0
    for p in parts:
        try:
            with wave.open(str(p), "rb") as w:
                total += w.getnframes() / float(w.getframerate() or 1)
        except Exception:
            continue                # unreadable/partial WAV: skip it
    return int(total)


def delete_meeting(folder):
    """Permanently delete one meeting folder - notes, transcript, recording.
    Refuses anything that isn't a folder inside the meetings library.
    Returns True when it's gone."""
    try:
        root = meetings_dir().resolve()
        target = Path(folder).resolve()
    except Exception:
        return False
    if target == root or root not in target.parents or not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
    except OSError:
        return False
    return not target.exists()


def delete_all_meetings(keep=None):
    """Delete every meeting folder (listed or aborted leftovers) except
    ``keep`` - a meeting still being recorded or processed. Returns how many
    folders were removed."""
    try:
        folders = [p for p in meetings_dir().iterdir() if p.is_dir()]
        keep_path = Path(keep).resolve() if keep else None
    except Exception:
        return 0
    removed = 0
    for folder in folders:
        if keep_path is not None and folder.resolve() == keep_path:
            continue
        if delete_meeting(folder):
            removed += 1
    return removed


def _folder_timestamp(name):
    # "_2", "_3"...: a second meeting started within the same second.
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:_\d+)?$", name)
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
