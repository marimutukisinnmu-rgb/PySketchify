import subprocess
import sys
import tkinter as tk


def probe_video(path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "default=noprint_wrappers=1",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)

    info = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key] = value.strip()

    return int(info["width"]), int(info["height"]), info["r_frame_rate"]


def main():
    if len(sys.argv) < 2:
        print("使い方: python test.py input.mp4")
        sys.exit(1)

    video = sys.argv[1]
    width, height, fps = probe_video(video)
    frame_size = width * height * 3

    print("=" * 60)
    print("FFmpeg → raw RGB24 → そのまま返却テスト")
    print(f"width      = {width}")
    print(f"height     = {height}")
    print(f"r_frame_rate = {fps}")
    print(f"width × height × 3 = {frame_size} bytes/frame")
    print("=" * 60)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", video,
        "-map", "0:v:0",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=frame_size * 2,
    )

    frame_number = 0
    frame_100 = None

    try:
        while True:
            data = process.stdout.read(frame_size)
            if not data:
                break

            frame_number += 1

            if frame_number == 100:
                # 描画処理を一切せず、受信したdataをそのまま返す。
                output_data = data
                frame_100 = output_data

                print("\n--- 100 frame ---")
                print(f"受信 data            = {len(data)} bytes")
                print(f"入力 frame_size      = {frame_size} bytes")
                print(f"output_data          = {len(output_data)} bytes")
                print(f"data is output_data  = {data is output_data}")
                print(f"data == output_data  = {data == output_data}")
                print("描画処理             = なし")
                print("変換処理             = なし")
                print("返却                 = dataそのまま")
                print("-------------------")
                break
    finally:
        process.stdout.close()
        process.terminate()
        process.wait()

    if frame_100 is None:
        print("100 frame目を取得できませんでした。")
        return

    # output_dataを加工せず、そのまま表示する。
    root = tk.Tk()
    root.title(f"PySketchify test.py - frame 100 ({width}x{height})")

    ppm = f"P6\n{width} {height}\n255\n".encode("ascii") + frame_100
    image = tk.PhotoImage(data=ppm, format="PPM")

    label = tk.Label(root, image=image)
    label.pack()
    root.mainloop()


if __name__ == "__main__":
    main()
