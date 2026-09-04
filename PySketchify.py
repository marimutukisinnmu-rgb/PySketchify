from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
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

try:
    from streaming_pipeline import run_streaming_pipeline, choose_queue_frames
except ImportError:
    run_streaming_pipeline = None
    choose_queue_frames = None

APP_NAME = "PySketchify"
VERSION = "0.5.1"
DEFAULT_MARGIN_GB = 0.9
DEFAULT_CHUNK_TARGET_GB = 2.0
DEFAULT_COMPRESSION_RATIO = 0.40
DEFAULT_OUTPUT_SUFFIX = "_pysketchify"
MAX_STREAMING_HEIGHT = 1080
SUPPORTED_INPUT_EXTENSIONS = {".mp4", ".webm", ".mov", ".wmv", ".avi", ".mkv", ".mts", ".m2ts", ".avchd"}

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
    gpu_names: tuple[str, ...] = ()

def default_temp_dir() -> Path:
    return Path(__file__).resolve().parent / "tmp"

def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(input_path.stem + DEFAULT_OUTPUT_SUFFIX + ".mp4")

def validate_input_path(path: Path) -> None:
    if not path.exists(): raise FileNotFoundError(f"入力ファイルがありません: {path}")
    if not path.is_file(): raise ValueError(f"入力先がファイルではありません: {path}")
    if path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS: raise ValueError(f"対応していない入力形式です: {path.suffix or '(拡張子なし)'}")

def cleanup_temp(temp_dir: Path) -> None:
    if not temp_dir.exists(): return
    try: shutil.rmtree(temp_dir)
    except OSError: pass

def cleanup_default_temp(temp_dir: Path, was_default: bool) -> None:
    if was_default: cleanup_temp(temp_dir)

def find_executable(name: str) -> Optional[str]: return shutil.which(name)

def require_ffmpeg() -> tuple[str, str]:
    ffmpeg, ffprobe = find_executable("ffmpeg"), find_executable("ffprobe")
    if not ffmpeg or not ffprobe: raise RuntimeError("FFmpeg / FFprobe が見つかりません。PATHに ffmpeg と ffprobe を追加してください。")
    return ffmpeg, ffprobe

def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)

def _ratio_to_float(value: str) -> float:
    if not value or value in {"0/0", "N/A"}: return 0.0
    if "/" in value:
        a, b = value.split("/", 1)
        try: return float(a) / float(b)
        except (ValueError, ZeroDivisionError): return 0.0
    try: return float(value)
    except ValueError: return 0.0

