"""Cloud cover layer from DWD ICON-D2 (total cloud cover CLCT, 2.2 km, hourly).

Model data, not satellite observation (DWD offers no real-time satellite cloud
product on Open Data). Hourly fields are blended linearly to the 5-minute radar
frame times so the animation stays smooth.
"""
from __future__ import annotations

import bz2
import io
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes
import numpy as np
import requests
from PIL import Image
from scipy import ndimage

from render import Extent, inv_merc_x, inv_merc_y, merc_x, merc_y

log = logging.getLogger("dwd_radar.clouds")

BASE = "https://opendata.dwd.de/weather/nwp/icon-d2/grib/{hh:02d}/clct/"
UA = {"User-Agent": "ha-dwd-radar (Home Assistant add-on)"}
NAME_RE = re.compile(r'href="(icon-d2_germany_regular-lat-lon_single-level_(\d{10})_(\d{3})_2d_clct\.grib2\.bz2)"')


class Field:
    def __init__(self, data: np.ndarray, lat0: float, lon0: float, dlat: float, dlon: float):
        self.data, self.lat0, self.lon0, self.dlat, self.dlon = data, lat0, lon0, dlat, dlon


def decode(raw: bytes) -> Field:
    gid = eccodes.codes_new_from_message(raw)
    try:
        if eccodes.codes_get(gid, "gridType") != "regular_ll":
            raise ValueError(f"unerwarteter Gittertyp {eccodes.codes_get(gid, 'gridType')}")
        ni, nj = eccodes.codes_get(gid, "Ni"), eccodes.codes_get(gid, "Nj")
        la1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        lo1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        la2 = eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees")
        lo2 = eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees")
        vals = eccodes.codes_get_values(gid).astype(np.float32)
        missing = eccodes.codes_get(gid, "missingValue")
    finally:
        eccodes.codes_release(gid)
    if lo1 > 180:
        lo1 -= 360
    if lo2 > 180:
        lo2 -= 360
    d = vals.reshape(nj, ni)
    d[d == missing] = np.nan
    dlat = (la2 - la1) / (nj - 1)      # negative if scanning north->south
    dlon = (lo2 - lo1) / (ni - 1)
    return Field(d, la1, lo1, dlat, dlon)


