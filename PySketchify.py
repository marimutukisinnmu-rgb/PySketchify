from __future__ import annotations

import json
import os
import shutil
import signal
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

try:
    from sketch_renderer import PencilSettings, PEN_TYPES, make_processor
except ImportError:
    PencilSettings = None
    PEN_TYPES = ("●", "■", "▲")
    make_processor = None

APP_NAME = "PySketchify"
VERSION = "0.6.0"
DEFAULT_MARGIN_GB = 0.9
DEFAULT_OUTPUT_SUFFIX = "_pysketchify"
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
class ResourceSnapshot:
    ram_total: Optional[int]
    ram_available: Optional[int]
    vram_dedicated_total: Optional[int]
    vram_dedicated_used: Optional[int]
    vram_shared_total: Optional[int]
    vram_shared_used: Optional[int]
    gpu_names: tuple[str, ...] = ()

def default_temp_dir() -> Path: return Path(__file__).resolve().parent / "tmp"
def default_output_path(input_path: Path) -> Path: return input_path.with_name(input_path.stem + DEFAULT_OUTPUT_SUFFIX + ".mp4")
def validate_input_path(path: Path) -> None:
    if not path.exists(): raise FileNotFoundError(f"入力ファイルがありません: {path}")
    if not path.is_file(): raise ValueError(f"入力先がファイルではありません: {path}")
    if path.suffix.lower() not in SUPPORTED_INPUT_EXTENSIONS: raise ValueError(f"対応していない入力形式です: {path.suffix or '(拡張子なし)'}")
def find_executable(name: str) -> Optional[str]: return shutil.which(name)
def require_ffmpeg() -> tuple[str, str]:
    ffmpeg, ffprobe = find_executable("ffmpeg"), find_executable("ffprobe")
    if not ffmpeg or not ffprobe: raise RuntimeError("FFmpeg / FFprobe が見つかりません。PATHに ffmpeg と ffprobe を追加してください。")
    return ffmpeg, ffprobe
def run_command(args: list[str]) -> subprocess.CompletedProcess[str]: return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
def _ratio_to_float(value: str) -> float:
    if not value or value in {"0/0", "N/A"}: return 0.0
    if "/" in value:
        a,b=value.split("/",1)
        try:return float(a)/float(b)
        except (ValueError,ZeroDivisionError):return 0.0
    try:return float(value)
    except ValueError:return 0.0

