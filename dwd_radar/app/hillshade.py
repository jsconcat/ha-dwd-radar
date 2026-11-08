"""One-off generation of subtle hillshade tiles from open elevation data.

Source: AWS Terrain Tiles (Mapzen "terrarium" encoding, open data).
Output: transparent PNG tiles {z}/{x}/{y}.png – shadows dark, sunny slopes
slightly bright, flat land fully transparent. Rendered between basemap and radar.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path

import numpy as np
import requests
from PIL import Image

log = logging.getLogger("dwd_radar.hillshade")

SRC = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
UA = {"User-Agent": "ha-dwd-radar/0.1 (Home Assistant add-on)"}


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tiles_for(box: tuple[float, float, float, float], z: int):
    lat_min, lat_max, lon_min, lon_max = box
    x0, y0 = lonlat_to_tile(lon_min, lat_max, z)
    x1, y1 = lonlat_to_tile(lon_max, lat_min, z)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            yield x, y


class Hillshade:
    def __init__(self, out_dir: Path, cache_dir: Path, strength: float = 1.0):
        self.out = out_dir
        self.cache = cache_dir
        self.strength = strength
        self.s = requests.Session()

    def _elev(self, z: int, x: int, y: int) -> np.ndarray:
        n = 2 ** z
        x %= n
        if y < 0 or y >= n:
            return np.zeros((256, 256), np.float32)
        p = self.cache / f"{z}_{x}_{y}.png"
        if p.exists():
            data = p.read_bytes()
        else:
            r = self.s.get(SRC.format(z=z, x=x, y=y), timeout=30, headers=UA)
            r.raise_for_status()
            data = r.content
            p.write_bytes(data)
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)
        e = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
        return np.maximum(e, 0)   # sea/lakes = 0

    def _tile(self, z: int, x: int, y: int) -> Image.Image:
        # 3x3 mosaic so gradients are continuous across tile edges
        big = np.block([[self._elev(z, x + dx, y + dy) for dx in (-1, 0, 1)] for dy in (-1, 0, 1)])
        e = big[255:513, 255:513]
        n = 2 ** z
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
        px_m = 156543.03392 * math.cos(math.radians(lat)) / n
        # vertical exaggeration grows when zoomed out, so low ranges stay visible
        zf = max(1.0, 2.6 - 0.15 * z)
        drow, dcol = np.gradient(e * zf, px_m)
        dzdx = dcol[1:-1, 1:-1]            # east
        dzdy = -drow[1:-1, 1:-1]           # north (rows grow southwards)
        az, alt = math.radians(315), math.radians(45)
        lx, ly, lz = math.cos(alt) * math.sin(az), math.cos(alt) * math.cos(az), math.sin(alt)
        norm = np.sqrt(dzdx ** 2 + dzdy ** 2 + 1)
        shade = (-dzdx * lx - dzdy * ly + lz) / norm
        d = shade - math.sin(alt)               # 0 on flat ground
        rgba = np.zeros((256, 256, 4), np.uint8)
        dark = d < 0
        a_dark = np.clip(-d * 1.6, 0, 1) * 0.42 * self.strength
        a_light = np.clip(d * 1.6, 0, 1) * 0.30 * self.strength
        rgba[..., 0:3] = np.where(dark[..., None], np.array([40, 48, 60], np.uint8), np.uint8(255))
        rgba[..., 3] = np.round(np.where(dark, a_dark, a_light) * 255).astype(np.uint8)
        return Image.fromarray(rgba, "RGBA")

    def ensure(self, jobs: list[tuple[tuple[float, float, float, float], int, int]]) -> None:
        """jobs: list of (box, zmin, zmax). Skips work if already generated."""
        sig = hashlib.sha1(json.dumps([jobs, self.strength, 2]).encode()).hexdigest()[:12]
        marker = self.out / f".done_{sig}"
        if marker.exists():
            log.info("Relief-Kacheln vorhanden (%s)", sig)
            return
        self.out.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        todo = [(z, x, y) for box, z0, z1 in jobs for z in range(z0, z1 + 1) for x, y in tiles_for(box, z)]
        log.info("Erzeuge %d Relief-Kacheln (einmalig) ...", len(todo))
        for i, (z, x, y) in enumerate(todo, 1):
            p = self.out / str(z) / str(x) / f"{y}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            img = self._tile(z, x, y)
            tmp = p.with_suffix(".tmp")
            img.save(tmp, "PNG", optimize=True)
            os.replace(tmp, p)
            if i % 100 == 0:
                log.info("  Relief %d/%d", i, len(todo))
        for old in self.out.glob(".done_*"):
            old.unlink()
        marker.write_text("ok")
        log.info("Relief-Kacheln fertig")