class Clouds:
    def __init__(self, extent: Extent, out_dir: Path, res_m: float = 1000.0, opacity: float = 1.0):
        self.res = res_m
        x0, x1 = merc_x(extent.lon_min), merc_x(extent.lon_max)
        y0, y1 = merc_y(extent.lat_min), merc_y(extent.lat_max)
        self.nx = max(2, int(round((x1 - x0) / res_m)))
        self.ny = max(2, int(round((y1 - y0) / res_m)))
        x1 = x0 + self.nx * res_m
        y0 = y1 - self.ny * res_m
        self.bounds = [[inv_merc_y(y0), inv_merc_x(x0)], [inv_merc_y(y1), inv_merc_x(x1)]]
        xs = x0 + (np.arange(self.nx) + 0.5) * res_m
        ys = y1 - (np.arange(self.ny) + 0.5) * res_m
        lon = np.degrees(xs / 6378137.0)
        lat = np.degrees(2 * np.arctan(np.exp(ys / 6378137.0)) - np.pi / 2)
        self.LON, self.LAT = np.meshgrid(lon, lat)
        self.out = out_dir
        self.opacity = opacity
        self.http = requests.Session()
        self.run: datetime | None = None
        self.fields: dict[int, np.ndarray] = {}     # step -> field on target grid (0..100 %)
        self.last_check = 0.0
        self.error: str | None = None

    # ---------------------------------------------------------- data access
    def _listing(self, hh: int) -> dict[tuple[datetime, int], str]:
        r = self.http.get(BASE.format(hh=hh), timeout=30, headers=UA)
        r.raise_for_status()
        out = {}
        for m in NAME_RE.finditer(r.text):
            run = datetime.strptime(m.group(2), "%Y%m%d%H").replace(tzinfo=timezone.utc)
            out[(run, int(m.group(3)))] = m.group(1)
        return out

    def _to_target(self, f: Field) -> np.ndarray:
        rows = (self.LAT - f.lat0) / f.dlat
        cols = (self.LON - f.lon0) / f.dlon
        # crop for speed
        r0 = max(0, int(rows.min()) - 4); r1 = min(f.data.shape[0], int(rows.max()) + 5)
        c0 = max(0, int(cols.min()) - 4); c1 = min(f.data.shape[1], int(cols.max()) + 5)
        sub = np.nan_to_num(f.data[r0:r1, c0:c1], nan=0.0)
        sub = ndimage.gaussian_filter(sub, 0.8, mode="nearest")
        return ndimage.map_coordinates(sub, [rows - r0, cols - c0], order=1, mode="nearest").astype(np.float32)

    def refresh(self, need_from: datetime, need_to: datetime, force: bool = False) -> None:
        """Pick the newest complete ICON-D2 run and load the hourly steps needed."""
        now = datetime.now(timezone.utc)
        if not force and (now.timestamp() - self.last_check) < 600 and self.run:
            return
        self.last_check = now.timestamp()
        latest = now.replace(minute=0, second=0, microsecond=0)
        latest -= timedelta(hours=latest.hour % 3)
        listings: dict[int, dict] = {}
        # 1st pass: newest run that covers the whole window (incl. the past);
        # 2nd pass: newest run at all (past frames before its start get no clouds)
        for full_cover in (True, False):
            for k in range(0, 6):
                run = latest - timedelta(hours=3 * k)
                if full_cover and run > need_from:
                    continue
                s0 = max(0, math.floor((need_from - run).total_seconds() / 3600))
                s1 = math.ceil((need_to - run).total_seconds() / 3600)
                if s1 > 45:
                    continue
                if run.hour not in listings:
                    listings[run.hour] = self._listing(run.hour)
                files = listings[run.hour]
                names = {s: files.get((run, s)) for s in range(s0, s1 + 1)}
                if not all(names.values()):
                    continue
                if run == self.run and all(s in self.fields for s in names):
                    return
                fields = {}
                for s, name in names.items():
                    if run == self.run and s in self.fields:
                        fields[s] = self.fields[s]
                        continue
                    r = self.http.get(BASE.format(hh=run.hour) + name, timeout=60, headers=UA)
                    r.raise_for_status()
                    fields[s] = self._to_target(decode(bz2.decompress(r.content)))
                if run != self.run:
                    log.info("ICON-D2-Lauf %s geladen (Schritte %d–%d)", run.strftime("%d.%m. %H UTC"), s0, s1)
                self.run, self.fields = run, fields
                self.error = None
                return
        raise RuntimeError("kein vollständiger ICON-D2-Lauf mit CLCT gefunden")

    # ------------------------------------------------------------- render
    def cover_at(self, t: datetime) -> np.ndarray | None:
        if not self.run:
            return None
        h = (t - self.run).total_seconds() / 3600
        s0 = math.floor(h)
        w = h - s0
        a, b = self.fields.get(s0), self.fields.get(s0 + 1)
        if a is None:
            return None
        if b is None or w < 1e-6:
            return a
        return a * (1 - w) + b * w

    def png(self, cc: np.ndarray) -> bytes:
        c = np.clip(cc, 0, 100) / 100.0
        alpha = np.clip((c - 0.10) / 0.90, 0, 1) ** 1.05 * 0.80 * self.opacity
        # thin clouds white, overcast light grey
        col = np.stack([252 - 50 * c, 252 - 46 * c, 253 - 40 * c], axis=-1)
        rgba = np.dstack([col, alpha * 255]).round().astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def path_for(self, t: datetime) -> Path:
        return self.out / f"c_{self.run:%Y%m%d%H}_{t:%Y%m%d%H%M}.png"
