from __future__ import annotations

"""Range-based multiprocessing stage for the internal drawing renderer."""

import multiprocessing as mp
import os
import queue
import signal
import time
from dataclasses import dataclass

from parallel_workers import choose_worker_count


@dataclass
class RangeStats:
    frames: int = 0
    frame_rate: float = 0.0
    workers: int = 1


def _worker_loop(worker_id, task_queue, result_queue, width, height, settings):
    # On Windows, Ctrl+C can be delivered to every process attached to the
    # console. Workers must NOT handle SIGINT themselves; the parent owns the
    # stop event and explicitly terminates workers during shutdown.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass

    from sketch_renderer import make_processor
    processor = make_processor(settings)
    while True:
        try:
            task = task_queue.get()
        except (EOFError, OSError):
            return
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
            try:
                result_queue.put((worker_id, start, start + len(frames) - 1, None, repr(exc)))
            except Exception:
                pass


class RangeParallelProcessor:
    """Persistent processes with bounded queues and explicit Ctrl+C-safe teardown."""
    def __init__(self, width, height, settings, worker_count=None, ram_available=None):
        frame_bytes = max(1, width * height * 3)
        self.worker_count = worker_count or choose_worker_count(os.cpu_count(), ram_available, frame_bytes)
        self.worker_count = max(1, self.worker_count)
        self.width, self.height, self.settings = width, height, settings
        self.ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context("fork")
        self.task_queues = []
        self.result_queue = self.ctx.Queue(maxsize=self.worker_count * 2)
        self.processes = []
        self._stopped = False

    def start(self):
        self._stopped = False
        for worker_id in range(1, self.worker_count + 1):
            q = self.ctx.Queue(maxsize=1)
            p = self.ctx.Process(
                target=_worker_loop,
                args=(worker_id, q, self.result_queue, self.width, self.height, self.settings),
                name=f"PySketchify-Draw-{worker_id}",
                daemon=True,
            )
            p.start()
            self.task_queues.append(q)
            self.processes.append(p)

    def process_stream(self, frame_iter, total_frames, block_size, progress_callback=None, stop_event=None):
        """Process ranges while remaining responsive to stop requests and Ctrl+C."""
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
            try:
                self.task_queues[worker_slot].put((start, frames), timeout=0.2)
            except (queue.Full, EOFError, OSError):
                return False
            active[worker_slot] = (start, len(frames))
            return True

        for slot in range(self.worker_count):
            if not submit(slot, next_index):
                break
            next_index += active[slot][1]

        while active:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                worker_id, start, end, output, error = self.result_queue.get(timeout=0.2)
            except queue.Empty:
                if any(not p.is_alive() and p.exitcode not in (None, 0) for p in self.processes):
                    raise RuntimeError("描画Workerが異常終了しました。")
                continue
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
                    if stop_event is not None and stop_event.is_set():
                        break
                    yield frame
                    completed += 1
                    if progress_callback:
                        elapsed = max(0.001, time.perf_counter() - started)
                        progress_callback(RangeStats(completed, completed / elapsed, self.worker_count))
                if stop_event is not None and stop_event.is_set():
                    break
                next_emit += len(ordered)
            if not (stop_event is not None and stop_event.is_set()):
                if submit(slot, next_index):
                    next_index += active[slot][1]

    def stop(self):
        """Explicitly tear down queues/processes so multiprocessing atexit cannot hang."""
        if self._stopped:
            return
        self._stopped = True

        # Do not let multiprocessing's atexit handler wait for queue feeder threads.
        for q in self.task_queues + [self.result_queue]:
            try:
                q.cancel_join_thread()
            except Exception:
                pass

        for q in self.task_queues:
            try:
                q.put_nowait(None)
            except Exception:
                pass

        for p in self.processes:
            try:
                p.join(timeout=0.5)
            except (KeyboardInterrupt, OSError):
                pass

        for p in self.processes:
            if p.is_alive():
                try:
                    p.terminate()
                except Exception:
                    pass

        for p in self.processes:
            try:
                p.join(timeout=0.5)
            except (KeyboardInterrupt, OSError):
                pass

        for q in self.task_queues + [self.result_queue]:
            try:
                q.close()
            except Exception:
                pass

        self.task_queues.clear()
        self.processes.clear()
