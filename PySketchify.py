from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None


APP_NAME = "PySketchify"
VERSION = "0.1.0"
DEFAULT_MARGIN_GB = 0.9
DEFAULT_CHUNK_TARGET_GB = 2.0
SUPPORTED_INPUT_EXTENSIONS = {
    ".mp4", ".webm", ".mov", ".wmv", ".avi", ".mkv", ".mts", ".m2ts", ".avchd"
}


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    duration: float
    frame_count: int
    pix_fmt: str
    codec: str
    audio_streams: int
    subtitle_streams: int


@dataclass
class ChunkPlan:
    index: int
    start_frame: int
    end_frame: int
    frame_count: int
    estimated_bytes: int
    filename: str


@dataclass
class ResourceSnapshot:
    ram_total: Optional[int]
    ram_available: Optional[int]
    vram_dedicated_total: Optional[int]
    vram_dedicated_used: Optional[int]
    vram_shared_total: Optional[int]
    vram_shared_used: Optional[int]


# ---------------------------------------------------------------------------
# FFmpeg / FFprobe
# ---------------------------------------------------------------------------


def find_executable(name: str) -> Optional[str]:
    return shutil.which(name)


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "FFmpeg / FFprobe が見つかりません。PATHに ffmpeg と ffprobe を追加してください。"
        )
    return ffmpeg, ffprobe


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _ratio_to_float(value: str) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    if "/" in value:
        a, b = value.split("/", 1)
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def probe_video(path: Path) -> VideoInfo:
    _, ffprobe = require_ffmpeg()
    command = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe failed:\n{result.stderr.strip()}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("映像ストリームが見つかりません。")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _ratio_to_float(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0.0)

    frame_count_raw = video.get("nb_frames")
    if frame_count_raw and str(frame_count_raw).isdigit():
        frame_count = int(frame_count_raw)
    elif fps > 0 and duration > 0:
        frame_count = max(1, round(fps * duration))
    else:
        frame_count = 0

    return VideoInfo(
        path=str(path),
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        frame_count=frame_count,
        pix_fmt=str(video.get("pix_fmt") or "unknown"),
        codec=str(video.get("codec_name") or "unknown"),
        audio_streams=sum(1 for s in streams if s.get("codec_type") == "audio"),
        subtitle_streams=sum(1 for s in streams if s.get("codec_type") == "subtitle"),
    )


# ---------------------------------------------------------------------------
# Capacity estimation
# ---------------------------------------------------------------------------


def estimate_frame_bytes(width: int, height: int, bits: int = 8, channels: int = 3) -> int:
    bytes_per_channel = 2 if bits > 8 else 1
    return width * height * channels * bytes_per_channel


def estimate_png_bytes(
    width: int,
    height: int,
    frame_count: int,
    compression_ratio: float = 0.4,
    bits: int = 8,
) -> int:
    raw_per_frame = estimate_frame_bytes(width, height, bits=bits)
    return max(0, int(raw_per_frame * compression_ratio * frame_count))


def estimate_chunk_bytes(
    width: int,
    height: int,
    frame_count: int,
    compression_ratio: float = 0.4,
    bits: int = 8,
) -> int:
    # This is intentionally an estimate only. Actual lossless video chunk size
    # depends heavily on content and codec settings.
    return estimate_png_bytes(width, height, frame_count, compression_ratio, bits)


def add_safety_margin(bytes_required: int, margin_gb: float = DEFAULT_MARGIN_GB) -> int:
    return bytes_required + int(margin_gb * 1024**3)


def format_bytes(value: int | float) -> str:
    value = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.2f} {units[i]}"


# ---------------------------------------------------------------------------
# System resources
# ---------------------------------------------------------------------------


def get_ram_snapshot() -> tuple[Optional[int], Optional[int]]:
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return int(vm.total), int(vm.available)
    except Exception:
        return None, None