def probe_video(path: Path) -> VideoInfo:
    validate_input_path(path); _,ffprobe=require_ffmpeg(); result=run_command([ffprobe,"-v","error","-print_format","json","-show_streams","-show_format",str(path)])
    if result.returncode!=0: raise RuntimeError(f"FFprobe failed:\n{result.stderr.strip()}")
    try:data=json.loads(result.stdout)
    except json.JSONDecodeError as exc:raise RuntimeError("FFprobeのJSON解析に失敗しました。") from exc
    streams=data.get("streams",[]); video=next((s for s in streams if s.get("codec_type")=="video"),None)
    if video is None:raise RuntimeError("映像ストリームが見つかりません。")
    width,height=int(video.get("width") or 0),int(video.get("height") or 0); fps=_ratio_to_float(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    duration=float(video.get("duration") or data.get("format",{}).get("duration") or 0.0); frame_count=int(video.get("nb_frames")) if video.get("nb_frames") and str(video.get("nb_frames")).isdigit() else (max(1,round(fps*duration)) if fps>0 and duration>0 else 0)
    if width<=0 or height<=0:raise RuntimeError("動画の解像度を取得できませんでした。")
    if fps<=0:raise RuntimeError("動画のFPSを取得できませんでした。")
    return VideoInfo(str(path),width,height,fps,duration,frame_count,str(video.get("pix_fmt") or "unknown"),str(video.get("codec_name") or "unknown"),sum(1 for s in streams if s.get("codec_type")=="audio"),sum(1 for s in streams if s.get("codec_type")=="subtitle"))

def format_bytes(value:int|float)->str:
    value=float(max(0,value)); units=("B","KB","MB","GB","TB","PB"); i=0
    while value>=1024 and i<len(units)-1:value/=1024;i+=1
    return f"{value:.2f} {units[i]}"
def get_ram_snapshot()->tuple[Optional[int],Optional[int]]:
    try:
        import psutil
        vm=psutil.virtual_memory();return int(vm.total),int(vm.available)
    except Exception:return None,None
def _powershell_json(script:str)->object|None:
    if os.name!="nt":return None
    result=run_command(["powershell","-NoProfile","-Command",script])
    if result.returncode!=0 or not result.stdout.strip():return None
    try:return json.loads(result.stdout)
    except json.JSONDecodeError:return None
def get_resource_snapshot()->ResourceSnapshot:
    ram_total,ram_available=get_ram_snapshot()
    if os.name!="nt":return ResourceSnapshot(ram_total,ram_available,None,None,None,None,())
    data=_powershell_json("Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress")
    if not data:return ResourceSnapshot(ram_total,ram_available,None,None,None,None,())
    if isinstance(data,dict):data=[data]
    total=0;names=[]
    for gpu in data if isinstance(data,list) else []:
        if not isinstance(gpu,dict):continue
        try:total+=int(gpu.get("AdapterRAM") or 0)
        except (TypeError,ValueError):pass
        name=str(gpu.get("Name") or "").strip()
        if name:names.append(name)
    return ResourceSnapshot(ram_total,ram_available,total or None,None,None,None,tuple(names))
def cleanup_temp(temp_dir:Path)->None:
    if not temp_dir.exists():return
    try:shutil.rmtree(temp_dir)
    except OSError:pass

class ProcessingSession:
    def __init__(self,input_path:Path,temp_dir:Optional[Path]=None,output_path:Optional[Path]=None):
        self.input_path=input_path;self.temp_dir=Path(temp_dir).expanduser().resolve() if temp_dir else default_temp_dir();self.output_path=Path(output_path).expanduser().resolve() if output_path else default_output_path(input_path);self.work_root=self.temp_dir/(input_path.stem+"_pysketchify");self.temp_dir.mkdir(parents=True,exist_ok=True);self.work_root.mkdir(parents=True,exist_ok=True);self.stop_requested=False;self.stop_event=threading.Event()
    def stop(self):self.stop_requested=True;self.stop_event.set()
    def finish_cleanup(self):cleanup_temp(self.temp_dir)

def make_pencil_settings(width_var,pen_var):
    if PencilSettings is None:raise RuntimeError("numpy / Pillow が必要です。pip install numpy pillow")
    return PencilSettings(width=int(width_var.get()),pen_type=pen_var.get()).normalized()
def run_video(info:VideoInfo,session:ProcessingSession,settings,progress_callback=None,copy_audio=True):
    if run_streaming_pipeline is None or make_processor is None:raise RuntimeError("必要なモジュールを読み込めません。streaming_pipeline.py / sketch_renderer.py を確認してください。")
    resources=get_resource_snapshot();q=choose_queue_frames(info.width,info.height,resources.ram_available) if choose_queue_frames else 1
    if info.width*info.height*3>30_000_000:q=1
    print(f"[STREAM] {info.width}x{info.height} | queue={q} frame | RAM空き={format_bytes(resources.ram_available) if resources.ram_available else '不明'} | audio_copy={'ON' if copy_audio else 'OFF'}")
    processor=make_processor(settings);last=[0.0]
    def callback(stats):
        if progress_callback:progress_callback(stats)
        now=time.perf_counter()
        if now-last[0]>=0.25 or (info.frame_count and stats.frames>=info.frame_count):
            last[0]=now;print(f"処理中... | {stats.frames:,}/{info.frame_count:,} frame | {stats.frame_rate:.1f} frame/s")
    return run_streaming_pipeline(session.input_path,session.output_path,info.width,info.height,info.fps,info.frame_count,processor=processor,queue_frames=q,threads=1,progress_callback=callback,stop_event=session.stop_event,copy_audio=copy_audio)

class PySketchifyApp:
    def __init__(self,root:tk.Tk):
        self.root=root;self.root.title(f"{APP_NAME} {VERSION}");self.root.geometry("1080x800");self.session=None;self.info=None;self.completed_frames=0;self.processing_frames=0;self.waiting_frames=0
        self.input_var=tk.StringVar();self.temp_var=tk.StringVar(value=str(default_temp_dir()));self.output_var=tk.StringVar();self.status_var=tk.StringVar(value="入力動画を選択してください。");self.progress_var=tk.DoubleVar(value=0);self.stats_var=tk.StringVar(value="処理済み：0 枚  処理中：0 枚  処理待ち：0 枚");self.resource_var=tk.StringVar();self.info_var=tk.StringVar(value="未選択");self.pen_width_var=tk.IntVar(value=3);self.pen_type_var=tk.StringVar(value="●");self.copy_audio_var=tk.BooleanVar(value=False);self._build();self.root.protocol("WM_DELETE_WINDOW",self.close)
    def _path_row(self,parent,label,variable,command,button_text="参照"):
        row=ttk.Frame(parent);row.pack(fill="x",pady=4);ttk.Label(row,text=label,width=22).pack(side="left");ttk.Entry(row,textvariable=variable).pack(side="left",fill="x",expand=True);ttk.Button(row,text=button_text,command=command).pack(side="left",padx=(8,0))
    def _build(self):
        main=ttk.Frame(self.root,padding=14);main.pack(fill="both",expand=True);ttk.Label(main,text=APP_NAME,font=("Segoe UI",20,"bold")).pack(anchor="w");ttk.Label(main,text="動画 → 内部自動お絵描き → 手描き風動画",font=("Segoe UI",10)).pack(anchor="w",pady=(0,12))
        paths=ttk.LabelFrame(main,text="入出力",padding=10);paths.pack(fill="x");self._path_row(paths,"入力先",self.input_var,self.choose_input,"動画を選択");self._path_row(paths,"一時ファイル場所（任意）",self.temp_var,self.choose_temp,"変更");ttk.Label(paths,text="未指定なら (pyがある場所)/tmp。処理終了後にtmpを削除します。").pack(anchor="w",padx=(22,0));self._path_row(paths,"出力先",self.output_var,self.choose_output,"保存先")
        controls=ttk.LabelFrame(main,text="ペン設定 / 音声",padding=10);controls.pack(fill="x",pady=10);ttk.Button(controls,text="ペンの太さ(px)",command=self.open_pen_width_dialog).pack(side="left");ttk.Label(controls,textvariable=self.pen_width_var,width=6).pack(side="left",padx=(6,18));ttk.Label(controls,text="ペンの種類").pack(side="left");ttk.Combobox(controls,textvariable=self.pen_type_var,values=PEN_TYPES,state="readonly",width=6).pack(side="left",padx=6);ttk.Label(controls,text="●=丸  ■=角  ▲=三角").pack(side="left",padx=10);ttk.Checkbutton(controls,text="Hzを合成",variable=self.copy_audio_var).pack(side="left",padx=(18,0))
        info_frame=ttk.LabelFrame(main,text="動画情報 / リソース",padding=10);info_frame.pack(fill="x");ttk.Label(info_frame,textvariable=self.info_var).pack(anchor="w");ttk.Label(info_frame,textvariable=self.resource_var).pack(anchor="w",pady=(6,0))
        preview=ttk.LabelFrame(main,text="処理中 / プレビュー",padding=10);preview.pack(fill="both",expand=True);ttk.Label(preview,text="処理中の最新フレームをここへ表示します。\n内部で解析 → ストローク生成 → 描画 → FFmpegエンコードを行います。",anchor="center",justify="center").pack(fill="both",expand=True)
        ttk.Progressbar(main,variable=self.progress_var,maximum=100).pack(fill="x",pady=(10,4));ttk.Label(main,textvariable=self.stats_var).pack(anchor="w");ttk.Label(main,textvariable=self.status_var).pack(anchor="w",pady=(4,8));buttons=ttk.Frame(main);buttons.pack(fill="x");self.start_button=ttk.Button(buttons,text="処理開始",command=self.start);self.start_button.pack(side="left");self.stop_button=ttk.Button(buttons,text="停止",command=self.stop,state="disabled");self.stop_button.pack(side="left",padx=8)
    def open_pen_width_dialog(self):
        win=tk.Toplevel(self.root);win.title("ペンの太さ(px)");win.resizable(False,False);frame=ttk.Frame(win,padding=16);frame.pack(fill="both",expand=True);value=tk.IntVar(value=self.pen_width_var.get());ttk.Label(frame,text="ペンの太さ(px)").pack(anchor="w");scale=ttk.Scale(frame,from_=1,to=64,orient="horizontal",command=lambda v:value.set(round(float(v))));scale.set(value.get());scale.pack(fill="x",pady=8);value_label=ttk.Label(frame,text="3 px");value_label.pack();canvas=tk.Canvas(frame,width=300,height=90,highlightthickness=1);canvas.pack(pady=10)
        def refresh(*_):
            n=max(1,min(64,value.get()));value_label.config(text=f"{n} px");canvas.delete("all");r=max(1,min(38,n));cx,cy=150,45;canvas.create_oval(cx-r,cy-r,cx+r,cy+r,fill="black",outline="black")
        def on_scale(v):value.set(round(float(v)));refresh()
        scale.config(command=on_scale);refresh();ttk.Label(frame,text="プレビューサイズ").pack();ttk.Button(frame,text="OK",command=lambda:(self.pen_width_var.set(value.get()),win.destroy())).pack(pady=(4,0))
    def choose_input(self):
        path=filedialog.askopenfilename(title="入力動画を選択",filetypes=[("Video files","*.mp4 *.webm *.mov *.wmv *.avi *.mkv *.mts *.m2ts *.avchd"),("All files","*.*")])
        if path:selected=Path(path).resolve();self.input_var.set(str(selected));self.output_var.set(str(default_output_path(selected)));self.analyze_input()
    def choose_temp(self):
        path=filedialog.askdirectory(title="一時ファイル場所を選択")
        if path:self.temp_var.set(str(Path(path).resolve()));self.status_var.set("一時ファイル場所を変更しました。")
    def choose_output(self):
        current=Path(self.output_var.get()) if self.output_var.get() else None;path=filedialog.asksaveasfilename(title="出力先を選択",initialdir=str(current.parent) if current else str(Path.home()),initialfile=current.name if current else "output_pysketchify.mp4",defaultextension=".mp4",filetypes=[("MP4 video","*.mp4"),("All files","*.*")])
        if path:self.output_var.set(str(Path(path).resolve()))
    def analyze_input(self):
        try:
            self.info=probe_video(Path(self.input_var.get()));resources=get_resource_snapshot();temp=Path(self.temp_var.get()) if self.temp_var.get() else default_temp_dir();temp.mkdir(parents=True,exist_ok=True);free=shutil.disk_usage(temp).free;audio_state="ON" if self.copy_audio_var.get() else "OFF";self.info_var.set(f"{self.info.width} × {self.info.height} | {self.info.fps:.6g} FPS | {self.info.frame_count:,} frame | audio {self.info.audio_streams} | subtitle {self.info.subtitle_streams}\n内部自動お絵描き: ON | Hz合成: {audio_state} | 一時先空き: {format_bytes(free)}");self.resource_var.set(f"GPU: {', '.join(resources.gpu_names) if resources.gpu_names else '検出情報なし'} | RAM空き: {format_bytes(resources.ram_available) if resources.ram_available is not None else '不明'} | 専用VRAM総量: {format_bytes(resources.vram_dedicated_total) if resources.vram_dedicated_total is not None else '不明'}");self.status_var.set("解析完了。ペン設定と音声設定を確認して処理開始できます。")
        except Exception as exc:self.info=None;self.status_var.set(f"解析エラー: {exc}");messagebox.showerror(APP_NAME,str(exc))
    def start(self):
        if not self.input_var.get():self.choose_input()
        if not self.input_var.get():return
        if not self.info:self.analyze_input()
        if not self.info:return
        output=Path(self.output_var.get()).expanduser().resolve() if self.output_var.get() else default_output_path(Path(self.input_var.get()))
        if output.exists() and not messagebox.askyesno(APP_NAME,f"出力ファイルが既にあります。上書きしますか？\n\n{output}"):return
        try:settings=make_pencil_settings(self.pen_width_var,self.pen_type_var)
        except Exception as exc:messagebox.showerror(APP_NAME,str(exc));return
        temp=Path(self.temp_var.get()).expanduser().resolve() if self.temp_var.get() else default_temp_dir();self.session=ProcessingSession(Path(self.input_var.get()),temp_dir=temp,output_path=output);self.start_button.config(state="disabled");self.stop_button.config(state="normal");self.progress_var.set(0);self.completed_frames=0;self.processing_frames=0;self.waiting_frames=self.info.frame_count;self._update_live_counts();threading.Thread(target=self._worker,args=(settings,not self.copy_audio_var.get()),daemon=True).start()
    def _worker(self,settings,copy_audio):
        assert self.session and self.info
        try:
            def progress(stats):
                self.completed_frames=stats.frames;self.processing_frames=0 if stats.frames>=self.info.frame_count else 1;self.waiting_frames=max(0,self.info.frame_count-self.completed_frames-self.processing_frames);percent=stats.frames/self.info.frame_count*100 if self.info.frame_count else 0;self.root.after(0,self._update_stream_progress,percent,stats.frame_rate)
            run_video(self.info,self.session,settings,progress,copy_audio=copy_audio)
            if not self.session.stop_requested:self.completed_frames=self.info.frame_count
            self.processing_frames=0;self.waiting_frames=max(0,self.info.frame_count-self.completed_frames);self.root.after(0,self._worker_done)
        except Exception as exc:self.root.after(0,self._worker_error,str(exc))
        finally:self.session.finish_cleanup()
    def _update_live_counts(self):self.stats_var.set(f"処理済み：{self.completed_frames:,} 枚  処理中：{self.processing_frames:,} 枚  処理待ち：{self.waiting_frames:,} 枚")
    def _update_stream_progress(self,percent,speed):
        self.progress_var.set(percent);self._update_live_counts();eta_text="ETA: --:--"
        if speed>0 and self.info is not None:
            remaining=max(0,self.info.frame_count-self.completed_frames);eta_seconds=remaining/speed;minutes,seconds=divmod(int(eta_seconds+0.5),60);hours,minutes=divmod(minutes,60);eta_text=f"ETA: {hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"ETA: {minutes:02d}:{seconds:02d}"
        self.status_var.set(f"内部自動お絵描き中... | {speed:.1f} frame/s | {eta_text}")
    def _worker_done(self):self.status_var.set("完了: "+(str(self.session.output_path) if self.session else ""));self.start_button.config(state="normal");self.stop_button.config(state="disabled")
    def _worker_error(self,message):self.status_var.set(f"エラー: {message}");self.start_button.config(state="normal");self.stop_button.config(state="disabled");messagebox.showerror(APP_NAME,message)
    def stop(self):
        if self.session:self.session.stop();self.status_var.set("停止要求を送信しました。Worker / FFmpegを停止しています。")
    def close(self):
        if self.session and not self.session.stop_event.is_set():self.session.stop();self.status_var.set("終了処理中... Worker / FFmpegを停止しています。");self.root.after(100,self._close_when_stopped);return
        self.root.destroy()
    def _close_when_stopped(self):
        if self.session and self.session.stop_event.is_set():self.root.destroy()
        else:self.root.after(100,self._close_when_stopped)

def main():
    if len(sys.argv)>1:
        input_path=Path(sys.argv[1]).expanduser().resolve();temp_dir=Path(sys.argv[2]).expanduser().resolve() if len(sys.argv)>2 else None;output_path=Path(sys.argv[3]).expanduser().resolve() if len(sys.argv)>3 else None
        if PencilSettings is None:raise RuntimeError("numpy / Pillow が必要です。pip install numpy pillow")
        info=probe_video(input_path);session=ProcessingSession(input_path,temp_dir=temp_dir,output_path=output_path)
        try:run_video(info,session,PencilSettings())
        finally:session.stop();session.finish_cleanup()
        return
    if tk is None:print("Tkinterが利用できません。動画ファイルを引数に指定してください。");return
    root=tk.Tk();app=PySketchifyApp(root)
    def handle_sigint(signum,frame):
        if app.session is not None:print("[STOP] Ctrl+C を受信しました。停止処理を開始します。");app.stop()
        else:print("[STOP] Ctrl+C を受信しました。終了します。");root.after(0,root.destroy)
    previous_sigint=signal.getsignal(signal.SIGINT);signal.signal(signal.SIGINT,handle_sigint)
    try:root.mainloop()
    except KeyboardInterrupt:
        app.stop()
        try:root.update()
        except Exception:pass
    finally:
        signal.signal(signal.SIGINT,previous_sigint)
        if app.session is not None:app.session.stop()

if __name__=="__main__":main()
