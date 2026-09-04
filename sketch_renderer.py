from __future__ import annotations

"""Fast, internal image-to-strokes renderer for PySketchify.

The renderer does not move a mouse or use an external drawing application. It
analyzes each decoded frame, generates a bounded set of strokes, and rasterizes
those strokes directly into the output frame.
"""

from dataclasses import dataclass
import math

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
except ImportError as exc:  # pragma: no cover - dependency message
    raise ImportError("sketch_renderer requires numpy and Pillow. Install: pip install numpy pillow") from exc


PEN_TYPES = ("●", "■", "▲")


@dataclass(frozen=True)
class PencilSettings:
    width: int = 3
    pen_type: str = "●"
    detail: float = 0.65
    color_strength: float = 0.82
    line_strength: float = 0.88
    analysis_max_size: int = 720

    def normalized(self) -> "PencilSettings":
        return PencilSettings(
            width=max(1, min(64, int(self.width))),
            pen_type=self.pen_type if self.pen_type in PEN_TYPES else "●",
            detail=max(0.1, min(1.0, float(self.detail))),
            color_strength=max(0.0, min(1.0, float(self.color_strength))),
            line_strength=max(0.0, min(1.0, float(self.line_strength))),
            analysis_max_size=max(256, min(1280, int(self.analysis_max_size))),
        )


def _stroke(draw: ImageDraw.ImageDraw, points, width: int, pen_type: str, fill) -> None:
    if len(points) < 2:
        return
    if pen_type == "■":
        draw.line(points, fill=fill, width=width, joint="curve")
        return
    if pen_type == "▲":
        # Draw a small triangle stamp along the stroke direction.
        r = max(1.0, width * 0.5)
        for i in range(0, len(points), max(1, len(points) // 8)):
            x, y = points[i]
            if i + 1 < len(points):
                dx, dy = points[i + 1][0] - x, points[i + 1][1] - y
            elif i:
                dx, dy = x - points[i - 1][0], y - points[i - 1][1]
            else:
                dx, dy = 1.0, 0.0
            length = math.hypot(dx, dy) or 1.0
            ux, uy = dx / length, dy / length
            px, py = -uy, ux
            tip = (x + ux * r * 1.7, y + uy * r * 1.7)
            left = (x - ux * r + px * r, y - uy * r + py * r)
            right = (x - ux * r - px * r, y - uy * r - py * r)
            draw.polygon((tip, left, right), fill=fill)
        return
    # ●
    draw.line(points, fill=fill, width=width, joint="curve")
    radius = max(1, width // 2)
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def _edge_strokes(gray: np.ndarray, settings: PencilSettings, sx: float, sy: float):
    gx = np.zeros_like(gray, dtype=np.float32)
    gy = np.zeros_like(gray, dtype=np.float32)
    gx[:, 1:-1] = gray[:, 2:].astype(np.float32) - gray[:, :-2].astype(np.float32)
    gy[1:-1, :] = gray[2:, :].astype(np.float32) - gray[:-2, :].astype(np.float32)
    mag = np.hypot(gx, gy)
    threshold = np.percentile(mag, 88.0 - 18.0 * settings.detail)
    ys, xs = np.where(mag >= max(8.0, threshold))
    # Bound the number of strokes so high-resolution video remains predictable.
    max_points = max(500, int(gray.size * (0.002 + 0.004 * settings.detail)))
    if len(xs) > max_points:
        stride = max(1, len(xs) // max_points)
        xs, ys = xs[::stride], ys[::stride]
    strokes = []
    step = max(1, int(2.5 - settings.detail * 1.5))
    for x, y in zip(xs[::step], ys[::step]):
        dx, dy = float(gx[y, x]), float(gy[y, x])
        length = math.hypot(dx, dy) or 1.0
        # Tangent direction keeps the stroke flowing along contours.
        tx, ty = -dy / length, dx / length
        span = 2.0 + settings.detail * 5.0
        p1 = ((x - tx * span) * sx, (y - ty * span) * sy)
        p2 = ((x + tx * span) * sx, (y + ty * span) * sy)
        strength = min(255, int(55 + min(1.0, mag[y, x] / 160.0) * 170 * settings.line_strength))
        strokes.append((p1, p2, strength))
    return strokes


def _color_dabs(image: Image.Image, analysis: Image.Image, settings: PencilSettings, sx: float, sy: float):
    arr = np.asarray(analysis, dtype=np.uint8)
    small_h, small_w = arr.shape[:2]
    step = max(3, int(7 - settings.detail * 4))
    dabs = []
    for y in range(step // 2, small_h, step):
        for x in range(step // 2, small_w, step):
            r, g, b = map(int, arr[y, x, :3])
            if settings.color_strength < 1.0:
                r = int(255 + (r - 255) * settings.color_strength)
                g = int(255 + (g - 255) * settings.color_strength)
                b = int(255 + (b - 255) * settings.color_strength)
            dabs.append(((x * sx, y * sy), (r, g, b, 210)))
    return dabs


def render_frame(frame: bytes, index: int, width: int, height: int, settings: PencilSettings) -> bytes:
    settings = settings.normalized()
    expected = width * height * 3
    if len(frame) != expected:
        raise ValueError(f"invalid RGB frame size: {len(frame)} != {expected}")
    source = Image.frombytes("RGB", (width, height), frame)
    scale = min(1.0, settings.analysis_max_size / max(width, height))
    aw = max(1, int(width * scale))
    ah = max(1, int(height * scale))
    analysis = source.resize((aw, ah), Image.Resampling.BILINEAR)
    gray = np.asarray(analysis.convert("L").filter(ImageFilter.GaussianBlur(radius=0.45)), dtype=np.uint8)

    # White paper base mixed with the original image preserves recognizable color
    # while the contour strokes provide the hand-drawn structure.
    base = analysis.copy()
    if settings.color_strength < 1.0:
        white = Image.new("RGB", analysis.size, (255, 255, 255))
        base = Image.blend(white, base, settings.color_strength)
    canvas = base.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    sx, sy = width / aw, height / ah
    line_width = max(1, int(settings.width / max(0.5, scale)))

    for p1, p2, strength in _edge_strokes(gray, settings, sx, sy):
        _stroke(draw, [p1, p2], line_width, settings.pen_type, (25, 25, 25, strength))

    # A sparse color pass makes the result resemble colored pencil rather than
    # a generic grayscale edge filter.
    if settings.color_strength > 0.05:
        for (x, y), fill in _color_dabs(analysis, analysis, settings, sx, sy):
            radius = max(1, line_width // 2)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)

    result = canvas.convert("RGB").resize((width, height), Image.Resampling.BICUBIC)
    return result.tobytes()


def make_processor(settings: PencilSettings):
    normalized = settings.normalized()

    def processor(frame: bytes, index: int, width: int, height: int) -> bytes:
        return render_frame(frame, index, width, height, normalized)

    return processor
