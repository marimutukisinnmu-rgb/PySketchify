from __future__ import annotations

"""Range-based multiprocessing stage for the internal drawing renderer."""

import multiprocessing as mp
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from parallel_workers import choose_worker_count


@dataclass
class RangeStats:
    frames: int = 0
    frame_rate: float = 0.0
    workers: int = 1


def _worker_loop(worker_id, task_queue, result_queue, width, height, settings):
    from sketch_renderer import make_processor
    processor = make_processor(settings)
    while True:
        task = task_queue.get()
        if task is None:
            return
        start, frames = task
        try:
            output = []
            for offset, frame in enumerate(frames):
                index = start + offset
                output.append((index, processor(frame, index, width, height)))
            result_queue.put((worker_id, start, start + len(frames) - 1, output, None))
        except BaseException as exc:
            result_queue.put((worker_id, start, start + len(frames) - 1, None, repr(exc)))


class RangeParallelProcessor:
    """Persistent processes; each submitted job is a contiguous frame range."""
    def __init__(self, width, height, settings, worker_count=None, ram_available=None):
        frame_bytes = max(1, width * height * 3)
        self.worker_count = worker_count or choose_worker_count(os.cpu_count(), ram_available, frame_bytes)
        self.worker_count = max(1, self.worker_count)
        self.width, self.height, self.settings = width, height, settings
        self.ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context("fork")
        self.task_queues = []
        self.result_queue = self.ctx.Queue(maxsize=self.worker_count * 2)
        self.processes = []

    def start(self):
        for worker_id in range(1, self.worker_count + 1):
            q = self.ctx.Queue(maxsize=1)
            p = self.ctx.Process(target=_worker_loop,
                                 args=(worker_id, q, self.result_queue, self.width, self.height, self.settings),
                                 name=f"PySketchify-Draw-{worker_id}")
            p.start()
            self.task_queues.append(q)
            self.processes.append(p)

    def process_stream(self, frame_iter, total_frames, block_size, progress_callback=None, stop_event=None):
        """Process contiguous ranges while keeping only one range per worker in RAM."""
        iterator = iter(frame_iter)
        active = {}
        next_index = 0
        completed = 0
        buffers = {}
        next_emit = 0
        started = time.perf_counter()

        def submit(worker_slot, start):
            frames = []
            for _ in range(block_size):
                if stop_event is not None and stop_event.is_set():
                    return False
                try:
                    frames.append(next(iterator))
                except StopIteration:
                    break
            if not frames:
                return False
            self.task_queues[worker_slot].put((start, frames))
            active[worker_slot] = (start, len(frames))
            return True

        for slot in range(self.worker_count):
            if not submit(slot, next_index):
                break
            next_index += active[slot][1]

        while active:
            if stop_event is not None and stop_event.is_set():
                break
            worker_id, start, end, output, error = self.result_queue.get()
            slot = None
            for candidate, job in active.items():
                if job[0] == start:
                    slot = candidate
                    break
            if slot is None:
                raise RuntimeError(f"不明なWorker結果: {worker_id} {start}-{end}")
            del active[slot]
            if error:
                raise RuntimeError(f"Worker {worker_id} ({start + 1}-{end + 1}) failed: {error}")
            buffers[start] = output
            while next_emit in buffers:
                ordered = buffers.pop(next_emit)
                for _, frame in ordered:
                    yield frame
                    completed += 1
                    if progress_callback:
                        elapsed = max(0.001, time.perf_counter() - started)
                        progress_callback(RangeStats(completed, completed / elapsed, self.worker_count))
                next_emit += len(ordered)
            if not (stop_event is not None and stop_event.is_set()):
                if submit(slot, next_index):
                    next_index += active[slot][1]

    def stop(self):
        for q in self.task_queues:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        for p in self.processes:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
                p.join(timeout=1)
        self.task_queues.clear()
        self.processes.clear()


def run_parallel_video(input_path, output_path, width, height, fps, frame_count, settings,
                       worker_count=None, ram_available=None, block_size=None,
                       progress_callback=None, stop_event=None):
    """Decode once, draw in contiguous multiprocessing ranges, encode in order."""
    frame_size = width * height * 3
    if block_size is None:
        # Aim for <= ~256 MiB per worker's input range, capped at 1000 frames.
        budget = 256 * 1024**2
        block_size = max(1, min(1000, budget // max(frame_size, 1)))
    processor = RangeParallelProcessor(width, height, settings, worker_count, ram_available)
    decoder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_path),
         "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    encoder = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", f"{fps:.12g}", "-i", "pipe:0", "-an", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-threads", "1", "-movflags", "+faststart", "-y", str(output_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    processor.start()
    started = time.perf_counter()

    def frames():
        while True:
            data = decoder.stdout.read(frame_size) if decoder.stdout else b""
            if not data:
                return
            if len(data) != frame_size:
                raise RuntimeError(f"不完全なフレーム: {len(data)}/{frame_size}")
            yield data

    try:
        assert encoder.stdin is not None
        for frame in processor.process_stream(frames(), frame_count, block_size, progress_callback, stop_event):
            if stop_event is not None and stop_event.is_set():
                break
            encoder.stdin.write(frame)
        if stop_event is None or not stop_event.is_set():
            encoder.stdin.close()
            if decoder.wait(timeout=30) != 0:
                raise RuntimeError((decoder.stderr.read() if decoder.stderr else b"").decode(errors="replace"))
            if encoder.wait(timeout=60) != 0:
                raise RuntimeError((encoder.stderr.read() if encoder.stderr else b"").decode(errors="replace"))
            if frame_count and not progress_callback:
                pass
            if not output_path.exists() or output_path.stat().st_size <= 0:
                raise RuntimeError("出力MP4が生成されませんでした。")
    finally:
        processor.stop()
        if stop_event is not None and stop_event.is_set():
            try: encoder.stdin.close()
            except Exception: pass
        for p in (decoder, encoder):
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=2)
                except subprocess.TimeoutExpired: p.kill()
    return RangeStats(frames=frame_count, frame_rate=frame_count / max(0.001, time.perf_counter() - started), workers=processor.worker_count)