def _powershell_json(script: str) -> Optional[dict]:
    if os.name != "nt":
        return None
    command = ["powershell", "-NoProfile", "-Command", script]
    result = run_command(command)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def get_windows_gpu_snapshot() -> ResourceSnapshot:
    ram_total, ram_available = get_ram_snapshot()

    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,CurrentHorizontalResolution,CurrentVerticalResolution | "
        "ConvertTo-Json -Compress"
    )
    data = _powershell_json(script)
    if not data:
        return ResourceSnapshot(ram_total, ram_available, None, None, None, None)

    if isinstance(data, dict):
        data = [data]

    dedicated_total = 0
    names = []
    for gpu in data:
        try:
            dedicated_total += int(gpu.get("AdapterRAM") or 0)
        except (TypeError, ValueError):
            pass
        names.append(str(gpu.get("Name") or ""))

    # WMI AdapterRAM is not a reliable live VRAM usage metric. Keep usage as
    # unknown rather than inventing it. Shared memory is also OS-managed.
    return ResourceSnapshot(
        ram_total=ram_total,
        ram_available=ram_available,
        vram_dedicated_total=dedicated_total or None,
        vram_dedicated_used=None,
        vram_shared_total=None,
        vram_shared_used=None,
    )


def get_resource_snapshot() -> ResourceSnapshot:
    if os.name == "nt":
        return get_windows_gpu_snapshot()
    ram_total, ram_available = get_ram_snapshot()
    return ResourceSnapshot(ram_total, ram_available, None, None, None, None)


# ---------------------------------------------------------------------------
# Chunk planning
# ---------------------------------------------------------------------------


def choose_chunk_frame_count(info: VideoInfo, target_gb: float = DEFAULT_CHUNK_TARGET_GB) -> int:
    # Conservative estimate based on 8-bit RGB. This is only a planning value;
    # actual chunk sizes are checked while FFmpeg creates them.
    estimated_per_frame = max(1, estimate_frame_bytes(info.width, info.height))
    target_bytes = max(64 * 1024**2, int(target_gb * 1024**3))
    count = max(1, target_bytes // estimated_per_frame)

    # Avoid gigantic chunk counts at low resolutions while keeping high-res
    # chunks reasonably small.
    return int(max(1, min(count, 100_000)))


def build_chunk_plan(info: VideoInfo, target_gb: float = DEFAULT_CHUNK_TARGET_GB) -> list[ChunkPlan]:
    if info.frame_count <= 0:
        raise ValueError("総フレーム数を取得できないため、チャンク計画を作成できません。")

    chunk_frames = choose_chunk_frame_count(info, target_gb)
    plan: list[ChunkPlan] = []
    start = 1
    index = 1
    while start <= info.frame_count:
        end = min(info.frame_count, start + chunk_frames - 1)
        count = end - start + 1
        estimate = estimate_chunk_bytes(info.width, info.height, count)
        plan.append(
            ChunkPlan(
                index=index,
                start_frame=start,
                end_frame=end,
                frame_count=count,
                estimated_bytes=estimate,
                filename=f"frame-{start:08d}-{end:08d}.mkv",
            )
        )
        start = end + 1
        index += 1
    return plan


def save_manifest(work_dir: Path, info: VideoInfo, plan: list[ChunkPlan], required_bytes: int) -> Path:
    manifest = {
        "app": APP_NAME,
        "version": VERSION,
        "video": asdict(info),
        "estimated_required_bytes": required_bytes,
        "chunks": [asdict(chunk) for chunk in plan],
    }
    path = work_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Lossless chunk extraction
# ---------------------------------------------------------------------------


def extract_chunk(
    input_path: Path,
    output_path: Path,
    start_frame: int,
    end_frame: int,
    fps: float,
    progress_callback=None,
) -> None:
    ffmpeg, _ = require_ffmpeg()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # FFV1 in Matroska is used as a lossless intermediate. This intentionally
    # avoids generation loss while the hand-drawing stage is developed.
    vf = f"select=between(n\\,{start_frame - 1}\\,{end_frame - 1})"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(input_path),
        "-vf", vf,
        "-vsync", "0",
        "-an",
        "-c:v", "ffv1",
        "-level", "3",
        "-g", "1",
        "-threads", "0",
        "-y",
        str(output_path),
    ]

    result = run_command(command)
    if result.returncode != 0:
        raise RuntimeError(f"チャンク作成失敗: {output_path.name}\n{result.stderr.strip()}")

    if progress_callback:
        progress_callback(output_path)


# ---------------------------------------------------------------------------
# Processing session
# ---------------------------------------------------------------------------


