from __future__ import annotations

"""Optional GPU backend for PySketchify.

The backend is deliberately optional: CPU-only installations keep working without
PyTorch. CUDA is preferred on NVIDIA; DirectML is used on Windows for compatible
AMD/Intel/other DirectX 12 GPUs when torch-directml is installed.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GPUInfo:
    backend: str
    name: str
    device: object | None = None


def detect_gpu() -> GPUInfo:
    requested = os.environ.get("PYSKETCHIFY_GPU", "auto").strip().lower()
    if requested in {"0", "off", "false", "cpu"}:
        return GPUInfo("cpu", "CPU only")

    try:
        import torch
    except Exception:
        torch = None

    if torch is not None and requested in {"auto", "cuda", "nvidia"}:
        try:
            if torch.cuda.is_available():
                return GPUInfo("cuda", torch.cuda.get_device_name(0), torch.device("cuda"))
        except Exception:
            pass
        if requested in {"cuda", "nvidia"}:
            return GPUInfo("cpu", "CUDA unavailable")

    if os.name == "nt" and requested in {"auto", "directml", "dml", "amd", "intel"}:
        try:
            import torch_directml
            device = torch_directml.device()
            return GPUInfo("directml", "DirectML GPU", device)
        except Exception:
            pass

    return GPUInfo("cpu", "CPU only")


def gpu_available() -> bool:
    return detect_gpu().backend != "cpu"


def edge_magnitude(gray):
    """Return Sobel magnitude as a NumPy uint8 array, using the detected GPU."""
    import numpy as np

    info = detect_gpu()
    if info.backend == "cpu":
        gx = np.zeros_like(gray, dtype=np.float32)
        gy = np.zeros_like(gray, dtype=np.float32)
        gx[:, 1:-1] = gray[:, 2:].astype(np.float32) - gray[:, :-2].astype(np.float32)
        gy[1:-1, :] = gray[2:, :].astype(np.float32) - gray[:-2, :].astype(np.float32)
        return np.hypot(gx, gy)

    try:
        import torch
        x = torch.from_numpy(gray.astype(np.float32, copy=False)).unsqueeze(0).unsqueeze(0).to(info.device)
        kernel_x = torch.tensor([[-1.0, 0.0, 1.0]], device=info.device).reshape(1, 1, 1, 3)
        kernel_y = torch.tensor([[-1.0], [0.0], [1.0]], device=info.device).reshape(1, 1, 3, 1)
        gx = torch.nn.functional.conv2d(x, kernel_x, padding=(0, 1))
        gy = torch.nn.functional.conv2d(x, kernel_y, padding=(1, 0))
        mag = torch.sqrt(gx * gx + gy * gy).squeeze().detach().cpu().numpy()
        return mag
    except Exception:
        # A broken/unsupported GPU operator must never make a video unusable.
        gx = np.zeros_like(gray, dtype=np.float32)
        gy = np.zeros_like(gray, dtype=np.float32)
        gx[:, 1:-1] = gray[:, 2:].astype(np.float32) - gray[:, :-2].astype(np.float32)
        gy[1:-1, :] = gray[2:, :].astype(np.float32) - gray[:-2, :].astype(np.float32)
        return np.hypot(gx, gy)
