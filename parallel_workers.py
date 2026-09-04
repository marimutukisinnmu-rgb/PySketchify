from __future__ import annotations

"""Parallel frame-range workers.

Each worker owns a contiguous frame range (for example 1-1000), which keeps
frame-local drawing state reusable inside that worker. Results are returned in
original frame order by the coordinator.
"""

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class FrameRange:
    worker_id: int
    start: int
    end: int


def choose_worker_count(cpu_count: Optional[int] = None, ram_available: Optional[int] = None,
                        frame_bytes: int = 1) -> int:
    """Choose a conservative worker count from current CPU/RAM resources."""
    import os
    cpus = max(1, cpu_count or (os.cpu_count() or 1))
    workers = max(1, cpus - 1)
    if ram_available is not None and frame_bytes > 0:
        # Reserve substantial RAM for FFmpeg, the GUI and two queue stages.
        ram_workers = max(1, int(ram_available // max(frame_bytes * 6, 64 * 1024**2)))
        workers = min(workers, ram_workers)
    return max(1, min(workers, 8))


def make_ranges(total_frames: int, worker_count: int, block_size: Optional[int] = None) -> list[FrameRange]:
    if total_frames <= 0:
        return []
    worker_count = max(1, min(worker_count, total_frames))
    if block_size is None:
        block_size = (total_frames + worker_count - 1) // worker_count
    block_size = max(1, block_size)
    ranges: list[FrameRange] = []
    start = 1
    worker_id = 1
    while start <= total_frames:
        end = min(total_frames, start + block_size - 1)
        ranges.append(FrameRange(worker_id, start, end))
        start = end + 1
        worker_id += 1
    return ranges


def ordered_results(futures, ranges: Iterable[FrameRange]):
    """Yield completed worker results in frame-range order, not completion order."""
    results = {}
    for future in as_completed(futures):
        result = future.result()
        results[result[0]] = result[1]
    for frame_range in sorted(ranges, key=lambda r: r.start):
        yield frame_range, results[frame_range.worker_id]


def run_parallel_ranges(ranges: list[FrameRange], worker_fn: Callable,
                        max_workers: int) -> list:
    """Run contiguous ranges in separate processes and return ordered results."""
    if not ranges:
        return []
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(worker_fn, r.worker_id, r.start, r.end): r
            for r in ranges
        }
        results = []
        completed = {}
        for future in as_completed(future_map):
            r = future_map[future]
            completed[r.worker_id] = future.result()
        for r in sorted(ranges, key=lambda x: x.start):
            results.append((r, completed[r.worker_id]))
        return results
