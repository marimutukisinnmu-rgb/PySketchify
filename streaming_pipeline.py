from __future__ import annotations

"""Bounded FFmpeg streaming pipeline with low-priority concurrent Hz-audio generation."""

import multiprocessing as mp
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

DEFAULT_QUEUE_FRAMES = 4
QUEUE_TIMEOUT = 0.10
PROCESS_WAIT_TIMEOUT = 60.0
FORCE_KILL_TIMEOUT = 2.0

@dataclass
class StreamingStats:
    frames: int = 0
    started: float = 0.0
    finished: float = 0.0
    workers: int = 1
    @property
    def elapsed(self): return max(0.000001, (self.finished or time.perf_counter()) - self.started)
    @property
    def frame_rate(self): return self.frames / self.elapsed

FrameProcessor = Callable[[bytes, int, int, int], bytes]

def choose_queue_frames(width, height, ram_available):
    frame_bytes = max(1, width * height * 3)
    if ram_available is None: return DEFAULT_QUEUE_FRAMES if frame_bytes < 12_000_000 else 1
    budget = max(frame_bytes * 2, ram_available // 32)
    return int(max(1, min(8, budget // (frame_bytes * 2))))

def passthrough_processor(frame, index, width, height): return frame

def _read_exact(stream, size):
    parts = []; remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk: break
        parts.append(chunk); remaining -= len(chunk)
    return b"".join(parts)

def _terminate(process):
    if process is not None and process.poll() is None:
        try:
            process.terminate(); process.wait(timeout=FORCE_KILL_TIMEOUT)
        except Exception:
            try: process.kill(); process.wait(timeout=FORCE_KILL_TIMEOUT)
            except Exception: pass

def _make_video_encoder(output_path, width, height, fps, audio_path=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{fps:.12g}", "-i", "pipe:0"]
    if audio_path is not None:
        cmd += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a?", "-c:a", "copy"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", "-movflags", "+faststart", "-y", str(output_path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

def _mux_audio(video_path: Path, audio_path: Path, output_path: Path):
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", "-y", str(output_path)
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise RuntimeError("音声と動画の結合に失敗しました: " + result.stderr.decode(errors="replace"))

def _hz_process_entry(input_path_str: str, delay_ms: float, result_queue):
    """Generate Hz audio in a separate, low-priority process so video stays responsive."""
    try:
        # Do not let native math libraries create extra CPU threads in this worker.
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
        try:
            import psutil
            p = psutil.Process(os.getpid())
            if os.name == "nt":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            else:
                p.nice(10)
        except Exception:
            pass
        from audio_hz import make_hz_audio
        path = make_hz_audio(Path(input_path_str), delay_ms=delay_ms)
        result_queue.put((str(path) if path is not None else None, None))
    except BaseException as exc:
        try:
            result_queue.put((None, repr(exc)))
        except Exception:
            pass

def _start_hz_worker(input_path: Path, delay_ms: float, stop_event):
    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue(1)
    process = ctx.Process(target=_hz_process_entry, args=(str(input_path), float(delay_ms), result_queue), name="PySketchify-HzWorker")
    process.start()
    return process, result_queue

def _finish_hz_worker(process, result_queue):
    process.join()
    if not result_queue.empty():
        path_str, error = result_queue.get()
        if error is not None:
            raise RuntimeError(f"Hz音声Workerでエラー: {error}")
        if path_str is None:
            raise RuntimeError("Hz音声Workerが音声を生成できませんでした。")
        return Path(path_str)
    if process.exitcode != 0:
        raise RuntimeError(f"Hz音声Workerが異常終了しました: exitcode={process.exitcode}")
    raise RuntimeError("Hz音声Workerから結果を受信できませんでした。")

def _run_parallel_drawing(input_path, output_path, width, height, fps, frame_count, settings, ram_available, progress_callback, stop_event, copy_audio=False, hz_delay_ms: float = 1.93):
    from parallel_pipeline import RangeParallelProcessor
    frame_size = width * height * 3
    processor = RangeParallelProcessor(width, height, settings, ram_available=ram_available)
    total_budget = ram_available or 512 * 1024**2
    per_worker_budget = max(8 * 1024**2, min(64 * 1024**2, total_budget // max(1, processor.worker_count * 4)))
    block_size = max(1, min(1000, per_worker_budget // max(1, frame_size)))
    hz_mode = bool(copy_audio)
    print(f"[DRAW] range workers={processor.worker_count} | range={block_size} frame | frame={frame_size/1024/1024:.2f} MiB | hz_audio={'ON' if hz_mode else 'OFF'} | delay={hz_delay_ms:.2f} ms")

    decoder = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_path), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    hz_process = None; hz_queue = None; audio_path = None; encoder = None
    video_only_path = output_path
    mux_needed = False
    try:
        if hz_mode:
            print("[HZ] Worker開始 | 低優先度Process | 動画Workerと並列実行")
            hz_process, hz_queue = _start_hz_worker(input_path, hz_delay_ms, stop_event)
            video_only_path = output_path.with_name(output_path.stem + "__video_only.mp4")
            mux_needed = True
            encoder_audio_path = None
        else:
            encoder_audio_path = input_path
        encoder = _make_video_encoder(video_only_path, width, height, fps, audio_path=encoder_audio_path)
        stats = StreamingStats(started=time.perf_counter(), workers=processor.worker_count)
        def frames():
            while not (stop_event and stop_event.is_set()):
                frame = _read_exact(decoder.stdout, frame_size) if decoder.stdout else b""
                if not frame: return
                if len(frame) != frame_size: raise RuntimeError(f"不完全なフレーム: {len(frame)}/{frame_size}")
                yield frame
        try:
            processor.start(); assert encoder.stdin is not None
            for frame in processor.process_stream(frames(), frame_count, block_size, progress_callback, stop_event):
                if stop_event and stop_event.is_set(): break
                encoder.stdin.write(frame)
            if not (stop_event and stop_event.is_set()):
                encoder.stdin.close()
                if decoder.wait(timeout=PROCESS_WAIT_TIMEOUT) != 0:
                    raise RuntimeError((decoder.stderr.read() if decoder.stderr else b"").decode(errors="replace"))
                if encoder.wait(timeout=PROCESS_WAIT_TIMEOUT) != 0:
                    raise RuntimeError((encoder.stderr.read() if encoder.stderr else b"").decode(errors="replace"))
                if hz_mode:
                    assert hz_process is not None and hz_queue is not None
                    audio_path = _finish_hz_worker(hz_process, hz_queue)
                    print("[HZ] Worker完了 | 動画Worker完了後にMux")
                    _mux_audio(video_only_path, audio_path, output_path)
                    mux_needed = False
                    video_only_path.unlink(missing_ok=True)
                if not output_path.exists() or output_path.stat().st_size <= 0: raise RuntimeError("出力MP4が生成されませんでした。")
            stats.finished = time.perf_counter(); return stats
        finally:
            processor.stop(); _terminate(decoder); _terminate(encoder)
    finally:
        if hz_process is not None and hz_process.is_alive():
            hz_process.terminate(); hz_process.join(timeout=FORCE_KILL_TIMEOUT)
        if audio_path is not None:
            try: audio_path.unlink(missing_ok=True)
            except OSError: pass
        if mux_needed and video_only_path != output_path:
            try: video_only_path.unlink(missing_ok=True)
            except OSError: pass
        _terminate(decoder); _terminate(encoder)

def run_streaming_pipeline(input_path: Path, output_path: Path, width: int, height: int, fps: float, frame_count: int = 0,
                           processor: FrameProcessor = passthrough_processor, queue_frames: Optional[int] = None,
                           threads: int = 1, progress_callback=None, stop_event=None, copy_audio: bool = False,
                           hz_delay_ms: float = 1.93) -> StreamingStats:
    if width <= 0 or height <= 0: raise ValueError("ストリーミングには正しい解像度が必要です。")
    if fps <= 0: raise ValueError("ストリーミングには正しいFPSが必要です。")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    settings = getattr(processor, "pencil_settings", None)
    if settings is not None:
        try:
            import psutil; ram_available = int(psutil.virtual_memory().available)
        except Exception: ram_available = None
        return _run_parallel_drawing(input_path, output_path, width, height, fps, frame_count, settings, ram_available, progress_callback, stop_event, copy_audio=copy_audio, hz_delay_ms=hz_delay_ms)

    frame_size = width * height * 3; qsize = max(1, queue_frames or DEFAULT_QUEUE_FRAMES); stop = stop_event or threading.Event()
    from queue import Queue, Empty, Full
    raw_queue = Queue(maxsize=qsize); encoded_queue = Queue(maxsize=qsize); errors = []; stats = StreamingStats(started=time.perf_counter())
    decoder = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_path), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", str(max(1, threads)), "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    hz_process = None; hz_queue = None; generated_audio_path = None; video_only_path = output_path; mux_needed = False; encoder = None
    try:
        if copy_audio:
            print("[HZ] Worker開始 | 低優先度Process | 動画Workerと並列実行")
            hz_process, hz_queue = _start_hz_worker(input_path, hz_delay_ms, stop)
            video_only_path = output_path.with_name(output_path.stem + "__video_only.mp4")
            mux_needed = True
        encoder = _make_video_encoder(video_only_path, width, height, fps, audio_path=None if copy_audio else input_path)

        def put(q, item):
            while not stop.is_set():
                try: q.put(item, timeout=QUEUE_TIMEOUT); return True
                except Full: pass
            return False
        def get(q):
            while not stop.is_set():
                try: return q.get(timeout=QUEUE_TIMEOUT)
                except Empty: pass
            return None
        def dec():
            try:
                i = 0
                while not stop.is_set():
                    f = _read_exact(decoder.stdout, frame_size)
                    if not f: break
                    if len(f) != frame_size: raise RuntimeError("FFmpegから不完全なフレームを受信しました")
                    if not put(raw_queue, (i, f)): return
                    i += 1
                put(raw_queue, None)
            except BaseException as e: errors.append(e); stop.set()
        def proc():
            try:
                while not stop.is_set():
                    item = get(raw_queue)
                    if item is None:
                        if not stop.is_set(): put(encoded_queue, None)
                        return
                    i, f = item; out = processor(f, i, width, height)
                    if len(out) != frame_size: raise RuntimeError("フレーム処理結果のサイズが元フレームと一致しません")
                    if not put(encoded_queue, (i, out)): return
            except BaseException as e: errors.append(e); stop.set()
        def enc():
            try:
                expected = 0
                while not stop.is_set():
                    item = get(encoded_queue)
                    if item is None:
                        if not stop.is_set(): encoder.stdin.close()
                        return
                    i, f = item
                    if i != expected: raise RuntimeError(f"フレーム順序が壊れました: expected={expected}, got={i}")
                    encoder.stdin.write(f); expected += 1; stats.frames = expected
                    if progress_callback: progress_callback(stats)
            except BaseException as e: errors.append(e); stop.set()
        workers = [threading.Thread(target=dec), threading.Thread(target=proc), threading.Thread(target=enc)]
        for w in workers: w.start()
        for w in workers: w.join()
        if stop_event and stop_event.is_set(): return stats
        if decoder.wait(timeout=PROCESS_WAIT_TIMEOUT) != 0: raise RuntimeError("FFmpeg decode failed")
        if encoder.wait(timeout=PROCESS_WAIT_TIMEOUT) != 0: raise RuntimeError((encoder.stderr.read() if encoder.stderr else b"").decode(errors="replace"))
        if copy_audio:
            assert hz_process is not None and hz_queue is not None
            generated_audio_path = _finish_hz_worker(hz_process, hz_queue)
            print("[HZ] Worker完了 | 動画Worker完了後にMux")
            _mux_audio(video_only_path, generated_audio_path, output_path)
            mux_needed = False
            video_only_path.unlink(missing_ok=True)
        stats.finished = time.perf_counter()
        if errors: raise RuntimeError(str(errors[0]))
        if frame_count and stats.frames != frame_count: raise RuntimeError(f"処理フレーム数が一致しません: {stats.frames}/{frame_count}")
        if not output_path.exists() or output_path.stat().st_size <= 0: raise RuntimeError("出力MP4が生成されませんでした")
        return stats
    finally:
        _terminate(decoder); _terminate(encoder)
        if hz_process is not None and hz_process.is_alive(): hz_process.terminate(); hz_process.join(timeout=FORCE_KILL_TIMEOUT)
        generated_audio_path = generated_audio_path or None
        if generated_audio_path is not None:
            try: generated_audio_path.unlink(missing_ok=True)
            except OSError: pass
        if mux_needed and video_only_path != output_path:
            try: video_only_path.unlink(missing_ok=True)
            except OSError: pass
