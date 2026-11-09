"""Raw data layer for the WebGL page: 8-bit encoded fields and motion fields as small PNGs.

Rain:  v = 0 dry, else 1 + 24*log2(rate/0.05)  (0.05 .. ~50 mm/h, log scale), stored with 16 bit
       precision as RGB: R = high byte, G = low byte of round(v*256). Bilinear filtering of both
       channels stays exact (linear), so iso-lines of thresholds do not show 8-bit terraces.
Cloud: byte = cover % * 2.55
Flow:  RGB, R/G = 128 + dx/scale, 128 + dy/scale (pixels of the field grid per keyframe step)
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

import flow


def enc_rain(rate: np.ndarray) -> np.ndarray:
    r = np.nan_to_num(rate, nan=0.0)
    e = np.round(1 + 24 * np.log2(np.maximum(r, 0.05) / 0.05))
    return np.where(r < 0.05, 0, np.clip(e, 1, 255)).astype(np.uint8)


def enc_cloud(cc: np.ndarray) -> np.ndarray:
    return np.round(np.clip(np.nan_to_num(cc, nan=0.0), 0, 100) * 2.55).astype(np.uint8)


def png_l(a: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(a, "L").save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def rain_png(rate: np.ndarray) -> bytes:
    r = np.nan_to_num(rate, nan=0.0)
    v = 1 + 24 * np.log2(np.maximum(r, 0.05) / 0.05)
    e = np.where(r < 0.05, 0, np.clip(np.round(v * 256), 256, 65535)).astype(np.uint16)
    rgb = np.zeros(r.shape + (3,), np.uint8)
    rgb[..., 0] = e >> 8
    rgb[..., 1] = e & 255
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def cloud_png(cc: np.ndarray) -> bytes:
    return png_l(enc_cloud(cc))


def flow_png(a: np.ndarray, b: np.ndarray) -> tuple[bytes, float]:
    f = flow.field(a.astype(np.float32), b.astype(np.float32))
    scale = max(float(np.abs(f).max()) / 120.0, 0.05)
    rgb = np.zeros(f.shape[:2] + (3,), np.uint8)
    rgb[..., 0] = np.clip(np.round(128 + f[..., 0] / scale), 0, 255)
    rgb[..., 1] = np.clip(np.round(128 + f[..., 1] / scale), 0, 255)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG", compress_level=6)
    return buf.getvalue(), round(scale, 5)
