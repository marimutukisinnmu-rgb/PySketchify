from __future__ import annotations

import argparse
import shutil
import threading
import time
from pathlib import Path

from console_log import info, ok, section, kv, error

APP_NAME = "PySketchify"
VERSION = "0.5.0"
DEFAULT_OUTPUT_SUFFIX = "_pysketchify"
MAX_STREAMING_HEIGHT = 1080


def default_temp_dir() -> Path:
    return Path(__file__).resolve().parent / "tmp"


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(input_path.stem + DEFAULT_OUTPUT_SUFFIX + ".mp4")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PySketchify CLI launcher")
    parser.add_argument("input", nargs="?", help="入力動画")
    parser.add_argument("--temp", default="", help="一時ファイル場所。未指定なら ./tmp")
    parser.add_argument("--output", default="", help="出力動画")
    return parser.parse_args()


def _run_streaming(input_path: Path, output_path: Path, info_data) -> None:
    from streaming_pipeline import choose_queue_frames, run_streaming_pipeline

    ram_available = None
    try:
        import psutil  # type: ignore
        ram_available = int(psutil.virtual_memory().available)
    except Exception:
        pass

    queue_frames = choose_queue_frames(info_data.width, info_data.height, ram_available)
    threads = 1
    if info_data.width * info_data.height <= 1280 * 720:
        threads = 2

    section("ストリーミング処理")
    kv("方式", "FFmpeg → RAMキュー → 処理 → RAMキュー → FFmpeg")
    kv("対象", f"高さ {info_data.height}px 以下")
    kv("RAMキュー", f"最大 {queue_frames} frame")
    kv("処理スレッド", threads)
    kv("保持", "動画全体をRAMへ展開しない")

    last_report = [0.0]
    started = time.perf_counter()
    stop_event = threading.Event()

    def progress(stats) -> None:
        now = time.perf_counter()
        if now - last_report[0] < 0.25 and stats.frames < info_data.frame_count:
            return
        last_report[0] = now
        total = info_data.frame_count or 0
        percent = (stats.frames / total * 100.0) if total else 0.0
        print(
            f"\r処理済み: {stats.frames:,}/{total:,} 枚 "
            f"({percent:6.2f}%) | {stats.frame_rate:8.1f} frame/s",
            end="",
            flush=True,
        )

    try:
        stats = run_streaming_pipeline(
            input_path=input_path,
            output_path=output_path,
            width=info_data.width,
            height=info_data.height,
            fps=info_data.fps,
            frame_count=info_data.frame_count,
            queue_frames=queue_frames,
            threads=threads,
            progress_callback=progress,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        stop_event.set()
        print("\n")
        raise

    elapsed = max(0.001, time.perf_counter() - started)
    print()
    ok(f"ストリーミング処理完了: {stats.frames:,} frame / {stats.frames / elapsed:.1f} frame/s")


def main() -> int:
    args = parse_args()

    section(f"{APP_NAME} {VERSION}")
    info("CLIを起動しました。")

    if not args.input:
        info("入力動画が指定されていません。GUIから選択してください。")
        try:
            import PySketchify
            PySketchify.main()
            return 0
        except Exception as exc:
            error(str(exc))
            return 1

    input_path = Path(args.input).expanduser().resolve()
    temp_path = Path(args.temp).expanduser().resolve() if args.temp else default_temp_dir()
    output_path = Path(args.output).expanduser().resolve() if args.output else default_output_path(input_path)

    section("入出力設定")
    kv("入力先", input_path)
    kv("一時ファイル場所", temp_path)
    kv("出力先", output_path)

    if not input_path.is_file():
        error(f"入力ファイルがありません: {input_path}")
        return 2

    try:
        import PySketchify
        video_info = PySketchify.probe_video(input_path)
    except Exception as exc:
        error(str(exc))
        return 1

    section("動画解析")
    kv("解像度", f"{video_info.width} × {video_info.height}")
    kv("FPS", video_info.fps)
    kv("総フレーム数", f"{video_info.frame_count:,}")
    kv("音声", f"{video_info.audio_streams} track")
    kv("字幕", f"{video_info.subtitle_streams} track")

    # 1080p以下は、元動画をチャンクごとに何度も読み直さず、
    # FFmpegを1本だけ走らせる有界ストリーミング経路を使う。
    if video_info.height <= MAX_STREAMING_HEIGHT:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(output_path.parent).free
            kv("出力先空き容量", PySketchify.format_bytes(free))
            if free < 1 * 1024**3:
                error("出力先の空き容量が1GB未満です。")
                return 3
            _run_streaming(input_path, output_path, video_info)
            ok(f"出力: {output_path}")
            return 0
        except KeyboardInterrupt:
            info("Ctrl+C により停止しました。", level="WARN")
            return 130
        except Exception as exc:
            error(str(exc))
            return 1

    info("1080pを超えるため、チャンク/タイル処理経路を使用します。")
    try:
        PySketchify.console_run(input_path, temp_dir=temp_path, output_path=output_path)
        ok("処理エンジンが終了しました")
        return 0
    except KeyboardInterrupt:
        info("Ctrl+C により停止しました。", level="WARN")
        return 130
    except Exception as exc:
        error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