class ProcessingSession:
    def __init__(self, input_path: Path, work_root: Optional[Path] = None):
        self.input_path = input_path
        self.work_root = work_root or (input_path.parent / f"{input_path.stem}_pysketchify_work")
        self.source_dir = self.work_root / "source_chunks"
        self.processed_dir = self.work_root / "processed_chunks"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.stop_requested = False

    def stop(self):
        self.stop_requested = True

    def prepare(self) -> tuple[VideoInfo, list[ChunkPlan], int]:
        info = probe_video(self.input_path)
        plan = build_chunk_plan(info)
        estimated = sum(chunk.estimated_bytes for chunk in plan)
        required = add_safety_margin(estimated)
        save_manifest(self.work_root, info, plan, required)
        return info, plan, required

    def run_extraction(self, info: VideoInfo, plan: list[ChunkPlan], progress_callback=None):
        for chunk in plan:
            if self.stop_requested:
                break
            output = self.source_dir / chunk.filename
            if output.exists() and output.stat().st_size > 0:
                if progress_callback:
                    progress_callback(chunk, output, "exists")
                continue
            extract_chunk(
                self.input_path,
                output,
                chunk.start_frame,
                chunk.end_frame,
                info.fps,
            )
            if progress_callback:
                progress_callback(chunk, output, "created")


# ---------------------------------------------------------------------------
# Console mode
# ---------------------------------------------------------------------------


def print_video_info(info: VideoInfo):
    print("=" * 68)
    print(f"{APP_NAME} {VERSION}")
    print("=" * 68)
    print(f"入力       : {info.path}")
    print(f"解像度     : {info.width} × {info.height}")
    print(f"入力FPS    : {info.fps:.6g} FPS")
    print(f"時間       : {info.duration:.3f} 秒")
    print(f"フレーム数 : {info.frame_count:,} 枚")
    print(f"映像codec  : {info.codec}")
    print(f"pixel fmt  : {info.pix_fmt}")
    print(f"音声       : {info.audio_streams} track")
    print(f"字幕       : {info.subtitle_streams} track")
    print("=" * 68)


def console_run(path: Path):
    session = ProcessingSession(path)
    info, plan, required = session.prepare()
    print_video_info(info)

    print(f"チャンク数           : {len(plan):,}")
    print(f"推定フレーム容量     : {format_bytes(sum(c.estimated_bytes for c in plan))}")
    print(f"安全余裕             : +{DEFAULT_MARGIN_GB:.1f} GB")
    print(f"推定必要容量         : {format_bytes(required)}")
    print(f"作業フォルダ         : {session.work_root}")

    free = shutil.disk_usage(session.work_root).free
    print(f"現在の空き容量       : {format_bytes(free)}")
    if free < required:
        raise RuntimeError(
            f"ディスク容量不足です。必要推定 {format_bytes(required)} / 空き {format_bytes(free)}"
        )

    print("\nチャンク展開を開始します。")

    total = len(plan)
    started = time.perf_counter()

    def progress(chunk: ChunkPlan, output: Path, state: str):
        done = chunk.index
        elapsed = max(0.001, time.perf_counter() - started)
        frame_done = min(info.frame_count, chunk.end_frame)
        speed = frame_done / elapsed
        print(
            f"[{done:>5}/{total:<5}] {output.name} | "
            f"{frame_done:,}/{info.frame_count:,} frame | {speed:.1f} frame/s | {state}"
        )

    session.run_extraction(info, plan, progress)
    print("完了。")


# ---------------------------------------------------------------------------
# Minimal GUI
# ---------------------------------------------------------------------------


class PySketchifyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry("900x620")
        self.session: Optional[ProcessingSession] = None
        self.info: Optional[VideoInfo] = None
        self.plan: list[ChunkPlan] = []

        self.path_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="動画を選択してください。")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.stats_var = tk.StringVar(value="")

        self._build()

    def _build(self):
        root = self.root
        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text=APP_NAME, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(main, text="動画 → 手描き風動画変換基盤 / v0.1", font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 12))

        select = ttk.Frame(main)
        select.pack(fill="x")
        ttk.Entry(select, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(select, text="動画を選択", command=self.choose_video).pack(side="left", padx=(8, 0))

        info_frame = ttk.LabelFrame(main, text="動画情報", padding=10)
        info_frame.pack(fill="x", pady=12)
        self.info_label = ttk.Label(info_frame, text="未選択")
        self.info_label.pack(anchor="w")

        self.canvas_frame = ttk.LabelFrame(main, text="処理プレビュー（後続実装）", padding=10)
        self.canvas_frame.pack(fill="both", expand=True)
        ttk.Label(
            self.canvas_frame,
            text="ここに最新X枚のフレームをペイントアプリ風に表示します。",
            anchor="center",
        ).pack(fill="both", expand=True)

        ttk.Progressbar(main, variable=self.progress_var, maximum=100).pack(fill="x", pady=(10, 4))
        ttk.Label(main, textvariable=self.stats_var).pack(anchor="w")
        ttk.Label(main, textvariable=self.status_var).pack(anchor="w", pady=(4, 8))

        buttons = ttk.Frame(main)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="チャンク展開開始", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)

    def choose_video(self):
        path = filedialog.askopenfilename(
            title="入力動画を選択",
            filetypes=[
                ("Video files", "*.mp4 *.webm *.mov *.wmv *.avi *.mkv *.mts *.m2ts *.avchd"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.path_var.set(path)
        try:
            self.info = probe_video(Path(path))
            self.plan = build_chunk_plan(self.info)
            estimated = sum(c.estimated_bytes for c in self.plan)
            required = add_safety_margin(estimated)
            free = shutil.disk_usage(Path(path).parent).free
            self.info_label.config(
                text=(
                    f"{self.info.width} × {self.info.height} | "
                    f"{self.info.fps:.6g} FPS | {self.info.frame_count:,} frame | "
                    f"audio {self.info.audio_streams} | subtitle {self.info.subtitle_streams}\n"
                    f"チャンク {len(self.plan):,} 個 | 推定 {format_bytes(required)} | "
                    f"空き {format_bytes(free)}"
                )
            )
            self.status_var.set("解析完了。")
        except Exception as exc:
            self.status_var.set(f"解析エラー: {exc}")
            messagebox.showerror(APP_NAME, str(exc))

    def start(self):
        if not self.path_var.get():
            self.choose_video()
            if not self.path_var.get():
                return
        if not self.info:
            self.choose_video()
            if not self.info:
                return

        required = add_safety_margin(sum(c.estimated_bytes for c in self.plan))
        work_root = Path(self.path_var.get()).parent / f"{Path(self.path_var.get()).stem}_pysketchify_work"
        free = shutil.disk_usage(work_root.parent).free
        if free < required:
            messagebox.showwarning(
                APP_NAME,
                f"ディスク容量が不足しています。\n\n"
                f"推定必要容量: {format_bytes(required)}\n"
                f"空き容量: {format_bytes(free)}",
            )
            return

        self.session = ProcessingSession(Path(self.path_var.get()), work_root)
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.progress_var.set(0)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        assert self.session is not None
        assert self.info is not None
        total = len(self.plan)
        started = time.perf_counter()

        for chunk in self.plan:
            if self.session.stop_requested:
                break
            output = self.session.source_dir / chunk.filename
            try:
                if not output.exists():
                    extract_chunk(self.session.input_path, output, chunk.start_frame, chunk.end_frame, self.info.fps)
                done = chunk.index
                elapsed = max(0.001, time.perf_counter() - started)
                speed = min(self.info.frame_count, chunk.end_frame) / elapsed
                percent = done / total * 100.0
                self.root.after(0, self._update_progress, percent, done, speed, output.name)
            except Exception as exc:
                self.root.after(0, self._worker_error, str(exc))
                return

        self.root.after(0, self._worker_done)

    def _update_progress(self, percent, done, speed, filename):
        self.progress_var.set(percent)
        assert self.info is not None
        self.stats_var.set(
            f"処理済みチャンク: {done:,} / {len(self.plan):,} | "
            f"処理速度: {speed:.1f} frame/s | 最新: {filename}"
        )
        self.status_var.set("チャンク展開中...")

    def _worker_done(self):
        stopped = self.session.stop_requested if self.session else False
        self.status_var.set("停止しました。" if stopped else "チャンク展開完了。")
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")

    def _worker_error(self, message):
        self.status_var.set(f"エラー: {message}")
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")
        messagebox.showerror(APP_NAME, message)

    def stop(self):
        if self.session:
            self.session.stop()
            self.status_var.set("停止要求を送信しました。現在のFFmpeg処理終了後に停止します。")


def main():
    if len(sys.argv) > 1:
        input_path = Path(sys.argv[1]).expanduser().resolve()
        if not input_path.exists():
            print(f"ファイルがありません: {input_path}", file=sys.stderr)
            raise SystemExit(2)
        console_run(input_path)
        return

    if tk is None:
        print("Tkinterが利用できません。動画ファイルを引数に指定してください。")
        print("例: python PySketchify.py input.mkv")
        return

    root = tk.Tk()
    PySketchifyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
