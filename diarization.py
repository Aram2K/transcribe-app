"""Local, offline speaker diarization via sherpa-onnx (Apache-2.0 code).

Turns an audio waveform into speaker turns ``[(start_s, end_s, speaker_id), ...]``
using a pyannote segmentation model (MIT-licensed weights) + a speaker-embedding
model, clustered locally. Runs on CPU by design so it never competes with Whisper
for the GPU's limited VRAM.

Everything is local/offline and degrades gracefully: if sherpa-onnx isn't
installed or the models aren't downloaded yet, ``is_available()`` is False and
callers fall back to an unlabeled transcript. Nothing here ever raises into the
recording pipeline - failures return empty results and are logged.

NOTE (licensing, before shipping): the segmentation weights are MIT, but the
embedding weights (3D-Speaker ERes2Net, redistributed by sherpa-onnx) carry a
per-model ModelScope license that must be confirmed for commercial use, or
swapped for an explicitly commercial-cleared embedding (e.g. a WeSpeaker/NeMo
model from the same sherpa-onnx release). Tracked as a launch to-do.
"""
import logging
import os
import tarfile
import threading
import urllib.request

import numpy as np

import storage

logger = logging.getLogger("transcribe")

SAMPLE_RATE = 16000

# Token-free, commercially-licensed assets from sherpa-onnx GitHub releases.
# Segmentation = pyannote/segmentation-3.0 (MIT). Embedding = 3D-Speaker ERes2Net.
_SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-segmentation-models/"
            "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
_EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/"
            "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")

_SEG_REL = os.path.join("sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
_EMB_REL = "embedding.onnx"

_lock = threading.Lock()       # serialize model build + inference
_diarizer = None               # cached OfflineSpeakerDiarization
_diarizer_key = None           # (num_speakers, threshold) the cache was built for


def _models_dir():
    return storage.path_for("models/diarization")


def seg_model_path():
    return _models_dir() / _SEG_REL


def emb_model_path():
    return _models_dir() / _EMB_REL


def models_downloaded():
    return seg_model_path().exists() and emb_model_path().exists()


def sherpa_installed():
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:
        return False


def is_available():
    """True when diarization can actually run right now."""
    return sherpa_installed() and models_downloaded()


def download_models(on_progress=None):
    """Fetch the segmentation + embedding models into the app data dir.

    Safe to call when already present (no-op). Returns True on success. Never
    raises - logs and returns False so the caller can fall back gracefully.
    """
    try:
        d = _models_dir()
        d.mkdir(parents=True, exist_ok=True)

        if not emb_model_path().exists():
            if on_progress:
                on_progress("Downloading speaker model (40 MB)...")
            tmp = d / "embedding.onnx.part"
            urllib.request.urlretrieve(_EMB_URL, tmp)
            tmp.replace(emb_model_path())

        if not seg_model_path().exists():
            if on_progress:
                on_progress("Downloading segmentation model (7 MB)...")
            arch = d / "seg.tar.bz2"
            urllib.request.urlretrieve(_SEG_URL, arch)
            with tarfile.open(arch) as tf:
                tf.extractall(d)
            try:
                arch.unlink()
            except OSError:
                pass

        return models_downloaded()
    except Exception as e:
        logger.warning("Diarization model download failed: %s", e)
        return False


def _seg_model_for_speed():
    """Prefer the int8-quantized segmentation model (smaller + faster on CPU)
    when present, else the full one."""
    int8 = _models_dir() / "sherpa-onnx-pyannote-segmentation-3-0" / "model.int8.onnx"
    return str(int8) if int8.exists() else str(seg_model_path())


def _build_diarizer(num_speakers, threshold):
    import sherpa_onnx
    # Recording is finished by the time diarization runs, so use plenty of CPU
    # threads to keep the post-meeting wait short.
    nthreads = max(2, min(8, (os.cpu_count() or 4)))
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=_seg_model_for_speed()),
            num_threads=nthreads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(emb_model_path()), num_threads=nthreads),
        clustering=sherpa_onnx.FastClusteringConfig(
            # num_clusters>0 pins a known speaker count; -1 lets the threshold
            # decide. A higher threshold merges more aggressively (fewer
            # speakers) - 0.7 is a saner meeting default than the 0.5 the
            # examples ship (which over-splits).
            num_clusters=int(num_speakers) if num_speakers and num_speakers > 0 else -1,
            threshold=float(threshold)),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    return sherpa_onnx.OfflineSpeakerDiarization(config)


