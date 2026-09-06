from __future__ import annotations

"""FFT-based repeated frequency split/recombine audio transform."""

import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_CHUNK = 65536
DEFAULT_REPEATS = 3


def _decode_audio(input_path: Path) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_path), "-vn", "-ac", str(DEFAULT_CHANNELS), "-ar", str(DEFAULT_SAMPLE_RATE), "-f", "f32le", "pipe:1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("音声デコードに失敗しました: " + proc.stderr.decode(errors="replace"))
    return proc.stdout


def _split_recombine(signal: np.ndarray, repeats: int) -> np.ndarray:
    out = signal.astype(np.float32, copy=True)
    for _ in range(max(1, int(repeats))):
        result = np.empty_like(out)
        for start in range(0, len(out), DEFAULT_CHUNK):
            chunk = out[start:start + DEFAULT_CHUNK]
            if chunk.size == 0:
                continue
            spectrum = np.fft.rfft(chunk)
            rebuilt = np.zeros(len(chunk), dtype=np.float64)
            for bin_index, value in enumerate(spectrum):
                isolated = np.zeros_like(spectrum)
                isolated[bin_index] = value
                rebuilt += np.fft.irfft(isolated, n=len(chunk))
            result[start:start + len(chunk)] = rebuilt.astype(np.float32)
        out = result
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.999:
        out *= np.float32(0.999 / peak)
    return out


def make_hz_audio(input_path: Path, repeats: int = DEFAULT_REPEATS) -> Path | None:
    raw = _decode_audio(input_path)
    if not raw:
        return None
    samples = np.frombuffer(raw, dtype=np.float32)
    usable = samples.size - (samples.size % DEFAULT_CHANNELS)
    if usable <= 0:
        return None
    samples = samples[:usable].reshape(-1, DEFAULT_CHANNELS)
    processed = np.stack([_split_recombine(samples[:, ch], repeats) for ch in range(DEFAULT_CHANNELS)], axis=1)
    fd, path = tempfile.mkstemp(prefix="pysketchify_hz_", suffix=".wav")
    import os
    os.close(fd)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(DEFAULT_CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(DEFAULT_SAMPLE_RATE)
        pcm = np.clip(processed, -1.0, 1.0)
        wav.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
    return Path(path)
