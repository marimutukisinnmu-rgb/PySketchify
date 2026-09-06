from __future__ import annotations

"""FFT-based repeated frequency split/recombine audio transform with configurable delay."""

import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_CHUNK = 65536
DEFAULT_BANDS = 256
DEFAULT_REPEATS = 3
DEFAULT_DELAY_MS = 1.93


def _decode_audio(input_path: Path) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path), "-vn",
            "-ac", str(DEFAULT_CHANNELS),
            "-ar", str(DEFAULT_SAMPLE_RATE),
            "-f", "f32le", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("音声デコードに失敗しました: " + proc.stderr.decode(errors="replace"))
    return proc.stdout


def _split_recombine(
    signal: np.ndarray,
    repeats: int,
    delay_ms: float,
) -> np.ndarray:
    """Split into frequency bands and apply a sin(FFT-sum)*delay-ms shift repeatedly."""
    out = signal.astype(np.float32, copy=True)
    bands = max(2, DEFAULT_BANDS)
    delay_scale_seconds = float(delay_ms) / 1000.0

    for _ in range(max(1, int(repeats))):
        result = np.empty_like(out)
        for start in range(0, len(out), DEFAULT_CHUNK):
            chunk = out[start:start + DEFAULT_CHUNK]
            if chunk.size == 0:
                continue

            spectrum = np.fft.rfft(chunk)
            fft_sum = float(np.sum(np.abs(spectrum), dtype=np.float64))
            delay_seconds = float(np.sin(fft_sum) * delay_scale_seconds)

            rebuilt = np.zeros(chunk.size, dtype=np.float64)
            edges = np.linspace(0, len(spectrum), bands + 1, dtype=np.int32)
            frequencies = np.fft.rfftfreq(chunk.size, d=1.0 / DEFAULT_SAMPLE_RATE)

            for band in range(bands):
                lo, hi = int(edges[band]), int(edges[band + 1])
                if hi <= lo:
                    continue
                isolated = np.zeros_like(spectrum)
                isolated[lo:hi] = spectrum[lo:hi]
                phase = np.exp(-2j * np.pi * frequencies[lo:hi] * delay_seconds)
                isolated[lo:hi] *= phase
                rebuilt += np.fft.irfft(isolated, n=chunk.size).real

            result[start:start + len(chunk)] = rebuilt.astype(np.float32)
        out = result

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.999:
        out *= np.float32(0.999 / peak)
    return out


def make_hz_audio(
    input_path: Path,
    repeats: int = DEFAULT_REPEATS,
    delay_ms: float = DEFAULT_DELAY_MS,
) -> Path | None:
    raw = _decode_audio(input_path)
    if not raw:
        return None

    samples = np.frombuffer(raw, dtype=np.float32)
    usable = samples.size - (samples.size % DEFAULT_CHANNELS)
    if usable <= 0:
        return None

    samples = samples[:usable].reshape(-1, DEFAULT_CHANNELS)
    processed = np.stack(
        [_split_recombine(samples[:, ch], repeats, delay_ms) for ch in range(DEFAULT_CHANNELS)],
        axis=1,
    )

    fd, path = tempfile.mkstemp(prefix="pysketchify_hz_", suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(DEFAULT_CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(DEFAULT_SAMPLE_RATE)
        pcm = np.clip(processed, -1.0, 1.0)
        wav.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
    return Path(path)