def diarize(audio, num_speakers=None, threshold=0.7, on_progress=None):
    """Diarize 16 kHz mono float32 ``audio`` into speaker turns.

    Returns a list of ``(start_s, end_s, speaker_id)`` sorted by start time, or
    ``[]`` if diarization is unavailable, the audio is too short, or anything
    fails. Never raises. ``on_progress(done, total)`` receives sherpa's
    chunk-level progress when the installed version supports a callback.
    """
    if not is_available():
        return []
    try:
        audio = np.asarray(audio, dtype=np.float32)
    except Exception:
        return []
    if audio.ndim > 1:
        audio = audio.reshape(audio.shape[0], -1).mean(axis=1)
    if len(audio) < SAMPLE_RATE:  # < 1s: nothing meaningful to cluster
        return []

    global _diarizer, _diarizer_key
    key = (int(num_speakers) if num_speakers else 0, round(float(threshold), 3))
    try:
        callback = None
        if on_progress is not None:
            def callback(*args):
                # sherpa passes (num_processed_chunks, num_total_chunks[, arg]).
                try:
                    if len(args) >= 2:
                        on_progress(int(args[0]), max(1, int(args[1])))
                except Exception:
                    pass
                return 0
        with _lock:
            if _diarizer is None or _diarizer_key != key:
                _diarizer = _build_diarizer(num_speakers, threshold)
                _diarizer_key = key
            if callback is not None:
                try:
                    result = _diarizer.process(audio, callback=callback).sort_by_start_time()
                except TypeError:
                    # Older sherpa without the callback kwarg.
                    result = _diarizer.process(audio).sort_by_start_time()
            else:
                result = _diarizer.process(audio).sort_by_start_time()
        return [(float(r.start), float(r.end), int(r.speaker)) for r in result]
    except Exception as e:
        logger.warning("Diarization failed: %s", e)
        return []


def attribute_segments(segments, turns, offset=0.0):
    """Label transcript ``segments`` with speakers from diarization ``turns``.

    ``segments``: iterable of dicts with ``start``/``end`` (seconds) and ``text``.
    ``turns``: ``[(start, end, speaker_id), ...]`` from :func:`diarize`.
    ``offset``: seconds to add to each segment's time before matching, when the
    segment clock and the diarized-audio clock have a known shift.

    Each segment is assigned the speaker with the maximum *time overlap* (not the
    midpoint - a segment can straddle a turn boundary). Returns a new list of
    segments with an added ``speaker`` key (int, or None when no turn overlaps).
    """
    out = []
    for seg in segments:
        s = float(seg.get("start", 0.0)) + offset
        e = float(seg.get("end", s)) + offset
        best_spk, best_overlap = None, 0.0
        nearest_spk, nearest_gap = None, float("inf")
        for (ts, te, spk) in turns:
            overlap = min(e, te) - max(s, ts)
            if overlap > best_overlap:
                best_overlap, best_spk = overlap, spk
            gap = max(ts - e, s - te, 0.0)  # 0 if overlapping, else time distance
            if gap < nearest_gap:
                nearest_gap, nearest_spk = gap, spk
        labeled = dict(seg)
        # Prefer the max-overlap speaker; if a segment falls in a gap between
        # turns, snap it to the nearest turn rather than leaving it unlabeled.
        # Only None when there are no turns at all.
        labeled["speaker"] = best_spk if best_spk is not None else nearest_spk
        out.append(labeled)
    return out


def build_speaker_transcript(labeled_segments, names=None):
    """Render labeled segments as a readable, speaker-grouped transcript:

        Speaker 1: ...
        Speaker 2: ...

    Consecutive segments from the same speaker are merged into one block.
    Speaker ids are renumbered to stable 1-based labels in first-appearance
    order; ``names`` may override a label for a given speaker id (e.g.
    ``{0: "You"}``); an unmatched (None) speaker renders as "Speaker ?".
    """
    names = names or {}
    label_map = {}
    next_label = [1]

    def name_for(spk):
        if spk in names:
            return names[spk]
        if spk is None:
            return "Speaker ?"
        if spk not in label_map:
            label_map[spk] = next_label[0]
            next_label[0] += 1
        return f"Speaker {label_map[spk]}"

    lines = []
    cur, parts = object(), []  # sentinel cur so the first segment always starts a block
    for seg in labeled_segments:
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        spk = seg.get("speaker")
        if spk != cur and parts:
            lines.append(f"{name_for(cur)}: {' '.join(parts)}")
            parts = []
        cur = spk
        parts.append(txt)
    if parts:
        lines.append(f"{name_for(cur)}: {' '.join(parts)}")
    return "\n".join(lines)