def probe_video(path: Path) -> VideoInfo:
    validate_input_path(path); _, ffprobe = require_ffmpeg()
    result = run_command([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if result.returncode != 0: raise RuntimeError(f"FFprobe failed:\n{result.stderr.strip()}")
    try: data = json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise RuntimeError("FFprobeのJSON解析に失敗しました。") from exc
    streams = data.get("streams", []); video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None: raise RuntimeError("映像ストリームが見つかりません。")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    fps = _ratio_to_float(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0.0)
    frame_count = int(video.get("nb_frames")) if video.get("nb_frames") and str(video.get("nb_frames")).isdigit() else (max(1, round(fps * duration)) if fps > 0 and duration > 0 else 0)
    if width <= 0 or height <= 0: raise RuntimeError("動画の解像度を取得できませんでした。")
    if fps <= 0: raise RuntimeError("動画のFPSを取得できませんでした。")
    return VideoInfo(str(path), width, height, fps, duration, frame_count, str(video.get("pix_fmt") or "unknown"), str(video.get("codec_name") or "unknown"), sum(1 for s in streams if s.get("codec_type") == "audio"), sum(1 for s in streams if s.get("codec_type") == "subtitle"))

def estimate_frame_bytes(width: int, height: int, bits: int = 8, channels: int = 3) -> int:
    return width * height * channels * (2 if bits > 8 else 1)

def estimate_png_bytes(width: int, height: int, frame_count: int, compression_ratio: float = DEFAULT_COMPRESSION_RATIO, bits: int = 8) -> int:
    return max(0, int(estimate_frame_bytes(width, height, bits=bits) * compression_ratio * frame_count))

def estimate_chunk_bytes(width: int, height: int, frame_count: int, compression_ratio: float = DEFAULT_COMPRESSION_RATIO, bits: int = 8) -> int:
    return estimate_png_bytes(width, height, frame_count, compression_ratio, bits)

def add_safety_margin(bytes_required: int, margin_gb: float = DEFAULT_MARGIN_GB) -> int:
    return max(0, bytes_required) + int(margin_gb * 1024**3)

def format_bytes(value: int | float) -> str:
    value = float(max(0, value)); units = ("B", "KB", "MB", "GB", "TB", "PB"); i = 0
    while value >= 1024 and i < len(units)-1: value /= 1024; i += 1
    return f"{value:.2f} {units[i]}"

def get_ram_snapshot() -> tuple[Optional[int], Optional[int]]:
    try:
        import psutil
        vm = psutil.virtual_memory(); return int(vm.total), int(vm.available)
    except Exception: return None, None

def _powershell_json(script: str) -> object | None:
    if os.name != "nt": return None
    result = run_command(["powershell", "-NoProfile", "-Command", script])
    if result.returncode != 0 or not result.stdout.strip(): return None
    try: return json.loads(result.stdout)
    except json.JSONDecodeError: return None

def get_windows_gpu_snapshot() -> ResourceSnapshot:
    ram_total, ram_available = get_ram_snapshot()
    data = _powershell_json("Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress")
    if not data: return ResourceSnapshot(ram_total, ram_available, None, None, None, None, ())
    if isinstance(data, dict): data = [data]
    dedicated_total = 0; names: list[str] = []
    for gpu in data if isinstance(data, list) else []:
        if not isinstance(gpu, dict): continue
        try: dedicated_total += int(gpu.get("AdapterRAM") or 0)
        except (TypeError, ValueError): pass
        name = str(gpu.get("Name") or "").strip()
        if name: names.append(name)
    return ResourceSnapshot(ram_total, ram_available, dedicated_total or None, None, None, None, tuple(names))

def get_resource_snapshot() -> ResourceSnapshot:
    if os.name == "nt": return get_windows_gpu_snapshot()
    total, available = get_ram_snapshot(); return ResourceSnapshot(total, available, None, None, None, None, ())

def choose_chunk_frame_count(info: VideoInfo, target_gb: float = DEFAULT_CHUNK_TARGET_GB) -> int:
    per_frame = max(1, estimate_frame_bytes(info.width, info.height)); target = max(64*1024**2, int(target_gb*1024**3))
    return int(max(1, min(target//per_frame, 100_000)))

def build_chunk_plan(info: VideoInfo, target_gb: float = DEFAULT_CHUNK_TARGET_GB) -> list[ChunkPlan]:
    if info.frame_count <= 0: raise ValueError("総フレーム数を取得できないため、チャンク計画を作成できません。")
    chunk_frames = choose_chunk_frame_count(info, target_gb); plan=[]; start=1; index=1
    while start <= info.frame_count:
        end=min(info.frame_count, start+chunk_frames-1); count=end-start+1
        plan.append(ChunkPlan(index,start,end,count,estimate_chunk_bytes(info.width,info.height,count),f"frame-{start:08d}-{end:08d}.mkv")); start=end+1; index+=1
    return plan

def extract_chunk(input_path: Path, output_path: Path, start_frame: int, end_frame: int) -> None:
    ffmpeg,_=require_ffmpeg(); output_path.parent.mkdir(parents=True,exist_ok=True)
    if end_frame < start_frame: raise ValueError("end_frame は start_frame 以上である必要があります。")
    vf=f"select=between(n\\,{start_frame-1}\\,{end_frame-1})"
    result=run_command([ffmpeg,"-hide_banner","-loglevel","error","-i",str(input_path),"-vf",vf,"-vsync","0","-an","-c:v","ffv1","-level","3","-threads","1","-y",str(output_path)])
    if result.returncode != 0: raise RuntimeError(f"チャンク作成失敗: {output_path.name}\n{result.stderr.strip()}")

class ProcessingSession:
    def __init__(self,input_path:Path,temp_dir:Optional[Path]=None,output_path:Optional[Path]=None):
        self.input_path=input_path
        self.temp_is_default=temp_dir is None or Path(temp_dir).resolve()==default_temp_dir().resolve()
        self.temp_dir=Path(temp_dir).expanduser().resolve() if temp_dir else default_temp_dir()
        self.output_path=Path(output_path).expanduser().resolve() if output_path else default_output_path(input_path)
        self.work_root=self.temp_dir/f"{input_path.stem}_pysketchify"
        self.source_dir=self.work_root/"source_chunks"
        self.processed_dir=self.work_root/"processed_chunks"
        self.work_root.mkdir(parents=True,exist_ok=True)
        self.source_dir.mkdir(parents=True,exist_ok=True)
        self.processed_dir.mkdir(parents=True,exist_ok=True)
        self.stop_requested=False
        self.stop_event=threading.Event()
    def stop(self):
        self.stop_requested=True
        self.stop_event.set()
    def prepare(self):
        info=probe_video(self.input_path); plan=build_chunk_plan(info); required=add_safety_margin(sum(c.estimated_bytes for c in plan)); return info,plan,required
    def finish_cleanup(self):
        cleanup_temp(self.temp_dir)

def print_video_info(info:VideoInfo):
    print("="*72); print(f"{APP_NAME} {VERSION}"); print("="*72); print(f"入力       : {info.path}"); print(f"解像度     : {info.width} × {info.height}"); print(f"入力FPS    : {info.fps:.6g} FPS"); print(f"時間       : {info.duration:.3f} 秒"); print(f"フレーム数 : {info.frame_count:,} 枚"); print(f"映像codec  : {info.codec}"); print(f"pixel fmt  : {info.pix_fmt}"); print(f"音声       : {info.audio_streams} track"); print(f"字幕       : {info.subtitle_streams} track"); print("="*72)

def run_lowres_streaming(info:VideoInfo,session:ProcessingSession,progress_callback=None):
    if run_streaming_pipeline is None: raise RuntimeError("streaming_pipeline.py を読み込めません。")
    resources=get_resource_snapshot(); q=choose_queue_frames(info.width,info.height,resources.ram_available) if choose_queue_frames else 4
    print("[STREAM] 1080p以下のためストリーミング処理を使用"); print(f"[STREAM] RAM空き: {format_bytes(resources.ram_available) if resources.ram_available else '不明'} | キュー: {q} frame")
    last=[0.0]
    def callback(stats):
        if progress_callback: progress_callback(stats)
        now=time.perf_counter()
        if now-last[0]>=0.25 or (info.frame_count and stats.frames>=info.frame_count):
            last[0]=now; print(f"処理中... | {stats.frames:,}/{info.frame_count:,} frame | {stats.frame_rate:.1f} frame/s")
    return run_streaming_pipeline(session.input_path,session.output_path,info.width,info.height,info.fps,info.frame_count,queue_frames=q,threads=1,progress_callback=callback,stop_event=session.stop_event)

def console_run(path:Path,temp_dir:Optional[Path]=None,output_path:Optional[Path]=None):
    session=ProcessingSession(path,temp_dir=temp_dir,output_path=output_path)
    try:
        info,plan,required=session.prepare(); print_video_info(info); resources=get_resource_snapshot(); print(f"GPU        : {', '.join(resources.gpu_names) if resources.gpu_names else '検出情報なし'}"); print(f"RAM空き    : {format_bytes(resources.ram_available) if resources.ram_available is not None else '不明'}"); print(f"一時ファイル先 : {session.temp_dir}"); print(f"出力先         : {session.output_path}")
        if info.height<=MAX_STREAMING_HEIGHT:
            print("モード         : 1080p以下 / ストリーミング")
            run_lowres_streaming(info,session)
            return
        print(f"チャンク数     : {len(plan):,}"); print(f"推定必要容量   : {format_bytes(required)}"); session.temp_dir.mkdir(parents=True,exist_ok=True); free=shutil.disk_usage(session.temp_dir).free; print(f"一時先空き容量 : {format_bytes(free)}")
        if free<required: raise RuntimeError(f"ディスク容量不足です。必要推定 {format_bytes(required)} / 空き {format_bytes(free)}")
        print("\nチャンク展開を開始します。"); started=time.perf_counter(); completed=0
        for chunk in plan:
            if session.stop_requested: break
            output=session.source_dir/chunk.filename
            if not output.exists() or output.stat().st_size==0: extract_chunk(session.input_path,output,chunk.start_frame,chunk.end_frame)
            completed+=chunk.frame_count; elapsed=max(0.001,time.perf_counter()-started); print(f"[{chunk.index}/{len(plan)}] {output.name} | {completed:,}/{info.frame_count:,} frame | {completed/elapsed:.1f} frame/s")
    finally:
        session.finish_cleanup()

class PySketchifyApp:
    def __init__(self,root:tk.Tk):
        self.root=root; self.root.title(f"{APP_NAME} {VERSION}"); self.root.geometry("1060x760"); self.session=None; self.info=None; self.plan=[]; self.completed_frames=0; self.processing_frames=0; self.waiting_frames=0; self.input_var=tk.StringVar(); self.temp_var=tk.StringVar(value=str(default_temp_dir())); self.output_var=tk.StringVar(); self.status_var=tk.StringVar(value="入力動画を選択してください。"); self.progress_var=tk.DoubleVar(value=0); self.stats_var=tk.StringVar(value="処理済み：0 枚  処理中：0 枚  処理待ち：0 枚"); self.resource_var=tk.StringVar(); self.info_var=tk.StringVar(value="未選択"); self._build(); self.root.protocol("WM_DELETE_WINDOW", self.close)
    def _path_row(self,parent,label,variable,command,button_text="参照"):
        row=ttk.Frame(parent); row.pack(fill="x",pady=4); ttk.Label(row,text=label,width=22).pack(side="left"); ttk.Entry(row,textvariable=variable).pack(side="left",fill="x",expand=True); ttk.Button(row,text=button_text,command=command).pack(side="left",padx=(8,0))
    def _build(self):
        main=ttk.Frame(self.root,padding=14); main.pack(fill="both",expand=True); ttk.Label(main,text=APP_NAME,font=("Segoe UI",20,"bold")).pack(anchor="w"); ttk.Label(main,text="動画 → 手描き風動画変換",font=("Segoe UI",10)).pack(anchor="w",pady=(0,12)); paths=ttk.LabelFrame(main,text="入出力",padding=10); paths.pack(fill="x"); self._path_row(paths,"入力先",self.input_var,self.choose_input,"動画を選択"); self._path_row(paths,"一時ファイル場所（任意）",self.temp_var,self.choose_temp,"変更"); ttk.Label(paths,text="入力なしの場合は (pyがある場所)/tmp。処理終了後、この自動生成tmpは削除します。").pack(anchor="w",padx=(22,0),pady=(0,4)); self._path_row(paths,"出力先",self.output_var,self.choose_output,"保存先"); info_frame=ttk.LabelFrame(main,text="動画情報 / リソース",padding=10); info_frame.pack(fill="x",pady=12); ttk.Label(info_frame,textvariable=self.info_var).pack(anchor="w"); ttk.Label(info_frame,textvariable=self.resource_var).pack(anchor="w",pady=(6,0)); preview=ttk.LabelFrame(main,text="処理中 / プレビュー",padding=10); preview.pack(fill="both",expand=True); ttk.Label(preview,text="ここに最新フレームをペイントアプリ風に表示します。\n現在はストリーミング / チャンク基盤を実行します。",anchor="center",justify="center").pack(fill="both",expand=True); ttk.Progressbar(main,variable=self.progress_var,maximum=100).pack(fill="x",pady=(10,4)); ttk.Label(main,textvariable=self.stats_var).pack(anchor="w"); ttk.Label(main,textvariable=self.status_var).pack(anchor="w",pady=(4,8)); buttons=ttk.Frame(main); buttons.pack(fill="x"); self.start_button=ttk.Button(buttons,text="処理開始",command=self.start); self.start_button.pack(side="left"); self.stop_button=ttk.Button(buttons,text="停止",command=self.stop,state="disabled"); self.stop_button.pack(side="left",padx=8)
    def choose_input(self):
        path=filedialog.askopenfilename(title="入力動画を選択",filetypes=[("Video files","*.mp4 *.webm *.mov *.wmv *.avi *.mkv *.mts *.m2ts *.avchd"),("All files","*.*")]);
        if path: selected=Path(path).resolve(); self.input_var.set(str(selected)); self.output_var.set(str(default_output_path(selected))); self.analyze_input()
    def choose_temp(self):
        path=filedialog.askdirectory(title="一時ファイル場所を選択");
        if path: self.temp_var.set(str(Path(path).resolve())); self.status_var.set("一時ファイル場所を変更しました。")
    def choose_output(self):
        current=Path(self.output_var.get()) if self.output_var.get() else None; path=filedialog.asksaveasfilename(title="出力先を選択",initialdir=str(current.parent) if current else str(Path.home()),initialfile=current.name if current else "output_pysketchify.mp4",defaultextension=".mp4",filetypes=[("MP4 video","*.mp4"),("All files","*.*")]);
        if path: self.output_var.set(str(Path(path).resolve()))
    def analyze_input(self):
        try:
            self.info=probe_video(Path(self.input_var.get())); self.plan=build_chunk_plan(self.info); resources=get_resource_snapshot(); streaming=self.info.height<=MAX_STREAMING_HEIGHT; mode="1080p以下 / ストリーミング" if streaming else "チャンク / タイル"; storage="全フレーム展開なし" if streaming else format_bytes(add_safety_margin(sum(c.estimated_bytes for c in self.plan))); temp=Path(self.temp_var.get()) if self.temp_var.get() else default_temp_dir(); temp.mkdir(parents=True,exist_ok=True); free=shutil.disk_usage(temp).free; self.info_var.set(f"{self.info.width} × {self.info.height} | {self.info.fps:.6g} FPS | {self.info.frame_count:,} frame | audio {self.info.audio_streams} | subtitle {self.info.subtitle_streams}\nモード: {mode} | 推定必要容量: {storage} | 一時先空き: {format_bytes(free)}"); self.resource_var.set(f"GPU: {', '.join(resources.gpu_names) if resources.gpu_names else '検出情報なし'} | RAM空き: {format_bytes(resources.ram_available) if resources.ram_available is not None else '不明'} | 専用VRAM総量: {format_bytes(resources.vram_dedicated_total) if resources.vram_dedicated_total is not None else '不明'}"); self.status_var.set("解析完了。")
        except Exception as exc: self.info=None; self.plan=[]; self.status_var.set(f"解析エラー: {exc}"); messagebox.showerror(APP_NAME,str(exc))
    def start(self):
        if not self.input_var.get(): self.choose_input()
        if not self.input_var.get(): return
        if not self.info: self.analyze_input()
        if not self.info: return
        temp=Path(self.temp_var.get()).expanduser().resolve() if self.temp_var.get() else default_temp_dir(); output=Path(self.output_var.get()).expanduser().resolve() if self.output_var.get() else default_output_path(Path(self.input_var.get())); self.session=ProcessingSession(Path(self.input_var.get()),temp_dir=temp,output_path=output)
        if output.exists() and not messagebox.askyesno(APP_NAME,f"出力ファイルが既にあります。上書きしますか？\n\n{output}"): return
        self.start_button.config(state="disabled"); self.stop_button.config(state="normal"); self.progress_var.set(0); self.completed_frames=0; self.processing_frames=0; self.waiting_frames=self.info.frame_count; self._update_live_counts(); threading.Thread(target=self._worker,daemon=True).start()
    def _worker(self):
        assert self.session and self.info
        try:
            if self.info.height<=MAX_STREAMING_HEIGHT:
                def progress(stats):
                    self.completed_frames=stats.frames; self.processing_frames=0 if stats.frames>=self.info.frame_count else 1; self.waiting_frames=max(0,self.info.frame_count-stats.frames-self.processing_frames); percent=stats.frames/self.info.frame_count*100 if self.info.frame_count else 0; self.root.after(0,self._update_stream_progress,percent,stats.frame_rate)
                run_lowres_streaming(self.info,self.session,progress); self.completed_frames=self.info.frame_count if not self.session.stop_requested else self.completed_frames; self.processing_frames=0; self.waiting_frames=max(0,self.info.frame_count-self.completed_frames); self.root.after(0,self._update_progress,100 if not self.session.stop_requested else self.progress_var.get(),0,self.session.output_path.name)
            else:
                started=time.perf_counter()
                for chunk in self.plan:
                    if self.session.stop_requested: break
                    output=self.session.source_dir/chunk.filename; self.processing_frames=chunk.frame_count; self.waiting_frames=max(0,self.info.frame_count-self.completed_frames-self.processing_frames); self.root.after(0,self._update_live_counts)
                    if not output.exists() or output.stat().st_size==0: extract_chunk(self.session.input_path,output,chunk.start_frame,chunk.end_frame)
                    self.completed_frames+=chunk.frame_count; self.processing_frames=0; speed=self.completed_frames/max(0.001,time.perf_counter()-started); self.root.after(0,self._update_progress,self.completed_frames/self.info.frame_count*100,speed,output.name)
                self.waiting_frames=max(0,self.info.frame_count-self.completed_frames); self.root.after(0,self._update_live_counts)
        except Exception as exc: self.root.after(0,self._worker_error,str(exc)); return
        finally:
            if self.session: self.session.finish_cleanup()
        self.root.after(0,self._worker_done)
    def _update_live_counts(self): self.stats_var.set(f"処理済み：{self.completed_frames:,} 枚  処理中：{self.processing_frames:,} 枚  処理待ち：{self.waiting_frames:,} 枚")
    def _update_stream_progress(self,percent,speed): self.progress_var.set(percent); self._update_live_counts(); self.status_var.set(f"ストリーミング処理中... | {speed:.1f} frame/s")
    def _update_progress(self,percent,speed,filename): self.progress_var.set(percent); self._update_live_counts(); self.status_var.set(f"処理中... | {speed:.1f} frame/s | {filename}")
    def _worker_done(self):
        stopped=self.session.stop_requested if self.session else False; self.status_var.set("停止しました。" if stopped else f"完了: {self.session.output_path if self.session else ''}"); self.start_button.config(state="normal"); self.stop_button.config(state="disabled")
    def _worker_error(self,message): self.status_var.set(f"エラー: {message}"); self.start_button.config(state="normal"); self.stop_button.config(state="disabled"); messagebox.showerror(APP_NAME,message)
    def stop(self):
        if self.session: self.session.stop(); self.status_var.set("停止要求を送信しました。FFmpegを停止しています。")
    def close(self):
        if self.session and self.session.stop_event.is_set() is False:
            self.session.stop()
            self.status_var.set("終了処理中... FFmpegを停止しています。")
            self.root.after(100, self._close_when_stopped)
            return
        self.root.destroy()
    def _close_when_stopped(self):
        if self.session and self.session.stop_event.is_set():
            self.root.destroy()
        else:
            self.root.after(100, self._close_when_stopped)

def main():
    if len(sys.argv)>1:
        input_path=Path(sys.argv[1]).expanduser().resolve(); temp_dir=Path(sys.argv[2]).expanduser().resolve() if len(sys.argv)>2 else None; output_path=Path(sys.argv[3]).expanduser().resolve() if len(sys.argv)>3 else None; console_run(input_path,temp_dir=temp_dir,output_path=output_path); return
    if tk is None: print("Tkinterが利用できません。動画ファイルを引数に指定してください。"); return
    root=tk.Tk(); PySketchifyApp(root); root.mainloop()

if __name__=="__main__": main()