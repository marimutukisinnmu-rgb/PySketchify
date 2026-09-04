from __future__ import annotations

"""Bounded FFmpeg streaming pipeline used for low-resolution videos.

FFmpeg decoder -> bounded RAM queue -> processor -> bounded RAM queue -> encoder.
The processor hook is ready for the hand-drawing engine. The default processor
is lossless pass-through so the infrastructure can be tested before the effect
engine is connected.
"""

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Callable, Optional


MAX_STREAMING_HEIGHT = 1080
DEFAULT_QUEUE_FRAMES = 4


@dataclass
class StreamingStats:
    frames: int = 0
    started: float = 0.0
    finished: float = 0.0

    @property
    def elapsed(self) -> float:
        end = self.finished or time.perf_counter()
        return max(0.000001, end - self.started)

    @property
    def frame_rate(self) -> float:
        return self.frames / self.elapsed


FrameProcessor = Callable[[bytes, int, int, int], bytes]


def choose_queue_frames(width: int, height: int, ram_available: Optional[int]) -> int:
    """Choose a small bounded queue from current RAM."""
    frame_bytes = max(1, width * height * 3)
    if ram_available is None:
        return DEFAULT_QUEUE_FRAMES
    budget = max(frame_bytes, ram_available // 32)
    count = max(1, min(8, budget // frame_bytes))
    return int(count)


def passthrough_processor(frame: bytes, index: int, width: int, height: int) -> bytes:
    return frame


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def run_streaming_pipeline(
    input_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    frame_count: int = 0,
    processor: FrameProcessor = passthrough_processor,
    queue_frames: Optional[int] = None,
    threads: int = 1,
    progress_callback: Optional[Callable[[StreamingStats], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> StreamingStats:
    """Stream frames with bounded memory usage.

    No full video or full frame list is held in RAM. Queue capacity is bounded,
    so a fast decoder cannot outrun a slower processing stage indefinitely.
    """
    if width <= 0 or height <= 0:
        raise ValueError("ストリーミングには正しい解像度が必要です。")
    if fps <= 0:
        raise ValueError("ストリーミングには正しいFPSが必要です。")
    if height > MAX_STREAMING_HEIGHT:
        raise ValueError("1080pを超える動画はチャンク/タイル処理を使用してください。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_size = width * height * 3
    qsize = max(1, queue_frames or DEFAULT_QUEUE_FRAMES)
    raw_queue: Queue[Optional[tuple[int, bytes]]] = Queue(maxsize=qsize)
    encoded_queue: Queue[Optional[tuple[int, bytes]]] = Queue(maxsize=qsize)
    stop = stop_event or threading.Event()
    stats = StreamingStats(started=time.perf_counter())
    errors: list[BaseException] = []

    decoder = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-threads", str(max(1, threads)), "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    encoder = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}",
            "-r", f"{fps:.12g}",
            "-i", "pipe:0",
            "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-threads", str(max(1, threads)), "-y", str(output_path),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    def decoder_worker() -> None:
        try:
            assert decoder.stdout is not None
            index = 0
            while not stop.is_set():
                frame = decoder.stdout.read(frame_size)
                if not frame:
                    break
                if len(frame) != frame_size:
                    raise RuntimeError("FFmpegから不完全なフレームを受信しました。")
                raw_queue.put((index, frame))
                index += 1
            raw_queue.put(None)
        except BaseException as exc:
            errors.append(exc)
            stop.set()
            try:
                raw_queue.put_nowait(None)
            except Exception:
                pass

    def processor_worker() -> None:
        try:
            while not stop.is_set():
                item = raw_queue.get()
                if item is None:
                    encoded_queue.put(None)
                    return
                index, frame = item
                processed = processor(frame, index, width, height)
                if len(processed) != frame_size:
                    raise RuntimeError("フレーム処理結果のサイズが元フレームと一致しません。")
                encoded_queue.put((index, processed))
        except BaseException as exc:
            errors.append(exc)
            stop.set()
            try:
                encoded_queue.put_nowait(None)
            except Exception:
                pass

    def encoder_worker() -> None:
        try:
            assert encoder.stdin is not None
            expected = 0
            while not stop.is_set():
                item = encoded_queue.get()
                if item is None:
                    # EOF is required for FFmpeg to finish the output container.
                    encoder.stdin.close()
                    return
                index, frame = item
                if index != expected:
                    raise RuntimeError(f"フレーム順序が壊れました: expected={expected}, got={index}")
                encoder.stdin.write(frame)
                expected += 1
                stats.frames = expected
                if progress_callback:
                    progress_callback(stats)
            try:
                encoder.stdin.close()
            except Exception:
                pass
        except BaseException as exc:
            errors.append(exc)
            stop.set()

    workers = [
        threading.Thread(target=decoder_worker, name="PySketchify-Decode", daemon=True),
        threading.Thread(target=processor_worker, name="PySketchify-Process", daemon=True),
        threading.Thread(target=encoder_worker, name="PySketchify-Encode", daemon=True),
    ]
    for worker in workers:
        worker.start()

    try:
        for worker in workers:
            worker.join()
    finally:
        stop.set()
        _terminate_process(decoder)
        _terminate_process(encoder)
        decoder_err = decoder.stderr.read().decode("utf-8", errors="replace") if decoder.stderr else ""
        encoder_err = encoder.stderr.read().decode("utf-8", errors="replace") if encoder.stderr else ""
        stats.finished = time.perf_counter()

    if errors:
        raise RuntimeError(str(errors[0]))
    if decoder.returncode not in (0, None):
        raise RuntimeError(f"FFmpeg decode failed:\n{decoder_err.strip()}")
    if encoder.returncode not in (0, None):
        raise RuntimeError(f"FFmpeg encode failed:\n{encoder_err.strip()}")
    if stop_event is not None and stop_event.is_set():
        return stats
    if frame_count and stats.frames != frame_count:
        raise RuntimeError(f"処理フレーム数が一致しません: {stats.frames}/{frame_count}")
    return stats
