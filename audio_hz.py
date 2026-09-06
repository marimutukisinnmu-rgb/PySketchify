from __future__ import annotations

"""FFT-based repeated frequency split/recombine audio transform with configurable delay."""

import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_CHUNK = 65536
DEFAULT_BANDS = 256
DEFAULT_REPEATS = 3
DEFAULT_DELAY_MS = 1.93


def _split_recombine(signal: np.ndarray, repeats: int, delay_ms: float) -> np.ndarray:
    """Split into frequency bands, phase-shift each band, recombine, and repeat."""
    out = signal.astype(np.float32, copy=True)
    bands = max(2, int(DEFAULT_BANDS))
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

            frequencies = np.fft.rfftfreq(chunk.size, d=1.0 / DEFAULT_SAMPLE_RATE)
            edges = np.linspace(0, len(spectrum), bands + 1, dtype=np.int32)
            shifted = np.zeros_like(spectrum)

            for band in range(bands):
                lo, hi = int(edges[band]), int(edges[band + 1])
                if hi <= lo:
                    continue
                shifted[lo:hi] = spectrum[lo:hi] * np.exp(
                    -2j * np.pi * frequencies[lo:hi] * delay_seconds
                )

            result[start:start + len(chunk)] = np.fft.irfft(
                shifted, n=chunk.size
            ).real.astype(np.float32)
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
    """Create a temporary WAV while processing the source audio incrementally."""
    proc = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path), "-vn",
            "-ac", str(DEFAULT_CHANNELS),
            "-ar", str(DEFAULT_SAMPLE_RATE),
            "-f", "f32le", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    fd, path = tempfile.mkstemp(prefix="pysketchify_hz_", suffix=".wav")
    os.close(fd)
    output_path = Path(path)
    processed_frames = 0
    last_report = time.perf_counter()

    try:
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(DEFAULT_CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(DEFAULT_SAMPLE_RATE)

            raw_chunk_bytes = DEFAULT_CHUNK * DEFAULT_CHANNELS * 4
            assert proc.stdout is not None
            while True:
                raw = proc.stdout.read(raw_chunk_bytes)
                if not raw:
                    break
                usable = len(raw) - (len(raw) % (DEFAULT_CHANNELS * 4))
                if usable <= 0:
                    continue

                samples = np.frombuffer(raw[:usable], dtype=np.float32)
                samples = samples.reshape(-1, DEFAULT_CHANNELS)
                channels = []
                for ch in range(DEFAULT_CHANNELS):
                    channels.append(_split_recombine(samples[:, ch], repeats, delay_ms))
                processed = np.stack(channels, axis=1)

                pcm = np.clip(processed, -1.0, 1.0)
                wav.writeframes((pcm * 32767.0).astype(np.int16).tobytes())
                processed_frames += samples.shape[0]

                now = time.perf_counter()
                if now - last_report >= 1.0:
                    print(
                        f"[HZ] 音声合成中... {processed_frames / DEFAULT_SAMPLE_RATE:.1f} sec",
                        flush=True,
                    )
                    last_report = now

        returncode = proc.wait(timeout=60)
        if returncode != 0:
            stderr = proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(
                "音声デコードに失敗しました: " + stderr.decode(errors="replace")
            )
        if processed_frames <= 0:
            output_path.unlink(missing_ok=True)
            return None
        print(
            f"[HZ] 音声合成完了: {processed_frames / DEFAULT_SAMPLE_RATE:.1f} sec",
            flush=True,
        )
        return output_path
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        output_path.unlink(missing_ok=True)
        raise
