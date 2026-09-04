from __future__ import annotations

import argparse
from pathlib import Path

from console_log import info, ok, section, kv, error

APP_NAME = "PySketchify"
VERSION = "0.4.0"
DEFAULT_OUTPUT_SUFFIX = "_pysketchify"


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

    ok("入力ファイルを確認しました")
    info("PySketchify.py の処理エンジンを起動します。")

    try:
        import PySketchify
        # The existing engine accepts the input path directly.  The selected
        # paths are printed here so the CLI always exposes the run configuration.
        PySketchify.console_run(input_path)
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
