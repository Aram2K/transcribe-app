"""Save and export meeting recordings.

The recorder keeps the meeting as 16 kHz mono float32; each recording
segment (a fresh meeting, or a resumed one) is written as audio_partN.wav so
nothing is ever overwritten. Export concatenates the parts in order and writes
WAV (stdlib) or MP3 (PyAV's libmp3lame, already shipped with faster-whisper).
"""
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

RATE = 16000


def float_to_int16(audio):
    a = np.asarray(audio, dtype=np.float32)
    if a.size == 0:
        return np.zeros(0, dtype=np.int16)
    return (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)


def write_wav(path, audio_f32, rate=RATE):
    """16-bit mono PCM WAV from float32 samples. Returns the path."""
    data = float_to_int16(audio_f32)
    path = Path(path)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def read_wav_int16(path):
    """(int16 samples, rate). Stereo files are downmixed."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"Unsupported sample width {width}")
    data = np.frombuffer(frames, dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return data, rate


def concat_wavs(paths):
    """Join recording parts in order -> (int16 samples, rate)."""
    chunks, rate = [], RATE
    for p in paths:
        data, rate = read_wav_int16(p)
        chunks.append(data)
    if not chunks:
        return np.zeros(0, dtype=np.int16), rate
    return np.concatenate(chunks), rate


def duration_seconds(paths):
    total = 0.0
    for p in paths:
        try:
            with wave.open(str(p), "rb") as w:
                total += w.getnframes() / float(w.getframerate() or RATE)
        except Exception:
            pass
    return total


def write_mp3(path, samples_int16, rate=RATE, bitrate=96_000):
    """MP3 via PyAV/libmp3lame. Returns True on success, False if the encoder
    is unavailable - the caller then offers WAV instead."""
    try:
        import av
    except Exception:
        return False
    try:
        samples = np.asarray(samples_int16, dtype=np.int16)
        with av.open(str(path), "w") as container:
            stream = container.add_stream("libmp3lame", rate=rate)
            stream.bit_rate = bitrate
            step = rate            # one-second frames; the encoder re-frames internally
            pts = 0
            for i in range(0, len(samples), step):
                chunk = samples[i:i + step]
                frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = rate
                frame.pts = pts
                frame.time_base = Fraction(1, rate)
                pts += len(chunk)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        return True
    except Exception:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
        return False


def export_recording(parts, out_path):
    """Write all ``parts`` as one file; the extension picks the format
    (.mp3 or .wav). Returns the written path, or None when nothing to export."""
    parts = [p for p in parts if Path(p).is_file()]
    if not parts:
        return None
    data, rate = concat_wavs(parts)
    out = Path(out_path)
    if out.suffix.lower() == ".mp3":
        if write_mp3(out, data, rate):
            return out
        out = out.with_suffix(".wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return out
