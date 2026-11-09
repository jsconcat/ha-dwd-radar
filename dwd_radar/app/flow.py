"""Motion-compensated intermediate frames (optical flow, OpenCV DIS).

Between two precipitation fields A (t=0) and B (t=1) the motion field F (A -> B) is
estimated on a log-scaled image. Intermediate frames at tau in (0,1) are built by
warping A forward by tau*F and B backward by (1-tau)*F and blending both.
"""
from __future__ import annotations

import threading

import cv2
import numpy as np

cv2.setNumThreads(1)                 # radar and model threads both use this module; keep CPU load bounded
_local = threading.local()
_lock = threading.Lock()             # OpenCV objects are not thread-safe; serialize all calls


def _engine():
    dis = getattr(_local, "dis", None)
    if dis is None:
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        dis.setUseSpatialPropagation(True)
        _local.dis = dis
    return dis


def _to_u8(r: np.ndarray) -> np.ndarray:
    v = np.log10(np.maximum(np.nan_to_num(r, nan=0.0), 0.03) / 0.03) / np.log10(100 / 0.03)
    return np.clip(v * 255, 0, 255).astype(np.uint8)


def has_rain(r: np.ndarray, thr: float = 0.08) -> bool:
    return bool(np.nanmax(r) >= thr) if r.size else False


def intermediates(a: np.ndarray, b: np.ndarray, taus: list[float]) -> list[np.ndarray]:
    """Return motion-interpolated fields for each tau (0 < tau < 1)."""
    a = np.nan_to_num(a, nan=0.0).astype(np.float32)
    b = np.nan_to_num(b, nan=0.0).astype(np.float32)
    h, w = a.shape
    if not (has_rain(a) or has_rain(b)):
        return [np.zeros_like(a) for _ in taus]
    with _lock:
        return _interp(a, b, taus, h, w)


def _interp(a, b, taus, h, w):
    flow = _engine().calc(_to_u8(a), _to_u8(b), None)          # displacement A -> B (px)
    # smooth the flow a bit: large-scale motion, no jitter
    flow = cv2.GaussianBlur(flow, (0, 0), 3)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    out = []
    for t in taus:
        ax = gx - t * flow[..., 0]
        ay = gy - t * flow[..., 1]
        bx = gx + (1 - t) * flow[..., 0]
        by = gy + (1 - t) * flow[..., 1]
        wa = cv2.remap(a, ax, ay, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        wb = cv2.remap(b, bx, by, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        out.append((1 - t) * wa + t * wb)
    return out


def field(a: np.ndarray, b: np.ndarray, down: int = 8) -> np.ndarray:
    """Motion field A -> B in pixels of the input grid, downsampled by `down` (shape h/down x w/down x 2)."""
    h, w = a.shape
    with _lock:
        f = _engine().calc(_to_u8(a), _to_u8(b), None)
        f = cv2.GaussianBlur(f, (0, 0), 3)
        fh, fw = (h + down - 1) // down, (w + down - 1) // down
        return cv2.resize(f, (fw, fh), interpolation=cv2.INTER_AREA)
