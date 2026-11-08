"""Reprojection (DWD polar stereographic -> Web Mercator), smoothing and colouring."""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass

import numpy as np
import pyproj
from PIL import Image
from scipy import ndimage

from dwd import Grid

log = logging.getLogger("dwd_radar.render")

R_MERC = 6378137.0


def merc_x(lon: float) -> float:
    return math.radians(lon) * R_MERC


def merc_y(lat: float) -> float:
    return R_MERC * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def inv_merc_x(x: float) -> float:
    return math.degrees(x / R_MERC)


def inv_merc_y(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y / R_MERC)) - math.pi / 2)


# Colour scale (mm/h -> RGBA), interpolated in log10(rate).
# Turquoise for light rain -> green -> yellow -> orange -> red -> magenta (extreme).
STOPS = [
    (0.08, (130, 225, 235, 0)),
    (0.2,  (110, 218, 228, 140)),
    (0.5,  (64, 200, 212, 185)),
    (1.0,  (40, 182, 160, 200)),
    (2.0,  (62, 178, 72, 212)),
    (4.0,  (165, 208, 40, 222)),
    (7.0,  (250, 222, 40, 228)),
    (12.0, (250, 160, 30, 234)),
    (20.0, (236, 72, 36, 240)),
    (35.0, (190, 22, 60, 245)),
    (60.0, (165, 32, 150, 250)),
]
LUT_MIN, LUT_MAX, LUT_N = math.log10(0.08), math.log10(100.0), 1024


def build_lut(opacity: float = 1.0) -> np.ndarray:
    xs = np.array([math.log10(s[0]) for s in STOPS])
    cols = np.array([s[1] for s in STOPS], dtype=np.float64)
    grid = np.linspace(LUT_MIN, LUT_MAX, LUT_N)
    lut = np.stack([np.interp(grid, xs, cols[:, i]) for i in range(4)], axis=1)
    lut[:, 3] *= opacity
    return np.clip(np.round(lut), 0, 255).astype(np.uint8)


# WarnWetter-like discrete classes (mm/h lower bound, RGB). First three sampled from the WarnWetter app.
CLASSES = [
    (0.1, (126, 251, 253)),   # cyan
    (0.2, (96, 201, 158)),    # mint
    (0.5, (67, 151, 66)),     # green
    (1.0, (36, 112, 44)),     # dark green
    (2.0, (247, 232, 52)),    # yellow
    (5.0, (247, 156, 32)),    # orange
    (10.0, (226, 44, 36)),    # red
    (20.0, (150, 20, 30)),    # dark red
    (50.0, (196, 52, 196)),   # magenta
]
CLASS_ALPHA = 0.88


def classify_png(rate: np.ndarray, opacity: float = 1.0, supersample: int = 2) -> bytes:
    """Discrete colour classes with smooth (anti-aliased) hard edges.

    The field is upsampled before classification and the result box-filtered back down,
    so class borders follow smooth curves instead of pixel steps.
    """
    r = np.nan_to_num(rate, nan=0.0).astype(np.float32)
    h, w = r.shape
    if supersample > 1:
        lr = np.log(np.maximum(r, 0.0) + 0.02)
        lr = ndimage.zoom(lr, supersample, order=1, mode="nearest", grid_mode=True)
        r = np.exp(lr) - 0.02
    bounds = np.array([c[0] for c in CLASSES], dtype=np.float32)
    idx = np.digitize(r, bounds)                      # 0 = below first class
    lut = np.zeros((len(CLASSES) + 1, 4), np.uint8)
    for i, (_, rgb) in enumerate(CLASSES, start=1):
        lut[i, :3] = rgb
        lut[i, 3] = int(round(255 * CLASS_ALPHA * opacity))
    rgba = lut[idx]
    img = Image.fromarray(rgba, "RGBA")
    if supersample > 1:
        # premultiplied downsampling avoids dark fringes at the transparent border
        a = rgba[..., 3:4].astype(np.float32) / 255.0
        pm = np.dstack([rgba[..., :3] * a, a * 255]).astype(np.float32)
        pm = pm.reshape(h, supersample, w, supersample, 4).mean(axis=(1, 3))
        al = pm[..., 3:4] / 255.0
        rgb = np.where(al > 0, pm[..., :3] / np.maximum(al, 1e-6), 0)
        img = Image.fromarray(np.dstack([rgb, pm[..., 3:4]]).clip(0, 255).round().astype(np.uint8), "RGBA")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@dataclass
class Extent:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


