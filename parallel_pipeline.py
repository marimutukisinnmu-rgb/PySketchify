from __future__ import annotations

"""Range-based multiprocessing stage for the internal drawing renderer."""

import multiprocessing as mp
import os
import time
from dataclasses import dataclass
from typing import Optional

from parallel_workers import FrameRange, choose_worker_count, make_ranges


@dataclass
class RangeStats:
    frames: int = 0
    frame_rate: float = 0.0
    workers: int = 1


def _worker_loop(worker_id, task_queue, result_queue, width, height, settings):
    # Imports happen inside the child so Pillow/NumPy state is created per worker.
    from sketch_renderer import make_processor
    processor = make_processor(settings)
    while True:
        task = task_queue.get()
        if task is None:
            return
        start, end, frames = task
        try:
            output = []
            for offset, frame in enumerate(frames):
                index = start + offset
                output.append((index, processor(frame, index, width, height)))
            result_queue.put((worker_id, start, end, output, None))
        except BaseException as exc:
            result_queue.put((worker_id, start, end, None, repr(exc)))


class RangeParallelProcessor:
    """Persistent workers; each worker receives a contiguous frame range."""

    def __init__(self, width: int, height: int, settings, worker_count: Optional[int] = None,
                 ram_available: Optional[int] = None):
        frame_bytes = max(1, width * height * 3)
        self.worker_count = worker_count or choose_worker_count(os.cpu_count(), ram_available, frame_bytes)
        self.worker_count = max(1, self.worker_count)
        self.width = width
        self.height = height
        self.settings = settings
        self.ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context("fork")
        self.task_queues = []
        self.result_queue = self.ctx.Queue(maxsize=self.worker_count * 2)
        self.processes = []

    def start(self):
        for worker_id in range(1, self.worker_count + 1):
            q = self.ctx.Queue(maxsize=2)
            p = self.ctx.Process(target=_worker_loop,
                                 args=(worker_id, q, self.result_queue,
                                       self.width, self.height, self.settings),
                                 name=f"PySketchify-Draw-{worker_id}")
            p.start()
            self.task_queues.append(q)
            self.processes.append(p)

    def process_ranges(self, frame_iter, total_frames: int, block_size: Optional[int] = None,
                       progress_callback=None, stop_event=None):
        ranges = make_ranges(total_frames, self.worker_count, block_size)
        if not ranges:
            return RangeStats(workers=self.worker_count)
        iterator = iter(frame_iter)
        pending = {}
        next_range = 0
        next_output = 0
        completed = 0
        started = time.perf_counter()
        buffers = {}

        def submit(r):
            frames = []
            for _ in range(r.end - r.start + 1):
                if stop_event is not None and stop_event.is_set():
                    return False
                try:
                    frames.append(next(iterator))
                except StopIteration:
                    return False
            self.task_queues[(r.worker_id - 1) % self.worker_count].put((r.start, r.end, frames))
            pending[r.worker_id] = r
            return True

        # Keep at most one range per worker in memory. This is intentionally
        # bounded: range size should be chosen from available RAM by caller.
        while next_range < len(ranges) and len(pending) < self.worker_count:
            if not submit(ranges[next_range]):
                break
            next_range += 1

        while pending:
            if stop_event is not None and stop_event.is_set():
                break
            worker_id, start, end, output, error = self.result_queue.get()
            r = pending.pop(worker_id)
            if error:
                raise RuntimeError(f"Worker {worker_id} ({start}-{end}) failed: {error}")
            buffers[r.start] = output
            while next_output < len(ranges) and ranges[next_output].start in buffers:
                ordered = buffers.pop(ranges[next_output].start)
                for index, frame in ordered:
                    yield index, frame
                    completed += 1
                    if progress_callback:
                        elapsed = max(0.001, time.perf_counter() - started)
                        progress_callback(RangeStats(completed, completed / elapsed, self.worker_count))
                next_output += 1
            if next_range < len(ranges):
                if not submit(ranges[next_range]):
                    next_range = len(ranges)
                else:
                    next_range += 1

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