class Renderer:
    """Maps DWD grid cells onto a fixed Web Mercator raster.

    The source coordinate of every target pixel is computed once (per grid
    geometry); each frame then only needs filtering + one map_coordinates call.
    """

    def __init__(self, extent: Extent, res_m: float, smooth_km: float = 1.0, opacity: float = 1.0,
                 style: str = "classes"):
        self.res = float(res_m)
        x0, x1 = merc_x(extent.lon_min), merc_x(extent.lon_max)
        y0, y1 = merc_y(extent.lat_min), merc_y(extent.lat_max)
        self.nx = max(2, int(round((x1 - x0) / self.res)))
        self.ny = max(2, int(round((y1 - y0) / self.res)))
        # snap the extent to whole pixels (keeps overlay bounds exact)
        x1 = x0 + self.nx * self.res
        y0 = y1 - self.ny * self.res
        self.merc_box = (x0, y0, x1, y1)
        self.bounds = [[inv_merc_y(y0), inv_merc_x(x0)], [inv_merc_y(y1), inv_merc_x(x1)]]
        self.smooth_km = smooth_km
        self.lut = build_lut(opacity)
        self.opacity = opacity
        self.style = style
        self._grid_key = None

    # ---- geometry -----------------------------------------------------
    def _prepare(self, grid: Grid) -> None:
        if self._grid_key == grid.key():
            return
        x0, y0, x1, y1 = self.merc_box
        xs = x0 + (np.arange(self.nx) + 0.5) * self.res
        ys = y1 - (np.arange(self.ny) + 0.5) * self.res
        X, Y = np.meshgrid(xs, ys)
        tr = pyproj.Transformer.from_crs("EPSG:3857", pyproj.CRS.from_proj4(grid.projdef), always_xy=True)
        PX, PY = tr.transform(X, Y)
        col = (PX - grid.ul_x) / grid.xscale - 0.5     # cell centres
        row = (grid.ul_y - PY) / grid.yscale - 0.5
        m = 6
        r0 = max(0, int(math.floor(row.min())) - m); r1 = min(grid.ysize, int(math.ceil(row.max())) + m + 1)
        c0 = max(0, int(math.floor(col.min())) - m); c1 = min(grid.xsize, int(math.ceil(col.max())) + m + 1)
        if r0 >= r1 or c0 >= c1:
            raise ValueError("Ausschnitt liegt außerhalb des DWD-Rasters")
        self.crop = (r0, r1, c0, c1)
        self.coords = np.stack([row - r0, col - c0]).astype(np.float32)
        self.sigma_px = self.smooth_km * 1000.0 / grid.xscale
        self._grid_key = grid.key()
        log.info("Raster vorbereitet: Ziel %dx%d px @ %.0f m, Quelle Zeilen %d-%d Spalten %d-%d",
                 self.nx, self.ny, self.res, r0, r1, c0, c1)

    # ---- per frame ----------------------------------------------------
    def rate_on_target(self, grid: Grid, rate: np.ndarray) -> np.ndarray:
        """Smoothed rain rate (mm/h) on the Mercator raster; NaN outside coverage."""
        self._prepare(grid)
        r0, r1, c0, c1 = self.crop
        src = rate[r0:r1, c0:c1]
        valid = np.isfinite(src)
        # smooth in log space: keeps cores, softens pixel edges
        v = np.log(np.where(valid, src, 0.0) + 0.05)
        if self.sigma_px > 0:
            v = ndimage.gaussian_filter(v, self.sigma_px, mode="nearest")
        cov = ndimage.gaussian_filter(valid.astype(np.float32), max(self.sigma_px, 0.5), mode="nearest")
        out = ndimage.map_coordinates(v, self.coords, order=3, mode="nearest", prefilter=True)
        covo = ndimage.map_coordinates(cov, self.coords, order=1, mode="constant", cval=0.0)
        r = np.exp(out) - 0.05
        r[r < 0] = 0
        r[covo < 0.5] = np.nan
        return r.astype(np.float32)

    def colorize(self, r: np.ndarray) -> np.ndarray:
        rr = np.where(np.isfinite(r), r, 0.0)
        idx = (np.log10(np.maximum(rr, 1e-6)) - LUT_MIN) / (LUT_MAX - LUT_MIN) * (LUT_N - 1)
        idx = np.clip(idx, 0, LUT_N - 1).astype(np.int32)
        rgba = self.lut[idx]
        rgba[rr < STOPS[0][0]] = 0
        return rgba

    def png_from_target(self, r: np.ndarray) -> bytes:
        if self.style == "classes":
            return classify_png(r, self.opacity)
        rgba = self.colorize(r)
        img = Image.fromarray(rgba, "RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def png(self, grid: Grid, rate: np.ndarray) -> tuple[bytes, float]:
        r = self.rate_on_target(grid, rate)
        if self.style == "classes":
            return classify_png(r, self.opacity), float(np.nanmax(r)) if np.isfinite(r).any() else 0.0
        rgba = self.colorize(r)
        img = Image.fromarray(rgba, "RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), float(np.nanmax(r)) if np.isfinite(r).any() else 0.0
